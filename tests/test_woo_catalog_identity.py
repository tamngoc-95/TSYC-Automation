"""Regression tests for the mandatory pre-create catalog identity check,
the create reservation and the historical create recovery (2026-10-22).

Production context: ~1,896 of 1,986 live products have no SKU, so an
exact-SKU search never proves a book is new. FB-HIST-2026-IMG-004-CAN-0031
was one create away from duplicating the owner's #1422.

No network, no Supabase, no WooCommerce.
"""
from __future__ import annotations

import copy

import pytest

import catalog_identity_loader as loader
import create_woocommerce_draft as cwd
import pipeline_state
import recover_historical_woo_create as rec
from src.domain import woo_catalog_identity as wci
from src.domain import woo_remote_lifecycle
from src.domain.rules import catalog_identity_index as idx
from support.fake_supabase import FakeSupabaseRepository

HOST = "https://tiemsachyeucon.com"
SKU = "TSYC-FB-HIST-2026-IMG-004-CAN-0031"
BLOCKCHAIN = idx.Listing(title="Blockchain - Khởi Nguồn Cho Một Nền Kinh Tế Mới", author="Melanie Swan",
                         publisher="NXB Đại Học Kinh Tế Quốc Dân")


def woo(pid, name, status="publish", sku="", lang="vi"):
    prefix = "" if lang == "vi" else f"/{lang}"
    return {"id": pid, "name": name, "sku": sku, "status": status,
            "permalink": f"{HOST}{prefix}/product/p-{pid}/" if status != "draft" else f"{HOST}/?post_type=product&p={pid}",
            "meta_data": []}


def snap(*products, complete=True, removed=(), local=(), groups=None, barcodes=None):
    return wci.CatalogSnapshot(woo_products=list(products), complete=complete,
                               incomplete_reason=None if complete else "HTTP 500",
                               removed_entries=list(removed), local_entries=list(local),
                               translation_group_of=groups or {}, barcodes=barcodes or {})


def check(listing, snapshot, barcode=None, sku=SKU):
    return wci.check_before_create(listing, product_code=sku, snapshot=snapshot, barcode=barcode)


# --- classification -----------------------------------------------------

def test_existing_owner_product_without_sku_like_1422_blocks_create():
    result = check(BLOCKCHAIN, snap(woo(1422, "Blockchain – Khởi Nguồn Cho Một Nền Kinh Tế Mới -Melanie Swan")))
    assert result.classification == wci.EXISTING_PRODUCT
    assert not result.create_allowed
    assert "product name carries the author" in result.evidence[0]["reason"]


def test_title_suffix_that_is_not_the_author_is_only_a_possible_duplicate():
    result = check(BLOCKCHAIN, snap(woo(1, "Blockchain – Khởi Nguồn Cho Một Nền Kinh Tế Mới - Tập Hai Phần Mở Rộng")))
    assert result.classification == wci.POSSIBLE_DUPLICATE


def test_new_book_is_the_only_creatable_result():
    result = check(BLOCKCHAIN, snap(woo(5, "Hoàng Tử Bé"), woo(6, "Dế Mèn Phiêu Lưu Ký")))
    assert result.classification == wci.NEW_CONFIRMED and result.create_allowed


def test_same_book_already_carrying_another_sku_is_existing():
    result = check(BLOCKCHAIN, snap(woo(9, "Blockchain - Khởi Nguồn Cho Một Nền Kinh Tế Mới", sku="TSYC-OTHER-CAN-1")))
    assert result.classification == wci.EXISTING_PRODUCT


def test_own_sku_anywhere_in_the_catalog_blocks_and_trash_means_removed():
    assert check(BLOCKCHAIN, snap(woo(7, "x", sku=SKU))).classification == wci.EXISTING_PRODUCT
    assert check(BLOCKCHAIN, snap(woo(7, "x", sku=SKU, status="trash"))).classification == wci.REMOTE_REMOVED


