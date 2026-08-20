from __future__ import annotations

import json
import os
import zipfile
from io import BytesIO
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageDraw

from pdf_to_editable_word.app import unique_output_stem
from pdf_to_editable_word.converter import PdfToWordConverter
from pdf_to_editable_word.document_model import BoundingBox, DocumentModel, ImageObject, PageModel, TextSpan
from pdf_to_editable_word.font_resolver import FontResolver
from pdf_to_editable_word.ocr import LocalOcrEngine, LocalRapidOcr, LocalTesseractOcr
from pdf_to_editable_word.scan_mask import suppress_ocr_text_under_stamps
from pdf_to_editable_word.word_builder import PositionedWordBuilder


def _make_digital_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 72), "Editable text must survive conversion", fontsize=14)
    page.insert_text((72, 108), "Second positioned line", fontsize=11)
    for y in (140, 170, 200):
        page.draw_line((72, y), (390, y), color=(0, 0, 0), width=1)
    for x in (72, 231, 390):
        page.draw_line((x, 140), (x, 200), color=(0, 0, 0), width=1)
    page.insert_text((88, 160), "Table A", fontsize=12)
    page.insert_text((247, 160), "Table B", fontsize=12)
    document.save(path)
    document.close()


def test_digital_pdf_creates_editable_docx_and_report(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _make_digital_pdf(source)
    output, qa, report = PdfToWordConverter().convert(source, tmp_path)

    assert output.exists()
    assert report.exists()
    assert qa.status in {"PASS", "PASS_WITH_WARNING"}
    with zipfile.ZipFile(output) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    assert "Editable text must survive conversion" in xml
    assert "<wp:anchor" in xml
    assert 'behindDoc="1"' in xml
    details = json.loads(report.read_text(encoding="utf-8"))
    assert details["pages"][0]["source_kind"] == "digital"
    assert details["pages"][0]["table_count"] == 1
    assert details["qa"]["status"] == qa.status
    assert details["qa"]["metrics"]["missing_editable_text_spans"] == 0


def test_batch_output_names_do_not_overwrite_duplicate_pdf_names(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    reserved_stems: set[str] = set()
    first_source = tmp_path / "first" / "contract.pdf"
    second_source = tmp_path / "second" / "contract.pdf"
    first_source.parent.mkdir()
    second_source.parent.mkdir()
    _make_digital_pdf(first_source)
    _make_digital_pdf(second_source)

    first_stem = unique_output_stem(first_source, output_dir, reserved_stems)
    first_output, _qa, first_report = PdfToWordConverter().convert(first_source, output_dir, output_stem=first_stem)
    second_stem = unique_output_stem(second_source, output_dir, reserved_stems)
    second_output, _qa, second_report = PdfToWordConverter().convert(second_source, output_dir, output_stem=second_stem)

    assert first_stem == "contract"
    assert second_stem == "contract (2)"
    assert first_output.name == "contract.docx"
    assert second_output.name == "contract (2).docx"
    assert first_output.exists()
    assert second_output.exists()
    assert first_report.name == "contract.conversion.json"
    assert second_report.name == "contract (2).conversion.json"
    assert first_report.exists()
    assert second_report.exists()


def test_qa_fails_when_any_source_text_is_not_in_the_word_document(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _make_digital_pdf(source)
    output, qa, _report = PdfToWordConverter().convert(source, tmp_path)

    with zipfile.ZipFile(output) as archive:
        entries = {entry.filename: archive.read(entry.filename) for entry in archive.infolist()}
    document_xml = entries["word/document.xml"].replace(
        b"Second positioned line", b"Second missing line", 1
    )
    entries["word/document.xml"] = document_xml
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)

    from pdf_to_editable_word.qa import run_editability_qa
    from pdf_to_editable_word.document_model import PdfAnalyzer

    model = PdfAnalyzer().analyze(source)
    altered_qa = run_editability_qa(model, output)
    assert "editable_text_spans_missing_from_document_xml" in altered_qa.flags
    assert altered_qa.metrics["missing_editable_text_spans"] == 1
    assert altered_qa.status == "FAIL"


def _make_stamp_png(color: tuple[int, int, int] = (210, 0, 0)) -> bytes:
    image = Image.new("RGB", (180, 180), "white")
    painter = ImageDraw.Draw(image)
    painter.ellipse((10, 10, 170, 170), outline=color, width=10)
    painter.regular_polygon((90, 90, 54), n_sides=5, rotation=0, fill=color)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_red_stamp_is_separate_transparent_docx_media(tmp_path: Path) -> None:
    source = tmp_path / "seal.pdf"
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 72), "Text remains editable under an independent stamp", fontsize=13)
    page.insert_image((170, 150, 290, 270), stream=_make_stamp_png())
    document.save(source)
    document.close()

    output, qa, report = PdfToWordConverter().convert(source, tmp_path)

    with zipfile.ZipFile(output) as archive:
        media_files = [name for name in archive.namelist() if name.startswith("word/media/")]
        assert len(media_files) == 1
        stamp = Image.open(BytesIO(archive.read(media_files[0]))).convert("RGBA")
        assert stamp.getpixel((0, 0))[3] == 0
        xml = archive.read("word/document.xml").decode("utf-8")
    assert "Text remains editable under an independent stamp" in xml
    details = json.loads(report.read_text(encoding="utf-8"))
    assert details["pages"][0]["stamp_count"] == 1
    assert qa.status == "PASS_WITH_WARNING"


