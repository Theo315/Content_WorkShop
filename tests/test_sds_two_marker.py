"""Synthetic revised two-component SDS; no customer PDF is committed."""
import uuid
import pymupdf as fitz
import pytest
from backend import sds, sds_two_marker as two, storage
from tests.test_pipeline import client


def fixture_pdf(paint=2, varnish=2):
    variant = "Volkswagen LC9A PURE WHITE"
    kit = "Kit identifier: Asturcolor paint and clear varnish kit for Volkswagen (LC9A PURE WHITE)"
    with fitz.open() as doc:
        for index in range(paint+varnish):
            page = doc.new_page(width=595.276,height=841.89)
            is_paint = index < paint
            component,count,position = ("Paint",paint,index+1) if is_paint else ("Clear varnish",varnish,index-paint+1)
            page.insert_text((36,72.30408),f"Asturcolor {variant} - 2 x 12 ml touch-up marker kit",fontsize=8.3)
            subtitle = ("Paint marker" if is_paint else "Clear-varnish marker") + " - 12 ml | Kit: 12 ml paint + 12 ml clear varnish"
            page.insert_text((36,88.6037),subtitle,fontsize=7.6)
            technical = ("25/04/2019; revised 19/11/2021; v2" if is_paint else "17/12/2018; revised 11/07/2024; v3")
            page.insert_text((36,104.00342),f"Technical source: {technical} | Document update: 30/10/2026",fontsize=7)
            page.insert_text((36,799.20349),f"{component} - page {position}/{count} | Document page {index+1}/{paint+varnish}",fontsize=7.2)
            page.insert_text((36,817.00342),f"Asturcolor | {variant} | 12 ml per component",fontsize=7)
            page.insert_text((46.6,400),f"Protected synthetic safety text page {index+1}. CAS: KEEP. H226. 12 ml.",fontsize=8)
            if index in (0,paint):
                product = f"Asturcolor Touch-up Paint Marker - {variant} - 12 ml" if is_paint else "Asturcolor Clear Varnish Marker - 12 ml"
                page.insert_text((46.6,153.1),"1.1 Product identifier",fontsize=8.1)
                page.insert_text((180.6378,153.1),product,fontsize=8.1)
                page.insert_text((180.6378,163.6),kit,fontsize=7.1)
                page.insert_text((180.6378,184.6),"UFI: PAINT-TEST" if is_paint else "UFI: VARNISH-TEST",fontsize=8.1)
        return doc.tobytes()


def item(value="547 MIDNIGHT BLUE", brand="Volkswagen"):
    return sds.parse_items("template",brand,sds.DEFAULT_TITLE,value)[0]


@pytest.mark.parametrize("paint,varnish",[(2,2),(24,19)])
def test_structure_and_all_identification_fields(paint,varnish):
    source,product = fixture_pdf(paint,varnish),item()
    output,validation = sds.generate(product["title"],source,item=product)
    assert validation["pages"] == paint+varnish
    assert validation["identification_regions_exact"] == 2*(paint+varnish)+3
    assert validation["dates_preserved"] and validation["protected_text_preserved"]
    with sds.open_pdf(output) as doc:
        sds.preflight(doc)
        for index,page in enumerate(doc):
            assert "Volkswagen 547 MIDNIGHT BLUE" in two.text(page,two.HEADER)
            assert "Volkswagen 547 MIDNIGHT BLUE" in two.text(page,two.FOOTER)
            assert f"Protected synthetic safety text page {index+1}" in page.get_text()
            assert "LC9A" not in page.get_text()
        assert two.text(doc[paint],two.PRODUCT) == "Asturcolor Clear Varnish Marker - 12 ml"
        for index in (0,paint):
            assert two.text(doc[index],two.KIT) == "Kit identifier: " + product["title"]
            assert "UFI: " in doc[index].get_text()


def test_generated_reference_new_brands_and_glyphs():
    source = fixture_pdf()
    for product in (item(),item("744\tPLATA BRILLANTE METALIZADO","Mercedes-Benz"),item("LF3K Červená","Škoda")):
        source,validation = sds.generate(product["title"],source,item=product)
        assert validation["protected_text_preserved"]
        with sds.open_pdf(source) as doc:
            sds.preflight(doc)
            assert all("\x00" not in page.get_text() for page in doc)


def test_code_only_row_does_not_print_parser_color_sentinel():
    product=item("547")
    output,_=sds.generate(product["title"],fixture_pdf(),item=product)
    with sds.open_pdf(output) as doc:
        assert two.text(doc[0],two.HEADER)=="Asturcolor Volkswagen 547 - 2 x 12 ml touch-up marker kit"


def test_bad_pagination_last_page_damage_and_missing_identity_rejected():
    source,product = fixture_pdf(),item()
    with sds.open_pdf(source) as doc:
        doc[3].add_redact_annot(two.NUMBER,fill=(1,1,1))
        doc[3].apply_redactions()
        with pytest.raises(sds.SDSError,match="нумерація"):
            sds.preflight(doc)
    output,_ = sds.generate(product["title"],source,item=product)
    with sds.open_pdf(output) as doc:
        doc[3].insert_text((46.6,430),"Unexpected body change",fontsize=8)
        corrupted=doc.tobytes()
    with pytest.raises(sds.SDSError,match="захищений текст.*4"):
        sds.validate_output(corrupted,source,product["title"],item=product)
    with pytest.raises(sds.SDSError,match="марка"):
        sds.generate(product["title"],source)


def test_api_settings_batch_previews_and_timestamp(client):
    source=fixture_pdf(24,19)
    reference=storage.save_bytes(source,"two-component-reference.pdf","application/pdf")
    response=client.put("/api/settings/sds/reference",json={"token":reference["token"]})
    assert response.status_code==200,response.text
    assert response.json()["pages"]==43
    batch_id=str(uuid.uuid4())
    for index,product in enumerate((item(),item("LA7W SILVER"))):
        response=client.post("/api/sds/generate",json=dict(item=product,render_previews=True,preview_count=4,
            batch_id=batch_id,batch_index=index,batch_size=2,last_written_at="2026-11-04T18:43:00+02:00"))
        assert response.status_code==200,response.text
        result=response.json()
        assert result["validation"]["pages"]==43 and len(result["previews"])==4
        assert "p25.png" in result["previews"][2]["name"]
        download=client.get(result["file"]["url"])
        sds.validate_output(download.content,source,product["title"],item=product)
    grouped=client.get("/api/history/batches/"+batch_id).json()
    assert grouped["count"]==2 and not grouped["partial"]
    assert all(entry["last_written_at"]=="2026-11-04T18:43:00+02:00" for entry in grouped["items"])
