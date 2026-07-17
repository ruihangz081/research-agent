"""OCR pipeline with preprocessing, orientation, coordinates, and deduplication."""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from typing import Protocol

from PIL import Image, ImageOps

from .models import ContentBlock, ExtractionWarning, ImageBlock, PageInfo, SourceDocument, SourceLocator
from .enums import BlockType, LocatorType


@dataclass(frozen=True)
class OCRWord:
    text: str
    confidence: float
    left: float
    top: float
    width: float
    height: float


@dataclass(frozen=True)
class OCRPage:
    text: str
    words: tuple[OCRWord, ...]
    angle: int = 0
    language: str = "eng"


class OCREngine(Protocol):
    def recognize(self, image: Image.Image, languages: str) -> OCRPage: ...


class TesseractEngine:
    """Tesseract adapter; the binary and trained languages are checked at runtime."""
    def __init__(self, executable: str | None = None):
        self.executable = executable or shutil.which("tesseract")
        if not self.executable:
            raise RuntimeError("tesseract executable is required for OCR")

    def recognize(self, image: Image.Image, languages: str) -> OCRPage:
        import pytesseract
        from pytesseract import Output
        pytesseract.pytesseract.tesseract_cmd = self.executable
        processed = preprocess_image(image)
        config = "--psm 3"
        try:
            osd = pytesseract.image_to_osd(processed, config="--psm 0")
            match = re.search(r"Rotate:\s*(\d+)", osd)
            angle = int(match.group(1)) if match else 0
        except pytesseract.TesseractError:
            angle = 0
        if angle:
            processed = processed.rotate(angle, expand=True)
        values = pytesseract.image_to_data(processed, lang=languages, config=config, output_type=Output.DICT)
        words: list[OCRWord] = []
        for index, raw in enumerate(values["text"]):
            text = raw.strip()
            try:
                confidence = max(0.0, min(1.0, float(values["conf"][index]) / 100))
            except (TypeError, ValueError):
                confidence = 0.0
            if text:
                words.append(OCRWord(text, confidence, float(values["left"][index]), float(values["top"][index]), float(values["width"][index]), float(values["height"][index])))
        return OCRPage(" ".join(word.text for word in words), tuple(words), angle, languages)


def preprocess_image(image: Image.Image) -> Image.Image:
    """Normalize contrast and scale while retaining the original image separately."""
    image = ImageOps.exif_transpose(image).convert("L")
    if max(image.size) < 1600:
        scale = 1600 / max(image.size)
        image = image.resize((int(image.width * scale), int(image.height * scale)))
    image = ImageOps.autocontrast(image)
    return image.point(lambda value: 255 if value > 185 else 0)


def _normalize(text: str) -> str:
    return re.sub(r"\W+", "", text.casefold())


def is_duplicate_ocr(native_text: str, ocr_text: str) -> bool:
    native = _normalize(native_text)
    ocr = _normalize(ocr_text)
    if not native or not ocr:
        return False
    return ocr in native or native in ocr or len(set(native) & set(ocr)) / max(len(set(ocr)), 1) > 0.9


def render_pdf_pages(data: bytes, page_numbers: list[int] | None = None, dpi: int = 220) -> dict[int, Image.Image]:
    import fitz
    pdf = fitz.open(stream=data, filetype="pdf")
    selected = page_numbers or list(range(1, len(pdf) + 1))
    rendered: dict[int, Image.Image] = {}
    scale = dpi / 72
    for page_number in selected:
        if not 1 <= page_number <= len(pdf):
            continue
        pixmap = pdf[page_number - 1].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        rendered[page_number] = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    pdf.close()
    return rendered


def ocr_document(document: SourceDocument, image_pages: dict[int, Image.Image], engine: OCREngine) -> SourceDocument:
    """Add OCR as derived blocks, never replacing native extraction."""
    blocks = list(document.blocks)
    pages = list(document.pages)
    warnings = list(document.warnings)
    ocr_count = 0
    for page_number, image in sorted(image_pages.items()):
        result = engine.recognize(image, "chi_sim+eng")
        native = next((page.text for page in pages if page.page_number == page_number), "")
        page = next((page for page in pages if page.page_number == page_number), None)
        if page:
            page.is_scanned = True
            page.ocr_confidence = sum(word.confidence for word in result.words) / len(result.words) if result.words else 0
            page.text = page.text or result.text
        else:
            pages.append(PageInfo(page_number=page_number, text=result.text, is_scanned=True,
                                  ocr_confidence=sum(word.confidence for word in result.words) / len(result.words) if result.words else 0))
        if is_duplicate_ocr(native, result.text):
            warnings.append(ExtractionWarning(code="ocr_deduplicated", message="OCR output duplicated native text", page_number=page_number, method="ocr"))
            continue
        if result.text.strip():
            bbox = None
            if result.words:
                left = min(word.left for word in result.words)
                top = min(word.top for word in result.words)
                right = max(word.left + word.width for word in result.words)
                bottom = max(word.top + word.height for word in result.words)
                bbox = (left, top, right, bottom)
            blocks.append(ContentBlock(block_id=f"ocr_{document.source_id}_{page_number}", source_id=document.source_id,
                                       block_type=BlockType.OCR_TEXT, text=result.text, order=len(blocks), page_number=page_number,
                                       locator=SourceLocator(locator_type=LocatorType.PAGE, page_number=page_number, bbox=bbox),
                                       confidence=sum(word.confidence for word in result.words) / len(result.words) if result.words else 0,
                                       is_derived=True, derivation_method=f"ocr:{result.language}:angle={result.angle}"))
            ocr_count += 1
    document.blocks = blocks
    document.pages = sorted(pages, key=lambda page: page.page_number)
    document.warnings = warnings
    document.quality.ocr_pages = ocr_count
    document.quality.warnings = len(warnings)
    document.quality.score = min(1.0, document.quality.score + (0.15 if ocr_count else 0))
    return document
