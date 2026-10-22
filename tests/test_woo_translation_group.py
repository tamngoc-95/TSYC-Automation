"""Offline tests for src/domain/woo_translation_group.py.

Two shapes are covered:
  * production today (read-only snapshot 2026-10-22): free Polylang with
    independent VI/EN/DE product copies that share one SKU;
  * the Polylang for WooCommerce contract (tests/support/fake_pllwc_store),
    exercising the staging matrix T1-T7 of docs/POLYLANG_WOOCOMMERCE_PLAN.md
    against TSYC's own rules. This proves TSYC's logic, not the add-on.

No network, no Supabase, no WooCommerce.
"""
from __future__ import annotations

import pytest

import create_woocommerce_draft as cwd
from src.domain import woo_translation_group as wtg
from src.domain.rules import preorder_inventory_rules as preorder
from support.fake_pllwc_store import FakePllwcStore

SKU = "TSYC-FAHASA-2026-002-CAN-0001"
PROD = "https://tiemsachyeucon.com"


def render(content):
    return cwd.build_description(content), cwd.build_short_description(content)


def approved(lang, name, **overrides):
    row = {
        "content_language": lang,
        "content_status": "APPROVED",
        "review_required": False,
        "product_name": name,
        "short_description": f"{name} short",
        "long_description": f"{name} long description.",
    }
    row.update(overrides)
    return row


CONTENTS = {
    "en": approved("en", "From a Boy to a Grown-Up"),
    "de": approved("de", "Vom Jungen zum Erwachsenen"),
}


def manual(pid, lang, status="publish", **fields):
    prefix = "" if lang == "vi" else f"/{lang}"
    row = {
        "id": pid, "sku": SKU, "status": status, "permalink": f"{PROD}{prefix}/product/p-{pid}/",
        "regular_price": "9.99", "sale_price": "", "manage_stock": True, "stock_quantity": 0,
        "backorders": "yes", "stock_status": "onbackorder",
    }
    row.update(fields)
    return row


def codes(group):
    return sorted(issue.code for issue in group.issues)


# --- language detection -------------------------------------------------

@pytest.mark.parametrize("product, expected", [
    ({"lang": "de", "permalink": f"{PROD}/product/x/"}, ("de", wtg.BASIS_ADDON)),
    ({"permalink": f"{PROD}/en/product/x/"}, ("en", wtg.BASIS_PERMALINK_PREFIX)),
    ({"permalink": f"{PROD}/de/product/x/"}, ("de", wtg.BASIS_PERMALINK_PREFIX)),
    ({"permalink": f"{PROD}/product/x/"}, ("vi", wtg.BASIS_PERMALINK_DEFAULT)),
    ({"permalink": f"{PROD}/?post_type=product&p=3311"}, ("vi", wtg.BASIS_PERMALINK_DEFAULT)),
    ({"permalink": f"{PROD}/english-books/x/"}, ("vi", wtg.BASIS_PERMALINK_DEFAULT)),
    ({"permalink": ""}, (None, None)),
    ({"lang": ""}, (None, None)),
])
def test_product_language(product, expected):
    assert wtg.product_language(product) == expected


# --- production today: manual copies ------------------------------------

def test_manual_group_reports_backorder_drift_like_can_0022():
    group = wtg.evaluate_translation_group(SKU, [
        manual(3780, "vi", backorders="notify"), manual(3926, "en"), manual(3927, "de"),
    ])
    assert group.mode == wtg.MODE_MANUAL
    assert group.ok
    assert set(group.members) == {"vi", "en", "de"}
    assert wtg.SHARED_FIELD_DRIFT in codes(group)
    assert wtg.NOT_ADDON_MANAGED in codes(group)


def test_manual_group_flags_sellable_translation_of_draft_source_like_can_0003():
    group = wtg.evaluate_translation_group(SKU, [
        manual(3311, "vi", status="draft"), manual(4014, "en"), manual(4018, "de"),
    ])
    sellable = [i for i in group.issues if i.code == wtg.SELLABLE_WITHOUT_SOURCE]
    assert [i.severity for i in sellable] == [wtg.WARNING, wtg.WARNING]


