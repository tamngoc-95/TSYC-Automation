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


def record(store, lang, pid):
    """TSYC's sync record of a product it created, with the baseline read
    right after its last confirmed write."""
    product = store.products.get(pid, {})
    return ma.TsycProductRecord(lang=lang, woo_id=pid, internal_product_id=IPID,
                                baseline_modified_gmt=product.get("date_modified_gmt"),
                                baseline_text={f: str(product.get(f) or "") for f in ma.wtg.TRANSLATED_FIELDS})


def source_records(store, source_id):
    return {"vi": record(store, "vi", source_id)}


def link_map(store, source_id, verified=True):
    return ma.PolylangLinkMap(source_id=source_id, translations=store.polylang_translations(source_id),
                              mechanism="staging-proven endpoint", verified_on_staging=verified)


LIVE = object()  # today: no Polylang map can be read


def resolve(store, source_id=3780, sku=SKU, *, links=None, records=None, pages=None):
    groups, _ = plg.build_groups(list(store.products.values()),
                                 store.all_page_links() if pages is None else pages)
    if links is None:
        links = link_map(store, source_id) if source_id in store.products else None
    elif links is LIVE:
        links = None
    return ma.resolve_group(product_code=sku, source_id=source_id, products_by_id=store.products,
                            sku_matches=store.sku_matches(sku), storefront_groups=groups,
                            tsyc_records=source_records(store, source_id) if records is None else records,
                            link_map=links)


def plan(store, resolution, records=None, **kwargs):
    return ma.plan_language_actions(
        resolution, internal_product_id=IPID, products_by_id=store.products,
        approved_contents=kwargs.pop("contents", CONTENTS), render=render,
        tsyc_records=source_records(store, resolution.source_id) if records is None else records, **kwargs)


# --- existing relationships and language IDs ----------------------------

def test_existing_linked_trio_resolves_to_exact_language_ids():
    store = FakePolylangStore()
    can_0022(store)
    resolution = resolve(store)
    assert resolution.status == ma.RESOLVED
    assert resolution.members == {"vi": 3780, "en": 3926, "de": 3927}
    assert {e.source for e in resolution.evidence} == {"TSYC_RECORD", "SKU", "HREFLANG", "POLYLANG_LINK"}


def test_live_today_without_a_polylang_map_nothing_is_resolved():
    """SKU and hreflang agree on the live trio, yet without Polylang's own
    map from a staging-proven mechanism the links stay unproven."""
    store = FakePolylangStore()
    can_0022(store)
    resolution = resolve(store, links=LIVE)
    assert resolution.status == ma.UNVERIFIABLE
    assert resolution.members == {"vi": 3780, "en": 3926, "de": 3927}  # for read-only reporting
    assert set(plan(store, resolution, create_authorized=True, link_mechanism=Mechanism()).values()) == {ma.REFUSE}
    unproven = resolve(store, links=link_map(store, 3780, verified=False))
    assert unproven.status == ma.UNVERIFIABLE
    assert any("not proven on staging" in r for r in unproven.reasons)


def test_missing_map_never_proves_that_no_translation_exists():
    store = FakePolylangStore()
    store.add(4868, "vi", "co-tich-1", status="draft", sku=SKU, meta=OWNED)
    resolution = resolve(store, source_id=4868, links=LIVE)
    assert resolution.status == ma.UNVERIFIABLE
    assert any("that no translation exists cannot be proven" in r for r in resolution.reasons)
    assert set(plan(store, resolution, create_authorized=True, link_mechanism=Mechanism()).values()) == {ma.REFUSE}


def test_hreflang_disagreeing_with_polylang_is_a_conflict():
    """A stale or wrong hreflang (cache, bad theme template) never wins:
    the sources must agree."""
    store = FakePolylangStore()
    can_0022(store)
    pages = store.all_page_links()
    pages[3780] = {"vi": pages[3780]["vi"], "de": pages[3780]["de"]}  # EN missing on the VI page
    resolution = resolve(store, pages=pages)
    assert resolution.status == ma.CONFLICT


