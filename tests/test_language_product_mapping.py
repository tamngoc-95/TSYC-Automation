"""Offline tests for the per-language Woo product mapping
(src/domain/language_product_mapping.py), its read-only audit
(scripts/audit_language_product_mapping.py), the staging-ready link
reader (src/services/polylang_link_reader.py) and their use by the
multilingual adapter.

No network, no Supabase, no WooCommerce.
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

import audit_language_product_mapping as audit
import create_woocommerce_draft as cwd
from src.domain import language_product_mapping as lpm
from src.domain import multilingual_adapter as ma
from src.services import polylang_link_reader as plr
from support.fake_polylang_store import FakePolylangStore
from support.fake_supabase import FakeSupabaseClient, FakeSupabaseRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODE = "TSYC-FAHASA-2026-002-CAN-0001"
IPID = "11111111-1111-4111-8111-111111111111"
SYNC_ID = "22222222-2222-4222-8222-222222222222"
OWNED = {"_tsyc_internal_product_id": IPID}


def meta(values):
    return [{"key": k, "value": v} for k, v in values.items()]


def sync_row(woo_id=4868, **overrides):
    created = {"id": woo_id, "sku": CODE, "status": "draft", "name": "Cổ tích 1",
               "description": "<p>Mô tả</p>", "short_description": "<p>Ngắn</p>",
               "date_modified_gmt": "2026-10-20T08:00:00", "meta_data": meta(OWNED)}
    row = {
        "sync_id": SYNC_ID, "internal_product_id": IPID, "woocommerce_product_id": woo_id,
        "woocommerce_status": "DRAFT_CREATED", "product_sku": CODE,
        "request_payload": {"sku": CODE, "status": "draft", "name": "Cổ tích 1", "meta_data": meta(OWNED)},
        "response_payload": {"woocommerce_product": created, "uploaded_media": []},
    }
    row.update(overrides)
    return row


PRODUCT = {"internal_product_id": IPID, "product_code": CODE}


def vi_row(**overrides):
    row, _ = lpm.derive_vi_mapping(sync_row(), PRODUCT)
    return lpm.LanguageProduct(**{**row.__dict__, **overrides})


def en_row(woo_id=4869, **overrides):
    values = dict(internal_product_id=IPID, product_code=CODE, language_code="en", woocommerce_product_id=woo_id,
                  origin=lpm.ORIGIN_TSYC_CREATED, mapping_status=lpm.RECORDED, request_id="req-en-1",
                  baseline_source=lpm.BASELINE_TSYC_WRITE, baseline_modified_gmt="2026-10-21T08:00:00",
                  baseline_text={"name": "Fairy tale 1", "description": "", "short_description": ""})
    values.update(overrides)
    return lpm.LanguageProduct(**values)


def remote(pid, *, sku=CODE, status="draft", lang_prefix="", owner=IPID, lang_meta=None,
           modified="2026-10-20T08:00:00"):
    values = {}
    if owner:
        values["_tsyc_internal_product_id"] = owner
    if lang_meta:
        values["_tsyc_language"] = lang_meta
    link = (f"https://tiemsachyeucon.com{lang_prefix}/product/x-{pid}/" if status == "publish"
            else f"https://tiemsachyeucon.com/?post_type=product&p={pid}")
    return {"id": pid, "sku": sku, "status": status, "permalink": link, "date_modified_gmt": modified,
            "meta_data": meta(values)}


def codes(findings, severity=None):
    return sorted(f.code for f in findings if severity is None or f.severity == severity)


# --- VI mapping from the existing sync row ---------------------------------

def test_vi_mapping_is_derived_from_tsycs_own_create_request_and_response():
    row, findings = lpm.derive_vi_mapping(sync_row(), PRODUCT)
    assert findings == []
    assert (row.language_code, row.woocommerce_product_id, row.origin, row.mapping_status, row.sync_id) == (
        "vi", 4868, lpm.ORIGIN_TSYC_CREATED, lpm.RECORDED, SYNC_ID)
    assert row.baseline_source == lpm.BASELINE_CREATE_RESPONSE
    assert row.baseline_modified_gmt == "2026-10-20T08:00:00"
    assert row.baseline_text == {"name": "Cổ tích 1", "description": "<p>Mô tả</p>",
                                 "short_description": "<p>Ngắn</p>"}


@pytest.mark.parametrize("request_payload", [
    {"sku": CODE, "status": "publish", "meta_data": meta(OWNED)},
    {"sku": "OTHER", "status": "draft", "meta_data": meta(OWNED)},
    {"sku": CODE, "status": "draft", "meta_data": meta({"_tsyc_internal_product_id": "someone-else"})},
    {"sku": CODE, "status": "draft"},
    {},
])
def test_no_ownership_record_without_tsycs_own_draft_create_request(request_payload):
    row, findings = lpm.derive_vi_mapping(sync_row(request_payload=request_payload), PRODUCT)
    assert row is None and codes(findings) == [lpm.NOT_TSYC_CREATE_REQUEST]


def test_remote_meta_alone_never_creates_a_record():
    """Even when the live product carries TSYC's meta, a sync row without
    TSYC's own request yields nothing."""
    row, _ = lpm.derive_vi_mapping(sync_row(request_payload={}), PRODUCT)
    assert row is None


