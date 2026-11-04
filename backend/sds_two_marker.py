"""Bounded identification replacement for the revised paint + varnish layout.

Recognise the component pagination and fields, not a fixed total page count.
All output page text outside the identity fields remains checked against source.
"""
from dataclasses import dataclass, replace
from functools import lru_cache
import re
import uuid

import pymupdf as fitz

from .sds import FONT, SDSError, normalized, open_pdf, validate_title, word_signature
from .sds_three_marker import Replacement, fitted

HEADER = fitz.Rect(36, 64, 559, 76)
SUBTITLE = fitz.Rect(36, 81, 559, 91)
TECHNICAL = fitz.Rect(36, 97, 559, 107)
NUMBER = fitz.Rect(36, 791, 559, 803)
FOOTER = fitz.Rect(36, 810, 559, 820)
PRODUCT = fitz.Rect(180.6378, 145.5, 548.6378, 156)
KIT = fitz.Rect(180.6378, 157, 548.6378, 177)
PAGE = re.compile(r"(Paint|Clear varnish) - page (\d+)/(\d+) \| Document page (\d+)/(\d+)")
HEADING = re.compile(r"Asturcolor (.+) - 2 x 12 ml touch-up marker kit")
DATE = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")


def text(page, rect):
    return normalized(page.get_text(clip=rect))


def matches(doc):
    return bool(PAGE.fullmatch(text(doc[0], NUMBER)))


@dataclass(frozen=True)
class Prepared:
    paint_pages: int
    varnish_pages: int
    fields: tuple
    protected: tuple
    dates: tuple


def analyse(doc):
    first = PAGE.fullmatch(text(doc[0], NUMBER))
    if not first or first[1] != "Paint" or first[2] != "1" or int(first[5]) != doc.page_count:
        raise SDSError("Не розпізнано структуру двокомпонентного SDS.")
    paint_pages, varnish_pages = int(first[3]), doc.page_count - int(first[3])
    if paint_pages < 1 or varnish_pages < 1:
        raise SDSError("SDS повинен містити окремі блоки фарби та лаку.")
    heading, kit = text(doc[0], HEADER), text(doc[0], KIT)
    identity = HEADING.fullmatch(heading)
    if not identity or not kit.startswith("Kit identifier: "):
        raise SDSError("Невідомі поля ідентифікації двокомпонентного SDS.")
    variant = identity[1]
    fields, protected, dates = [], [], []
    updated = None
    for index, page in enumerate(doc):
        if abs(page.rect.width - 595.276) > 2 or abs(page.rect.height - 841.89) > 2:
            raise SDSError("Двокомпонентний SDS повинен мати сторінки A4.")
        paint = index < paint_pages
        name, count, position = ("Paint", paint_pages, index+1) if paint else ("Clear varnish", varnish_pages, index-paint_pages+1)
        expected_number = f"{name} - page {position}/{count} | Document page {index+1}/{doc.page_count}"
        if text(page, NUMBER) != expected_number:
            raise SDSError(f"Непослідовна нумерація компонентів SDS на сторінці {index+1}.")
        expected_subtitle = ("Paint marker" if paint else "Clear-varnish marker") + " - 12 ml | Kit: 12 ml paint + 12 ml clear varnish"
        technical = text(page, TECHNICAL)
        prefix = "Technical source: " + ("25/04/2019; revised 19/11/2021; v2" if paint else "17/12/2018; revised 11/07/2024; v3") + " | Document update: "
        if text(page, SUBTITLE) != expected_subtitle or not technical.startswith(prefix) or not DATE.fullmatch(technical[len(prefix):]):
            raise SDSError(f"Невідповідна структура компонента або дат SDS на сторінці {index+1}.")
        if updated is None:
            updated = technical[len(prefix):]
        if technical[len(prefix):] != updated:
            raise SDSError("Дати оновлення двокомпонентного SDS не узгоджені.")
        footer = f"Asturcolor | {variant} | 12 ml per component"
        if text(page, HEADER) != heading or text(page, FOOTER) != footer:
            raise SDSError(f"Неузгоджені назви в колонтитулах SDS на сторінці {index+1}.")
        current = [Replacement(index, HEADER, HEADER, heading, 8.3, 72.30408, True, kind="header", original=heading),
                   Replacement(index, FOOTER, FOOTER, footer, 7, 817.00342, kind="footer", original=footer)]
        if index in (0, paint_pages):
            expected_product = f"Asturcolor Touch-up Paint Marker - {variant} - 12 ml" if paint else "Asturcolor Clear Varnish Marker - 12 ml"
            if text(page, PRODUCT) != expected_product or text(page, KIT) != kit:
                raise SDSError(f"Не розпізнано назви компонентів SDS на сторінці {index+1}.")
            current.append(Replacement(index, KIT, KIT, kit, 8.1, 163.6, kind="kit", original=kit))
            if paint:
                current.append(Replacement(index, PRODUCT, PRODUCT, expected_product, 8.1, 153.1, kind="paint", original=expected_product))
        fields.extend(current)
        textpage = page.get_textpage(flags=fitz.TEXTFLAGS_TEXT)
        protected.append(tuple(word_signature(page, [p.target for p in current], textpage.extractWORDS())))
        dates.append(tuple(DATE.findall(textpage.extractText())))
    return Prepared(paint_pages, varnish_pages, tuple(fields), tuple(protected), tuple(dates))


