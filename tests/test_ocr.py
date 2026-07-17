from pathlib import Path

from PIL import Image

from research_agent.sources.models import ContentBlock, PageInfo, SourceDocument, SourceLocator
from research_agent.sources.enums import BlockType, LocatorType
from research_agent.sources.ocr import OCRPage, OCRWord, is_duplicate_ocr, ocr_document, preprocess_image


class FakeEngine:
    def __init__(self, text: str, confidence: float = 0.91):
        self.text = text
        self.confidence = confidence

    def recognize(self, image: Image.Image, languages: str) -> OCRPage:
        return OCRPage(self.text, (OCRWord(self.text, self.confidence, 10, 20, 80, 30),), angle=90, language=languages)


def test_preprocessing_scales_and_normalizes_image() -> None:
    image = Image.new("RGB", (100, 50), (120, 120, 120))
    result = preprocess_image(image)
    assert result.mode == "L"
    assert max(result.size) >= 1600


def test_ocr_preserves_native_text_and_adds_derived_coordinates() -> None:
    document = SourceDocument(source_id="src", document_id="doc", pages=[PageInfo(page_number=1, text="native value")],
                              blocks=[ContentBlock(block_id="native", source_id="src", block_type=BlockType.PARAGRAPH,
                                                   text="native value", locator=SourceLocator(locator_type=LocatorType.PAGE, page_number=1))])
    output = ocr_document(document, {1: Image.new("RGB", (100, 100), "white")}, FakeEngine("scanned value"))
    assert len(output.blocks) == 2
    derived = output.blocks[-1]
    assert derived.is_derived is True
    assert derived.derivation_method == "ocr:chi_sim+eng:angle=90"
    assert derived.locator.bbox == (10, 20, 90, 50)
    assert output.pages[0].ocr_confidence == 0.91


def test_duplicate_ocr_is_kept_as_warning_and_not_repeated() -> None:
    document = SourceDocument(source_id="src", document_id="doc", pages=[PageInfo(page_number=1, text="same text")],
                              blocks=[ContentBlock(block_id="native", source_id="src", block_type=BlockType.PARAGRAPH, text="same text")])
    output = ocr_document(document, {1: Image.new("RGB", (20, 20), "white")}, FakeEngine("same text"))
    assert len(output.blocks) == 1
    assert any(w.code == "ocr_deduplicated" for w in output.warnings)
    assert is_duplicate_ocr("中文 Revenue 42", "中文Revenue42")
