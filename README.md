# PDF To Editable Word

Windows-first local application that converts each selected PDF into an editable DOCX. Its UI uses the standard Windows Python runtime rather than a large third-party GUI toolkit to keep the portable executable small.

The application has one user workflow: choose a PDF, choose an output folder, and convert. It automatically uses the PDF text layer when available and invokes local OCR only when a page does not contain usable text.

## Current Scope

- Rebuilds digital-PDF text as editable, page-positioned Word paragraphs.
- Detects scanned pages and invokes local Tesseract OCR when it is available.
- Preserves extracted PDF images; red or blue seal-like images receive an alpha-preserving transparent-background pass before being overlaid in Word.
- Writes a conversion report and flags every file for final visual review in Word.
- Includes a Windows cloud-build workflow for a portable executable.

## Important Acceptance Boundary

PDF uses fixed coordinates while Word is a reflowing document format. The converter preserves page size and object positions as far as Word permits, but a target Windows computer with Microsoft Word remains the final visual acceptance environment. The current implementation is not yet an acceptance-ready claim of exact fidelity for arbitrary complex tables, forms, mixed fonts, or overlapping objects.

## Development

```bash
uv venv .venv --python 3.9
uv pip install --python .venv/bin/python -e . pytest pyinstaller
.venv/bin/python -m pytest
```

## Windows Cloud Build

Push this directory to a GitHub repository, then run the `Build Windows EXE` workflow. Download the `PDFtoEditableWord-windows` artifact and extract `PDFtoEditableWord.exe`. The build bundles Tesseract and Chinese language data for offline scanned-PDF recognition.

The executable performs no network upload. The app only uses the network during the build to obtain the open-source Chinese language model; the finished executable processes PDFs locally.
