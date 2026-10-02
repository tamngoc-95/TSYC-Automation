"""
Regression tests: a CONFIRMED remote WooCommerce deletion/trash is an
intentional, terminal state -- never recreated, never a generic
REMOTE_WOO_ID_WITH_LOCAL_STATUS_MISMATCH audit ERROR, never a recovery
item (2026-10-02: 31 deleted + 16 trashed shop-owner removals).

Offline: pure functions plus FakeSupabaseRepository; every remote lookup
is monkeypatched.
"""
from __future__ import annotations

from typing import Any

import pytest

import audit_pipeline_state as audit
import sync_woocommerce_product_status as sync_mod
from pipeline_state import derive_candidate_state, derive_sync_recovery_state
from src.domain import woo_remote_lifecycle as wrl
from src.domain.woocommerce_status import WooCommerceStatus, WooCommerceSyncStatus
from support.fake_supabase import FakeSupabaseRepository

WC_ID = 4321
SKU = "TSYC-FB-HIST-2026-001-CAN-0007"
INTERNAL_PRODUCT_ID = "product-1"
SYNC_ID = "sync-1"


# --- evaluate_remote_removal ---------------------------------------------


def test_404_with_no_sku_matches_is_confirmed_deleted():
    state, _ = wrl.evaluate_remote_removal(WC_ID, SKU, 404, None, [], [])
    assert state == wrl.REMOTE_DELETED


@pytest.mark.parametrize(
    "sku_any,sku_trash",
    [
        (None, []),  # status=any search uncertain
        ([], None),  # trash search uncertain
        ([], [{"id": WC_ID, "sku": SKU}]),  # found in trash
        ([{"id": 999, "sku": SKU}], []),  # another live product has the SKU
    ],
)
def test_404_without_conclusive_sku_evidence_is_not_confirmed(sku_any, sku_trash):
    state, _ = wrl.evaluate_remote_removal(WC_ID, SKU, 404, None, sku_any, sku_trash)
    assert state is None


def test_trash_with_same_id_and_sku_is_confirmed_trashed():
    body = {"id": WC_ID, "sku": SKU, "status": "trash"}
    state, _ = wrl.evaluate_remote_removal(WC_ID, SKU, 200, body, [], [])
    assert state == wrl.REMOTE_TRASHED


@pytest.mark.parametrize(
    "body,sku_any",
    [
        ({"id": WC_ID, "sku": "OTHER", "status": "trash"}, []),  # SKU mismatch
        ({"id": 1, "sku": SKU, "status": "trash"}, []),  # id mismatch
        ({"id": WC_ID, "sku": SKU, "status": "publish"}, []),  # not removed
        ({"id": WC_ID, "sku": SKU, "status": "trash"}, [{"id": 77, "sku": SKU}]),  # live duplicate
        ({"id": WC_ID, "sku": SKU, "status": "trash"}, None),  # uncertain search
    ],
)
def test_trash_without_exact_identity_is_not_confirmed(body, sku_any):
    state, _ = wrl.evaluate_remote_removal(WC_ID, SKU, 200, body, sku_any, [])
    assert state is None


@pytest.mark.parametrize("http_status", [500, 401, None])
def test_uncertain_get_is_never_removal(http_status):
    state, _ = wrl.evaluate_remote_removal(WC_ID, SKU, http_status, None, [], [])
    assert state is None


# --- confirmed_remote_removal marker consistency --------------------------


def _marker(state=wrl.REMOTE_DELETED, wc_id=WC_ID):
    return wrl.build_removal_marker(state, wc_id, SKU, "reason", "2026-10-02T00:00:00Z", "test")


def test_marker_must_match_stored_woo_id_and_no_active_recovery():
    good = {"woocommerce_product_id": WC_ID, "response_payload": {"remote_removal": _marker()}}
    assert wrl.confirmed_remote_removal(good) == wrl.REMOTE_DELETED

    wrong_id = {"woocommerce_product_id": 1, "response_payload": {"remote_removal": _marker()}}
    assert wrl.confirmed_remote_removal(wrong_id) is None

    recovering = {
        "woocommerce_product_id": WC_ID,
        "response_payload": {"remote_removal": _marker(), "recovery_required": True},
    }
    assert wrl.confirmed_remote_removal(recovering) is None

    assert wrl.confirmed_remote_removal({"woocommerce_product_id": WC_ID, "response_payload": {}}) is None


def test_marker_records_intent_and_never_recreate():
    marker = _marker(wrl.REMOTE_TRASHED)
    assert marker["intentional"] is True
    assert marker["recreate"] is False
    with pytest.raises(ValueError):
        wrl.build_removal_marker("FAILED", WC_ID, SKU, "r", "t", "x")


