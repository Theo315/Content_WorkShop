"""Readable Linux port of the bundled SDS generator, recovered by static analysis.

The supplied 41-page PDF is the source of truth. This module replaces product
titles only; validation checks every other word and the original revision dates.
It does not infer chemical properties or certify regulatory correctness.
"""
from collections import Counter
from dataclasses import dataclass, asdict
import re
import unicodedata
import pymupdf as fitz
from .settings import ASSETS, MAX_ITEMS

# Keep search/redaction boxes at glyph height. Noto ascenders otherwise include
# part of the previous line in these tightly-spaced original title regions.
fitz.TOOLS.set_small_glyph_heights(True)

REFERENCE = ASSETS / "sds/reference.pdf"
FONT = ASSETS / "fonts/NotoSans-Regular.ttf"
MAIN_RECT = fitz.Rect(138, 64, 560, 100)
EXTRA_RECT = fitz.Rect(193, 133, 561, 163.5)
MAIN_BASELINES = (75.0338134765625, 85.6068115234375, 96.1807861328125)
EXTRA_BASELINES = (141.9317626953125, 151.17279052734375, 160.413818359375)
REQUIRED_DATES = ("Date of compilation: 25/04/2019", "Revised: 19/11/2021",
                  "Date of compilation: 17/12/2018", "Revised: 11/07/2024")
DEFAULT_TITLE = "Asturcolor Reparador Arañazos Coche, Lápiz Retoque Automóvil, Bolígrafo Reparaarañazos, 2 Piezas Pintura Retoque {BRAND} con Base + Laca, Fabricado en España ({VALUE})"


class SDSError(ValueError):
    pass


def normalized(text):
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def safe_part(value):
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_") or "UNKNOWN"


def filename(code, brand, color):
    return f"{safe_part(code)}_SDS_Asturcolor_{safe_part(brand)}_{safe_part(color)}_repaired_no_filter_mask.pdf"


def parse_items(mode, brand="", title_template="", values_text="", exact_text=""):
    items = []
    if mode == "template":
        brand, title_template = brand.strip(), title_template.strip()
        if not brand or not title_template:
            raise SDSError("Вкажіть марку автомобіля та шаблон назви.")
        if not any(p in title_template for p in ("{VALUE}", "{CODE}", "{COLOR}", "{BRAND}")):
            raise SDSError("Шаблон має містити {VALUE}, {CODE}, {COLOR} або {BRAND}.")
        for number, value in enumerate(values_text.splitlines(), 1):
            value = value.strip()
            if not value:
                continue
            parts = value.split(maxsplit=1)
            code, color = parts[0], parts[1] if len(parts) == 2 else "COLOR"
            title = title_template
            for key, content in {"VALUE": value, "CODE": code, "COLOR": color, "BRAND": brand}.items():
                title = title.replace("{" + key + "}", content)
            if "{" in title or "}" in title:
                raise SDSError(f"Рядок {number}: невідомий placeholder у назві.")
            items.append(dict(code=code, brand=brand, color=color, title=title, filename=filename(code, brand, color)))
    elif mode == "exact":
        for number, line in enumerate(exact_text.splitlines(), 1):
            if not line.strip():
                continue
            parts = [part.strip() for part in line.split("|", 3)]
            if len(parts) != 4 or not all(parts):
                raise SDSError(f"Рядок {number}: формат CODE | BRAND | COLOR | TITLE.")
            code, brand, color, title = parts
            items.append(dict(code=code, brand=brand, color=color, title=title, filename=filename(code, brand, color)))
    else:
        raise SDSError("Невідомий режим генерації.")
    if not items or len(items) > MAX_ITEMS:
        raise SDSError(f"Вкажіть від 1 до {MAX_ITEMS} назв.")
    counts = Counter(item["filename"].lower() for item in items)
    if any(n > 1 for n in counts.values()):
        raise SDSError("Дублікати назв файлів. Змініть повторювані код/марку/колір.")
    for item in items:
        validate_title(item["title"])
    return items


def validate_title(title):
    if not title.strip() or len(title) > 500:
        raise SDSError("Назва повинна містити від 1 до 500 символів.")
    font = fitz.Font(fontfile=str(FONT))
    unsupported = sorted({ch for ch in title if not ch.isspace() and not font.has_glyph(ord(ch))})
    if unsupported:
        raise SDSError("Шрифт не підтримує символи: " + " ".join(unsupported))


def open_pdf(data):
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        raise SDSError("Не вдалося прочитати PDF.") from None
    if doc.needs_pass or not 0 < doc.page_count <= 200:
        doc.close()
        raise SDSError("Потрібен PDF без пароля, від 1 до 200 сторінок.")
    return doc


def title_regions(doc):
    return [normalized(doc[i].get_text("text", clip=MAIN_RECT)) for i in range(23)] + [normalized(doc[0].get_text("text", clip=EXTRA_RECT))]


