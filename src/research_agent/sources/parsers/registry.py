"""Format-aware parsers that normalize material into SourceDocument."""
from __future__ import annotations

import csv
import io
import mimetypes
import re
import shutil
import subprocess
import tempfile
import zipfile
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Callable

from bs4 import BeautifulSoup
from docx import Document as DocxDocument
from openpyxl import load_workbook
from PIL import Image
from pptx import Presentation
from pypdf import PdfReader
from striprtf.striprtf import rtf_to_text

from ..enums import BlockType, LocatorType
from ..models import (
    ContentBlock, DocumentMetadata, ExtractionQuality, ExtractionWarning, ImageBlock,
    PageInfo, SourceDocument, SourceLocator, TableBlock, TableCell,
)
from ..security import sanitize_untrusted_text


class ParseError(ValueError):
    pass


class ParseResult:
    def __init__(self, document: SourceDocument):
        self.document = document


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _block(source_id: str, index: int, text: str, block_type: BlockType, locator: SourceLocator | None = None, **kwargs) -> ContentBlock:
    text, injection_warnings = sanitize_untrusted_text(text)
    attrs = dict(kwargs.pop("attributes", {}))
    if injection_warnings:
        attrs["untrusted_instruction_warning"] = injection_warnings
    return ContentBlock(block_id=f"blk_{source_id}_{index}", source_id=source_id, block_type=block_type,
                        text=text, order=index, locator=locator, attributes=attrs, **kwargs)


def _quality(pages: list[PageInfo], blocks: list[ContentBlock], tables: list[TableBlock], warnings: list[ExtractionWarning]) -> ExtractionQuality:
    text_blocks = sum(bool(block.text.strip()) for block in blocks)
    score = min(1.0, (0.45 if text_blocks else 0) + (0.3 if pages or blocks else 0) + (0.25 if not any(w.severity in {"high", "error"} for w in warnings) else 0))
    return ExtractionQuality(score=score, text_coverage=1 if text_blocks else 0,
                             layout_coverage=1 if pages else 0, table_coverage=1 if tables else 0,
                             warnings=len(warnings))


def _document(source_id: str, blocks: list[ContentBlock], *, pages=None, tables=None, images=None, warnings=None, metadata=None) -> SourceDocument:
    pages, tables, images, warnings = pages or [], tables or [], images or [], warnings or []
    return SourceDocument(source_id=source_id, document_id=f"doc_{source_id}", metadata=metadata or DocumentMetadata(),
                          pages=pages, blocks=blocks, tables=tables, images=images,
                          warnings=warnings, quality=_quality(pages, blocks, tables, warnings))


def parse_text(source_id: str, data: bytes, filename: str) -> SourceDocument:
    text = _decode(data)
    blocks: list[ContentBlock] = []
    headings: list[str] = []
    for index, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if filename.lower().endswith((".md", ".markdown")) and stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            headings = headings[: level - 1] + [stripped[level:].strip()]
            block_type = BlockType.TITLE if level == 1 and index == 0 else BlockType.HEADING
        else:
            block_type = BlockType.PARAGRAPH
        blocks.append(_block(source_id, len(blocks), stripped, block_type,
                             SourceLocator(locator_type=LocatorType.OFFSET, char_start=text.find(line), char_end=text.find(line) + len(line)),
                             heading_path=headings.copy()))
    return _document(source_id, blocks)


def parse_html(source_id: str, data: bytes, filename: str) -> SourceDocument:
    if filename.lower().endswith(".mhtml"):
        message = BytesParser(policy=policy.default).parsebytes(data)
        parts = [part for part in message.walk() if part.get_content_type() == "text/html"]
        html = parts[0].get_content() if parts else _decode(data)
    else:
        html = _decode(data)
    soup = BeautifulSoup(html, "html.parser")
    blocks: list[ContentBlock] = []
    for element in soup.find_all(["title", "h1", "h2", "h3", "h4", "p", "li", "blockquote", "pre"]):
        text = " ".join(element.get_text(" ", strip=True).split())
        if not text:
            continue
        tag = element.name
        kind = BlockType.TITLE if tag == "title" else BlockType.HEADING if tag.startswith("h") else BlockType.LIST if tag == "li" else BlockType.QUOTE if tag == "blockquote" else BlockType.PARAGRAPH
        blocks.append(_block(source_id, len(blocks), text, kind, SourceLocator(locator_type=LocatorType.OFFSET), heading_path=[]))
    return _document(source_id, blocks, metadata=DocumentMetadata(title=soup.title.get_text(strip=True) if soup.title else None))


