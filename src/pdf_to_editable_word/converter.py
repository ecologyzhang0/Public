from __future__ import annotations

from pathlib import Path
from typing import Callable

from .document_model import PdfAnalyzer
from .ocr import LocalTesseractOcr, OcrUnavailableError
from .qa import QaResult, run_editability_qa, write_conversion_report
from .scan_mask import build_ocr_cleaned_background, extract_scanned_stamps
from .word_builder import PositionedWordBuilder

ProgressCallback = Callable[[int, str], None]


class ConversionError(RuntimeError):
    pass


class PdfToWordConverter:
    def __init__(self, progress: ProgressCallback | None = None):
        self.progress = progress or (lambda _percent, _message: None)
        self.analyzer = PdfAnalyzer()
        self.ocr = LocalTesseractOcr()
        self.word_builder = PositionedWordBuilder()

    def convert(self, source_pdf: Path, output_dir: Path, ocr_mode: str = "auto") -> tuple[Path, QaResult, Path]:
        source_pdf = source_pdf.expanduser().resolve()
        output_dir = output_dir.expanduser().resolve()
        if source_pdf.suffix.lower() != ".pdf":
            raise ConversionError("请选择 PDF 文件。")
        if not source_pdf.is_file():
            raise ConversionError("找不到所选 PDF 文件。")
        output_dir.mkdir(parents=True, exist_ok=True)

        self.progress(8, "正在读取 PDF 结构")
        model = self.analyzer.analyze(source_pdf)
        for index, page in enumerate(model.pages):
            needs_ocr = ocr_mode == "always" or (ocr_mode == "auto" and page.source_kind == "scanned")
            if not needs_ocr:
                continue
            self.progress(15 + int(index / max(len(model.pages), 1) * 35), f"正在识别第 {page.number} 页")
            try:
                page.text_spans = self.ocr.extract(source_pdf, index)
                page.source_kind = "ocr"
                extracted_stamps = extract_scanned_stamps(page)
                if extracted_stamps:
                    page.qa_flags.append(f"scanned_stamps_extracted:{extracted_stamps}")
                if not page.text_spans:
                    page.qa_flags.append("ocr_returned_no_text")
                else:
                    cleaned_background = build_ocr_cleaned_background(page)
                    if cleaned_background is not None:
                        background_index = next(
                            index
                            for index, image in enumerate(page.images)
                            if image.is_page_background
                        )
                        page.images[background_index] = cleaned_background
                        page.qa_flags.append("ocr_text_background_cleaned")
            except OcrUnavailableError as error:
                page.qa_flags.append(f"ocr_unavailable:{error}")

        output_docx = output_dir / f"{source_pdf.stem}.docx"
        self.progress(62, "正在生成可编辑 Word 文件")
        self.word_builder.build(model, output_docx)
        self.progress(86, "正在检查可编辑内容")
        qa = run_editability_qa(model, output_docx)
        report_path = write_conversion_report(model, qa, output_docx, ocr_mode)
        self.progress(100, "转换完成")
        return output_docx, qa, report_path
