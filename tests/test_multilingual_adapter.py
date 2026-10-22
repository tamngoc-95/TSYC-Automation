"""Offline tests for the existing-architecture multilingual adapter
(src/domain/polylang_link_graph.py, src/domain/multilingual_adapter.py)
against tests/support/fake_polylang_store.py.

The fake store's hooks are assumptions (the Flatsome Child source has not
been inspected). These tests prove TSYC's rules under each assumption, not
the live hooks; staging remains required (docs/POLYLANG_EXISTING_ARCHITECTURE.md).

No network, no Supabase, no WooCommerce.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

import create_woocommerce_draft as cwd
from src.domain import multilingual_adapter as ma
from src.domain import polylang_link_graph as plg
from support.fake_polylang_store import FakePolylangStore, InterruptedWrite

SKU = "TSYC-FB-HIST-2026-AUTOIMPORT-CAN-0022"
IPID = "eb5ca895-e6ab-4ec8-abcd-1d25940f5565"
OWNED = {"_tsyc_internal_product_id": IPID}


def render(content):
    return cwd.build_description(content), cwd.build_short_description(content)


def approved(lang, name):
    return {"content_language": lang, "content_status": "APPROVED", "review_required": False,
            "product_name": name, "short_description": f"{name} short", "long_description": f"{name} long."}


CONTENTS = {"en": approved("en", "How to Talk So Kids Will Listen"),
            "de": approved("de", "Wie man mit Kindern spricht")}


@dataclass
class Mechanism:
    name: str = "staging-proven endpoint"
    verified_on_staging: bool = True


def can_0022(store, stock=0, vi_backorders="notify", backorders="yes"):
    """The live CAN-0022 trio: linked, same price/stock, VI backorders 'notify'."""
    common = {"sku": SKU, "price": "13.99", "image": 3779, "stock": stock}
    store.add(3780, "vi", "noi-sao-cho-tre-nghe-loi", meta=OWNED, backorders=vi_backorders, **common)
    store.add(3926, "en", "how-to-talk-so-kids-will-listen-noi-sao-cho-tre-nghe-loi", backorders=backorders, **common)
    store.add(3927, "de", "wie-man-mit-kindern-spricht-noi-sao-cho-tre-nghe-loi", backorders=backorders, **common)
    store.link(3780, 3926, 3927)


def resolve(store, source_id=3780, sku=SKU):
    groups, _ = plg.build_groups(list(store.products.values()), store.all_page_links())
    return ma.resolve_group(product_code=sku, source_id=source_id, products_by_id=store.products,
                            sku_matches=store.sku_matches(sku), storefront_groups=groups)


def plan(store, resolution, **kwargs):
    return ma.plan_language_actions(resolution, internal_product_id=IPID, products_by_id=store.products,
                                    approved_contents=kwargs.pop("contents", CONTENTS), render=render, **kwargs)


# --- existing relationships and language IDs ----------------------------

def test_existing_linked_trio_resolves_to_exact_language_ids():
    store = FakePolylangStore()
    can_0022(store)
    resolution = resolve(store)
    assert resolution.status == ma.RESOLVED
    assert resolution.members == {"vi": 3780, "en": 3926, "de": 3927}
    assert {e.source for e in resolution.evidence} == {"SKU", "HREFLANG"}


def test_catalog_graph_reports_backorder_drift_and_shape():
    store = FakePolylangStore()
    can_0022(store)
    groups, unlinked = plg.build_groups(list(store.products.values()), store.all_page_links())
    assert unlinked == [] and len(groups) == 1 and groups[0].status == plg.GROUP_OK
    assert [f.detail.split(":")[0] for f in groups[0].findings if f.code == plg.FIELD_DRIFT] == ["backorders"]


def test_owner_maintained_published_translations_are_never_written():
    store = FakePolylangStore()
    can_0022(store)
    actions = plan(store, resolve(store), create_authorized=True, link_mechanism=Mechanism())
    assert actions == {"en": ma.SKIP_OWNER_MAINTAINED, "de": ma.SKIP_OWNER_MAINTAINED}
    assert store.writes == []


# --- missing translations ----------------------------------------------

def test_source_only_book_plans_creation_only_with_authorization_and_a_proven_mechanism():
    store = FakePolylangStore()
    store.add(4868, "vi", "co-tich-1", status="draft", sku=SKU, price="", meta=OWNED)
    resolution = resolve(store, source_id=4868)
    assert resolution.status == ma.SOURCE_ONLY
    assert set(plan(store, resolution).values()) == {ma.CREATE_NOT_AUTHORIZED}
    assert set(plan(store, resolution, create_authorized=True).values()) == {ma.CREATE_BLOCKED_NO_LINK_MECHANISM}
    unproven = Mechanism(verified_on_staging=False)
    assert set(plan(store, resolution, create_authorized=True, link_mechanism=unproven).values()) == {
        ma.CREATE_BLOCKED_NO_LINK_MECHANISM}
    assert set(plan(store, resolution, create_authorized=True, link_mechanism=Mechanism()).values()) == {
        ma.CREATE_AND_LINK}


def test_missing_approved_content_skips_that_language_only():
    store = FakePolylangStore()
    store.add(4868, "vi", "co-tich-1", status="draft", sku=SKU, meta=OWNED)
    actions = plan(store, resolve(store, source_id=4868), contents={"en": CONTENTS["en"]},
                   create_authorized=True, link_mechanism=Mechanism())
    assert actions == {"en": ma.CREATE_AND_LINK, "de": ma.SKIP_NO_APPROVED_CONTENT}


# --- CAN-0003: draft source, published translations ----------------------

def test_draft_source_with_published_copies_like_can_0003_is_unverifiable():
    store = FakePolylangStore()
    store.add(3311, "vi", "tu-be-trai", status="draft", sku=SKU, meta=OWNED, image=3310)
    store.add(4014, "en", "from-a-boy-tu-be-trai", sku=SKU, image=3310)
    store.add(4018, "de", "vom-jungen-tu-be-trai", sku=SKU, image=3310)
    store.link(3311, 4014, 4018)
    resolution = resolve(store, source_id=3311)
    assert resolution.status == ma.UNVERIFIABLE
    assert any("vi #3311 (draft) has no public page" in r for r in resolution.reasons)
    assert set(plan(store, resolution, create_authorized=True, link_mechanism=Mechanism()).values()) == {ma.REFUSE}


# --- duplicate linking, wrong book / edition ------------------------------

def test_linked_copy_without_sku_belongs_to_the_group_by_its_polylang_link():
    """10 live groups: the VI carries the TSYC SKU, the linked copies none."""
    store = FakePolylangStore()
    can_0022(store)
    store.products[3926]["sku"] = store.products[3927]["sku"] = ""
    resolution = resolve(store)
    assert resolution.status == ma.RESOLVED
    assert resolution.members == {"vi": 3780, "en": 3926, "de": 3927}
    assert [e.source for e in resolution.evidence].count("HREFLANG_ONLY") == 2


def test_linked_copy_with_another_sku_is_a_conflict():
    store = FakePolylangStore()
    can_0022(store)
    store.products[3927]["sku"] = "OTHER-BOOK"
    resolution = resolve(store)
    assert resolution.status == ma.CONFLICT
    assert any("de #3927 is linked on the storefront but carries SKU 'OTHER-BOOK'" in r for r in resolution.reasons)


def test_unlinked_sku_copy_competing_with_the_linked_copy_is_a_conflict():
    store = FakePolylangStore()
    can_0022(store)
    store.products[3926]["sku"] = ""
    store.add(4001, "en", "how-to-talk-unlinked", sku=SKU)
    resolution = resolve(store)
    assert resolution.status == ma.CONFLICT
    assert any("en: SKU says #4001, storefront says #3926" in r for r in resolution.reasons)


def test_two_copies_in_one_language_are_never_resolved():
    store = FakePolylangStore()
    can_0022(store)
    store.add(3999, "en", "how-to-talk-so-kids-will-listen-2", sku=SKU, price="13.99")
    resolution = resolve(store)
    assert resolution.status == ma.CONFLICT
    assert any("DUPLICATE_LANGUAGE" in r for r in resolution.reasons)


def test_tsyc_source_must_be_the_vi_product_carrying_the_sku():
    store = FakePolylangStore()
    can_0022(store)
    assert resolve(store, source_id=3926).status == ma.SOURCE_INVALID
    assert resolve(store, source_id=3780, sku="TSYC-OTHER").status == ma.SOURCE_INVALID
    assert resolve(store, source_id=1).status == ma.SOURCE_INVALID


def test_same_image_other_edition_linked_by_bulk_link_v2_is_a_conflict():
    """v2 links on a shared featured image. A different edition sharing the
    cover gets linked into the group: TSYC must stop, not adopt it."""
    store = FakePolylangStore()
    store.add(3780, "vi", "noi-sao-cho-tre-nghe-loi", sku=SKU, image=3779, meta=OWNED)
    store.add(3926, "en", "how-to-talk-noi-sao-cho-tre-nghe-loi", sku=SKU, image=3779)
    store.add(5001, "vi", "noi-sao-cho-tre-nghe-loi-tai-ban-2024", image=3779)
    store.bulk_link_v2()
    groups, _ = plg.build_groups(list(store.products.values()), store.all_page_links())
    assert [g.status for g in groups] == [plg.GROUP_CONFLICTED]
    assert resolve(store).status == ma.CONFLICT


def test_bulk_link_v1_and_v2_disagreement_is_reported_not_merged():
    """v1 (slug root) and v2 (image) can link different sets; the last run
    wins in Polylang. The graph must expose a group whose members do not
    agree with the SKU evidence instead of trusting either plugin."""
    store = FakePolylangStore()
    store.add(10, "vi", "ehon-meo", sku=SKU, image=900, meta=OWNED)
    store.add(11, "en", "ehon-meo-en", sku=SKU, image=901)
    store.add(12, "en", "cat-picture-book", image=900)
    store.bulk_link_v1()
    assert resolve(store, source_id=10).status == ma.RESOLVED
    store.bulk_link_v2()
    assert resolve(store, source_id=10).status == ma.CONFLICT


def test_unlinked_published_copy_is_never_merged_on_slug_or_image():
    store = FakePolylangStore()
    store.add(10, "vi", "ehon-meo", sku=SKU, image=900, meta=OWNED)
    store.add(11, "en", "ehon-meo-en", image=900)
    groups, unlinked = plg.build_groups(list(store.products.values()), store.all_page_links())
    assert groups == [] and sorted(unlinked) == [10, 11]
    assert resolve(store, source_id=10).status == ma.SOURCE_ONLY


# --- price, inventory, backorders, orders --------------------------------

def test_payload_is_text_only_and_never_carries_price_stock_or_status():
    payload = ma.build_text_payload(CONTENTS["en"], "en", render)
    assert set(payload) == {"name", "description", "short_description"}
    for leaked in ({"regular_price": "1"}, {"stock_quantity": 0}, {"backorders": "yes"}, {"status": "publish"},
                   {"sku": SKU}):
        with pytest.raises(ValueError):
            ma.assert_text_only({**payload, **leaked})


@pytest.mark.parametrize("stock_sync", [True, False])
def test_orders_in_each_language_and_inventory_authority(stock_sync):
    store = FakePolylangStore(stock_sync=stock_sync)
    can_0022(store, stock=3, vi_backorders="no", backorders="no")
    store.place_order(3926)
    store.place_order(3927)
    findings = ma.inventory_authority_findings(resolve(store), store.products)
    if stock_sync:
        assert {p["stock_quantity"] for p in store.products.values()} == {1}
        assert findings == []
    else:
        assert store.products[3780]["stock_quantity"] == 3
        assert any("en #3926 stock_quantity=2" in f for f in findings)


def test_backorder_difference_is_reported_never_corrected():
    store = FakePolylangStore()
    can_0022(store)
    before = store.snapshot()
    findings = ma.inventory_authority_findings(resolve(store), store.products)
    assert len(findings) == 2 and all("backorders" in f for f in findings)
    assert store.snapshot() == before and store.writes == []


def test_meta_copy_hook_would_let_a_translation_save_overwrite_the_vi_price():
    """Why TSYC never saves a linked copy before staging proves the hooks:
    with a metadata-copy hook, saving EN pushes EN's values onto VI."""
    store = FakePolylangStore(meta_copy=True)
    can_0022(store)
    store.products[3926]["regular_price"] = "15.00"
    store.save(3926, {"name": "How to Talk So Kids Will Listen"})
    assert store.products[3780]["regular_price"] == "15.00"


