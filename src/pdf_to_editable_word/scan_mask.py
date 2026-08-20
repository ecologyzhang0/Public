from __future__ import annotations

from io import BytesIO
from statistics import median

from PIL import Image, ImageDraw

from .document_model import ImageObject, PageModel, TextSpan


def build_ocr_cleaned_background(page: PageModel) -> ImageObject | None:
    """Create a scan background with only recognized plain-paper text removed."""
    background = next((image for image in page.images if image.is_page_background), None)
    if background is None or not page.text_spans:
        return None
    try:
        source = Image.open(BytesIO(background.data)).convert("RGB")
    except OSError:
        return None

    overlay = Image.new("RGBA", source.size, (0, 0, 0, 0))
    painter = ImageDraw.Draw(overlay)
    x_scale = source.width / background.bbox.width
    y_scale = source.height / background.bbox.height
    applied = False
    for span in page.text_spans:
        if span.confidence < 0.55:
            continue
        box = _image_box(span, background, source.size, x_scale, y_scale)
        if box is None:
            continue
        fill = _plain_paper_color(source, box)
        if fill is None:
            continue
        painter.rectangle(box, fill=(*fill, 255))
        applied = True
    if not applied:
        return None

    cleaned = Image.alpha_composite(source.convert("RGBA"), overlay)
    output = BytesIO()
    # A full-page scan is an opaque document surface. RGB avoids a LibreOffice
    # rendering bug for alpha PNGs inside legacy positioned Word frames.
    cleaned.convert("RGB").save(output, format="PNG")
    return ImageObject(
        bbox=background.bbox,
        data=output.getvalue(),
        extension="png",
        is_page_background=True,
        is_ocr_cleaned_background=True,
        source_xref=background.source_xref,
    )


def _image_box(
    span: TextSpan,
    background: ImageObject,
    size: tuple[int, int],
    x_scale: float,
    y_scale: float,
) -> tuple[int, int, int, int] | None:
    padding_x = max(round(x_scale * 0.9), 2)
    padding_y = max(round(y_scale * 0.8), 2)
    x0 = round((span.bbox.x0 - background.bbox.x0) * x_scale) - padding_x
    y0 = round((span.bbox.y0 - background.bbox.y0) * y_scale) - padding_y
    x1 = round((span.bbox.x1 - background.bbox.x0) * x_scale) + padding_x
    y1 = round((span.bbox.y1 - background.bbox.y0) * y_scale) + padding_y
    x0 = min(max(x0, 0), size[0])
    y0 = min(max(y0, 0), size[1])
    x1 = min(max(x1, 0), size[0])
    y1 = min(max(y1, 0), size[1])
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    return x0, y0, x1, y1


def _plain_paper_color(source: Image.Image, box: tuple[int, int, int, int]) -> tuple[int, int, int] | None:
    x0, y0, x1, y1 = box
    sample_points: list[tuple[int, int]] = []
    for x in range(x0, x1, max((x1 - x0) // 12, 1)):
        sample_points.extend(((x, max(y0 - 2, 0)), (x, min(y1 + 2, source.height - 1))))
    for y in range(y0, y1, max((y1 - y0) // 5, 1)):
        sample_points.extend(((max(x0 - 2, 0), y), (min(x1 + 2, source.width - 1), y)))

    neutral: list[tuple[int, int, int]] = []
    for x, y in sample_points:
        red, green, blue = source.getpixel((x, y))
        brightness = (red + green + blue) / 3
        if brightness >= 155 and max(red, green, blue) - min(red, green, blue) <= 38:
            neutral.append((red, green, blue))
    if len(neutral) < 8:
        return None
    fill = tuple(round(median(channel)) for channel in zip(*neutral))
    variation = max(
        abs(component - fill[index])
        for sample in neutral
        for index, component in enumerate(sample)
    )
    if variation > 42:
        return None

    # Colored seals and photos need to remain untouched even when OCR misreads them as text.
    colored = 0
    x_step = max((x1 - x0) // 24, 1)
    y_step = max((y1 - y0) // 12, 1)
    for x in range(x0, x1, x_step):
        for y in range(y0, y1, y_step):
            red, green, blue = source.getpixel((x, y))
            if max(red, green, blue) - min(red, green, blue) > 70:
                colored += 1
    sampled = max(((x1 - x0) // x_step + 1) * ((y1 - y0) // y_step + 1), 1)
    if colored / sampled > 0.08:
        return None
    return fill