# --- sync_woocommerce_product_status write path ---------------------------


def _repository(internal_status, sync_status, review_reason=None):
    product = {
        "internal_product_id": INTERNAL_PRODUCT_ID,
        "candidate_id": "cand-1",
        "product_code": SKU,
        "title": "Book",
        "pricing_status": "PENDING",
        "content_status": "APPROVED",
        "image_status": "APPROVED",
        "woocommerce_status": internal_status,
        "review_required": review_reason is not None,
        "review_reason": review_reason,
        "product_metadata": {"keep": "me"},
    }
    sync = {
        "sync_id": SYNC_ID,
        "internal_product_id": INTERNAL_PRODUCT_ID,
        "woocommerce_product_id": WC_ID,
        "woocommerce_status": sync_status,
        "product_sku": SKU,
        "product_name": "Book",
        "response_payload": {"uploaded_media": [{"id": 1}], "media_upload_completed": True},
    }
    return (
        FakeSupabaseRepository({"internal_products": [product], "woocommerce_product_syncs": [sync]}),
        sync,
    )


def _run(monkeypatch, repository, sync, http_status, body, sku_results):
    monkeypatch.setattr(sync_mod, "get_woocommerce_product", lambda **kw: (http_status, body))
    monkeypatch.setattr(
        sync_mod, "search_remote_products_by_sku", lambda **kw: sku_results[kw["status"]]
    )
    return sync_mod.synchronize_one_product(
        repository=repository,
        sync_record=sync,
        store_url="https://example.com",
        api_version="wc/v3",
        consumer_key="ck",
        consumer_secret="cs",
        timeout_seconds=5,
    )


def test_sync_records_confirmed_deletion_as_terminal_and_keeps_woo_id(monkeypatch):
    repository, sync = _repository(
        WooCommerceStatus.FAILED,
        WooCommerceSyncStatus.FAILED,
        review_reason="The linked WooCommerce product could not be found.",
    )
    _run(monkeypatch, repository, sync, 404, {"message": "Invalid ID."}, {"any": [], "trash": []})

    stored_sync = repository.client.tables["woocommerce_product_syncs"][0]
    stored_product = repository.client.tables["internal_products"][0]

    assert stored_sync["woocommerce_product_id"] == WC_ID  # historical id preserved
    assert stored_sync["woocommerce_status"] == WooCommerceSyncStatus.FAILED
    assert stored_sync["error_code"] == "REMOTE_PRODUCT_DELETED"
    assert stored_sync["response_payload"]["uploaded_media"] == [{"id": 1}]  # provenance kept
    assert wrl.confirmed_remote_removal(stored_sync) == wrl.REMOTE_DELETED

    assert stored_product["woocommerce_status"] == WooCommerceStatus.FAILED
    assert stored_product["review_required"] is False
    assert stored_product["product_metadata"]["keep"] == "me"
    assert stored_product["product_metadata"]["remote_removal"]["state"] == wrl.REMOTE_DELETED
    assert stored_product["pricing_status"] == "PENDING"


def test_sync_404_with_uncertain_sku_search_stays_in_recovery(monkeypatch):
    repository, sync = _repository(WooCommerceStatus.DRAFT_CREATED, WooCommerceSyncStatus.DRAFT_CREATED)
    _run(monkeypatch, repository, sync, 404, {}, {"any": None, "trash": []})

    stored_sync = repository.client.tables["woocommerce_product_syncs"][0]
    stored_product = repository.client.tables["internal_products"][0]
    assert stored_sync["error_code"] == "WOOCOMMERCE_PRODUCT_NOT_FOUND"
    assert wrl.confirmed_remote_removal(stored_sync) is None
    assert stored_product["review_required"] is True


def test_sync_records_confirmed_trash_and_clears_only_woo_review_reason(monkeypatch):
    repository, sync = _repository(
        WooCommerceStatus.DRAFT_CREATED,
        WooCommerceSyncStatus.DRAFT_CREATED,
        review_reason="WooCommerce remote status 'trash' has no confirmed local reconciliation mapping",
    )
    body = {"id": WC_ID, "sku": SKU, "status": "trash", "name": "Book"}
    _run(monkeypatch, repository, sync, 200, body, {"any": [], "trash": []})

    stored_sync = repository.client.tables["woocommerce_product_syncs"][0]
    stored_product = repository.client.tables["internal_products"][0]
    assert stored_sync["error_code"] == "REMOTE_PRODUCT_TRASHED"
    assert wrl.confirmed_remote_removal(stored_sync) == wrl.REMOTE_TRASHED
    assert stored_product["woocommerce_status"] == WooCommerceStatus.FAILED
    assert stored_product["review_required"] is False