def test_ocr_artifacts_overlapping_a_stamp_are_not_added_as_editable_text() -> None:
    stamp = ImageObject(
        bbox=BoundingBox(300, 420, 420, 540),
        data=_make_stamp_png(),
        extension="png",
        is_stamp=True,
    )
    artifact = TextSpan(
        text="O",
        bbox=BoundingBox(315, 435, 405, 525),
        font_name="Arial",
        font_size=12,
        color=0,
        source="ocr",
    )
    nearby_text = TextSpan(
        text="Keep this editable",
        bbox=BoundingBox(72, 72, 220, 90),
        font_name="Arial",
        font_size=12,
        color=0,
        source="ocr",
    )
    page = PageModel(
        number=1,
        width=595,
        height=842,
        rotation=0,
        text_spans=[artifact, nearby_text],
        images=[stamp],
        source_kind="ocr",
    )

    assert suppress_ocr_text_under_stamps(page) == 1
    assert [span.text for span in page.text_spans] == ["Keep this editable"]


def test_converter_removes_ocr_stamp_artifact_before_writing_word(tmp_path: Path) -> None:
    original = tmp_path / "stamp-original.pdf"
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_image((320, 450, 440, 570), stream=_make_stamp_png())
    document.save(original)
    document.close()

    source_document = pymupdf.open(original)
    raster = source_document[0].get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
    raster_path = tmp_path / "stamp-scan.png"
    raster.save(raster_path)
    source_document.close()
    scanned = tmp_path / "stamp-scan.pdf"
    scanned_document = pymupdf.open()
    page = scanned_document.new_page(width=595, height=842)
    page.insert_image(page.rect, filename=raster_path)
    scanned_document.save(scanned)
    scanned_document.close()

    converter = PdfToWordConverter()
    converter.ocr.extract = lambda _path, _page: [
        TextSpan(
            text="STAMP_ARTIFACT",
            bbox=BoundingBox(325, 455, 415, 565),
            font_name="Arial",
            font_size=12,
            color=0,
            source="ocr",
        ),
        TextSpan(
            text="Keep editable text",
            bbox=BoundingBox(72, 72, 220, 90),
            font_name="Arial",
            font_size=12,
            color=0,
            source="ocr",
        ),
    ]
    output, _qa, report = converter.convert(scanned, tmp_path)

    with zipfile.ZipFile(output) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    details = json.loads(report.read_text(encoding="utf-8"))
    assert "STAMP_ARTIFACT" not in xml
    assert "Keep editable text" in xml
    assert any(flag.startswith("ocr_text_spans_suppressed_under_stamps:1") for flag in details["pages"][0]["qa_flags"])


