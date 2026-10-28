"""Bounded title/variant replacement for the 49-page, three-component SDS.

This changes product identification only, never the three mixture compositions.
Unknown layouts fail closed instead of reusing the legacy 41-page rectangles.
"""
from dataclasses import dataclass, replace
from functools import lru_cache
import re
import uuid

import pymupdf as fitz

from .sds import SDSError, FONT, normalized, open_pdf, validate_title, word_signature

HEADER = fitz.Rect(60, 63, 536, 75.5)
SUBTITLE = fitz.Rect(60, 77, 536, 89)
TITLE = re.compile(r"Asturcolor 3-Marker Car Touch-up Kit for (.+?) \((.+)\)")
DATE = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")
MIN_FONT_SIZE = 5.5
MIN_WIDTH_SCALE = .85


def canonical(text):
    return normalized(text.translate(str.maketrans({"\xad": "-", "\u2011": "-", "\u2010": "-"})))


def identity(title):
    match = TITLE.fullmatch(canonical(title))
    if not match:
        raise SDSError("Для 3-Marker використайте назву Asturcolor 3-Marker Car Touch-up Kit for {BRAND} ({VALUE}).")
    return match[1], match[2]


def preflight(doc, *, with_plans=False):
    if doc.page_count != 49:
        raise SDSError("Еталон 3-Marker повинен мати 49 сторінок.")
    headings = [canonical(page.get_text(clip=HEADER)) for page in doc]
    if len(set(headings)) != 1:
        raise SDSError("Еталон 3-Marker має неузгоджені або відсутні заголовки сторінок.")
    brand, value = identity(headings[0])
    for index, page in enumerate(doc):
        if abs(page.rect.width - 595.276) > 2 or abs(page.rect.height - 841.89) > 2:
            raise SDSError("Непідтримуваний розмір сторінки 3-Marker: очікується A4.")
        expected = ("Epoxy Primer + Paint + Clear Varnish" if index == 0 else
                    "Asturcolor Epoxy Primer - Marker 1 of 3" if index < 8 else
                    "Asturcolor Touch-up Paint - Marker 2 of 3" if index < 31 else
                    "Asturcolor Clear Varnish - Marker 3 of 3")
        if canonical(page.get_text(clip=SUBTITLE)) != expected:
            raise SDSError(f"Непідтримувана структура компонентів 3-Marker на сторінці {index + 1}.")
    # Require the identifying cover and paint fields too, not only page count.
    replacements = plans(doc, headings[0], brand, value)
    return (brand, value, replacements) if with_plans else (brand, value)


@dataclass
class Replacement:
    page: int
    source: fitz.Rect
    target: fitz.Rect
    text: str
    size: float
    baseline: float
    bold: bool = False
    centered: bool = False
    kind: str = ""
    original: str = ""


def lines(page):
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            yield line, canonical("".join(span["text"] for span in line["spans"]))


def replacement(page_index, line, text, target, centered=False, kind=""):
    spans = line["spans"]
    return Replacement(page_index, fitz.Rect(line["bbox"]) + (-.25, -.2, .25, .2),
                       fitz.Rect(target), text, min(s["size"] for s in spans), spans[0]["origin"][1],
                       any(s["flags"] & 16 for s in spans), centered, kind,
                       canonical("".join(span["text"] for span in spans)))


def cover_cell_right(page, line):
    """Use the actual stroked column borders, never old white redaction fills."""
    rect, baseline = fitz.Rect(line["bbox"]), line["spans"][0]["origin"][1]
    edges = []
    for drawing in page.get_drawings():
        if "s" not in drawing["type"]:
            continue
        for item in drawing["items"]:
            if item[0] == "l":
                a, b = item[1:3]
                if abs(a.x - b.x) < .1 and min(a.y, b.y) <= baseline <= max(a.y, b.y):
                    edges.append(a.x)
            elif item[0] == "re" and item[1].y0 <= baseline <= item[1].y1:
                edges.extend((item[1].x0, item[1].x1))
    # An outer page/section frame is not evidence of this table's column.
    if not any(rect.x0 - 12 <= x < rect.x0 for x in edges):
        return 354
    right = min((x for x in edges if rect.x1 < x <= 552), default=354)
    # Never expand into a neighbouring cell's text, even with an unusual grid.
    neighbours = [word[0] for word in page.get_text("words")
                  if word[0] > rect.x1 and word[1] < rect.y1 and word[3] > rect.y0]
    return min(right, min(neighbours) - 2) if neighbours else right