def parse_csv(source_id: str, data: bytes, filename: str) -> SourceDocument:
    delimiter = "\t" if filename.lower().endswith(".tsv") else ","
    rows = list(csv.reader(io.StringIO(_decode(data)), delimiter=delimiter))
    cells = [TableCell(row=r + 1, column=c + 1, value=value) for r, row in enumerate(rows) for c, value in enumerate(row)]
    table = TableBlock(table_id=f"tbl_{source_id}_1", source_id=source_id, rows=len(rows), columns=max((len(row) for row in rows), default=0), cells=cells,
                       locator=SourceLocator(locator_type=LocatorType.RANGE, table_id=f"tbl_{source_id}_1", cell_range=f"A1:{chr(64 + max((len(row) for row in rows), default=1))}{len(rows)}"))
    text = "\n".join(" | ".join(row) for row in rows)
    block = _block(source_id, 0, text, BlockType.TABLE, table.locator)
    return _document(source_id, [block], tables=[table])


def parse_pdf(source_id: str, data: bytes, filename: str) -> SourceDocument:
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise ParseError(f"cannot read PDF: {exc}") from exc
    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):
                return _document(source_id, [], warnings=[ExtractionWarning(code="encrypted_pdf", message="PDF password required", severity="high", method="pypdf")])
        except Exception:
            return _document(source_id, [], warnings=[ExtractionWarning(code="encrypted_pdf", message="PDF password required", severity="high", method="pypdf")])
    pages, blocks, warnings = [], [], []
    for page_number, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        page_info = PageInfo(page_number=page_number, text=text, is_scanned=not bool(text.strip()))
        pages.append(page_info)
        if text.strip():
            blocks.append(_block(source_id, len(blocks), text.strip(), BlockType.PARAGRAPH,
                                 SourceLocator(locator_type=LocatorType.PAGE, page_number=page_number)))
        else:
            warnings.append(ExtractionWarning(code="scanned_page", message="page has no native text; OCR required", page_number=page_number, method="pypdf"))
    metadata = DocumentMetadata(title=(reader.metadata.title if reader.metadata else None), authors=[reader.metadata.author] if reader.metadata and reader.metadata.author else [])
    return _document(source_id, blocks, pages=pages, warnings=warnings, metadata=metadata)


def parse_docx(source_id: str, data: bytes, filename: str) -> SourceDocument:
    document = DocxDocument(io.BytesIO(data))
    blocks: list[ContentBlock] = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if text:
            style = paragraph.style.name.lower() if paragraph.style else ""
            kind = BlockType.TITLE if "title" in style else BlockType.HEADING if "heading" in style else BlockType.PARAGRAPH
            blocks.append(_block(source_id, len(blocks), text, kind, SourceLocator(locator_type=LocatorType.PARAGRAPH, paragraph_index=len(blocks))))
    tables: list[TableBlock] = []
    for table_index, raw_table in enumerate(document.tables, 1):
        cells = [TableCell(row=r + 1, column=c + 1, value=cell.text.strip()) for r, row in enumerate(raw_table.rows) for c, cell in enumerate(row.cells)]
        table = TableBlock(table_id=f"tbl_{source_id}_{table_index}", source_id=source_id, rows=len(raw_table.rows), columns=len(raw_table.columns), cells=cells,
                           locator=SourceLocator(locator_type=LocatorType.RANGE, table_id=f"tbl_{source_id}_{table_index}"))
        tables.append(table)
        blocks.append(_block(source_id, len(blocks), "\n".join(" | ".join(cell.value for cell in row) for row in [[c for c in cells if c.row == r] for r in range(1, table.rows + 1)]), BlockType.TABLE, table.locator))
    return _document(source_id, blocks, tables=tables)


def parse_xlsx(source_id: str, data: bytes, filename: str) -> SourceDocument:
    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    blocks, tables = [], []
    for sheet in workbook.worksheets:
        rows = list(sheet.iter_rows(values_only=False))
        cells = [TableCell(row=r + 1, column=c + 1, value="" if cell.value is None else str(cell.value), formula=str(cell.value) if isinstance(cell.value, str) and cell.value.startswith("=") else None) for r, row in enumerate(rows) for c, cell in enumerate(row)]
        table_id = f"tbl_{source_id}_{sheet.title}"
        table = TableBlock(table_id=table_id, source_id=source_id, sheet_name=sheet.title, rows=len(rows), columns=max((len(row) for row in rows), default=0), cells=cells,
                           locator=SourceLocator(locator_type=LocatorType.SHEET, sheet_name=sheet.title, table_id=table_id))
        tables.append(table)
        text = "\n".join(" | ".join(c.value for c in cells if c.row == r) for r in range(1, table.rows + 1))
        blocks.append(_block(source_id, len(blocks), text, BlockType.TABLE, table.locator, attributes={"sheet_name": sheet.title}))
    return _document(source_id, blocks, tables=tables)