def test_trashed_and_permanently_removed_products_are_never_recreated():
    trashed = check(BLOCKCHAIN, snap(woo(8, "Blockchain - Khởi Nguồn Cho Một Nền Kinh Tế Mới", status="trash")))
    assert trashed.classification == wci.REMOTE_REMOVED
    legacy = wci.legacy_removed_entries(
        [{"internal_product_id": "ip1", "product_sku": "TSYC-A", "woocommerce_product_id": None,
          "response_payload": {"remote_loss_confirmed": {"previous_woocommerce_product_id": 3752}}}],
        {"ip1": "Blockchain - Khởi Nguồn Cho Một Nền Kinh Tế Mới"})
    assert legacy[0].remote_removed and "3752" in legacy[0].ref
    assert check(BLOCKCHAIN, snap(removed=legacy)).classification == wci.REMOTE_REMOVED


def test_same_title_different_isbn_is_a_distinct_edition():
    listing = idx.Listing(title="Hoàng Tử Bé", isbn="9786042000000", author="Antoine de Saint-Exupéry")
    local = [idx.IndexEntry(origin=idx.ORIGIN_INTERNAL_PRODUCT, ref="TSYC-X", title="Hoàng Tử Bé",
                            isbn="9786041111111")]
    assert check(listing, snap(local=local)).classification == wci.DISTINCT_EDITION


def test_same_isbn_on_a_different_title_is_an_identity_conflict():
    listing = idx.Listing(title="Hoàng Tử Bé", isbn="9786042000000")
    local = [idx.IndexEntry(origin=idx.ORIGIN_INTERNAL_PRODUCT, ref="TSYC-X", title="Dế Mèn Phiêu Lưu Ký",
                            isbn="9786042000000")]
    assert check(listing, snap(local=local)).classification == wci.IDENTITY_CONFLICT


def test_same_series_different_volume_is_never_merged_as_existing():
    listing = idx.Listing(title="Nhật Ký Chú Bé Nhút Nhát - Tập 21")
    result = check(listing, snap(woo(1, "Nhật Ký Chú Bé Nhút Nhát - Tập 20")))
    assert result.classification == wci.POSSIBLE_DUPLICATE
    assert not result.create_allowed


def test_single_book_versus_combo_is_a_sellable_unit_question():
    listing = idx.Listing(title="Combo Vô Cùng Tàn Nhẫn, Vô Cùng Yêu Thương (Bộ 3 Cuốn)")
    result = check(listing, snap(woo(1, "Vô Cùng Tàn Nhẫn, Vô Cùng Yêu Thương")))
    assert result.classification == wci.POSSIBLE_DUPLICATE


def test_book_existing_only_as_a_translated_copy_is_a_linked_translation():
    listing = idx.Listing(title="Từ Bé Trai, Tôi Trở Thành Người Lớn")
    result = check(listing, snap(woo(4014, "Từ Bé Trai, Tôi Trở Thành Người Lớn", lang="en")))
    assert result.classification == wci.LINKED_TRANSLATION


def test_linked_polylang_copies_count_as_one_logical_product():
    products = [woo(1601, "Giác Ngộ - Osho"), woo(1606, "Giác Ngộ - Osho", lang="en"),
                woo(1607, "Giác Ngộ - Osho", lang="de")]
    listing = idx.Listing(title="Giác Ngộ", author="Osho")
    result = check(listing, snap(*products, groups={1601: 1, 1606: 1, 1607: 1}))
    assert result.classification == wci.EXISTING_PRODUCT
    assert result.logical_products == ((1601, 1606, 1607),)


def test_publisher_barcode_is_kept_separate_from_isbn():
    listing = idx.Listing(title="Bé Tô Màu Gấu Baby", isbn=None)
    result = check(listing, snap(barcodes={"FB-HIST-X": "8931805360117"}), barcode="8931805360117")
    assert result.classification == wci.POSSIBLE_DUPLICATE and "barcode, not ISBN" in result.reason
    assert loader._barcode("8931805360117") == "8931805360117"
    assert loader._barcode("9786042000000") is None


def test_incomplete_catalog_fails_closed():
    result = check(BLOCKCHAIN, snap(complete=False))
    assert result.classification == wci.CATALOG_UNAVAILABLE and not result.create_allowed


