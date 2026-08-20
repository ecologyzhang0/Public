from __future__ import annotations

from dataclasses import asdict, dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw

try:
    import pymupdf
except ImportError:  # PyMuPDF before 1.24 exposed this module name.
    import fitz as pymupdf


@dataclass
class BoundingBox:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return max(self.x1 - self.x0, 1.0)

    @property
    def height(self) -> float:
        return max(self.y1 - self.y0, 1.0)


@dataclass
class TextSpan:
    text: str
    bbox: BoundingBox
    font_name: str
    font_size: float
    color: int
    east_asia_font_name: str | None = None
    bold: bool = False
    italic: bool = False
    underline: bool = False
    source: str = "text_layer"
    confidence: float = 1.0


@dataclass
class ImageObject:
    bbox: BoundingBox
    data: bytes
    extension: str
    is_stamp: bool = False
    is_page_background: bool = False
    is_ocr_cleaned_background: bool = False
    source_xref: int | None = None


@dataclass
class TableObject:
    bbox: BoundingBox
    values: list[list[str]]
    cell_boxes: list[list[BoundingBox | None]]


@dataclass
class VectorObject:
    bbox: BoundingBox
    data: bytes


@dataclass
class PageModel:
    number: int
    width: float
    height: float
    rotation: int
    text_spans: list[TextSpan] = field(default_factory=list)
    images: list[ImageObject] = field(default_factory=list)
    tables: list[TableObject] = field(default_factory=list)
    vectors: list[VectorObject] = field(default_factory=list)
    source_kind: str = "digital"
    qa_flags: list[str] = field(default_factory=list)


@dataclass
class DocumentModel:
    source_path: str
    pages: list[PageModel]

    def report_dict(self) -> dict:
        return {
            "source_pdf": self.source_path,
            "pages": [
                {
                    "number": page.number,
                    "size_points": {"width": page.width, "height": page.height},
                    "rotation": page.rotation,
                    "source_kind": page.source_kind,
                    "text_span_count": len(page.text_spans),
                    "image_count": len(page.images),
                    "stamp_count": sum(image.is_stamp for image in page.images),
                    "ocr_cleaned_background_count": sum(
                        image.is_ocr_cleaned_background for image in page.images
                    ),
                    "table_count": len(page.tables),
                    "vector_count": len(page.vectors),
                    "qa_flags": page.qa_flags,
                }
                for page in self.pages
            ],
        }


def _bbox(values: Iterable[float]) -> BoundingBox:
    x0, y0, x1, y1 = values
    return BoundingBox(float(x0), float(y0), float(x1), float(y1))


def _is_seal_like(data: bytes) -> bool:
    try:
        image = Image.open(BytesIO(data)).convert("RGB")
    except OSError:
        return False
    pixels = list(image.resize((96, 96)).getdata())
    if not pixels:
        return False
    colored = 0
    for red, green, blue in pixels:
        red_ink = red > 105 and red > green * 1.25 and red > blue * 1.25
        blue_ink = blue > 95 and blue > red * 1.18 and blue > green * 1.08
        if red_ink or blue_ink:
            colored += 1
    return colored / len(pixels) >= 0.025


def transparent_seal(data: bytes) -> bytes:
    """Preserve colored seal ink and alpha while removing a white paper background."""
    image = Image.open(BytesIO(data)).convert("RGBA")
    result = Image.new("RGBA", image.size)
    source = image.load()
    target = result.load()
    for y in range(image.height):
        for x in range(image.width):
            red, green, blue, alpha = source[x, y]
            saturation = max(red, green, blue) - min(red, green, blue)
            red_ink = red > 75 and red > green * 1.14 and red > blue * 1.14
            blue_ink = blue > 70 and blue > red * 1.08 and blue > green * 1.04
            if (red_ink or blue_ink) and saturation > 28:
                target[x, y] = (red, green, blue, alpha)
            else:
                target[x, y] = (red, green, blue, 0)
    output = BytesIO()
    result.save(output, format="PNG")
    return output.getvalue()