# --- content sync, idempotency, recovery ---------------------------------

def tsyc_draft_trio(store):
    store.add(4868, "vi", "co-tich-1", status="draft", sku=SKU, meta=OWNED)
    store.add(4869, "en", "fairy-tale-1", status="draft", sku=SKU, meta={**OWNED, "_tsyc_language": "en"})
    store.add(4870, "de", "maerchen-1", status="draft", sku=SKU, meta={**OWNED, "_tsyc_language": "de"})
    store.link(4868, 4869, 4870)


def test_draft_copy_without_tsyc_meta_has_unknown_language():
    store = FakePolylangStore()
    store.add(3311, "vi", "tu-be-trai", status="draft", sku=SKU, meta=OWNED)
    store.add(4100, "en", "from-a-boy", status="draft", sku=SKU)
    resolution = resolve(store, source_id=3311)
    assert resolution.status == ma.CONFLICT
    assert any("LANGUAGE_UNKNOWN" in r for r in resolution.reasons)


def test_drafts_without_public_pages_cannot_be_proven_linked():
    store = FakePolylangStore()
    tsyc_draft_trio(store)
    resolution = resolve(store, source_id=4868)
    assert resolution.status == ma.UNVERIFIABLE
    assert set(plan(store, resolution).values()) == {ma.REFUSE}


