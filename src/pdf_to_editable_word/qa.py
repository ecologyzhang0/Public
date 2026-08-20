from __future__ import annotations

import json
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from .document_model import DocumentModel


@dataclass
class QaResult:
    status: str
    flags: list[str]
    metrics: dict[str, int | float]

    def as_dict(self) -> dict:
        return {"status": self.status, "flags": self.flags, "metrics": self.metrics}


def run_editability_qa(model: DocumentModel, docx_path: Path) -> QaResult:
    flags: list[str] = []
    metrics: dict[str, int | float] = {
        "source_pages": len(model.pages),
        "source_text_spans": sum(len(page.text_spans) for page in model.pages),
        "source_images": sum(len(page.images) for page in model.pages),
        "source_stamps": sum(sum(image.is_stamp for image in page.images) for page in model.pages),
        "source_tables": sum(len(page.tables) for page in model.pages),
        "source_vector_overlays": sum(len(page.vectors) for page in model.pages),
        "ocr_cleaned_backgrounds": sum(
            sum(image.is_ocr_cleaned_background for image in page.images) for page in model.pages
        ),
    }
    if not docx_path.exists():
        return QaResult("FAIL", ["docx_missing"], metrics)
    try:
        with zipfile.ZipFile(docx_path) as archive:
            document_xml = archive.read("word/document.xml")
            media_files = [name for name in archive.namelist() if name.startswith("word/media/")]
        document_text = "".join(ElementTree.fromstring(document_xml).itertext())
    except (KeyError, zipfile.BadZipFile, ElementTree.ParseError):
        return QaResult("FAIL", ["docx_package_invalid"], metrics)

    expected_spans = [span.text.strip() for page in model.pages for span in page.text_spans if span.text.strip()]
    missing_spans = [text for text in expected_spans if text not in document_text]
    metrics["word_media_files"] = len(media_files)
    metrics["missing_editable_text_spans"] = len(missing_spans)
    if missing_spans:
        flags.append("editable_text_spans_missing_from_document_xml")
    if metrics["source_images"] and not media_files:
        flags.append("images_missing_from_docx")
    if any(page.source_kind == "scanned" for page in model.pages):
        flags.append("ocr_page_requires_visual_review")
    if any("text_layer_missing_or_insufficient" in page.qa_flags for page in model.pages):
        flags.append("source_text_layer_incomplete")
    if any(any(flag.startswith("ocr_unavailable:") for flag in page.qa_flags) for page in model.pages):
        flags.append("ocr_engine_unavailable")
    # A DOCX package inspection confirms editability primitives, not pixel-perfect rendering.
    flags.append("visual_similarity_requires_word_review")

    status = "FAIL" if missing_spans else "PASS_WITH_WARNING" if flags else "PASS"
    return QaResult(status, flags, metrics)


def write_conversion_report(
    model: DocumentModel, qa: QaResult, output_path: Path, ocr_mode: str
) -> Path:
    report = model.report_dict()
    report["output_docx"] = str(output_path)
    report["ocr_mode"] = ocr_mode
    report["qa"] = qa.as_dict()
    report_path = output_path.with_suffix(".conversion.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_path