def test_polylang_link_to_another_book_is_a_conflict():
    store = FakePolylangStore()
    can_0022(store)
    store.add(5200, "de", "anderes-buch", sku="TSYC-OTHER-BOOK")
    store.link(3780, 3926, 5200)           # Polylang now links the wrong DE book
    resolution = resolve(store)
    assert resolution.status == ma.CONFLICT
    assert any("de" in r and "#5200" in r for r in resolution.reasons)


def test_map_of_another_source_or_unread_member_is_a_conflict():
    store = FakePolylangStore()
    can_0022(store)
    wrong_source = ma.PolylangLinkMap(source_id=3926, translations={"vi": 3926}, mechanism="x",
                                      verified_on_staging=True)
    assert resolve(store, links=wrong_source).status == ma.CONFLICT
    unread = ma.PolylangLinkMap(source_id=3780, translations={"vi": 3780, "en": 3926, "de": 9999},
                                mechanism="x", verified_on_staging=True)
    resolution = resolve(store, links=unread)
    assert resolution.status == ma.CONFLICT
    assert any("#9999 is linked by Polylang but was not read" in r for r in resolution.reasons)


def test_conflicting_recorded_woo_ids_are_a_conflict():
    store = FakePolylangStore()
    tsyc_draft_trio(store)
    records = trio_records(store)
    records["en"] = record(store, "en", 4870)       # TSYC thinks DE #4870 is its EN draft
    resolution = resolve(store, source_id=4868, records=records)
    assert resolution.status == ma.CONFLICT
    store.add(4871, "en", "fairy-tale-1-owner", status="draft")
    relinked = ma.PolylangLinkMap(source_id=4868, translations={"vi": 4868, "en": 4871, "de": 4870},
                                  mechanism="x", verified_on_staging=True)
    resolution = resolve(store, source_id=4868, records=trio_records(store), links=relinked)
    assert resolution.status == ma.CONFLICT
    assert any("TSYC recorded en #4869, Polylang links 4871" in r for r in resolution.reasons)


def test_source_must_be_tsyc_recorded_vi_product():
    store = FakePolylangStore()
    can_0022(store)
    assert resolve(store, records={}).status == ma.SOURCE_INVALID
    assert resolve(store, records={"vi": record(store, "vi", 3926)}).status == ma.SOURCE_INVALID


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
    resolution = resolve(store, source_id=3311, links=LIVE)
    assert resolution.status == ma.UNVERIFIABLE   # the VI draft's link has no public page
    assert set(plan(store, resolution, create_authorized=True, link_mechanism=Mechanism()).values()) == {ma.REFUSE}
    # With Polylang's own map the group is proven, and the published
    # owner copies are still never written.
    proven = resolve(store, source_id=3311)
    assert proven.status == ma.RESOLVED
    assert set(plan(store, proven, create_authorized=True, link_mechanism=Mechanism()).values()) == {
        ma.SKIP_OWNER_MAINTAINED}


# --- duplicate linking, wrong book / edition ------------------------------

def test_linked_copy_without_sku_is_linked_but_its_identity_is_unproven():
    """10 live groups: the VI carries the TSYC SKU, the linked copies none.
    The link is Polylang's, but it may come from a bulk-link slug or image
    match, so without the SKU or a TSYC record the book is unproven."""
    store = FakePolylangStore()
    can_0022(store)
    store.products[3926]["sku"] = store.products[3927]["sku"] = ""
    resolution = resolve(store)
    assert resolution.status == ma.UNVERIFIABLE
    assert resolution.members == {"vi": 3780, "en": 3926, "de": 3927}
    assert [e.source for e in resolution.evidence].count("HREFLANG_ONLY") == 2
    assert sum("book identity is unproven" in r for r in resolution.reasons) == 2
    assert set(plan(store, resolution).values()) == {ma.REFUSE}


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


