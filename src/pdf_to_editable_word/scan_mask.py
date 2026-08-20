from __future__ import annotations

from io import BytesIO
from statistics import median

from PIL import Image, ImageDraw

from .document_model import BoundingBox, ImageObject, PageModel, TextSpan


def extract_scanned_stamps(page: PageModel) -> int:
    """Separate clear red or blue seals from a full-page scan without touching uncertain art."""
    background = next((image for image in page.images if image.is_page_background), None)
    if background is None:
        return 0
    try:
        source = Image.open(BytesIO(background.data)).convert("RGB")
    except OSError:
        return 0

    candidates = _seal_candidates(source)
    if not candidates:
        return 0
    cleaned = source.copy()
    extracted: list[ImageObject] = []
    for box in candidates:
        stamp = _transparent_stamp(source, box)
        if stamp is None:
            continue
        _erase_seal_ink(cleaned, box)
        x0, y0, x1, y1 = box
        extracted.append(
            ImageObject(
                bbox=BoundingBox(
                    background.bbox.x0 + x0 * background.bbox.width / source.width,
                    background.bbox.y0 + y0 * background.bbox.height / source.height,
                    background.bbox.x0 + x1 * background.bbox.width / source.width,
                    background.bbox.y0 + y1 * background.bbox.height / source.height,
                ),
                data=stamp,
                extension="png",
                is_stamp=True,
            )
        )
    if not extracted:
        return 0

    output = BytesIO()
    cleaned.save(output, format="PNG")
    background_index = page.images.index(background)
    page.images[background_index] = ImageObject(
        bbox=background.bbox,
        data=output.getvalue(),
        extension="png",
        is_page_background=True,
        source_xref=background.source_xref,
    )
    page.images.extend(extracted)
    return len(extracted)


def suppress_ocr_text_under_stamps(page: PageModel) -> int:
    """Remove OCR artifacts generated from a scanned stamp's ink and outline."""

    stamps = [image for image in page.images if image.is_stamp]
    if not stamps or not page.text_spans:
        return 0
    original_count = len(page.text_spans)
    page.text_spans = [
        span
        for span in page.text_spans
        if not any(_span_overlaps_stamp(span, stamp) for stamp in stamps)
    ]
    return original_count - len(page.text_spans)


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


def _seal_candidates(source: Image.Image) -> list[tuple[int, int, int, int]]:
    """Find square-ish groups of saturated red or blue ink on the low-resolution grid."""
    cell_size = max(round(max(source.width, source.height) / 180), 6)
    columns = (source.width + cell_size - 1) // cell_size
    rows = (source.height + cell_size - 1) // cell_size
    occupied: set[tuple[int, int]] = set()
    for row in range(rows):
        y0 = row * cell_size
        y1 = min(y0 + cell_size, source.height)
        for column in range(columns):
            x0 = column * cell_size
            x1 = min(x0 + cell_size, source.width)
            colored = 0
            sampled = 0
            for y in range(y0, y1, 2):
                for x in range(x0, x1, 2):
                    sampled += 1
                    if _is_strong_seal_ink(source.getpixel((x, y))):
                        colored += 1
            if colored >= max(2, sampled // 18):
                occupied.add((column, row))

    # A stamp ring and its inner character are often disconnected. Join nearby grid cells.
    expanded = occupied | {
        (column + dx, row + dy)
        for column, row in occupied
        for dx in (-1, 0, 1)
        for dy in (-1, 0, 1)
        if 0 <= column + dx < columns and 0 <= row + dy < rows
    }
    candidates: list[tuple[int, int, int, int]] = []
    while expanded:
        start = expanded.pop()
        component = {start}
        frontier = [start]
        while frontier:
            column, row = frontier.pop()
            for neighbor in ((column - 1, row), (column + 1, row), (column, row - 1), (column, row + 1)):
                if neighbor in expanded:
                    expanded.remove(neighbor)
                    component.add(neighbor)
                    frontier.append(neighbor)
        if not (9 <= len(component) <= 900):
            continue
        x0 = min(column for column, _ in component) * cell_size
        y0 = min(row for _, row in component) * cell_size
        x1 = min((max(column for column, _ in component) + 1) * cell_size, source.width)
        y1 = min((max(row for _, row in component) + 1) * cell_size, source.height)
        width, height = x1 - x0, y1 - y0
        aspect = width / max(height, 1)
        if not (0.52 <= aspect <= 1.92 and 48 <= min(width, height) and max(width, height) <= 900):
            continue
        padding = max(round(min(width, height) * 0.08), 5)
        candidates.append(
            (
                max(x0 - padding, 0),
                max(y0 - padding, 0),
                min(x1 + padding, source.width),
                min(y1 + padding, source.height),
            )
        )
    return candidates


def _transparent_stamp(source: Image.Image, box: tuple[int, int, int, int]) -> bytes | None:
    crop = source.crop(box).convert("RGBA")
    pixels = crop.load()
    colored = 0
    for y in range(crop.height):
        for x in range(crop.width):
            red, green, blue, _alpha = pixels[x, y]
            if _is_seal_ink((red, green, blue)):
                pixels[x, y] = (red, green, blue, 255)
                colored += 1
            else:
                pixels[x, y] = (red, green, blue, 0)
    if colored < 90:
        return None
    output = BytesIO()
    crop.save(output, format="PNG")
    return output.getvalue()


def _erase_seal_ink(source: Image.Image, box: tuple[int, int, int, int]) -> None:
    fill = _plain_paper_color(source, box) or (255, 255, 255)
    pixels = source.load()
    x0, y0, x1, y1 = box
    for y in range(y0, y1):
        for x in range(x0, x1):
            if _is_seal_ink(pixels[x, y]):
                pixels[x, y] = fill


def _is_seal_ink(pixel: tuple[int, int, int]) -> bool:
    red, green, blue = pixel
    return (
        (red >= 175 and red - max(green, blue) >= 4)
        or (blue >= 175 and blue - max(red, green) >= 4)
    )


def _is_strong_seal_ink(pixel: tuple[int, int, int]) -> bool:
    red, green, blue = pixel
    return (
        (red >= 105 and red - max(green, blue) >= 58)
        or (blue >= 100 and blue - max(red, green) >= 52)
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


def _span_overlaps_stamp(span: TextSpan, stamp: ImageObject) -> bool:
    overlap_width = max(0.0, min(span.bbox.x1, stamp.bbox.x1) - max(span.bbox.x0, stamp.bbox.x0))
    overlap_height = max(0.0, min(span.bbox.y1, stamp.bbox.y1) - max(span.bbox.y0, stamp.bbox.y0))
    if not overlap_width or not overlap_height:
        return False
    span_area = max(span.bbox.width * span.bbox.height, 1.0)
    overlap_ratio = overlap_width * overlap_height / span_area
    center_x = (span.bbox.x0 + span.bbox.x1) / 2
    center_y = (span.bbox.y0 + span.bbox.y1) / 2
    center_in_stamp = stamp.bbox.x0 <= center_x <= stamp.bbox.x1 and stamp.bbox.y0 <= center_y <= stamp.bbox.y1
    return center_in_stamp or overlap_ratio >= 0.2


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