def test_sku_mismatch_missing_product_and_missing_woo_id_give_no_mapping():
    assert codes(lpm.derive_vi_mapping(sync_row(product_sku="TSYC-X"), PRODUCT)[1]) == [lpm.SKU_MISMATCH]
    assert codes(lpm.derive_vi_mapping(sync_row(), None)[1]) == [lpm.NO_PRODUCT]
    row, findings = lpm.derive_vi_mapping(sync_row(woocommerce_product_id=None), PRODUCT)
    assert row is None and codes(findings) == [lpm.NO_WOO_ID]


def test_removed_product_is_mapped_as_terminal_removed():
    response = {"remote_removal": {"state": "REMOTE_TRASHED", "recreate": False}}
    row, findings = lpm.derive_vi_mapping(sync_row(response_payload=response), PRODUCT)
    assert row.mapping_status == lpm.REMOTE_REMOVED
    assert row.baseline_source is None and codes(findings) == [lpm.BASELINE_UNAVAILABLE]


def test_create_response_for_another_id_is_not_a_baseline():
    response = {"woocommerce_product": {"id": 9999, "date_modified_gmt": "x"}}
    row, findings = lpm.derive_vi_mapping(sync_row(response_payload=response), PRODUCT)
    assert row.baseline_source is None and lpm.BASELINE_UNAVAILABLE in codes(findings)


def test_recovery_required_is_reported():
    response = copy.deepcopy(sync_row()["response_payload"]) | {"recovery_required": True}
    assert lpm.RECOVERY_REQUIRED in codes(lpm.derive_vi_mapping(sync_row(response_payload=response), PRODUCT)[1])


# --- structural validation -------------------------------------------------

def test_valid_book_mapping_has_no_findings():
    assert lpm.validate_mappings([vi_row(), en_row()]) == []


def test_duplicate_language_and_duplicate_woo_id_are_errors():
    findings = lpm.validate_mappings([vi_row(), en_row(), en_row(woo_id=4870, request_id="req-en-2")])
    assert lpm.DUPLICATE_LANGUAGE in codes(findings, lpm.ERROR)
    findings = lpm.validate_mappings([vi_row(), en_row(woo_id=4868)])
    assert lpm.DUPLICATE_WOO_ID in codes(findings, lpm.ERROR)


def test_translation_needs_a_recorded_vi_row_of_the_same_book():
    assert lpm.MISSING_VI in codes(lpm.validate_mappings([en_row()]))
    assert lpm.MISSING_VI in codes(lpm.validate_mappings([vi_row(mapping_status=lpm.REMOTE_REMOVED), en_row()]))
    assert lpm.PRODUCT_CODE_MISMATCH in codes(lpm.validate_mappings([vi_row(), en_row(product_code="TSYC-X")]))


def test_tsyc_translation_needs_a_request_id_and_linked_copy_needs_link_evidence():
    assert lpm.REQUEST_ID_MISSING in codes(lpm.validate_mappings([vi_row(), en_row(request_id=None)]))
    linked = en_row(origin=lpm.ORIGIN_POLYLANG_LINKED, request_id=None)
    assert lpm.LINK_EVIDENCE_MISSING in codes(lpm.validate_mappings([vi_row(), linked]))
    proven = en_row(origin=lpm.ORIGIN_POLYLANG_LINKED, request_id=None, link_mechanism="tsyc/v1",
                    link_verified_at="2026-11-01T00:00:00+00:00")
    assert lpm.validate_mappings([vi_row(), proven]) == []