def trio_records(store):
    return {"vi": record(store, "vi", 4868), "en": record(store, "en", 4869), "de": record(store, "de", 4870)}


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
    resolution = resolve(store, source_id=4868, records=trio_records(store), links=LIVE)
    assert resolution.status == ma.UNVERIFIABLE
    assert set(plan(store, resolution, records=trio_records(store)).values()) == {ma.REFUSE}


def resolved_draft_trio(store):
    tsyc_draft_trio(store)
    records = trio_records(store)
    resolution = resolve(store, source_id=4868, records=records)
    assert resolution.status == ma.RESOLVED
    return resolution, records


def test_text_sync_updates_only_owned_drafts_and_is_idempotent():
    store = FakePolylangStore()
    resolution, records = resolved_draft_trio(store)
    assert plan(store, resolution, records) == {"en": ma.UPDATE_TEXT_DRAFT, "de": ma.UPDATE_TEXT_DRAFT}
    for lang in ("en", "de"):
        store.save(resolution.members[lang], ma.build_text_payload(CONTENTS[lang], lang, render))
    # Repeated runs, before and after the writer records its new baseline.
    assert plan(store, resolution, records) == {"en": ma.NO_OP, "de": ma.NO_OP}
    records = trio_records(store)
    for _ in range(2):
        assert plan(store, resolution, records) == {"en": ma.NO_OP, "de": ma.NO_OP}
    assert len(store.writes) == 2


def test_recorded_draft_whose_meta_was_removed_has_unknown_ownership():
    store = FakePolylangStore()
    resolution, records = resolved_draft_trio(store)
    store.products[4870]["meta_data"] = []
    assert ma.ownership_of(store.products[4870], "de", internal_product_id=IPID,
                           record=records["de"]) == ma.OWN_UNKNOWN
    assert plan(store, resolution, records)["de"] == ma.REFUSE


def test_owner_edit_after_tsyc_baseline_is_never_overwritten():
    store = FakePolylangStore()
    resolution, records = resolved_draft_trio(store)
    store.products[4869]["description"] = "<p>Owner's own text.</p>"
    store.products[4870]["date_modified_gmt"] = "2026-10-25T09:00:00"  # e.g. a sibling-save hook
    assert ma.ownership_of(store.products[4869], "en", internal_product_id=IPID,
                           record=records["en"]) == ma.OWN_TSYC_DRAFT_EDITED
    assert plan(store, resolution, records) == {"en": ma.SKIP_OWNER_MAINTAINED, "de": ma.SKIP_OWNER_MAINTAINED}


def test_recorded_draft_published_by_the_owner_is_owner_maintained():
    store = FakePolylangStore()
    resolution, records = resolved_draft_trio(store)
    store.products[4869]["status"] = "publish"
    assert ma.ownership_of(store.products[4869], "en", internal_product_id=IPID,
                           record=records["en"]) == ma.OWN_PUBLISHED
    assert plan(store, resolution, records)["en"] == ma.SKIP_OWNER_MAINTAINED


def test_recorded_draft_without_a_baseline_has_unknown_ownership():
    store = FakePolylangStore()
    resolution, records = resolved_draft_trio(store)
    records["en"] = ma.TsycProductRecord(lang="en", woo_id=4869, internal_product_id=IPID)
    assert plan(store, resolution, records)["en"] == ma.REFUSE