def test_price_formats_are_compared_numerically():
    group = wtg.evaluate_translation_group(SKU, [
        manual(1, "vi", regular_price="50"), manual(2, "en", regular_price="50.00"),
    ])
    assert wtg.SHARED_FIELD_DRIFT not in codes(group)


def test_single_unlinked_product_is_clean():
    group = wtg.evaluate_translation_group(SKU, [manual(1, "vi", status="draft")])
    assert group.ok and group.issues == []
    assert wtg.select_source_product(SKU, [manual(1, "vi")])["id"] == 1


@pytest.mark.parametrize("matches, code", [
    ([manual(1, "vi"), manual(2, "vi")], wtg.DUPLICATE_LANGUAGE),
    ([manual(2, "en"), manual(3, "de")], wtg.NO_SOURCE),
    ([manual(1, "vi"), manual(2, "en", permalink="")], wtg.LANGUAGE_UNKNOWN),
    ([manual(1, "vi"), {**manual(2, "en"), "sku": SKU + "-X"}], wtg.SKU_MISMATCH),
    ([manual(1, "vi"), {**manual(2, "en"), "lang": "en", "translations": {}}], wtg.MIXED_MODE),
])
def test_inconsistent_groups_are_errors_and_never_pick_a_winner(matches, code):
    group = wtg.evaluate_translation_group(SKU, matches)
    assert code in [i.code for i in group.errors]
    with pytest.raises(ValueError):
        wtg.select_source_product(SKU, matches)


def test_unused_sku_has_no_source():
    assert wtg.select_source_product(SKU, []) is None


def test_planner_refuses_manual_mode():
    group = wtg.evaluate_translation_group(SKU, [manual(1, "vi", status="draft")])
    assert wtg.plan_translation_actions(group, CONTENTS, render) == {
        "en": wtg.ACTION_REFUSE, "de": wtg.ACTION_REFUSE}


# --- Polylang for WooCommerce contract: staging matrix T1-T7 -----------

@pytest.fixture
def store():
    return FakePllwcStore(addon=True)


def create_translations(store, source):
    group = wtg.evaluate_translation_group(SKU, store.search_sku(SKU))
    created = {}
    for lang, action in wtg.plan_translation_actions(group, CONTENTS, render).items():
        assert action == wtg.ACTION_CREATE
        payload = wtg.build_translation_payload(store.get(source["id"]), lang, CONTENTS[lang], render)
        created[lang] = store.create(payload)
    return created


def test_t1_source_alone_is_its_own_group(store):
    source = store.seed_source(sku=SKU)
    assert source["lang"] == "vi"
    assert source["translations"] == {"vi": source["id"]}
    group = wtg.evaluate_translation_group(SKU, store.search_sku(SKU))
    assert group.ok and group.mode == wtg.MODE_ADDON and group.issues == []


def test_t2_translation_payload_is_draft_text_only(store):
    source = store.seed_source(sku=SKU)
    payload = wtg.build_translation_payload(source, "en", CONTENTS["en"], render)
    assert payload["status"] == "draft"
    assert payload["lang"] == "en"
    assert payload["translations"] == {"vi": source["id"]}
    assert not wtg.FORBIDDEN_TRANSLATION_FIELDS & payload.keys()
    assert set(payload) == {"name", "description", "short_description", "status", "lang", "translations"}
    assert payload["description"] == cwd.build_description(CONTENTS["en"])


def test_t2_t3_linked_group_shares_one_identity(store):
    source = store.seed_source(sku=SKU)
    created = create_translations(store, source)

    for lang, product in created.items():
        assert wtg.verify_translation_readback(store.get(source["id"]), store.get(product["id"]), lang) == []

    matches = store.search_sku(SKU)
    assert len(matches) == 3  # three rows, one logical book
    group = wtg.evaluate_translation_group(SKU, matches)
    assert group.ok and group.issues == []
    assert group.translation_map() == {"vi": source["id"], "en": created["en"]["id"], "de": created["de"]["id"]}
    assert wtg.select_source_product(SKU, matches)["id"] == source["id"]
    assert [w[0] for w in store.writes] == ["POST", "POST"]
    assert all(w[2]["status"] == "draft" for w in store.writes)


