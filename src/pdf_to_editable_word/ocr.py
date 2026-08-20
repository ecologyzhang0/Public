from __future__ import annotations

import csv
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

try:
    import pymupdf
except ImportError:
    import fitz as pymupdf

from .document_model import BoundingBox, TextSpan


class OcrUnavailableError(RuntimeError):
    pass


class LocalTesseractOcr:
    """Local OCR adapter. It never sends a page to a network service."""

    def __init__(self, executable: str | None = None, languages: str = "chi_sim+eng"):
        self.executable = executable or self._find_executable()
        self.languages = languages

    @staticmethod
    def _find_executable() -> str | None:
        configured = os.environ.get("PDF_TO_WORD_TESSERACT")
        if configured and Path(configured).is_file():
            return configured
        bundle_root = getattr(sys, "_MEIPASS", None)
        if bundle_root:
            bundled = Path(bundle_root) / "tesseract" / "tesseract.exe"
            if bundled.is_file():
                return str(bundled)
        return shutil.which("tesseract")

    def extract(self, pdf_path: Path, page_index: int) -> list[TextSpan]:
        if not self.executable:
            raise OcrUnavailableError(
                "该页面需要文字识别，但本机 OCR 组件不可用。"
            )
        document = pymupdf.open(pdf_path)
        try:
            page = document[page_index]
            scale = 200 / 72
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            with tempfile.TemporaryDirectory(prefix="pdf2word-ocr-") as temp_dir:
                image_path = Path(temp_dir) / "page.png"
                pixmap.save(str(image_path))
                command = [
                    self.executable,
                    str(image_path),
                    "stdout",
                    "-l",
                    self.languages,
                    "--psm",
                    "3",
                    "tsv",
                ]
                environment = None
                bundled_tessdata = Path(self.executable).parent / "tessdata"
                if bundled_tessdata.is_dir():
                    environment = {**os.environ, "TESSDATA_PREFIX": str(bundled_tessdata)}
                completed = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=180,
                    env=environment,
                )
                if completed.returncode != 0:
                    raise OcrUnavailableError(completed.stderr.strip() or "OCR failed.")
                return self._to_lines(completed.stdout, scale)
        finally:
            document.close()

    @staticmethod
    def _to_lines(tsv: str, scale: float) -> list[TextSpan]:
        lines: dict[tuple[str, str, str, str], list[dict[str, str]]] = defaultdict(list)
        for row in csv.DictReader(tsv.splitlines(), delimiter="	"):
            text = (row.get("text") or "").strip()
            confidence = float(row.get("conf") or -1)
            if not text or confidence < 0:
                continue
            key = (row["block_num"], row["par_num"], row["line_num"], row["page_num"])
            lines[key].append(row)

        spans: list[TextSpan] = []
        for words in lines.values():
            confidence = sum(float(word["conf"]) for word in words) / len(words) / 100
            x0 = min(float(word["left"]) for word in words) / scale
            y0 = min(float(word["top"]) for word in words) / scale
            x1 = max(float(word["left"]) + float(word["width"]) for word in words) / scale
            y1 = max(float(word["top"]) + float(word["height"]) for word in words) / scale
            text = " ".join(word["text"] for word in words)
            spans.append(
                TextSpan(
                    text=text,
                    bbox=BoundingBox(x0, y0, x1, y1),
                    font_name="Arial",
                    font_size=max((y1 - y0) * 0.78, 7),
                    color=0,
                    source="ocr",
                    confidence=confidence,
                )
            )
        return spans