def test_invalid_values_and_sync_on_translation_are_errors():
    findings = lpm.validate_mappings([vi_row(language_code="fr"), en_row(sync_id=SYNC_ID),
                                      en_row(woo_id=None, language_code="de", request_id="r")])
    assert {lpm.INVALID_VALUE, lpm.SYNC_ON_TRANSLATION} <= set(codes(findings))


def test_stored_vi_rows_must_match_the_sync_rows():
    derived = [vi_row()]
    assert codes(lpm.compare_stored_vi([], derived)) == [lpm.STORED_MISSING]
    assert lpm.compare_stored_vi([vi_row()], derived) == []
    assert codes(lpm.compare_stored_vi([vi_row(woocommerce_product_id=5000)], derived)) == [lpm.STORED_DIFFERS]


def test_row_round_trip_matches_migration_columns():
    row = en_row()
    assert lpm.LanguageProduct.from_row(row.as_row(), CODE) == row
    migration = (PROJECT_ROOT / "migrations" / "011_create_woocommerce_language_products.sql").read_text(
        encoding="utf-8")
    for column in row.as_row():
        assert f"    {column} " in migration or f"    {column}\n" in migration, column
    for value in (*lpm.STATUSES, *lpm.ORIGINS, lpm.BASELINE_CREATE_RESPONSE, lpm.BASELINE_TSYC_WRITE):
        assert f"'{value}'" in migration


# --- read-only remote verification ------------------------------------------

def test_consistent_remote_draft_has_no_error():
    findings = lpm.verify_remote([vi_row()], {4868: remote(4868)})
    assert codes(findings, lpm.ERROR) == [] and codes(findings) == []


def test_copied_ownership_meta_on_an_unmapped_product_is_an_error():
    """The "+" workflow plus a metadata-copy hook: EN carries TSYC's meta
    and the SKU but TSYC never recorded creating it."""
    catalog = {4868: remote(4868), 4900: remote(4900)}
    findings = lpm.verify_remote([vi_row()], catalog)
    assert codes(findings, lpm.ERROR) == [lpm.COPIED_OWNERSHIP_META]


def test_unmapped_sku_copy_is_reported_never_adopted():
    rows = [vi_row()]
    catalog = {4868: remote(4868), 4014: remote(4014, status="publish", lang_prefix="/en", owner=None)}
    findings = lpm.verify_remote(rows, catalog)
    assert codes(findings) == [lpm.UNMAPPED_SKU_COPY]
    assert rows == [vi_row()]


def test_mapped_product_with_wrong_sku_language_or_meta_is_an_error():
    assert codes(lpm.verify_remote([vi_row()], {4868: remote(4868, sku="OTHER")}), lpm.ERROR) == [
        lpm.REMOTE_SKU_MISMATCH]
    assert codes(lpm.verify_remote([vi_row()], {4868: remote(4868, owner="another-book")}), lpm.ERROR) == [
        lpm.OWNER_META_OTHER_BOOK]
    published_en = remote(4868, status="publish", lang_prefix="/en")
    assert lpm.LANGUAGE_MISMATCH in codes(lpm.verify_remote([vi_row()], {4868: published_en}), lpm.ERROR)
    rows = [vi_row(), en_row()]
    catalog = {4868: remote(4868), 4869: remote(4869, lang_meta="de", modified="2026-10-21T08:00:00")}
    assert codes(lpm.verify_remote(rows, catalog), lpm.ERROR) == [lpm.LANGUAGE_MISMATCH]


def test_lost_meta_makes_ownership_unknown():
    assert codes(lpm.verify_remote([vi_row()], {4868: remote(4868, owner=None)})) == [lpm.OWNER_META_MISSING]


def test_linked_copy_carrying_tsyc_meta_is_copied_meta():
    linked = en_row(origin=lpm.ORIGIN_POLYLANG_LINKED, request_id=None, link_mechanism="m",
                    link_verified_at="2026-11-01", baseline_modified_gmt=None)
    catalog = {4868: remote(4868), 4869: remote(4869, status="publish", lang_prefix="/en")}
    assert codes(lpm.verify_remote([vi_row(), linked], catalog), lpm.ERROR) == [lpm.OWNER_META_ON_LINKED_COPY]


