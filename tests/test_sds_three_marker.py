"""Synthetic non-customer fixture for the supported 49-page SDS layout."""
import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient

from backend import sds, sds_three_marker as three, storage, settings
from backend.app import app


def fixture_pdf(brand="BMW", value="668 Jet Black"):
    title = f"Asturcolor 3-Marker Car Touch-up Kit for {brand} ({value})"
    with fitz.open() as doc:
        for index in range(49):
            page = doc.new_page(width=595.276, height=841.89)
            width = fitz.get_text_length(title, fontsize=8.6)
            page.insert_text(((595.276-width)/2, 72), title, fontsize=8.6)
            subtitle = ("Epoxy Primer + Paint + Clear Varnish" if index == 0 else
                        "Asturcolor Epoxy Primer - Marker 1 of 3" if index < 8 else
                        "Asturcolor Touch-up Paint - Marker 2 of 3" if index < 31 else
                        "Asturcolor Clear Varnish - Marker 3 of 3")
            page.insert_text((150, 85), subtitle, fontsize=8.1)
            page.insert_text((69, 110), "Date 23/10/2026; Revision 10/02/2023. UFI: KEEP-UNCHANGED.", fontsize=8)
            page.insert_text((69, 310), "Protected safety text. CAS: 668-00-1. 12 mL. H226. Clear varnish.", fontsize=8)
            if index == 0:
                page.insert_text((69, 154), title.replace(f"({value})", value), fontsize=8.11)
                page.insert_text((147.05, 201), f"Asturcolor Touch-up Paint - {brand} {value}", fontsize=7.1)
                page.insert_text((69, 232), f"Three separate mixtures, unchanged components. The {brand} code", fontsize=8.11)
                page.insert_text((69, 243), "identifies only the paint variant.", fontsize=8.11)
            elif index == 4:
                page.insert_text((69, 283), f"9.1 Basic physical and chemical properties: {brand} colour names do not describe", fontsize=8.11)
            elif index == 8:
                page.insert_text((195, 141), f"Asturcolor Touch-up Paint - {brand} {value}", fontsize=8)
            elif index == 16:
                page.insert_text((69, 155), f"Asturcolor Touch-up Paint - {brand} {value}", fontsize=9)
        doc.set_metadata({"subject": "three-marker-synthetic-test"})
        return doc.tobytes()


@pytest.fixture(scope="module")
def reference():
    return fixture_pdf()


def test_placeholder_parser_is_not_the_problem():
    item = sds.parse_items("template", "BMW", "Asturcolor 3-Marker Car Touch-up Kit for {BRAND} ({VALUE})", "300 Alpine White")[0]
    assert item["title"] == "Asturcolor 3-Marker Car Touch-up Kit for BMW (300 Alpine White)"


@pytest.mark.parametrize("separator", ["\t", "\t\t", "\u00a0", "\u2003", "   "])
def test_spreadsheet_whitespace_is_normalized_before_pdf_insertion(separator):
    item = sds.parse_items("template", " BMW\t", "Asturcolor 3-Marker Car Touch-up Kit for {BRAND} ({VALUE})",
                           f"475{separator}Black{separator}Sapphire Metallic")[0]
    assert item["title"] == "Asturcolor 3-Marker Car Touch-up Kit for BMW (475 Black Sapphire Metallic)"
    assert item["color"] == "Black Sapphire Metallic"
    exact = sds.parse_items("exact", exact_text=f"475 | BMW | Black{separator}Sapphire Metallic | {item['title'].replace('475 ', '475' + separator)}")[0]
    assert exact["title"] == item["title"] and exact["color"] == item["color"]


@pytest.mark.parametrize("brand,value", [("BMW", "300 Alpine White"), ("Audi", "LY9B Brilliant Black"),
                                        ("Mercedes-Benz", "040 Black")])
def test_49_page_replacements_preserve_safety_data(reference, brand, value):
    title = f"Asturcolor 3-Marker Car Touch-up Kit for {brand} ({value})"
    result, validation = sds.generate(title, reference)
    assert validation["pages"] == 49 and validation["identification_regions_exact"] == 55
    assert validation["dates_preserved"] and validation["protected_text_preserved"]
    with sds.open_pdf(result) as doc:
        assert all(three.canonical(page.get_text(clip=three.HEADER)) == title for page in doc)
        assert all("CAS: 668-00-1" in page.get_text() and "UFI: KEEP-UNCHANGED" in page.get_text() for page in doc)
        assert "668 Jet Black" not in "\n".join(page.get_text() for page in doc)
        assert doc.metadata["subject"] == "three-marker-synthetic-test"
        if brand == "BMW":
            prose = [span for block in doc[4].get_text("dict")["blocks"] for line in block.get("lines", [])
                     for span in line["spans"] if span["text"].startswith("9.1 Basic")]
            assert prose and prose[0]["font"] == "Helvetica"
        # A generated 49-page PDF can itself be uploaded as the next reference.
        sds.preflight(doc)


def test_brand_detection_is_not_hardcoded_to_bmw():
    source = fixture_pdf("Mercedes-Benz", "040 Black")
    output, _ = sds.generate("Asturcolor 3-Marker Car Touch-up Kit for Audi (LY7G Quartz Grey)", source)
    with sds.open_pdf(output) as doc:
        text = "\n".join(page.get_text() for page in doc)
        assert "Mercedes-Benz" not in text and "040 Black" not in text
        assert "The Audi code" in text and "Audi colour names" in text


