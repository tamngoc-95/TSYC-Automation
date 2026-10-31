"""Offline tests for the read-only EN/DE translation discovery
(src/domain/translation_discovery.py, scripts/discover_translation_candidates.py).

No network, no Supabase, no WooCommerce.
"""
from __future__ import annotations

from pathlib import Path

import discover_translation_candidates as cli
from src.domain import language_product_mapping as lpm
from src.domain import multilingual_adapter as ma
from src.domain import polylang_link_graph as plg
from src.domain import translation_discovery as td

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOST = "tiemsachyeucon.com"
CODE = "TSYC-FB-HIST-2026-001-CAN-0007"
IPID = "11111111-1111-4111-8111-111111111111"
VI, EN, DE = 3738, 4006, 4007


def product(pid, *, lang="vi", sku=CODE, status="publish", owner=None, image=900):
    prefix = "" if lang == "vi" else f"/{lang}"
    link = (f"https://{HOST}{prefix}/product/book-{pid}/" if status == "publish"
            else f"https://{HOST}/?post_type=product&p={pid}")
    meta = [{"key": "_tsyc_internal_product_id", "value": owner}] if owner else []
    return {"id": pid, "sku": sku, "status": status, "name": f"Book {pid}", "permalink": link,
            "images": [{"id": image}], "meta_data": meta}


def vi_product(**overrides):
    return product(VI, owner=IPID, **overrides)


def row():
    return lpm.LanguageProduct(internal_product_id=IPID, product_code=CODE, language_code="vi",
                               woocommerce_product_id=VI, origin=lpm.ORIGIN_TSYC_CREATED,
                               mapping_status=lpm.RECORDED)


def url(p):
    return f"{HOST}{p['permalink'].split(HOST, 1)[1]}".rstrip("/")


def mutual(*products):
    links = {(p["permalink"].split("/")[3] if p["permalink"].split("/")[3] in ("en", "de") else "vi"): url(p)
             for p in products}
    return {p["id"]: dict(links) for p in products}


def discover(products, page_links, link_map=None):
    catalog = {p["id"]: p for p in products}
    groups, _ = plg.build_groups(products, page_links)
    return td.discover_book(row(), catalog, groups, page_links, link_map)


def classes(book):
    return {(c.language, c.woo_id): c.classification for c in book.candidates}


def test_mutual_hreflang_and_shared_sku_is_only_a_potential_link_without_an_authoritative_map():
    products = [vi_product(), product(EN, lang="en"), product(DE, lang="de")]
    book = discover(products, mutual(*products))
    assert classes(book) == {("de", DE): td.POTENTIAL_LINK, ("en", EN): td.POTENTIAL_LINK}
    assert book.adapter_status == ma.UNVERIFIABLE
    assert book.mapping_insert_allowed == {}
    assert book.outcome == td.POTENTIAL_LINK


def test_only_a_staging_proven_polylang_map_makes_a_verified_link():
    products = [vi_product(), product(EN, lang="en"), product(DE, lang="de")]
    links = mutual(*products)
    unproven = ma.PolylangLinkMap(VI, {"vi": VI, "en": EN, "de": DE}, "endpoint", verified_on_staging=False)
    assert set(classes(discover(products, links, unproven)).values()) == {td.POTENTIAL_LINK}
    proven = ma.PolylangLinkMap(VI, {"vi": VI, "en": EN, "de": DE}, "endpoint", verified_on_staging=True)
    book = discover(products, links, proven)
    assert set(classes(book).values()) == {td.VERIFIED_LINK}
    assert book.mapping_insert_allowed == {"en": EN, "de": DE}


def test_sku_copy_that_polylang_does_not_link_is_owner_maintained_and_never_mapped():
    products = [vi_product(), product(EN, lang="en")]
    book = discover(products, {VI: {}, EN: {}})
    assert classes(book) == {("en", EN): td.OWNER_MAINTAINED}
    assert book.mapping_insert_allowed == {}


