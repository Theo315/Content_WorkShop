"""Headless PDF find/replace with bounded fitting and collision checks."""
from dataclasses import dataclass
import re
import pymupdf as fitz
from .sds import SDSError, open_pdf, normalized, FONT, word_signature
from .settings import ASSETS


@dataclass
class Match:
    page: int
    rect: fitz.Rect
    text: str
    size: float
    color: tuple
    font: str


def parse_pages(spec, count):
    if spec.strip().lower() in ("", "all", "усі", "всі", "*"):
        return list(range(count))
    selected = set()
    for part in spec.replace(";", ",").split(","):
        match = re.fullmatch(r"\s*(\d+)(?:\s*-\s*(\d+))?\s*", part)
        if not match:
            raise SDSError("Сторінки: all або 1,3-5.")
        start, end = int(match[1]), int(match[2] or match[1])
        if not 1 <= start <= end <= count:
            raise SDSError("Сторінки поза межами PDF або некоректний діапазон.")
        selected.update(range(start - 1, end))
    return sorted(selected)


def style(page, rect):
    spans = [span for block in page.get_text("dict")["blocks"] if block.get("type") == 0
             for line in block["lines"] for span in line["spans"] if span["text"].strip()]
    overlapping = [(max(0, (fitz.Rect(span["bbox"]) & rect).get_area()), span) for span in spans]
    span = max(overlapping, key=lambda pair: pair[0])[1] if overlapping else {}
    color = span.get("color", 0)
    flags = span.get("flags", 0)
    variant = "BoldItalic" if flags & 16 and flags & 2 else "Bold" if flags & 16 else "Italic" if flags & 2 else "Regular"
    font = ASSETS / f"fonts/NotoSans-{variant}.ttf"
    return max(5.5, min(float(span.get("size", 10)), 72)), ((color >> 16 & 255) / 255, (color >> 8 & 255) / 255, (color & 255) / 255), str(font)


def find_matches(doc, search, pages_spec="all", exact_case=True, replace_all=True):
    needle = normalized(search)
    if not needle:
        raise SDSError("Поле пошуку порожнє.")
    matches = []
    for index in parse_pages(pages_spec, doc.page_count):
        page = doc[index]
        for rect in page.search_for(search):
            text = normalized(page.get_textbox(rect))
            if (text != needle) if exact_case else (text.casefold() != needle.casefold()):
                continue
            size, color, font = style(page, rect)
            matches.append(Match(index, rect, text, size, color, font))
            if len(matches) > 500:
                raise SDSError("Понад 500 збігів. Звузьте пошук або діапазон сторінок.")
            if not replace_all:
                return matches
    return matches


def obstacles_for(page, source):
    return [rect for w in page.get_text("words") if not source.contains((rect := fitz.Rect(w[:4])).tl + (rect.br - rect.tl) / 2)]


def intersects(rect, obstacles):
    return any((overlap := rect & obstacle).width > 0.4 and overlap.height > 1 for obstacle in obstacles)


def candidates(page, match):
    source = match.rect
    centre = (source.tl + source.br) / 2
    boxes = [drawing["rect"] for drawing in page.get_drawings()
             if drawing.get("rect") and drawing["rect"].contains(centre)
             and drawing["rect"].width > source.width + 4 and drawing["rect"].height > source.height + 4
             and drawing["rect"].get_area() < page.rect.get_area() * .8]
    box = min(boxes, key=lambda rect: rect.get_area()) if boxes else None
    left_limit = box.x0 + 2 if box else 18
    right = box.x1 - 2 if box else page.rect.x1 - 18
    bottom_limit = box.y1 - 2 if box else page.rect.y1 - 18
    obstacles = obstacles_for(page, source)
    band = fitz.Rect(source.x1, source.y0 - 1, right, source.y1 + 1)
    for obstacle in obstacles:
        overlap = band & obstacle
        if overlap.width > 0 and overlap.height > 1 and obstacle.x0 >= source.x1 - .5:
            right = min(right, obstacle.x0 - 1.5)
    left = max(left_limit, source.x0 - .6)
    rects = [fitz.Rect(left, source.y0 - .7, source.x1 + .8, source.y1 + .7),
             fitz.Rect(left, source.y0 - .7, max(source.x1 + .8, right), source.y1 + .7)]
    for lines in (2, 3):
        bottom = min(bottom_limit, source.y0 + max(source.height, match.size * 1.25) * lines + 1)
        rects.append(fitz.Rect(left, source.y0 - .7, max(source.x1 + .8, right), bottom))
    return [rect for candidate in rects if (rect := candidate & page.rect).width > 2 and rect.height > 2 and not intersects(rect, obstacles)]