# --- loader: partial catalog / API failure --------------------------------

class Response:
    def __init__(self, status, body, total=None):
        self.status_code = status
        self._body = body
        self.headers = {} if total is None else {"X-WP-Total": str(total)}

    def json(self):
        return self._body


def pages(any_rows, trash_rows, *, any_total=None, trash_total=None, fail=None):
    def http_get(url, *, params, **_):
        status = params["status"]
        if fail == (status, params["page"]):
            return Response(500, {})
        rows = any_rows if status == "any" else trash_rows
        total = (any_total if status == "any" else trash_total)
        start = (params["page"] - 1) * loader.PAGE_SIZE
        return Response(200, rows[start:start + loader.PAGE_SIZE], len(rows) if total is None else total)
    return http_get


def load(http_get):
    return loader.load_woo_catalog(products_url="u", auth=None, headers={}, timeout=5, http_get=http_get)


def test_loader_reads_every_status_including_trash():
    rows = [woo(i, f"b{i}") for i in range(150)]
    products, complete, reason = load(pages(rows, [woo(999, "t", status="trash")]))
    assert complete and reason is None and len(products) == 151


@pytest.mark.parametrize("kwargs, fragment", [
    ({"fail": ("any", 2)}, "HTTP 500"),
    ({"fail": ("trash", 1)}, "status=trash"),
    ({"any_total": 200}, "read 150 of 200"),
])
def test_loader_marks_partial_reads_incomplete(kwargs, fragment):
    rows = [woo(i, f"b{i}") for i in range(150)]
    _, complete, reason = load(pages(rows, [], **kwargs))
    assert not complete and fragment in reason


def test_loader_without_total_header_is_incomplete():
    def http_get(url, *, params, **_):
        response = Response(200, [])
        return response
    _, complete, reason = load(http_get)
    assert not complete and "X-WP-Total" in reason


# --- create guard pieces ---------------------------------------------------

def ready_product(**metadata):
    return {"internal_product_id": "ip-31", "candidate_id": "c-31", "product_code": SKU,
            "woocommerce_status": "READY_FOR_DRAFT", "product_metadata": dict(metadata)}


def test_blocking_result_is_recorded_and_unavailable_is_not():
    repo = FakeSupabaseRepository({"internal_products": [ready_product(other="kept")]})
    product = copy.deepcopy(repo.client.tables["internal_products"][0])
    cwd.record_catalog_identity_block(repo, product, wci.CreateCheck(wci.CATALOG_UNAVAILABLE, "x"))
    assert wci.MARKER_KEY not in repo.client.tables["internal_products"][0]["product_metadata"]
    cwd.record_catalog_identity_block(repo, product, wci.CreateCheck(wci.EXISTING_PRODUCT, "same book"))
    stored = repo.client.tables["internal_products"][0]["product_metadata"]
    assert stored["other"] == "kept" and stored[wci.MARKER_KEY]["terminal"] is True


def sync_row(**overrides):
    row = {"sync_id": "s1", "internal_product_id": "ip-31", "product_sku": SKU, "woocommerce_status": "PENDING",
           "woocommerce_product_id": None, "sync_attempt_count": 0, "updated_at": "2026-10-22T10:00:00",
           "response_payload": {"uploaded_media": []}}
    row.update(overrides)
    return row


def test_create_reservation_is_compare_and_set():
    repo = FakeSupabaseRepository({"woocommerce_product_syncs": [sync_row()]})
    stale = copy.deepcopy(repo.client.tables["woocommerce_product_syncs"][0])
    first = cwd.mark_sync_in_progress(repo, copy.deepcopy(stale))
    assert first["woocommerce_status"] == "IN_PROGRESS"
    assert first["response_payload"]["create_reservation"]["attempt"] == 1
    assert first["response_payload"]["uploaded_media"] == []
    # A second, concurrent creator still holding the old read loses.
    with pytest.raises(RuntimeError, match="Could not reserve"):
        cwd.mark_sync_in_progress(repo, stale)