def test_owner_edits_and_publish_are_informational():
    catalog = {4868: remote(4868, status="publish", modified="2026-10-25T00:00:00")}
    assert codes(lpm.verify_remote([vi_row()], catalog)) == [lpm.CHANGED_SINCE_BASELINE, lpm.REMOTE_PUBLISHED]


def test_removed_mapping_live_again_is_a_warning_and_unread_mapping_is_not_assumed_deleted():
    removed = vi_row(mapping_status=lpm.REMOTE_REMOVED)
    assert codes(lpm.verify_remote([removed], {4868: remote(4868, status="publish")})) == [
        lpm.REMOTE_PRESENT_AFTER_REMOVAL]
    assert codes(lpm.verify_remote([removed], {})) == []
    assert codes(lpm.verify_remote([vi_row()], {})) == [lpm.REMOTE_NOT_READ]


# --- adapter inputs and end-to-end with the adapter ------------------------

def test_adapter_inputs_take_only_recorded_tsyc_rows():
    linked = en_row(origin=lpm.ORIGIN_POLYLANG_LINKED, request_id=None, link_mechanism="m", link_verified_at="t")
    inputs = lpm.adapter_inputs([vi_row(), linked, en_row(language_code="de", woo_id=4870,
                                                          mapping_status=lpm.PENDING_CREATE)])
    assert set(inputs.tsyc_records) == {"vi"}
    assert inputs.uncertain_languages == ("de",)
    inputs = lpm.adapter_inputs([vi_row(), en_row(mapping_status=lpm.REMOTE_REMOVED)])
    assert inputs.blocked_languages == ("en",) and set(inputs.tsyc_records) == {"vi"}


def render(content):
    return cwd.build_description(content), cwd.build_short_description(content)


def approved(lang, name):
    return {"content_language": lang, "content_status": "APPROVED", "review_required": False,
            "product_name": name, "short_description": f"{name} short", "long_description": f"{name} long."}


CONTENTS = {"en": approved("en", "Fairy tale 1"), "de": approved("de", "Märchen 1")}


def adapter_run(store, rows, link_map):
    from src.domain import polylang_link_graph as plg
    inputs = lpm.adapter_inputs(rows)
    groups, _ = plg.build_groups(list(store.products.values()), store.all_page_links())
    resolution = ma.resolve_group(product_code=CODE, source_id=4868, products_by_id=store.products,
                                  sku_matches=store.sku_matches(CODE), storefront_groups=groups,
                                  tsyc_records=inputs.tsyc_records, link_map=link_map)
    actions = ma.plan_language_actions(
        resolution, internal_product_id=IPID, products_by_id=store.products, approved_contents=CONTENTS,
        render=render, tsyc_records=inputs.tsyc_records, create_authorized=True,
        link_mechanism=_Mechanism(),
        uncertain_languages=inputs.uncertain_languages, blocked_languages=inputs.blocked_languages)
    return resolution, actions


class _Mechanism:
    name = "staging-proven endpoint"
    verified_on_staging = True


def proven_map(store, pid=4868):
    return ma.PolylangLinkMap(pid, store.polylang_translations(pid), "staging-proven endpoint", True)


def test_mapping_feeds_the_adapter_and_a_removed_language_is_never_recreated():
    store = FakePolylangStore()
    store.add(4868, "vi", "co-tich-1", status="draft", sku=CODE, meta=OWNED)
    resolution, actions = adapter_run(store, [vi_row()], proven_map(store))
    assert resolution.status == ma.SOURCE_ONLY
    assert actions == {"en": ma.CREATE_AND_LINK, "de": ma.CREATE_AND_LINK}
    removed_en = en_row(mapping_status=lpm.REMOTE_REMOVED)
    _, actions = adapter_run(store, [vi_row(), removed_en], proven_map(store))
    assert actions["en"] == ma.REFUSE and actions["de"] == ma.CREATE_AND_LINK


def test_pending_create_is_reconciled_first_and_removed_vi_refuses_the_book():
    store = FakePolylangStore()
    store.add(4868, "vi", "co-tich-1", status="draft", sku=CODE, meta=OWNED)
    pending = en_row(woo_id=None, mapping_status=lpm.PENDING_CREATE)
    _, actions = adapter_run(store, [vi_row(), pending], proven_map(store))
    assert actions["en"] == ma.RECONCILE_FIRST
    resolution, actions = adapter_run(store, [vi_row(mapping_status=lpm.REMOTE_REMOVED)], proven_map(store))
    assert resolution.status == ma.SOURCE_INVALID and set(actions.values()) == {ma.REFUSE}