class PdfAnalyzer:
    """Builds a document model without assuming a form, page size, or stamp shape."""

    def analyze(self, source_path: Path) -> DocumentModel:
        document = pymupdf.open(source_path)
        pages: list[PageModel] = []
        try:
            for index, page in enumerate(document):
                rect = page.rect
                model = PageModel(
                    number=index + 1,
                    width=float(rect.width),
                    height=float(rect.height),
                    rotation=int(page.rotation),
                    images=self._read_images(document, page),
                    tables=self._read_tables(page),
                    vectors=self._read_vectors(page),
                )
                model.text_spans = self._read_text_spans(page)
                if len("".join(span.text.strip() for span in model.text_spans)) < 8:
                    model.source_kind = "scanned"
                    model.qa_flags.append("text_layer_missing_or_insufficient")
                pages.append(model)
        finally:
            document.close()
        return DocumentModel(source_path=str(source_path), pages=pages)

    @staticmethod
    def _read_text_spans(page) -> list[TextSpan]:
        spans: list[TextSpan] = []
        data = page.get_text("dict")
        for block in data.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text", "")
                    if not text or not text.strip():
                        continue
                    font_name = str(span.get("font", "Arial"))
                    flags = int(span.get("flags", 0))
                    normalized_font = font_name.casefold().replace("-", "").replace("_", "")
                    span_box = _bbox(span["bbox"])
                    spans.append(
                        TextSpan(
                            text=text,
                            bbox=span_box,
                            font_name=font_name,
                            font_size=max(float(span.get("size", 10)), 4.0),
                            color=int(span.get("color", 0)),
                            bold=bool(flags & 16) or "bold" in normalized_font or "black" in normalized_font,
                            italic=bool(flags & 2) or "italic" in normalized_font or "oblique" in normalized_font,
                            underline="underline" in normalized_font,
                        )
                    )
        return spans

    @staticmethod
    def _read_images(document, page) -> list[ImageObject]:
        images: list[ImageObject] = []
        seen: set[tuple[int, int, int, int, int]] = set()
        for image_ref in page.get_images(full=True):
            xref = int(image_ref[0])
            extracted = document.extract_image(xref)
            if not extracted:
                continue
            for rect in page.get_image_rects(xref):
                key = (xref, round(rect.x0), round(rect.y0), round(rect.x1), round(rect.y1))
                if key in seen:
                    continue
                seen.add(key)
                payload = extracted["image"]
                is_page_background = (
                    rect.width >= page.rect.width * 0.92
                    and rect.height >= page.rect.height * 0.92
                )
                is_stamp = not is_page_background and _is_seal_like(payload)
                images.append(
                    ImageObject(
                        bbox=BoundingBox(float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)),
                        data=transparent_seal(payload) if is_stamp else payload,
                        extension="png" if is_stamp else extracted.get("ext", "png"),
                        is_stamp=is_stamp,
                        is_page_background=is_page_background,
                        source_xref=xref,
                    )
                )
        return images

    @staticmethod
    def _read_vectors(page) -> list[VectorObject]:
        """Rasterize vector-only PDF decoration into a transparent overlay.

        Text is deliberately not part of this bitmap, so Word keeps it editable.
        """
        try:
            drawings = page.get_drawings()
        except Exception:
            return []
        if not drawings:
            return []

        scale = 2
        canvas = Image.new(
            "RGBA",
            (max(round(page.rect.width * scale), 1), max(round(page.rect.height * scale), 1)),
            (0, 0, 0, 0),
        )
        painter = ImageDraw.Draw(canvas)
        for drawing in drawings:
            stroke_color = drawing.get("color")
            fill_color = drawing.get("fill")
            if stroke_color is None and fill_color is None:
                continue

            def to_rgba(color, opacity):
                alpha = round(255 * float(opacity if opacity is not None else 1))
                return tuple(
                    round(max(0, min(component, 1)) * 255) for component in color
                ) + (alpha,)

            stroke_rgba = (
                to_rgba(stroke_color, drawing.get("stroke_opacity"))
                if stroke_color is not None
                else None
            )
            fill_rgba = (
                to_rgba(fill_color, drawing.get("fill_opacity"))
                if fill_color is not None
                else None
            )
            width = max(round(float(drawing.get("width") or 1) * scale), 1)
            for item in drawing.get("items", []):
                operator = item[0]
                if operator == "l":
                    start, end = item[1], item[2]
                    painter.line(
                        (start.x * scale, start.y * scale, end.x * scale, end.y * scale),
                        fill=stroke_rgba or fill_rgba,
                        width=width,
                    )
                elif operator == "re":
                    rect = item[1]
                    painter.rectangle(
                        (rect.x0 * scale, rect.y0 * scale, rect.x1 * scale, rect.y1 * scale),
                        fill=fill_rgba,
                        outline=stroke_rgba,
                        width=width,
                    )
                elif operator == "c" and len(item) == 5:
                    start, control_one, control_two, end = item[1:]
                    curve = []
                    for step in range(17):
                        t = step / 16
                        inverse_t = 1 - t
                        x = (
                            inverse_t**3 * start.x
                            + 3 * inverse_t**2 * t * control_one.x
                            + 3 * inverse_t * t**2 * control_two.x
                            + t**3 * end.x
                        )
                        y = (
                            inverse_t**3 * start.y
                            + 3 * inverse_t**2 * t * control_one.y
                            + 3 * inverse_t * t**2 * control_two.y
                            + t**3 * end.y
                        )
                        curve.append((x * scale, y * scale))
                    painter.line(curve, fill=stroke_rgba or fill_rgba, width=width)
                elif operator == "qu":
                    quad = item[1]
                    points = [quad.ul, quad.ur, quad.lr, quad.ll]
                    polygon = [(point.x * scale, point.y * scale) for point in points]
                    painter.polygon(polygon, fill=fill_rgba)
                    painter.line(polygon + [polygon[0]], fill=stroke_rgba or fill_rgba, width=width)
        content_box = canvas.getbbox()
        if not content_box:
            return []
        # Keep anti-aliased outer strokes inside the generated image rather than clipping them.
        padding = 4
        content_box = (
            max(content_box[0] - padding, 0),
            max(content_box[1] - padding, 0),
            min(content_box[2] + padding, canvas.width),
            min(content_box[3] + padding, canvas.height),
        )
        cropped = canvas.crop(content_box)
        output = BytesIO()
        cropped.save(output, format="PNG")
        x0, y0, x1, y1 = content_box
        return [
            VectorObject(
                bbox=BoundingBox(x0 / scale, y0 / scale, x1 / scale, y1 / scale),
                data=output.getvalue(),
            )
        ]

    @staticmethod
    def _read_tables(page) -> list[TableObject]:
        try:
            candidates = page.find_tables().tables
        except Exception:
            return []

        tables: list[TableObject] = []
        for candidate in candidates:
            try:
                values = [[value or "" for value in row] for row in candidate.extract()]
                cell_boxes = [
                    [_bbox(cell) if cell is not None else None for cell in row.cells]
                    for row in candidate.rows
                ]
            except Exception:
                continue
            if not values or not any(any(value.strip() for value in row) for row in values):
                continue
            tables.append(
                TableObject(
                    bbox=_bbox(candidate.bbox),
                    values=values,
                    cell_boxes=cell_boxes,
                )
            )
        return tables