def test_reservation_refuses_a_row_that_already_has_a_woo_id():
    repo = FakeSupabaseRepository({"woocommerce_product_syncs": [sync_row(woocommerce_product_id=4655)]})
    with pytest.raises(RuntimeError, match="Could not reserve"):
        cwd.mark_sync_in_progress(repo, copy.deepcopy(repo.client.tables["woocommerce_product_syncs"][0]))


def test_fresh_sku_recheck_before_post_stops_on_any_or_trash(monkeypatch):
    lookup = dict(store_url=HOST, api_version="wc/v3", consumer_key="k", consumer_secret="s", timeout_seconds=5, sku=SKU)
    monkeypatch.setattr(cwd, "find_product_by_sku", lambda **_: None)
    monkeypatch.setattr(cwd, "find_trashed_products_by_sku", lambda **_: [])
    cwd.assert_sku_still_absent(**lookup)
    monkeypatch.setattr(cwd, "find_trashed_products_by_sku", lambda **_: [{"id": 1, "sku": SKU}])
    with pytest.raises(RuntimeError, match="appeared in WooCommerce"):
        cwd.assert_sku_still_absent(**lookup)


def test_create_main_checks_the_catalog_before_reserving(monkeypatch):
    """Order inside main(): SKU check, then catalog check, and only then
    the sync reservation -- a blocked product creates no sync row."""
    source = (cwd.PROJECT_ROOT / "scripts" / "create_woocommerce_draft.py").read_text(encoding="utf-8")
    main_body = source[source.index("def main() -> None:"):]
    assert main_body.index("find_product_by_sku(") < main_body.index("run_catalog_identity_check(") \
        < main_body.index("create_sync_record(") < main_body.index("mark_sync_in_progress(") \
        < main_body.index("assert_sku_still_absent(") < main_body.index("create_woocommerce_product(")


# --- pipeline state ----------------------------------------------------------

def bundle(product, sync=None):
    return {"candidate": {"candidate_code": "FB-HIST-2026-IMG-004-CAN-0031", "candidate_id": "c-31"},
            "internal_product": product, "sync": sync, "references": [], "discovery_sources": [],
            "contents": [], "images": []}


def marker(classification):
    return wci.CreateCheck(classification, "r").marker(product_code=SKU, checked_at="t", checked_by="test")


def test_terminal_marker_derives_existing_woo_product():
    state = pipeline_state.derive_candidate_state(bundle(ready_product(**{wci.MARKER_KEY: marker(wci.EXISTING_PRODUCT)})))
    assert state.derived_state == "EXISTING_WOO_PRODUCT" and state.terminal
    assert "EXISTING_WOO_PRODUCT" in pipeline_state.ACCEPTED_WARNING_CODES


def test_non_terminal_marker_is_a_human_gate():
    state = pipeline_state.derive_candidate_state(bundle(ready_product(**{wci.MARKER_KEY: marker(wci.POSSIBLE_DUPLICATE)})))
    assert state.derived_state == "CATALOG_DUPLICATE_REVIEW" and state.human_gate


def uncertain_sync(**payload):
    return sync_row(woocommerce_status="FAILED", error_code="RemoteProtocolError",
                    response_payload={"error": {"exception_type": "RemoteProtocolError"},
                                      "uploaded_media": [{"wordpress_media_id": 4653}],
                                      "media_upload_completed": True, **payload})


def test_lost_create_response_stays_in_recovery_without_resolution():
    product = ready_product()
    assert pipeline_state.derive_sync_recovery_state(uncertain_sync(), product) == (
        "RECOVERY_REVIEW_REQUIRED", "CREATE_RESULT_UNCERTAIN")


