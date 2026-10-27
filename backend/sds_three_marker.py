"""Bounded title/variant replacement for the 49-page, three-component SDS.

This changes product identification only, never the three mixture compositions.
Unknown layouts fail closed instead of reusing the legacy 41-page rectangles.
"""
from dataclasses import dataclass
import re

import pymupdf as fitz

from .sds import SDSError, FONT, normalized, open_pdf, validate_title, word_signature

HEADER = fitz.Rect(60, 63, 536, 75.5)
SUBTITLE = fitz.Rect(60, 77, 536, 89)
TITLE = re.compile(r"Asturcolor 3-Marker Car Touch-up Kit for (.+?) \((.+)\)")
DATE = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")


def canonical(text):
    return normalized(text.translate(str.maketrans({"\xad": "-", "\u2011": "-", "\u2010": "-"})))


def identity(title):
    match = TITLE.fullmatch(canonical(title))
    if not match:
        raise SDSError("Для 3-Marker використайте назву Asturcolor 3-Marker Car Touch-up Kit for {BRAND} ({VALUE}).")
    return match[1], match[2]


def preflight(doc):
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
    plans(doc, headings[0], brand, value)
    return brand, value


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


def lines(page):
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            yield line, canonical("".join(span["text"] for span in line["spans"]))


def replacement(page_index, line, text, target, centered=False):
    spans = line["spans"]
    return Replacement(page_index, fitz.Rect(line["bbox"]) + (-.25, -.2, .25, .2),
                       fitz.Rect(target), text, min(s["size"] for s in spans), spans[0]["origin"][1],
                       any(s["flags"] & 16 for s in spans), centered)


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
                result.append(replacement(index, line, title, HEADER, centered=True))
                counts["header"] += 1
            elif index == 0 and 145 <= rect.y0 <= 150 and text in (old_title, old_title.replace(f"({old_value})", old_value)):
                result.append(replacement(index, line, title, (69, 145, 552, 158)))
                counts["cover"] += 1
            elif text == paint + old_variant and index in (0, 8, 16):
                right = 354 if index == 0 else 552
                result.append(replacement(index, line, paint + brand + " " + value,
                                          (rect.x0, rect.y0 - .5, right, rect.y1 + .5)))
                counts["paint"] += 1
            elif index in (0, 4) and ((index == 0 and text.startswith("Three separate mixtures,")) or
                                     (index == 4 and text.startswith("9.1 Basic physical and chemical properties:"))):
                # Replace the explicit brand reference in the explanatory line;
                # don't leave "BMW colour names" when making a different make.
                if not re.search(r"(?<!\w)" + re.escape(old_brand) + r"(?!\w)", text):
                    raise SDSError("Не знайдено марку в пояснювальному тексті 3-Marker.")
                changed = re.sub(r"(?<!\w)" + re.escape(old_brand) + r"(?!\w)", lambda _: brand, text)
                result.append(replacement(index, line, changed, (rect.x0, rect.y0 - .5, 557, rect.y1 + .5)))
                counts["prose"] += 1
    if counts != dict(header=49, cover=1, paint=3, prose=2):
        raise SDSError("Еталон 3-Marker має інші зони ідентифікації. Жодних змін не застосовано.")
    return result


def fitted(plan):
    font_path = FONT.with_name("NotoSans-Bold.ttf") if plan.bold else FONT
    font = fitz.Font(fontfile=str(font_path))
    # One line per bounded existing row: never push the component subtitle,
    # adjacent table cells or safety paragraphs down.
    size = min(plan.size, (plan.target.width - 2) / max(font.text_length(plan.text, fontsize=1), .01))
    if size < 5.5:
        raise SDSError(f"Сторінка {plan.page + 1}: назва не вміщується без накладання. Скоротіть назву марки/кольору.")
    width = font.text_length(plan.text, fontsize=size)
    x = (plan.target.x0 + plan.target.x1 - width) / 2 if plan.centered else plan.target.x0
    baseline = min(plan.baseline, plan.target.y1 - max(font.descender * -size, .5))
    return font_path, size, x, baseline


def validate_output(data, reference_data, title):
    brand, value = identity(title)
    with open_pdf(reference_data) as source, open_pdf(data) as result:
        preflight(source)
        expected = plans(source, title, brand, value)
        if result.page_count != 49:
            raise SDSError("Після генерації змінилася кількість сторінок 3-Marker.")
        for plan in expected:
            if canonical(result[plan.page].get_text(clip=plan.target)) != canonical(plan.text):
                raise SDSError(f"Перевірка 3-Marker: неправильна ідентифікація на сторінці {plan.page + 1}.")
        for index in range(49):
            excluded = [p.source | p.target for p in expected if p.page == index]
            if word_signature(source[index], excluded) != word_signature(result[index], excluded):
                raise SDSError(f"Змінено захищений текст 3-Marker на сторінці {index + 1}.")
        for before, after in zip(source, result):
            if DATE.findall(before.get_text()) != DATE.findall(after.get_text()):
                raise SDSError("Змінилися дати еталона 3-Marker.")
        varnish = sum(canonical(page.get_text()).count("Clear varnish") for page in result)
        return dict(pages=49, paint_title_regions_exact=50, clear_varnish_regions_exact=varnish,
                    dates_preserved=True, protected_text_preserved=True,
                    template_format="three-marker-49", identification_regions_exact=len(expected))


def generate(title, reference_data):
    validate_title(title)
    brand, value = identity(title)
    with open_pdf(reference_data) as doc:
        preflight(doc)
        replacements = plans(doc, title, brand, value)
        placements = [(plan, fitted(plan)) for plan in replacements]
        for index, page in enumerate(doc):
            current = [(plan, fit) for plan, fit in placements if plan.page == index]
            for plan, _ in current:
                page.add_redact_annot(plan.source, fill=(1, 1, 1), cross_out=False)
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
            for plan, (font_path, size, x, baseline) in current:
                page.insert_text((x, baseline), plan.text, fontname="ThreeMarkerBold" if plan.bold else "ThreeMarkerRegular",
                                 fontfile=str(font_path), fontsize=size, color=(0, 0, 0), overlay=True)
        doc.subset_fonts()
        data = doc.tobytes(garbage=4, deflate=True)
    return data, validate_output(data, reference_data, title)