def placement(page, match, replacement):
    align = fitz.TEXT_ALIGN_CENTER if abs((match.rect.x0 + match.rect.x1) / 2 - page.rect.width / 2) < max(18, match.rect.width * .15) else fitz.TEXT_ALIGN_LEFT
    if not replacement:
        return match.rect, match.size, align
    font = fitz.Font(fontfile=match.font)
    if any(not font.has_glyph(ord(ch)) for ch in replacement if not ch.isspace()):
        raise SDSError("Новий текст містить символи, яких немає у шрифті.")
    best = None
    with fitz.open() as scratch:
        probe = scratch.new_page(width=page.rect.width, height=page.rect.height)
        for rect in candidates(page, match):
            size = match.size
            while size >= 5.49:
                shape = probe.new_shape()
                remaining = shape.insert_textbox(rect, replacement, fontname="EditorNoto", fontfile=match.font,
                                                fontsize=size, color=match.color, align=align, lineheight=1.15)
                if remaining >= 0:
                    score = size - rect.get_area() / page.rect.get_area()
                    if best is None or score > best[0]:
                        best = (score, rect, size, align)
                    break
                size -= .25
    if best is None:
        raise SDSError(f"Сторінка {match.page + 1}: новий текст не вміщується без накладання. Скоротіть текст.")
    return best[1:]


def find(data, search, pages_spec="all", exact_case=True, replace_all=True):
    with open_pdf(data) as doc:
        return [{"page": m.page + 1, "text": m.text, "rect": list(m.rect)}
                for m in find_matches(doc, search, pages_spec, exact_case, replace_all)]


def edit(data, search, replacement, pages_spec="all", exact_case=True, replace_all=True):
    if len(replacement) > 2000:
        raise SDSError("Новий текст довший за 2000 символів.")
    with open_pdf(data) as doc:
        matches = find_matches(doc, search, pages_spec, exact_case, replace_all)
        if not matches:
            raise SDSError("Текст не знайдено на вибраних сторінках.")
        plans = []
        for match in matches:
            target, size, align = placement(doc[match.page], match, replacement)
            for other, rect, _, _ in plans:
                overlap = rect & target
                if other.page == match.page and overlap.width > .8 and overlap.height > .8:
                    raise SDSError("Області замін перетинаються. Виконайте заміни окремо.")
            plans.append((match, target, size, align))
        original_pages = doc.page_count
        protected = {}
        for index in range(doc.page_count):
            page_plans = [plan for plan in plans if plan[0].page == index]
            exclusions = [m.rect + (-.4, -.3, .4, .3) for m, _, _, _ in page_plans]
            protected[index] = word_signature(doc[index], exclusions)
            if not page_plans:
                continue
            page = doc[index]
            for match, _, _, _ in page_plans:
                page.add_redact_annot(match.rect + (-.35, -.25, .35, .25), fill=(1, 1, 1), cross_out=False)
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
            for match, target, size, align in page_plans:
                if replacement:
                    variant = match.font.rsplit("NotoSans-", 1)[-1].split(".")[0]
                    spare = page.insert_textbox(target, replacement, fontname="Editor" + variant, fontfile=match.font,
                                                fontsize=size, color=match.color, align=align, lineheight=1.15, overlay=True)
                    if spare < 0:
                        raise SDSError("Не вдалося вставити текст у розраховану область.")
        doc.subset_fonts()
        output = doc.tobytes(garbage=4, deflate=True, clean=True)
    # Verify no collateral text loss. Insertion regions are excluded from both sides.
    with open_pdf(data) as source, open_pdf(output) as result:
        if result.page_count != original_pages:
            raise SDSError("Після редагування змінилася кількість сторінок.")
        for index in range(original_pages):
            exclusions = [r | m.rect for m, r, _, _ in plans if m.page == index]
            if word_signature(source[index], exclusions) != word_signature(result[index], exclusions):
                raise SDSError(f"Після заміни змінено сусідній текст на сторінці {index + 1}.")
        for match, target, _, _ in plans:
            if replacement and normalized(result[match.page].get_textbox(target)) != normalized(replacement):
                raise SDSError("Вставлений текст не пройшов перевірку читання.")
    return output, len(matches), original_pages, matches[0].page