def test_resolution_needs_both_the_record_and_the_terminal_marker():
    resolution = {wci.RESOLUTION_KEY: {"state": wci.RESOLVED_NOT_RECREATED_EXISTING, "sku": SKU}}
    sync = uncertain_sync(**resolution)
    assert pipeline_state.derive_sync_recovery_state(sync, ready_product()) is not None
    review = ready_product(**{wci.MARKER_KEY: marker(wci.POSSIBLE_DUPLICATE)})
    assert pipeline_state.derive_sync_recovery_state(sync, review) is not None
    resolved = ready_product(**{wci.MARKER_KEY: marker(wci.EXISTING_PRODUCT)})
    assert pipeline_state.derive_sync_recovery_state(sync, resolved) is None
    wrong_sku = uncertain_sync(**{wci.RESOLUTION_KEY: {"state": wci.RESOLVED_NOT_RECREATED_EXISTING, "sku": "X"}})
    assert pipeline_state.derive_sync_recovery_state(wrong_sku, resolved) is not None
    state = pipeline_state.derive_candidate_state(bundle(resolved, sync))
    assert state.derived_state == "EXISTING_WOO_PRODUCT"


# --- historical recovery with legacy record formats ----------------------------

def legacy_sync():
    return sync_row(sync_id="s17", product_sku="TSYC-FB-HIST-2026-002-CAN-0017", woocommerce_status="FAILED",
                    error_code="woocommerce_rest_product_not_created",
                    response_payload={"error": {"code": "woocommerce_rest_product_not_created"},
                                      "remote_loss_confirmed": {"previous_woocommerce_product_id": 3752}})


def legacy_product():
    return {"internal_product_id": "ip-17", "candidate_id": "c-17", "product_code": "TSYC-FB-HIST-2026-002-CAN-0017",
            "woocommerce_status": "READY_FOR_DRAFT", "product_metadata": {}}


def no_catalog():
    raise AssertionError("catalog check must not run for this shape")


def test_legacy_remote_loss_is_recorded_as_owner_removal():
    plan = rec.plan_recovery(product=legacy_product(), sync=legacy_sync(), sku_any=[], sku_trash=[],
                             get_previous=lambda _id: (404, {"code": "woocommerce_rest_product_invalid_id"}),
                             run_catalog_check=no_catalog)
    assert plan.shape == rec.LEGACY_REMOTE_LOSS and plan.action == rec.ACTION_RECORD_OWNER_REMOVAL
    repo = FakeSupabaseRepository({"woocommerce_product_syncs": [legacy_sync()],
                                   "internal_products": [legacy_product()]})
    rec.apply_owner_removal(repo, legacy_product(), legacy_sync(), plan)
    sync = repo.client.tables["woocommerce_product_syncs"][0]
    assert sync["woocommerce_product_id"] == 3752
    assert sync["response_payload"]["remote_loss_confirmed"]["previous_woocommerce_product_id"] == 3752
    assert woo_remote_lifecycle.confirmed_remote_removal(sync) == woo_remote_lifecycle.REMOTE_DELETED
    product = repo.client.tables["internal_products"][0]
    state = pipeline_state.derive_candidate_state(bundle(product, sync))
    assert state.derived_state == "REMOTE_REMOVED" and state.terminal


@pytest.mark.parametrize("sku_any, sku_trash, get_status, why", [
    (None, [], 404, "uncertain"),
    ([], [], 500, "uncertain"),
    ([], [{"id": 3752, "sku": "TSYC-FB-HIST-2026-002-CAN-0017"}], 404, "trash"),
    ([{"id": 4000, "sku": "TSYC-FB-HIST-2026-002-CAN-0017"}], [], 404, "live product"),
])
def test_legacy_removal_needs_fresh_conclusive_evidence(sku_any, sku_trash, get_status, why):
    plan = rec.plan_recovery(product=legacy_product(), sync=legacy_sync(), sku_any=sku_any, sku_trash=sku_trash,
                             get_previous=lambda _id: (get_status, {}), run_catalog_check=no_catalog)
    assert plan.action == rec.ACTION_NONE