def test_copied_ownership_meta_on_an_unrecorded_copy_is_a_conflict():
    """The developer makes EN with Polylang's "+" and a metadata-copy hook
    copies `_tsyc_internal_product_id` (and SKU) from the VI draft. The
    meta alone must never make that copy TSYC's."""
    store = FakePolylangStore()
    store.add(4868, "vi", "co-tich-1", status="draft", sku=SKU, meta=OWNED)
    store.add(4900, "en", "fairy-tale-1", status="draft", sku=SKU, meta=dict(OWNED))
    store.link(4868, 4900)
    resolution = resolve(store, source_id=4868)
    assert resolution.status == ma.CONFLICT
    assert any("#4900 carries TSYC's ownership meta" in r for r in resolution.reasons)
    # Even with a forced resolution the copy is never written.
    forced = ma.GroupResolution(status=ma.RESOLVED, source_id=4868, members={"vi": 4868, "en": 4900})
    assert ma.ownership_of(store.products[4900], "en", internal_product_id=IPID, record=None) == ma.OWN_CONFLICT
    assert plan(store, forced)["en"] == ma.REFUSE


def test_copied_language_meta_contradicting_the_record_is_a_conflict():
    store = FakePolylangStore()
    tsyc_draft_trio(store)
    store.products[4870]["meta_data"] = [{"key": "_tsyc_internal_product_id", "value": IPID},
                                         {"key": "_tsyc_language", "value": "en"}]
    resolution = resolve(store, source_id=4868, records=trio_records(store))
    assert resolution.status == ma.CONFLICT


def test_linked_owner_draft_without_tsyc_meta_is_owner_maintained():
    store = FakePolylangStore()
    store.add(4868, "vi", "co-tich-1", status="draft", sku=SKU, meta=OWNED)
    store.add(4901, "en", "fairy-tale-1", status="draft")
    assert ma.ownership_of(store.products[4901], "en", internal_product_id=IPID,
                           record=None) == ma.OWN_LINKED_TRANSLATION
    forced = ma.GroupResolution(status=ma.RESOLVED, source_id=4868, members={"vi": 4868, "en": 4901})
    assert plan(store, forced)["en"] == ma.SKIP_OWNER_MAINTAINED


def test_recorded_translation_missing_from_the_group_is_refused():
    store = FakePolylangStore()
    resolution, records = resolved_draft_trio(store)
    shrunk = ma.GroupResolution(status=ma.RESOLVED, source_id=4868, members={"vi": 4868, "de": 4870})
    assert plan(store, shrunk, records, create_authorized=True, link_mechanism=Mechanism())["en"] == ma.REFUSE


def test_unlinked_tsyc_translation_is_a_conflict():
    store = FakePolylangStore()
    tsyc_draft_trio(store)
    store.group_of.pop(4870)            # DE exists and is recorded, but Polylang lost the link
    resolution = resolve(store, source_id=4868, records=trio_records(store))
    assert resolution.status == ma.CONFLICT
    assert any("TSYC recorded de #4870, Polylang links None" in r for r in resolution.reasons)


def test_resolution_and_planning_never_write():
    store = FakePolylangStore()
    resolution, records = resolved_draft_trio(store)
    before = store.snapshot()
    for _ in range(2):
        resolve(store, source_id=4868, records=records)
        plan(store, resolution, records, create_authorized=True, link_mechanism=Mechanism())
    assert store.snapshot() == before and store.writes == []


def test_interrupted_write_is_reconciled_before_anything_else():
    store = FakePolylangStore()
    resolution, records = resolved_draft_trio(store)
    with pytest.raises(InterruptedWrite):
        store.save(4869, ma.build_text_payload(CONTENTS["en"], "en", render), interrupt=True)
    assert plan(store, resolution, records, uncertain_languages=("en",))["en"] == ma.RECONCILE_FIRST
    # After reconciling (a fresh read), the saved text shows the write landed.
    assert plan(store, resolution, records)["en"] == ma.NO_OP


def test_interrupted_write_that_did_not_land_still_waits_for_reconciliation():
    store = FakePolylangStore()
    resolution, records = resolved_draft_trio(store)
    assert plan(store, resolution, records, uncertain_languages=("en",))["en"] == ma.RECONCILE_FIRST
    assert plan(store, resolution, records)["en"] == ma.UPDATE_TEXT_DRAFT


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