@pytest.mark.skipif(not LocalOcrEngine().is_available, reason="local OCR is unavailable")
@pytest.mark.parametrize("color", [(210, 0, 0), (0, 110, 210)])
def test_scanned_colored_stamp_is_separated_as_transparent_media(
    tmp_path: Path, color: tuple[int, int, int]
) -> None:
    original = tmp_path / "scanned-seal-source.pdf"
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 72), "Text remains editable below scanned seal", fontsize=13)
    page.insert_image((320, 450, 440, 570), stream=_make_stamp_png(color))
    document.save(original)
    document.close()

    source_document = pymupdf.open(original)
    raster = source_document[0].get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
    raster_path = tmp_path / "scanned-seal.png"
    raster.save(raster_path)
    source_document.close()

    scanned = tmp_path / "scanned-seal.pdf"
    scanned_document = pymupdf.open()
    page = scanned_document.new_page(width=595, height=842)
    page.insert_image(page.rect, filename=raster_path)
    scanned_document.save(scanned)
    scanned_document.close()

    output, _qa, report = PdfToWordConverter().convert(scanned, tmp_path)

    with zipfile.ZipFile(output) as archive:
        media = [Image.open(BytesIO(archive.read(name))).convert("RGBA") for name in archive.namelist() if name.startswith("word/media/")]
        xml = archive.read("word/document.xml").decode("utf-8")
    assert any(image.getpixel((0, 0))[3] == 0 for image in media)
    assert "<wp:anchor" in xml
    assert 'behindDoc="1"' in xml
    assert 'behindDoc="0"' in xml
    assert "Text remains editable below scanned seal" in xml
    details = json.loads(report.read_text(encoding="utf-8"))
    assert details["pages"][0]["stamp_count"] == 1
    assert any(flag.startswith("scanned_stamps_extracted:") for flag in details["pages"][0]["qa_flags"])


def test_digital_text_keeps_bold_italic_and_font_family(tmp_path: Path) -> None:
    source = tmp_path / "styled.pdf"
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 72), "Bold source", fontsize=14, fontname="hebo")
    page.insert_text((72, 104), "Italic source", fontsize=14, fontname="heit")
    document.save(source)
    document.close()

    output, _qa, _report = PdfToWordConverter().convert(source, tmp_path)

    with zipfile.ZipFile(output) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    assert "Bold source" in xml
    assert "Italic source" in xml
    assert "w:b" in xml
    assert "w:i" in xml
    assert FontResolver().resolve("ABCDEF+Calibri-Bold") == "Calibri"


def test_digital_filled_vector_remains_a_background_overlay(tmp_path: Path) -> None:
    source = tmp_path / "filled-vector.pdf"
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.draw_rect((72, 72, 360, 128), color=None, fill=(0.08, 0.35, 0.68))
    page.insert_text((88, 108), "White text on a filled form header", fontsize=14, color=(1, 1, 1))
    document.save(source)
    document.close()

    output, _qa, _report = PdfToWordConverter().convert(source, tmp_path)

    with zipfile.ZipFile(output) as archive:
        media = [
            Image.open(BytesIO(archive.read(name))).convert("RGBA")
            for name in archive.namelist()
            if name.startswith("word/media/")
        ]
        xml = archive.read("word/document.xml").decode("utf-8")
    assert any(
        pixel[3] > 0 and pixel[2] > pixel[0] + 60
        for image in media
        for pixel in image.getdata()
    )
    assert 'behindDoc="1"' in xml
    assert "White text on a filled form header" in xml


def test_digital_curved_vector_is_preserved_as_a_background_overlay(tmp_path: Path) -> None:
    source = tmp_path / "curved-vector.pdf"
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.draw_circle((128, 100), 28, color=(0.78, 0.05, 0.05), width=2)
    page.insert_text((180, 105), "Round checkbox remains visible", fontsize=13)
    document.save(source)
    document.close()

    output, _qa, _report = PdfToWordConverter().convert(source, tmp_path)

    with zipfile.ZipFile(output) as archive:
        media = [
            Image.open(BytesIO(archive.read(name))).convert("RGBA")
            for name in archive.namelist()
            if name.startswith("word/media/")
        ]
    assert any(
        pixel[3] > 0 and pixel[0] > pixel[1] + 80
        for image in media
        for pixel in image.getdata()
    )