def preflight(doc):
    return analyse(doc)


@lru_cache(maxsize=2)
def prepare(reference_data):
    with open_pdf(reference_data) as doc:
        return analyse(doc)


def output_plans(prepared, title, item):
    if not item or not all(normalized(item.get(key, "")) for key in ("brand", "code", "color")):
        raise SDSError("Для цього SDS потрібні марка, код і назва кольору з рядка генерації.")
    # The parser's COLOR sentinel means the user supplied a code alone.
    color = "" if item["color"] == "COLOR" else item["color"]
    variant = normalized(f"{item['brand']} {item['code']} {color}")
    validate_title(variant)
    values = dict(header=f"Asturcolor {variant} - 2 x 12 ml touch-up marker kit",
                  footer=f"Asturcolor | {variant} | 12 ml per component",
                  paint=f"Asturcolor Touch-up Paint Marker - {variant} - 12 ml", kit="Kit identifier: " + title)
    return [replace(field, text=values[field.kind]) for field in prepared.fields]


def kit_layout(title, fonts):
    """Fit the full kit name into its original two rows above the UFI."""
    regular, bold = fonts[False], fonts[True]
    prefix = "Kit identifier: "
    for tenth in range(81, 54, -1):
        size = tenth / 10
        remaining, rows = title.split(), []
        for row in range(2):
            width = KIT.width - 2 - (bold.text_length(prefix, fontsize=size) if row == 0 else 0)
            words = []
            while remaining and regular.text_length(" ".join(words + [remaining[0]]), fontsize=size) <= width:
                words.append(remaining.pop(0))
            rows.append(" ".join(words))
        if not remaining and rows[0]:
            return size, rows
    raise SDSError("Назва комплекту не вміщується у два рядки над UFI. Скоротіть шаблон назви.")


def validate_output(data, reference_data, title, item):
    prepared = prepare(reference_data)
    expected = output_plans(prepared, title, item)
    with open_pdf(data) as doc:
        if doc.page_count != prepared.paint_pages + prepared.varnish_pages:
            raise SDSError("Після генерації змінилася кількість сторінок SDS.")
        for index, page in enumerate(doc):
            current = [field for field in expected if field.page == index]
            for field in current:
                if text(page, field.target) != normalized(field.text):
                    raise SDSError(f"Неправильна назва у полі {field.kind} на сторінці {index+1}.")
            textpage = page.get_textpage(flags=fitz.TEXTFLAGS_TEXT)
            if prepared.protected[index] != tuple(word_signature(page, [p.target for p in current], textpage.extractWORDS())):
                raise SDSError(f"Змінено захищений текст SDS на сторінці {index+1}.")
            if prepared.dates[index] != tuple(DATE.findall(textpage.extractText())):
                raise SDSError(f"Змінено дати SDS на сторінці {index+1}.")
        return dict(pages=doc.page_count, paint_title_regions_exact=len(expected),
                    clear_varnish_regions_exact=prepared.varnish_pages, dates_preserved=True, protected_text_preserved=True,
                    template_format="two-marker", identification_regions_exact=len(expected), paint_pages=prepared.paint_pages)


def generate(title, reference_data, item):
    prepared = prepare(reference_data)
    plans = output_plans(prepared, title, item)
    fonts = {bold: fitz.Font(fontfile=str(FONT.with_name("NotoSans-Bold.ttf") if bold else FONT)) for bold in (False, True)}
    tag = uuid.uuid4().hex
    paths = {bold: str(FONT.with_name("NotoSans-Bold.ttf") if bold else FONT) for bold in (False, True)}
    aliases = {bold: ("TwoMarkerBold" if bold else "TwoMarkerRegular") + tag for bold in (False, True)}
    fitted_fonts = {FONT: fonts[False], FONT.with_name("NotoSans-Bold.ttf"): fonts[True]}
    placements = [(field, kit_layout(title, fonts) if field.kind == "kit" else fitted(field, fitted_fonts))
                  for field in plans if field.text != field.original]
    with open_pdf(reference_data) as doc:
        for index, page in enumerate(doc):
            current = [(field, layout) for field, layout in placements if field.page == index]
            if not current:
                continue
            for field, _ in current:
                page.add_redact_annot(field.source, fill=(1, 1, 1), cross_out=False)
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
            for field, layout in current:
                if field.kind == "kit":
                    size, rows = layout
                    prefix_width = fonts[True].text_length("Kit identifier: ", fontsize=size)
                    page.insert_text((KIT.x0, 163.6), "Kit identifier: ", fontname=aliases[True], fontfile=paths[True], fontsize=size)
                    for row, content in enumerate(rows):
                        if content:
                            page.insert_text((KIT.x0 + (prefix_width if row == 0 else 0), 163.6 + row*10.5), content,
                                             fontname=aliases[False], fontfile=paths[False], fontsize=size)
                else:
                    _, size, x, baseline, scale = layout
                    page.insert_text((x, baseline), field.text, fontname=aliases[field.bold], fontfile=paths[field.bold], fontsize=size,
                                     morph=(fitz.Point(x, baseline), fitz.Matrix(scale, 1)) if scale < 1 else None)
        doc.subset_fonts()
        result = doc.tobytes(garbage=2, deflate=True)
    return result, validate_output(result, reference_data, title, item)