def resolved_draft_trio(store):
    tsyc_draft_trio(store)
    return ma.GroupResolution(status=ma.RESOLVED, source_id=4868, members={"vi": 4868, "en": 4869, "de": 4870})


def test_text_sync_updates_only_owned_drafts_and_is_idempotent():
    store = FakePolylangStore()
    resolution = resolved_draft_trio(store)
    assert plan(store, resolution) == {"en": ma.UPDATE_TEXT_DRAFT, "de": ma.UPDATE_TEXT_DRAFT}
    for lang in ("en", "de"):
        store.save(resolution.members[lang], ma.build_text_payload(CONTENTS[lang], lang, render))
    assert plan(store, resolution) == {"en": ma.NO_OP, "de": ma.NO_OP}


def test_draft_copy_without_tsyc_ownership_is_owner_maintained():
    store = FakePolylangStore()
    resolution = resolved_draft_trio(store)
    store.products[4870]["meta_data"] = []
    assert plan(store, resolution)["de"] == ma.SKIP_OWNER_MAINTAINED


def test_interrupted_write_is_reconciled_before_anything_else():
    store = FakePolylangStore()
    resolution = resolved_draft_trio(store)
    with pytest.raises(InterruptedWrite):
        store.save(4869, ma.build_text_payload(CONTENTS["en"], "en", render), interrupt=True)
    assert plan(store, resolution, uncertain_languages=("en",))["en"] == ma.RECONCILE_FIRST
    # After reconciling (a fresh read), the saved text shows the write landed.
    assert plan(store, resolution)["en"] == ma.NO_OP


def test_unread_sibling_page_makes_the_group_conflicted():
    store = FakePolylangStore()
    can_0022(store)
    pages = store.all_page_links()
    pages[3927] = None
    groups, _ = plg.build_groups(list(store.products.values()), pages)
    assert groups[0].status == plg.GROUP_CONFLICTED
    assert any(f.code == plg.PAGE_UNREAD for f in groups[0].findings)


def test_sibling_save_coincidence_is_detected():
    store = FakePolylangStore(meta_copy=True)
    can_0022(store)
    store.save(3926, {"name": "x"})
    groups, _ = plg.build_groups(list(store.products.values()), store.all_page_links())
    assert plg.sibling_save_coincidences(groups, store.products) == [(3780, 3926, 3927)]


def test_audit_script_only_issues_get_requests():
    source = (cwd.PROJECT_ROOT / "scripts" / "audit_polylang_catalog.py").read_text(encoding="utf-8")
    for verb in ("requests.post", "requests.put", "requests.delete", "requests.patch", ".table(", '"orders'):
        assert verb not in source