def test_confirmed_removal_keeps_unrelated_review_reason(monkeypatch):
    repository, sync = _repository(
        WooCommerceStatus.FAILED, WooCommerceSyncStatus.FAILED, review_reason="Image ownership unclear."
    )
    _run(monkeypatch, repository, sync, 404, {}, {"any": [], "trash": []})
    stored_product = repository.client.tables["internal_products"][0]
    assert stored_product["review_required"] is True
    assert stored_product["review_reason"] == "Image ownership unclear."


def test_trash_with_live_duplicate_sku_is_still_an_anomaly(monkeypatch):
    repository, sync = _repository(WooCommerceStatus.DRAFT_CREATED, WooCommerceSyncStatus.DRAFT_CREATED)
    body = {"id": WC_ID, "sku": SKU, "status": "trash"}
    _run(monkeypatch, repository, sync, 200, body, {"any": [{"id": 9, "sku": SKU}], "trash": []})

    stored_sync = repository.client.tables["woocommerce_product_syncs"][0]
    assert stored_sync["error_code"] == "UNSUPPORTED_REMOTE_STATUS"
    assert wrl.confirmed_remote_removal(stored_sync) is None


# --- audit ------------------------------------------------------------------


def _removed_sync(state=wrl.REMOTE_DELETED):
    return {
        "internal_product_id": INTERNAL_PRODUCT_ID,
        "woocommerce_product_id": WC_ID,
        "response_payload": {"remote_removal": _marker(state)},
    }


@pytest.mark.parametrize("state", sorted(wrl.REMOTE_REMOVAL_STATES))
def test_audit_reports_confirmed_removal_as_accepted_warning(state):
    products = [{"internal_product_id": INTERNAL_PRODUCT_ID, "product_code": SKU, "woocommerce_status": "FAILED"}]
    issues: list[dict[str, str]] = []
    audit.audit_woocommerce(products, [_removed_sync(state)], issues)

    codes = {issue["code"]: issue["severity"] for issue in issues}
    assert "REMOTE_WOO_ID_WITH_LOCAL_STATUS_MISMATCH" not in codes
    assert codes["REMOTE_WOO_PRODUCT_REMOVED"] == "WARNING"

    from pipeline_state import ACCEPTED_WARNING_CODES

    assert "REMOTE_WOO_PRODUCT_REMOVED" in ACCEPTED_WARNING_CODES


def test_audit_still_errors_for_failed_product_without_confirmed_removal():
    products = [{"internal_product_id": INTERNAL_PRODUCT_ID, "product_code": SKU, "woocommerce_status": "FAILED"}]
    syncs = [{"internal_product_id": INTERNAL_PRODUCT_ID, "woocommerce_product_id": WC_ID, "response_payload": {}}]
    issues: list[dict[str, str]] = []
    audit.audit_woocommerce(products, syncs, issues)
    assert "REMOTE_WOO_ID_WITH_LOCAL_STATUS_MISMATCH" in {i["code"] for i in issues}


# --- pipeline state ------------------------------------------------------------


def _bundle(sync: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate": {"candidate_id": "cand-1", "candidate_code": "FB-HIST-2026-001-CAN-0007"},
        "references": [],
        "discovery_sources": [],
        "images": [],
        "internal_product": {
            "internal_product_id": INTERNAL_PRODUCT_ID,
            "candidate_id": "cand-1",
            "product_code": SKU,
            "woocommerce_status": "FAILED",
            "isbn": None,
            "weight_grams": None,
        },
        "contents": [],
        "sync": {**sync, "woocommerce_status": "FAILED"},
        "historical_local_media_paths": [],
        "historical_capability_available": None,
        "historical_capability_reason": None,
        "sibling_candidate_codes": [],
    }


def test_confirmed_removal_derives_terminal_remote_removed_not_recovery():
    bundle = _bundle(_removed_sync())
    assert derive_sync_recovery_state(bundle["sync"], bundle["internal_product"]) is None

    state = derive_candidate_state(bundle)
    assert state.derived_state == wrl.DERIVED_STATE_REMOTE_REMOVED
    assert state.terminal is True
    assert state.human_gate is False
    assert state.recovery_state is None


def test_failed_without_marker_remains_recovery():
    bundle = _bundle({"internal_product_id": INTERNAL_PRODUCT_ID, "woocommerce_product_id": WC_ID, "response_payload": {}})
    state = derive_candidate_state(bundle)
    assert state.recovery_state is not None