def test_copied_meta_on_a_plus_workflow_copy_blocks_the_book():
    store = FakePolylangStore()
    store.add(4868, "vi", "co-tich-1", status="draft", sku=CODE, meta=OWNED)
    store.add(4900, "en", "fairy-tale-1", status="draft", sku=CODE, meta=dict(OWNED))  # hook-copied
    store.link(4868, 4900)
    resolution, actions = adapter_run(store, [vi_row()], proven_map(store))
    assert resolution.status == ma.CONFLICT and set(actions.values()) == {ma.REFUSE}


# --- staging-ready link reader ---------------------------------------------

class _Response:
    def __init__(self, status, body=None, raises=False):
        self.status_code, self._body, self._raises = status, body, raises

    def json(self):
        if self._raises:
            raise ValueError("not json")
        return self._body


def reader_with(response, calls=None):
    def http_get(url, **kwargs):
        if calls is not None:
            calls.append((url, kwargs))
        if isinstance(response, Exception):
            raise response
        return response
    return plr.EndpointLinkReader(store_url="https://staging.example.test", auth=None, http_get=http_get)


GOOD = {"id": 4868, "lang": "vi", "sku": CODE, "status": "draft", "translations": {"vi": 4868, "en": "4869"}}


def test_reader_parses_a_valid_answer_and_issues_one_get():
    calls = []
    link_map = reader_with(_Response(200, GOOD), calls).read_translations(4868)
    assert link_map.translations == {"vi": 4868, "en": 4869}
    assert link_map.source_id == 4868 and link_map.verified_on_staging is False
    assert calls[0][0] == "https://staging.example.test/wp-json/tsyc/v1/translations/4868"


@pytest.mark.parametrize("response", [
    _Response(404), _Response(500), _Response(200, raises=True), _Response(200, ["x"]),
    ConnectionError("reset"),
])
def test_reader_failure_is_never_no_translations(response):
    with pytest.raises(plr.LinkReadError):
        reader_with(response).read_translations(4868)


@pytest.mark.parametrize("body", [
    {**GOOD, "id": 1},
    {**GOOD, "lang": "fr"},
    {**GOOD, "lang": "en"},
    {**GOOD, "translations": None},
    {**GOOD, "translations": {"vi": 4868, "fr": 7}},
    {**GOOD, "translations": {"vi": 4868, "en": 0}},
    {**GOOD, "translations": {"vi": 4868, "en": True}},
    {**GOOD, "translations": {"en": 4869}},
    {**GOOD, "translations": {"vi": 4868, "VI": 4868}},
    {**GOOD, "translations": {"vi": 4868, "en": 4868}},
])
def test_reader_rejects_malformed_or_inconsistent_answers(body):
    with pytest.raises(plr.LinkReadError):
        plr.parse_translation_response(body, 4868, mechanism="m", verified_on_staging=True)


def test_unverified_reader_map_is_ignored_by_the_adapter():
    store = FakePolylangStore()
    store.add(4868, "vi", "co-tich-1", status="draft", sku=CODE, meta=OWNED)
    link_map = reader_with(_Response(200, {**GOOD, "translations": {"vi": 4868}})).read_translations(4868)
    resolution, actions = adapter_run(store, [vi_row()], link_map)
    assert resolution.status == ma.UNVERIFIABLE and set(actions.values()) == {ma.REFUSE}


def test_staging_guard_refuses_production_and_unsafe_urls():
    plr.require_staging_target("https://staging.tiemsachyeucon.com", "https://tiemsachyeucon.com")
    for target in ("https://tiemsachyeucon.com", "https://www.tiemsachyeucon.com/", "http://staging.example.test",
                   "staging.example.test"):
        with pytest.raises(plr.StagingTargetError):
            plr.require_staging_target(target, "https://tiemsachyeucon.com")
    with pytest.raises(plr.StagingTargetError):
        plr.require_staging_target("https://staging.example.test", "")
    with pytest.raises(plr.StagingTargetError):
        plr.EndpointLinkReader(store_url="http://tiemsachyeucon.com", auth=None)