def plans(doc, title, brand, value):
    old_title = canonical(doc[0].get_text(clip=HEADER))
    old_brand, old_value = identity(old_title)
    old_variant = f"{old_brand} {old_value}"
    paint = "Asturcolor Touch-up Paint - "
    result = []
    counts = dict(header=0, cover=0, paint=0, prose=0)
    for index, page in enumerate(doc):
        for line, text in lines(page):
            rect = fitz.Rect(line["bbox"])
            if HEADER.contains((rect.tl + rect.br) / 2) and text == old_title:
                result.append(replacement(index, line, title, HEADER, centered=True, kind="header"))
                counts["header"] += 1
            elif index == 0 and 145 <= rect.y0 <= 150 and text in (old_title, old_title.replace(f"({old_value})", old_value)):
                result.append(replacement(index, line, title, (69, 145, 552, 158), kind="cover"))
                counts["cover"] += 1
            elif text == paint + old_variant and index in (0, 8, 16):
                right = cover_cell_right(page, line) if index == 0 else 552
                result.append(replacement(index, line, paint + brand + " " + value,
                                          (rect.x0, rect.y0 - .5, right, rect.y1 + .5), kind="paint"))
                counts["paint"] += 1
            elif index in (0, 4) and ((index == 0 and text.startswith("Three separate mixtures,")) or
                                     (index == 4 and text.startswith("9.1 Basic physical and chemical properties:"))):
                # Replace the explicit brand reference in the explanatory line;
                # don't leave "BMW colour names" when making a different make.
                if not re.search(r"(?<!\w)" + re.escape(old_brand) + r"(?!\w)", text):
                    raise SDSError("Не знайдено марку в пояснювальному тексті 3-Marker.")
                changed = re.sub(r"(?<!\w)" + re.escape(old_brand) + r"(?!\w)", lambda _: brand, text)
                result.append(replacement(index, line, changed, (rect.x0, rect.y0 - .5, 557, rect.y1 + .5), kind="prose"))
                counts["prose"] += 1
    if counts != dict(header=49, cover=1, paint=3, prose=2):
        raise SDSError("Еталон 3-Marker має інші зони ідентифікації. Жодних змін не застосовано.")
    return result


def fitted(plan, fonts=None):
    font_path = FONT.with_name("NotoSans-Bold.ttf") if plan.bold else FONT
    fonts = {} if fonts is None else fonts
    if font_path not in fonts:
        fonts[font_path] = fitz.Font(fontfile=str(font_path))
    font = fonts[font_path]
    # One line per bounded existing row: never push the component subtitle,
    # adjacent table cells or safety paragraphs down.
    available = plan.target.width - 2
    size = max(MIN_FONT_SIZE, min(plan.size, available / max(font.text_length(plan.text, fontsize=1), .01)))
    # Keep small table labels readable. A normal-width 5.48 pt label should not
    # fail a 5.5 pt floor: modest horizontal fitting preserves its height and
    # stays strictly inside the same cell. Bound compression for very long text.
    width_scale = min(1, available / max(font.text_length(plan.text, fontsize=size), .01))
    if width_scale < MIN_WIDTH_SCALE:
        raise SDSError(f"Сторінка {plan.page + 1}: назва не вміщується без накладання. Скоротіть назву марки/кольору.")
    width = font.text_length(plan.text, fontsize=size) * width_scale
    x = (plan.target.x0 + plan.target.x1 - width) / 2 if plan.centered else plan.target.x0
    baseline = min(plan.baseline, plan.target.y1 - max(font.descender * -size, .5))
    return font_path, size, x, baseline, width_scale


@dataclass(frozen=True)
class PreparedReference:
    brand: str
    replacements: tuple
    protected_words: tuple
    dates: tuple


@lru_cache(maxsize=2)
def prepare_reference(reference_data):
    """Cache source analysis by exact bytes, never live documents or text pages.

    Each output still has every identity, date and protected word checked. A new
    upload (including a regenerated PDF) necessarily gets its own analysis.
    """
    with open_pdf(reference_data) as source:
        brand, _, replacements = preflight(source, with_plans=True)
        protected, dates = [], []
        for index, page in enumerate(source):
            textpage = page.get_textpage(flags=fitz.TEXTFLAGS_TEXT)
            excluded = [p.source | p.target for p in replacements if p.page == index]
            protected.append(tuple(word_signature(page, excluded, textpage.extractWORDS())))
            dates.append(tuple(DATE.findall(textpage.extractText())))
        return PreparedReference(brand, tuple(replacements), tuple(protected), tuple(dates))