def preflight(doc):
    if doc.page_count == 49:
        from . import sds_three_marker
        return sds_three_marker.preflight(doc)
    if doc.page_count != 41:
        raise SDSError(f"Потрібен сумісний еталон на 41 сторінку або 3-Marker на 49 сторінок; отримано {doc.page_count}.")
    text = "\n".join(page.get_text() for page in doc)
    for date in REQUIRED_DATES:
        if date not in text:
            raise SDSError(f"Еталон не відповідає оригінальному формату: відсутня дата {date}.")
    regions = title_regions(doc)
    if not all(regions) or len(set(regions)) != 1:
        raise SDSError("Еталон має іншу структуру зон назв. Використайте вбудований еталон або PDF-редактор.")
    for i in range(41):
        if abs(doc[i].rect.width - doc[0].rect.width) > 1 or abs(doc[i].rect.height - doc[0].rect.height) > 1:
            raise SDSError("Розміри сторінок еталона неоднакові.")


def word_signature(page, exclusions):
    return [str(word[4]) for word in page.get_text("words")
            if not any(fitz.Rect(word[:4]).intersects(rect) for rect in exclusions)]


def wrap_title(title, font, width, initial_size):
    for tenth in range(round(initial_size * 10), 54, -1):
        size = min(initial_size, tenth / 10)
        lines, current = [], ""
        for word in title.split():
            candidate = (current + " " + word).strip()
            if current and font.text_length(candidate, fontsize=size) > width:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
        if len(lines) <= 3 and all(font.text_length(line, fontsize=size) <= width for line in lines):
            return lines, size
    raise SDSError("Назва не вміщується у три рядки. Скоротіть її.")


def insert_title(page, title, font, main=True):
    lines, size = wrap_title(title, font, 413.2201 if main else 363.3393,
                            9.013999938964844 if main else 7.908999919891357)
    for line, baseline in zip(lines, MAIN_BASELINES if main else EXTRA_BASELINES):
        x = 349.41905975341797 - font.text_length(line, fontsize=size) / 2 if main else 195.87399291992188
        page.insert_text((x, baseline), line, fontname="SDSNoto", fontfile=str(FONT), fontsize=size, color=(0, 0, 0), overlay=True)
    return size


def validate_output(data, reference_data, title):
    with open_pdf(reference_data) as reference:
        three_marker = reference.page_count == 49
    if three_marker:
        from . import sds_three_marker
        return sds_three_marker.validate_output(data, reference_data, title)
    with open_pdf(data) as doc, open_pdf(reference_data) as source:
        if doc.page_count != source.page_count:
            raise SDSError("Перевірка результату: кількість сторінок змінилася.")
        regions = title_regions(doc)
        if any(region != normalized(title) for region in regions):
            raise SDSError("Перевірка результату: назва не збігається в усіх 24 зонах.")
        source_text = "\n".join(p.get_text() for p in source)
        output_text = "\n".join(p.get_text() for p in doc)
        for date in REQUIRED_DATES:
            if output_text.count(date) != source_text.count(date):
                raise SDSError("Перевірка результату: змінилася дата еталона.")
        for i in range(41):
            excluded = ([MAIN_RECT] if i < 23 else []) + ([EXTRA_RECT] if i == 0 else [])
            if word_signature(source[i], excluded) != word_signature(doc[i], excluded):
                raise SDSError(f"Змінено захищений текст на сторінці {i + 1}.")
        varnish = sum(normalized(doc[i].get_text()).count("Clear varnish") for i in range(23, 41))
        original_varnish = sum(normalized(source[i].get_text()).count("Clear varnish") for i in range(23, 41))
        if varnish != original_varnish or "\x00" in output_text:
            raise SDSError("Перевірка тексту Clear varnish/символів не пройшла.")
        return dict(pages=41, paint_title_regions_exact=24, clear_varnish_regions_exact=varnish,
                    dates_preserved=True, protected_text_preserved=True)


def generate(title, reference_data=None):
    validate_title(title)
    reference_data = reference_data or REFERENCE.read_bytes()
    with open_pdf(reference_data) as reference:
        three_marker = reference.page_count == 49
    if three_marker:
        from . import sds_three_marker
        return sds_three_marker.generate(title, reference_data)
    with open_pdf(reference_data) as doc:
        preflight(doc)
        font = fitz.Font(fontfile=str(FONT))
        for index in range(23):
            page = doc[index]
            page.add_redact_annot(MAIN_RECT, fill=(1, 1, 1))
            if index == 0:
                page.add_redact_annot(EXTRA_RECT, fill=(1, 1, 1))
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
            insert_title(page, title, font)
            if index == 0:
                insert_title(page, title, font, main=False)
        doc.subset_fonts()
        data = doc.tobytes(garbage=4, deflate=True, clean=True)
    return data, validate_output(data, reference_data, title)


def render_page(data, index=0, max_width=1000):
    with open_pdf(data) as doc:
        if index < 0 or index >= doc.page_count:
            raise SDSError("Номер сторінки поза межами PDF.")
        page = doc[index]
        scale = min(2, max_width / page.rect.width)
        return page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes("png")