def test_copies_of_a_draft_vi_or_an_unread_page_are_unverifiable():
    products = [vi_product(status="draft"), product(EN, lang="en"), product(DE, lang="de")]
    book = discover(products, mutual(products[1], products[2]))
    assert set(classes(book).values()) == {td.UNVERIFIABLE}
    products = [vi_product(), product(EN, lang="en")]
    assert classes(discover(products, {VI: {}, EN: None})) == {("en", EN): td.UNVERIFIABLE}


def test_linked_copy_without_sku_and_image_only_lead_are_unverifiable():
    linked = product(EN, lang="en", sku="")
    lead = product(DE, lang="de", sku="")
    products = [vi_product(), linked, lead]
    links = mutual(products[0], linked)
    links[DE] = {}
    book = discover(products, links)
    assert classes(book) == {("de", DE): td.UNVERIFIABLE, ("en", EN): td.UNVERIFIABLE}
    by_id = {c.woo_id: c for c in book.candidates}
    assert by_id[DE].bases == [td.BASIS_IMAGE] and by_id[EN].bases == [td.BASIS_HREFLANG]


def test_copied_tsyc_meta_is_an_identity_conflict():
    products = [vi_product(), product(4858, sku=f"{CODE}-44", owner=IPID)]
    book = discover(products, {VI: {}, 4858: {}})
    assert classes(book) == {("vi", 4858): td.IDENTITY_CONFLICT}
    assert book.adapter_status == ma.CONFLICT


def test_linked_copy_with_another_sku_or_two_copies_in_one_language_is_a_conflict():
    other = product(EN, lang="en", sku="TSYC-OTHER")
    products = [vi_product(), other]
    assert classes(discover(products, mutual(*products))) == {("en", EN): td.IDENTITY_CONFLICT}
    products = [vi_product(), product(EN, lang="en"), product(4100, lang="en")]
    book = discover(products, {VI: {}, EN: {}, 4100: {}})
    assert set(classes(book).values()) == {td.IDENTITY_CONFLICT}


def test_conflicted_hreflang_group_is_a_conflict():
    products = [vi_product(), product(EN, lang="en"), product(DE, lang="de")]
    links = mutual(*products)
    links[DE] = {"de": url(products[2]), "vi": url(products[0])}  # asymmetric
    assert set(classes(discover(products, links)).values()) == {td.IDENTITY_CONFLICT}


def test_book_without_any_candidate_is_reported_as_such():
    book = discover([vi_product()], {VI: {}})
    assert book.candidates == [] and book.outcome == td.NO_CANDIDATE


def test_cli_run_reads_each_group_whole_and_plans_no_mapping():
    products = [vi_product(), product(EN, lang="en"), product(DE, lang="de"), product(5000, sku="")]
    catalog = {p["id"]: p for p in products}
    links = mutual(*products[:3])
    pages = {p["permalink"]: p["id"] for p in products}
    read = []

    def read_page(permalink):
        read.append(permalink)
        pid = pages[permalink]
        return "".join(f'<link rel="alternate" hreflang="{lang}" href="https://{target}/" />'
                       for lang, target in links.get(pid, {}).items())

    report = cli.run([row()], catalog, read_page, max_pages=10)
    assert sorted(pages[p] for p in read) == [VI, EN, DE]
    assert report["candidates_by_class"][td.POTENTIAL_LINK] == 2
    assert report["mapping_inserts_allowed"] == 0


def test_discovery_never_writes():
    for name in ("scripts/discover_translation_candidates.py", "src/domain/translation_discovery.py"):
        source = (PROJECT_ROOT / name).read_text(encoding="utf-8").replace("sys.path.insert(", "")
        for verb in ("requests.post", "requests.put", "requests.delete", "requests.patch",
                     ".insert(", ".update(", ".upsert(", ".delete(", "execute_sql"):
            assert verb not in source, (name, verb)