@pytest.mark.skipif(not LocalOcrEngine().is_available, reason="local OCR is unavailable")
def test_scanned_page_automatically_runs_ocr(tmp_path: Path) -> None:
    original = tmp_path / "original.pdf"
    _make_digital_pdf(original)
    original_document = pymupdf.open(original)
    image = original_document[0].get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
    raster_path = tmp_path / "page.png"
    image.save(raster_path)
    original_document.close()

    scanned = tmp_path / "scanned.pdf"
    scanned_document = pymupdf.open()
    page = scanned_document.new_page(width=595, height=842)
    page.insert_image(page.rect, filename=raster_path)
    scanned_document.save(scanned)
    scanned_document.close()

    output, qa, report = PdfToWordConverter().convert(scanned, tmp_path)

    with zipfile.ZipFile(output) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    assert "Editable text must survive conversion" in xml
    details = json.loads(report.read_text(encoding="utf-8"))
    assert details["pages"][0]["source_kind"] == "ocr"
    assert details["pages"][0]["text_span_count"] > 0
    assert details["pages"][0]["ocr_cleaned_background_count"] == 1
    assert "ocr_text_background_cleaned" in details["pages"][0]["qa_flags"]
    assert qa.status == "PASS_WITH_WARNING"


def test_chinese_ocr_uses_a_windows_east_asian_font(tmp_path: Path) -> None:
    tsv = """level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext
5\t1\t1\t1\t1\t1\t200\t240\t80\t40\t96.2\t合同
5\t1\t1\t1\t1\t2\t290\t240\t80\t40\t95.8\t编号
"""
    spans = LocalTesseractOcr._to_lines(tsv, scale=2.0)
    assert len(spans) == 1
    assert spans[0].text == "合同 编号"
    assert spans[0].east_asia_font_name == "Microsoft YaHei"

    output = tmp_path / "chinese-ocr.docx"
    PositionedWordBuilder().build(
        DocumentModel(
            source_path="synthetic.pdf",
            pages=[
                PageModel(
                    number=1,
                    width=595,
                    height=842,
                    rotation=0,
                    text_spans=spans,
                    source_kind="ocr",
                )
            ],
        ),
        output,
    )
    with zipfile.ZipFile(output) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    assert 'w:ascii="Arial"' in xml
    assert 'w:eastAsia="Microsoft YaHei"' in xml


def _cjk_font_path() -> Path | None:
    candidates = [
        Path(os.environ["PDF_TO_WORD_TEST_CJK_FONT"])
        if os.environ.get("PDF_TO_WORD_TEST_CJK_FONT")
        else None,
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("/System/Library/Fonts/STHeiti Medium.ttc"),
    ]
    return next((path for path in candidates if path is not None and path.is_file()), None)


@pytest.mark.skipif(
    not LocalRapidOcr().is_available or _cjk_font_path() is None,
    reason="RapidOCR or a local CJK test font is unavailable",
)
def test_rapid_ocr_preserves_chinese_contract_text(tmp_path: Path) -> None:
    font_path = _cjk_font_path()
    assert font_path is not None
    original = tmp_path / "chinese-original.pdf"
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_font(fontname="china", fontfile=str(font_path))
    page.insert_text((72, 72), "合同编号：XHH260700507", fontname="china", fontsize=16)
    page.insert_text((72, 112), "人民币金额：壹万贰仟叁佰元整", fontname="china", fontsize=14)
    page.insert_text((72, 152), "签署日期：2026年08月20日", fontname="china", fontsize=13)
    document.save(original)
    document.close()

    source_document = pymupdf.open(original)
    raster = source_document[0].get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
    raster_path = tmp_path / "chinese-scan.png"
    raster.save(raster_path)
    source_document.close()

    scanned = tmp_path / "chinese-scan.pdf"
    scanned_document = pymupdf.open()
    page = scanned_document.new_page(width=595, height=842)
    page.insert_image(page.rect, filename=raster_path)
    scanned_document.save(scanned)
    scanned_document.close()

    output, _qa, _report = PdfToWordConverter().convert(scanned, tmp_path)
    with zipfile.ZipFile(output) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    assert "合同编号：XHH260700507" in xml
    assert "人民币金额：壹万贰仟叁佰元整" in xml
    assert "签署日期：2026年08月20日" in xml