def test_t3_draft_creator_still_refuses_a_multi_row_sku(store, monkeypatch):
    source = store.seed_source(sku=SKU)
    create_translations(store, source)

    class Response:
        status_code = 200

        def json(self):
            return store.search_sku(SKU)

    monkeypatch.setattr(cwd.requests, "get", lambda *a, **k: Response())
    with pytest.raises(RuntimeError, match="ambiguous"):
        cwd.find_product_by_sku("https://x.test", "wc/v3", "ck_x", "cs_x", 5, SKU)


def test_t4_preorder_inventory_is_shared(store):
    source = store.seed_source(sku=SKU)
    created = create_translations(store, source)
    for qty, status in ((1, "instock"), (0, "onbackorder")):
        store.update(source["id"], {"stock_quantity": qty})
        for product in created.values():
            row = store.get(product["id"])
            assert row["stock_quantity"] == qty
            assert row["backorders"] == "yes"
            assert row["stock_status"] == status
    for product in [source, *created.values()]:
        assert preorder.inventory_matches_policy(store.get(product["id"]))
    assert wtg.evaluate_translation_group(SKU, store.search_sku(SKU)).issues == []


def test_t5_price_is_one_price(store):
    source = store.seed_source(sku=SKU)
    created = create_translations(store, source)
    store.update(source["id"], {"regular_price": "12.50"})  # staging test price only
    assert {store.get(p["id"])["regular_price"] for p in created.values()} == {"12.50"}
    assert wtg.evaluate_translation_group(SKU, store.search_sku(SKU)).ok


def test_broken_sync_in_addon_mode_is_an_error(store):
    source = store.seed_source(sku=SKU)
    created = create_translations(store, source)
    store.corrupt(created["de"]["id"], {"regular_price": "1.00"})
    group = wtg.evaluate_translation_group(SKU, store.search_sku(SKU))
    assert [i.code for i in group.errors] == [wtg.SHARED_FIELD_DRIFT]
    problems = wtg.verify_translation_readback(store.get(source["id"]), store.get(created["de"]["id"]), "de")
    assert problems == ["regular_price not synchronised from source"]


def test_t6_images_and_categories_follow_source(store):
    source = store.seed_source(sku=SKU)
    created = create_translations(store, source)
    for product in created.values():
        row = store.get(product["id"])
        assert row["images"] == source["images"]
        assert row["categories"] == source["categories"]


def test_t7_rollback_deletes_translation_and_leaves_source_untouched(store):
    source = store.seed_source(sku=SKU)
    before = store.get(source["id"])
    created = create_translations(store, source)

    store.delete(created["en"]["id"])
    group = wtg.evaluate_translation_group(SKU, store.search_sku(SKU))
    assert group.ok and set(group.members) == {"vi", "de"}
    assert wtg.plan_translation_actions(group, CONTENTS, render)["en"] == wtg.ACTION_CREATE

    store.delete(created["de"]["id"])
    after = store.get(source["id"])
    assert after == {**before, "translations": {"vi": source["id"]}}


def test_translation_map_mismatch_is_an_error(store):
    source = store.seed_source(sku=SKU)
    created = create_translations(store, source)
    store.products[created["en"]["id"]]["_translations"] = {"vi": source["id"], "en": created["en"]["id"]}
    group = wtg.evaluate_translation_group(SKU, store.search_sku(SKU))
    assert wtg.TRANSLATION_MAP_MISMATCH in [i.code for i in group.errors]


# --- read-only production audit -----------------------------------------

def test_audit_reports_only_flagged_tsyc_groups():
    import audit_woo_translation_groups as audit_script

    products = [
        manual(3780, "vi", backorders="notify"), manual(3926, "en"), manual(3927, "de"),
        {**manual(10, "vi"), "sku": "TSYC-SINGLE"},
        {**manual(11, "en"), "sku": ""},
        {**manual(12, "vi"), "sku": "OWNER-1"},
        {**manual(13, "de"), "sku": "OWNER-1"},
    ]
    report = audit_script.audit(products, "TSYC-")
    assert report["tsyc_skus"] == 2
    assert report["products_by_language"] == {"vi": 3, "en": 2, "de": 2}
    assert [g["sku"] for g in report["groups"]] == [SKU]
    assert report["errors"] == 0 and report["warnings"] == 2