def output_plans(prepared, title, brand, value):
    replacements = []
    for plan in prepared.replacements:
        if plan.kind in ("header", "cover"):
            text = title
        elif plan.kind == "paint":
            text = f"Asturcolor Touch-up Paint - {brand} {value}"
        else:
            text = re.sub(r"(?<!\w)" + re.escape(prepared.brand) + r"(?!\w)", lambda _: brand, plan.text)
        replacements.append(replace(plan, text=text))
    return replacements


def validate_output(data, reference_data, title):
    brand, value = identity(title)
    prepared = prepare_reference(reference_data)
    expected = output_plans(prepared, title, brand, value)
    with open_pdf(data) as result:
        if result.page_count != 49:
            raise SDSError("Після генерації змінилася кількість сторінок 3-Marker.")
        varnish = 0
        for index, page in enumerate(result):
            # Share the full-page text parse for protected words and dates.
            textpage = page.get_textpage(flags=fitz.TEXTFLAGS_TEXT)
            current = [plan for plan in expected if plan.page == index]
            for plan in current:
                # The Python extractTextbox implementation walks every glyph
                # on the page; bounded native extraction is much faster here.
                if canonical(page.get_text(clip=plan.target)) != canonical(plan.text):
                    raise SDSError(f"Перевірка 3-Marker: неправильна ідентифікація на сторінці {index + 1}.")
            excluded = [p.source | p.target for p in current]
            if prepared.protected_words[index] != tuple(word_signature(page, excluded, textpage.extractWORDS())):
                raise SDSError(f"Змінено захищений текст 3-Marker на сторінці {index + 1}.")
            text = textpage.extractText()
            if prepared.dates[index] != tuple(DATE.findall(text)):
                raise SDSError("Змінилися дати еталона 3-Marker.")
            if "\x00" in text:
                raise SDSError(f"Непідтримувані символи 3-Marker на сторінці {index + 1}.")
            varnish += canonical(text).count("Clear varnish")
        return dict(pages=49, paint_title_regions_exact=50, clear_varnish_regions_exact=varnish,
                    dates_preserved=True, protected_text_preserved=True,
                    template_format="three-marker-49", identification_regions_exact=len(expected))


def generate(title, reference_data):
    title = normalized(title)
    validate_title(title)
    brand, value = identity(title)
    prepared = prepare_reference(reference_data)
    replacements = output_plans(prepared, title, brand, value)
    with open_pdf(reference_data) as doc:
        # Keep already-correct fields in their original fonts/content streams.
        # In a same-make colour batch the explanatory primer lines need no edit.
        fonts = {}
        placements = [(plan, fitted(plan, fonts)) for plan in replacements
                      if plan.original != canonical(plan.text)]
        # A previously generated reference has subset fonts under our old
        # resource names. Reusing those names makes MuPDF ignore fontfile and
        # emit missing glyphs for the next colour. Bind fresh, complete fonts.
        font_tag = uuid.uuid4().hex
        for index, page in enumerate(doc):
            current = [(plan, fit) for plan, fit in placements if plan.page == index]
            if not current:
                continue
            for plan, _ in current:
                page.add_redact_annot(plan.source, fill=(1, 1, 1), cross_out=False)
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
            for plan, (font_path, size, x, baseline, width_scale) in current:
                page.insert_text((x, baseline), plan.text, fontname=("ThreeMarkerBold" if plan.bold else "ThreeMarkerRegular") + font_tag,
                                 fontfile=str(font_path), fontsize=size, color=(0, 0, 0), overlay=True,
                                 morph=(fitz.Point(x, baseline), fitz.Matrix(width_scale, 1)) if width_scale < 1 else None)
        doc.subset_fonts()
        # Deduplication (levels 3/4) compares unchanged source objects for every
        # item. Level 2 removes unused objects and compacts xrefs without it.
        data = doc.tobytes(garbage=2, deflate=True)
    return data, validate_output(data, reference_data, title)