def test_link_reader_and_audit_never_write():
    for name in ("src/services/polylang_link_reader.py", "scripts/audit_language_product_mapping.py",
                 "src/domain/language_product_mapping.py"):
        source = (PROJECT_ROOT / name).read_text(encoding="utf-8").replace("sys.path.insert(", "")
        for verb in ("requests.post", "requests.put", "requests.delete", "requests.patch",
                     ".insert(", ".update(", ".upsert(", ".delete(", "execute_sql"):
            assert verb not in source, (name, verb)


# --- audit script against a fake repository --------------------------------

class _MissingTableClient(FakeSupabaseClient):
    def table(self, name):
        if name == lpm.TABLE:
            raise RuntimeError(f"{{'code': 'PGRST205', 'message': \"Could not find the table 'public.{name}'\"}}")
        return super().table(name)


def fake_repository(missing_table=False, stored=None):
    tables = {"internal_products": [dict(PRODUCT)], "woocommerce_product_syncs": [sync_row()]}
    if stored is not None:
        tables[lpm.TABLE] = stored
    repository = FakeSupabaseRepository(tables)
    if missing_table:
        repository.client = _MissingTableClient(tables)
    return repository


class FakeCatalogReader:
    def __init__(self, products, per_page=1):
        self.products, self.per_page, self.calls = products, per_page, []

    def list_page(self, params):
        self.calls.append(dict(params))
        assert params["status"] == "any" and "meta_data" in params["_fields"]
        page = params["page"]
        chunk = self.products[(page - 1) * self.per_page: page * self.per_page]
        pages = -(-len(self.products) // self.per_page)
        return chunk, pages


def test_audit_reports_missing_table_and_plans_the_vi_backfill_without_writing():
    repository = fake_repository(missing_table=True)
    before = copy.deepcopy(repository.client.tables)
    report = audit.run(repository, reader=None, plan_backfill=True)
    assert report["table_state"] == audit.TABLE_MISSING
    assert report["derived_vi_mappings"] == 1 and report["derived_with_baseline"] == 1
    assert [r["woocommerce_product_id"] for r in report["backfill_plan"]] == [4868]
    assert report["backfill_plan"][0]["origin"] == lpm.ORIGIN_TSYC_CREATED
    assert repository.client.tables == before


def test_audit_compares_stored_rows_and_reads_the_whole_catalog():
    stored = [{**vi_row().as_row(), "language_product_id": "x"}]
    repository = fake_repository(stored=stored)
    reader = FakeCatalogReader([remote(4868), remote(4900), remote(4014, owner=None, status="publish",
                                                                   lang_prefix="/en")])
    report = audit.run(repository, reader=reader, plan_backfill=True)
    assert report["table_state"] == audit.TABLE_PRESENT and report["backfill_plan"] == []
    assert report["remote_catalog_products"] == 3 and len(reader.calls) == 3
    assert report["finding_counts"] == {"ERROR COPIED_OWNERSHIP_META": 1, "WARNING UNMAPPED_SKU_COPY": 1}


def test_audit_keeps_checking_books_the_backfill_left_unstored():
    """A book isolated by the backfill (no stored row) must not drop out of
    the remote check once other rows are stored."""
    other = "33333333-3333-4333-8333-333333333333"
    repository = fake_repository(stored=[{**vi_row().as_row(), "language_product_id": "x"}])
    repository.client.tables["internal_products"].append({"internal_product_id": other, "product_code": "TSYC-B"})
    isolated = sync_row(woo_id=4657, sync_id="s2", internal_product_id=other, product_sku="TSYC-B",
                        request_payload={"sku": "TSYC-B", "status": "draft",
                                         "meta_data": meta({"_tsyc_internal_product_id": other})})
    repository.client.tables["woocommerce_product_syncs"].append(isolated)
    reader = FakeCatalogReader([remote(4868), remote(4657, sku="TSYC-B", owner=other),
                                remote(4858, sku="TSYC-B", owner=other)])
    report = audit.run(repository, reader=reader, plan_backfill=False)
    assert report["finding_counts"].get("ERROR COPIED_OWNERSHIP_META") == 1
    assert report["finding_counts"].get("WARNING STORED_VI_MISSING") == 1


def test_audit_unexpected_read_error_is_raised_not_hidden():
    class Broken(FakeSupabaseClient):
        def table(self, name):
            if name == lpm.TABLE:
                raise RuntimeError("connection reset")
            return super().table(name)
    repository = fake_repository()
    repository.client = Broken(repository.client.tables)
    with pytest.raises(RuntimeError):
        audit.run(repository, reader=None, plan_backfill=False)