def test_audit_refuses_a_partial_catalog():
    import audit_woo_translation_groups as audit_script

    pages = audit_script.fetch_all_products(lambda page: [{"id": page}] if page <= 2 else [])
    assert pages == [{"id": 1}, {"id": 2}]
    with pytest.raises(RuntimeError, match="refusing a partial audit"):
        audit_script.fetch_all_products(lambda page: [{"id": page}])


def test_audit_script_only_issues_get_requests():
    source = (cwd.PROJECT_ROOT / "scripts" / "audit_woo_translation_groups.py").read_text(encoding="utf-8")
    for verb in ("requests.post", "requests.put", "requests.delete", "requests.patch", ".table("):
        assert verb not in source


# --- repeat synchronisation / recovery ----------------------------------

def test_repeat_sync_is_idempotent(store):
    source = store.seed_source(sku=SKU)
    create_translations(store, source)
    writes = len(store.writes)
    group = wtg.evaluate_translation_group(SKU, store.search_sku(SKU))
    assert wtg.plan_translation_actions(group, CONTENTS, render) == {
        "en": wtg.ACTION_NO_OP, "de": wtg.ACTION_NO_OP}
    assert len(store.writes) == writes


def test_changed_approved_text_updates_draft_translation_only(store):
    source = store.seed_source(sku=SKU)
    created = create_translations(store, source)
    store.update(created["de"]["id"], {"status": "publish"})  # owner published DE
    changed = {lang: {**c, "short_description": "new"} for lang, c in CONTENTS.items()}
    group = wtg.evaluate_translation_group(SKU, store.search_sku(SKU))
    assert wtg.plan_translation_actions(group, changed, render) == {
        "en": wtg.ACTION_UPDATE_TEXT, "de": wtg.ACTION_SKIP_OWNER_MAINTAINED}


def test_uncertain_create_reconciles_first_never_blind_retries(store):
    source = store.seed_source(sku=SKU)
    group = wtg.evaluate_translation_group(SKU, store.search_sku(SKU))
    actions = wtg.plan_translation_actions(group, CONTENTS, render, uncertain_languages=["en"])
    assert actions == {"en": wtg.ACTION_RECONCILE_FIRST, "de": wtg.ACTION_CREATE}


def test_published_source_is_owner_maintained(store):
    store.seed_source(sku=SKU, status="publish")
    group = wtg.evaluate_translation_group(SKU, store.search_sku(SKU))
    assert set(wtg.plan_translation_actions(group, CONTENTS, render).values()) == {
        wtg.ACTION_SKIP_OWNER_MAINTAINED}


def test_missing_or_unapproved_content_is_skipped(store):
    store.seed_source(sku=SKU)
    group = wtg.evaluate_translation_group(SKU, store.search_sku(SKU))
    contents = {"en": {**CONTENTS["en"], "content_status": "DRAFTED"}}
    assert wtg.plan_translation_actions(group, contents, render) == {
        "en": wtg.ACTION_SKIP_NO_APPROVED_CONTENT, "de": wtg.ACTION_SKIP_NO_APPROVED_CONTENT}


@pytest.mark.parametrize("source_fields, lang, content, message", [
    ({"status": "publish"}, "en", CONTENTS["en"], "not an automation draft"),
    ({}, "en", {**CONTENTS["en"], "content_status": "DRAFTED"}, "not APPROVED"),
    ({}, "en", {**CONTENTS["en"], "review_required": True}, "requires review"),
    ({}, "en", CONTENTS["de"], "does not match"),
    ({}, "vi", CONTENTS["en"], "Not a translation language"),
    ({}, "en", {**CONTENTS["en"], "product_name": " "}, "no product name"),
])
def test_payload_builder_refuses_unsafe_input(store, source_fields, lang, content, message):
    source = store.seed_source(sku=SKU, **source_fields)
    with pytest.raises(ValueError, match=message):
        wtg.build_translation_payload(source, lang, content, render)


def test_payload_builder_refuses_translated_product_as_source(store):
    source = store.seed_source(sku=SKU)
    created = create_translations(store, source)
    with pytest.raises(ValueError, match="not the VI product"):
        wtg.build_translation_payload(store.get(created["en"]["id"]), "de", CONTENTS["de"], render)