def test_uncertain_create_resolves_only_for_a_proven_existing_product():
    existing = wci.CreateCheck(wci.EXISTING_PRODUCT, "Woo 1422", logical_products=((1422,),))
    plan = rec.plan_recovery(product=ready_product(), sync=uncertain_sync(), sku_any=[], sku_trash=[],
                             get_previous=lambda _id: (None, None), run_catalog_check=lambda: existing)
    assert plan.shape == rec.UNCERTAIN_CREATE and plan.action == rec.ACTION_RESOLVE_EXISTING_PRODUCT
    repo = FakeSupabaseRepository({"woocommerce_product_syncs": [uncertain_sync()],
                                   "internal_products": [ready_product()]})
    rec.apply_catalog_result(repo, ready_product(), uncertain_sync(), plan)
    sync = repo.client.tables["woocommerce_product_syncs"][0]
    assert sync["woocommerce_status"] == "FAILED" and sync["woocommerce_product_id"] is None
    assert sync["error_code"] == "RemoteProtocolError"
    assert sync["response_payload"]["error"]["exception_type"] == "RemoteProtocolError"
    resolution = sync["response_payload"][wci.RESOLUTION_KEY]
    assert "cannot be proven" in resolution["residual_uncertainty"]
    product = repo.client.tables["internal_products"][0]
    assert pipeline_state.derive_candidate_state(bundle(product, sync)).derived_state == "EXISTING_WOO_PRODUCT"


@pytest.mark.parametrize("classification, action", [
    (wci.NEW_CONFIRMED, rec.ACTION_NONE),
    (wci.CATALOG_UNAVAILABLE, rec.ACTION_NONE),
    (wci.POSSIBLE_DUPLICATE, rec.ACTION_RECORD_CATALOG_REVIEW),
])
def test_uncertain_create_is_never_cleared_toward_a_new_create(classification, action):
    plan = rec.plan_recovery(product=ready_product(), sync=uncertain_sync(), sku_any=[], sku_trash=[],
                             get_previous=lambda _id: (None, None),
                             run_catalog_check=lambda: wci.CreateCheck(classification, "r"))
    assert plan.action == action
    if action == rec.ACTION_RECORD_CATALOG_REVIEW:
        repo = FakeSupabaseRepository({"woocommerce_product_syncs": [uncertain_sync()],
                                       "internal_products": [ready_product()]})
        rec.apply_catalog_result(repo, ready_product(), uncertain_sync(), plan)
        sync = repo.client.tables["woocommerce_product_syncs"][0]
        assert wci.RESOLUTION_KEY not in sync["response_payload"]
        product = repo.client.tables["internal_products"][0]
        assert pipeline_state.derive_sync_recovery_state(sync, product) is not None


def test_uncertain_create_with_sku_now_present_is_left_to_reconciliation():
    plan = rec.plan_recovery(product=ready_product(), sync=uncertain_sync(), sku_any=[{"id": 1, "sku": SKU}],
                             sku_trash=[], get_previous=lambda _id: (None, None), run_catalog_check=no_catalog)
    assert plan.action == rec.ACTION_NONE and "reconcile" in plan.reason


def test_repeat_execution_after_interruption_is_idempotent():
    """The first run wrote the owner removal; a rerun sees a sync with a Woo
    ID, which is no longer this tool's record shape, and does nothing."""
    plan = rec.plan_recovery(product=legacy_product(), sync=legacy_sync(), sku_any=[], sku_trash=[],
                             get_previous=lambda _id: (404, {}), run_catalog_check=no_catalog)
    repo = FakeSupabaseRepository({"woocommerce_product_syncs": [legacy_sync()],
                                   "internal_products": [legacy_product()]})
    rec.apply_owner_removal(repo, legacy_product(), legacy_sync(), plan)
    rerun = rec.plan_recovery(product=repo.client.tables["internal_products"][0],
                              sync=repo.client.tables["woocommerce_product_syncs"][0], sku_any=[], sku_trash=[],
                              get_previous=lambda _id: (404, {}), run_catalog_check=no_catalog)
    assert rerun.action == rec.ACTION_NONE
    # And a stale writer holding the pre-recovery row cannot write again.
    with pytest.raises(RuntimeError, match="changed since it was read"):
        rec.apply_owner_removal(repo, legacy_product(), legacy_sync(), plan)


def test_recovery_tool_only_issues_get_requests():
    source = (cwd.PROJECT_ROOT / "scripts" / "recover_historical_woo_create.py").read_text(encoding="utf-8")
    for verb in ("requests.post", "requests.put", "requests.delete", "requests.patch", "create_woocommerce_product("):
        assert verb not in source
