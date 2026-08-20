from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path

import pymupdf
import pytest
from PIL import Image, ImageDraw

from pdf_to_editable_word.converter import PdfToWordConverter
from pdf_to_editable_word.font_resolver import FontResolver
from pdf_to_editable_word.ocr import LocalTesseractOcr


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
    details = json.loads(report.read_text(encoding="utf-8"))
    assert details["pages"][0]["source_kind"] == "digital"
    assert details["pages"][0]["table_count"] == 1
    assert details["qa"]["status"] == qa.status


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


@pytest.mark.skipif(LocalTesseractOcr().executable is None, reason="local Tesseract is unavailable")
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
    assert any(image.getpixel((0, 0))[3] == 0 for image in media)
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


@pytest.mark.skipif(LocalTesseractOcr().executable is None, reason="local Tesseract is unavailable")
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