def test_direct_generator_cannot_embed_tabs_as_nul(reference):
    dirty = "Asturcolor 3-Marker Car Touch-up Kit for BMW (475\tBlack\u00a0Sapphire Metallic)"
    output, validation = three.generate(dirty, reference)
    assert validation["identification_regions_exact"] == 55
    with sds.open_pdf(output) as doc:
        assert all("\x00" not in page.get_text() for page in doc)
        assert all(three.canonical(page.get_text(clip=three.HEADER)) == dirty.replace("\t", " ").replace("\u00a0", " ") for page in doc)


def test_tab_separated_batch_first_unchanged_then_different_colors(reference, tmp_path, monkeypatch):
    import uuid
    first_title = "Asturcolor 3-Marker Car Touch-up Kit for BMW (300 Alpine White)"
    # Mimic re-uploading a generated first-colour PDF as the reference: the first
    # title is already right, while subsequent colours require real insertion.
    already_first, _ = sds.generate(first_title, reference)
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.setattr(settings, "IS_VERCEL", False)
    with TestClient(app) as client:
        upload = storage.save_bytes(already_first, "first-colour-reference.pdf", "application/pdf")
        response = client.post("/api/sds/parse", json={"mode":"template", "brand":"BMW",
            "title_template":"Asturcolor 3-Marker Car Touch-up Kit for {BRAND} ({VALUE})",
            "values_text":"300\tAlpine White\n475\tBlack Sapphire Metallic\n354\tTitanium Silver Metallic"})
        assert response.status_code == 200
        items, batch_id = response.json()["items"], str(uuid.uuid4())
        for index, item in enumerate(items):
            # Deliberately bypass the parser clean-up for one API request too.
            item["title"] = item["title"].replace(item["code"] + " ", item["code"] + "\t")
            result = client.post("/api/sds/generate", json={"item":item, "reference_token":upload["token"],
                "render_previews":False, "batch_id":batch_id, "batch_index":index, "batch_size":len(items)})
            assert result.status_code == 200, (index, result.text)
            assert result.json()["validation"]["identification_regions_exact"] == 55
            pdf = client.get(result.json()["file"]["url"]).content
            with sds.open_pdf(pdf) as doc:
                assert all("\x00" not in page.get_text() for page in doc)
        batch = client.get("/api/history/batches/" + batch_id).json()
        assert batch["count"] == batch["expected_count"] == 3 and not batch["partial"]


def test_generated_subset_font_is_not_reused_for_new_glyphs(reference):
    current, _ = sds.generate("Asturcolor 3-Marker Car Touch-up Kit for BMW (300 Alpine White)", reference)
    for value in ("475 Black Sapphire Metallic", "A52 Space Grey Metallic", "354 Titanium Silver Metallic"):
        title = f"Asturcolor 3-Marker Car Touch-up Kit for BMW ({value})"
        current, _ = sds.generate(title, current)
        with sds.open_pdf(current) as doc:
            assert all(three.canonical(page.get_text(clip=three.HEADER)) == title for page in doc)
            assert all("\x00" not in page.get_text() for page in doc)


def test_legacy_generated_subset_font_and_whitespace_are_safe():
    first_title = "Asturcolor BMW (300 Alpine White)"
    reference, _ = sds.generate(first_title)
    second_title = "Asturcolor BMW (475\tBlack\u00a0Sapphire Metallic)"
    output, validation = sds.generate(second_title, reference)
    assert validation["pages"] == 41 and validation["paint_title_regions_exact"] == 24
    with sds.open_pdf(output) as doc:
        assert sds.title_regions(doc) == [sds.normalized(second_title)] * 24
        assert all("\x00" not in page.get_text() for page in doc)


def test_unknown_layout_and_overlong_title_fail_closed(reference):
    with sds.open_pdf(reference) as doc:
        doc[31].add_redact_annot(three.SUBTITLE, fill=(1,1,1))
        doc[31].apply_redactions()
        with pytest.raises(sds.SDSError, match="структура компонентів"):
            sds.preflight(doc)
    title = "Asturcolor 3-Marker Car Touch-up Kit for " + "X" * 400 + " (A White)"
    with pytest.raises(sds.SDSError, match="не вміщується"):
        sds.generate(title, reference)
    with pytest.raises(sds.SDSError, match="Для 3-Marker"):
        sds.generate("Unrelated title", reference)


def test_saved_49_page_reference_reports_49_and_is_used(reference, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.setattr(settings, "IS_VERCEL", False)
    with TestClient(app) as client:
        uploaded = storage.save_bytes(reference, "three-markers.pdf", "application/pdf")
        response = client.put("/api/settings/sds/reference", json={"token": uploaded["token"]})
        assert response.status_code == 200, response.text
        assert response.json()["pages"] == 49
        assert client.get("/api/sds/reference").json()["pages"] == 49
        item = sds.parse_items("template", "BMW", "Asturcolor 3-Marker Car Touch-up Kit for {BRAND} ({VALUE})", "300 Alpine White")[0]
        generated = client.post("/api/sds/generate", json={"item":item, "render_previews":False})
        assert generated.status_code == 200, generated.text
        assert generated.json()["validation"]["pages"] == 49
        data = client.get(generated.json()["file"]["url"]).content
        with sds.open_pdf(data) as doc:
            assert len(doc) == 49
        restored = client.post("/api/settings/sds/reference/restore").json()
        assert restored["pages"] == 41