def parse_pptx(source_id: str, data: bytes, filename: str) -> SourceDocument:
    presentation = Presentation(io.BytesIO(data))
    blocks, pages = [], []
    for slide_number, slide in enumerate(presentation.slides, 1):
        texts = []
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text.strip():
                texts.append(shape.text.strip())
        text = "\n".join(texts)
        pages.append(PageInfo(page_number=slide_number, text=text))
        if text:
            blocks.append(_block(source_id, len(blocks), text, BlockType.PARAGRAPH, SourceLocator(locator_type=LocatorType.SLIDE, slide_number=slide_number)))
    return _document(source_id, blocks, pages=pages)


def parse_image(source_id: str, data: bytes, filename: str) -> SourceDocument:
    try:
        image = Image.open(io.BytesIO(data))
        image.verify()
        width, height = image.size
    except Exception as exc:
        raise ParseError(f"invalid image: {exc}") from exc
    image_block = ImageBlock(image_id=f"img_{source_id}_1", source_id=source_id, bbox=(0, 0, float(width), float(height)))
    warning = ExtractionWarning(code="ocr_pending", message="image text requires OCR worker", severity="warning", method="ocr")
    return _document(source_id, [], images=[image_block], warnings=[warning])


def parse_rtf(source_id: str, data: bytes, filename: str) -> SourceDocument:
    return parse_text(source_id, rtf_to_text(_decode(data)).encode(), filename)


def parse_archive(source_id: str, data: bytes, filename: str) -> SourceDocument:
    from .registry import parse_bytes
    blocks, tables, pages, images, warnings = [], [], [], [], []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            suffix = Path(member.filename).suffix.lower()
            if suffix == ".zip":
                warnings.append(ExtractionWarning(code="nested_archive_skipped", message=f"nested archive skipped: {member.filename}", severity="warning", method="zip"))
                continue
            try:
                nested = parse_bytes(f"{source_id}_{len(blocks)}", archive.read(member), member.filename).document
                for block in nested.blocks:
                    block.locator = SourceLocator(locator_type=LocatorType.ZIP_MEMBER, zip_member=member.filename, page_number=block.locator.page_number if block.locator else None)
                    blocks.append(block)
                tables.extend(nested.tables)
                pages.extend(nested.pages)
                images.extend(nested.images)
                warnings.extend(nested.warnings)
            except (ParseError, ValueError) as exc:
                warnings.append(ExtractionWarning(code="archive_member_failed", message=f"{member.filename}: {exc}", severity="warning", method="zip"))
    return _document(source_id, blocks, pages=pages, tables=tables, images=images, warnings=warnings)


def _legacy_convert(data: bytes, filename: str) -> tuple[bytes, str]:
    soffice = shutil.which("libreoffice") or shutil.which("soffice")
    if not soffice:
        raise ParseError("legacy Office conversion requires libreoffice")
    with tempfile.TemporaryDirectory(prefix="research-source-") as temp:
        input_path = Path(temp) / filename
        input_path.write_bytes(data)
        subprocess.run([soffice, "--headless", "--convert-to", "docx", "--outdir", temp, str(input_path)], check=True, capture_output=True, timeout=60)
        converted = Path(temp) / f"{input_path.stem}.docx"
        if not converted.exists():
            raise ParseError("libreoffice produced no converted document")
        return converted.read_bytes(), converted.name


def parse_bytes(source_id: str, data: bytes, filename: str) -> ParseResult:
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md", ".markdown"}:
        document = parse_text(source_id, data, filename)
    elif suffix in {".html", ".htm", ".mhtml"}:
        document = parse_html(source_id, data, filename)
    elif suffix in {".csv", ".tsv"}:
        document = parse_csv(source_id, data, filename)
    elif suffix == ".pdf":
        document = parse_pdf(source_id, data, filename)
    elif suffix == ".docx":
        document = parse_docx(source_id, data, filename)
    elif suffix == ".xlsx":
        document = parse_xlsx(source_id, data, filename)
    elif suffix == ".pptx":
        document = parse_pptx(source_id, data, filename)
    elif suffix in {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}:
        document = parse_image(source_id, data, filename)
    elif suffix == ".rtf":
        document = parse_rtf(source_id, data, filename)
    elif suffix == ".zip":
        document = parse_archive(source_id, data, filename)
    elif suffix in {".doc", ".xls", ".ppt"}:
        converted, converted_name = _legacy_convert(data, filename)
        document = parse_bytes(source_id, converted, converted_name).document
        document.warnings.append(ExtractionWarning(code="legacy_converted", message="legacy Office file converted with LibreOffice", method="libreoffice"))
    else:
        raise ParseError(f"unsupported format: {suffix}")
    return ParseResult(document)
