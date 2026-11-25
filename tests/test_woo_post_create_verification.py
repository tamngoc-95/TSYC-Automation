"""CLAUDE.md 6.3 post-create verification of a new WooCommerce draft.

Offline: requests.get is monkeypatched; no live WooCommerce or Supabase.
"""
from __future__ import annotations

import copy
import inspect
from typing import Any

import pytest

import create_woocommerce_draft as cwd
import pipeline_state
from src.domain.rules import woo_post_create_verification as v

from support.fake_supabase import FakeSupabaseRepository

WOO_ID = 5500
SKU = "TSYC-FAHASA-2026-006-CAN-0001"
IP_ID = "ip-1"


def sent_payload(preorder: bool = True) -> dict[str, Any]:
    payload = {
        "name": "Bé Học Màu Sắc",
        "type": "simple",
        "status": "draft",
        "sku": SKU,
        "description": "<h3>Giới thiệu</h3>\n<p>Cuốn sách &amp; trò chơi.</p>",
        "short_description": "<p>Ngắn gọn.</p>",
        "manage_stock": False,
        "images": [{"id": 901}, {"id": 902}],
        "meta_data": [{"key": "_tsyc_internal_product_id", "value": IP_ID}],
    }
    if preorder:
        payload.update({"manage_stock": True, "stock_quantity": 0, "backorders": "yes"})
    return payload


def remote_product(**overrides: Any) -> dict[str, Any]:
    product = {
        "id": WOO_ID,
        "sku": SKU,
        "name": "Bé Học Màu Sắc",
        "status": "draft",
        "regular_price": "",
        "sale_price": "",
        "price": "",
        "manage_stock": True,
        "stock_quantity": 0,
        "backorders": "yes",
        # WordPress may re-wrap markup / re-encode entities.
        "description": "<h3>Giới thiệu</h3><p>Cuốn sách & trò chơi.</p>",
        "short_description": "Ngắn gọn.",
        "images": [{"id": 901, "src": "x"}, {"id": 902, "src": "y"}],
        "meta_data": [{"id": 1, "key": "_tsyc_internal_product_id", "value": IP_ID}],
    }
    product.update(overrides)
    return product


def check(remote=None, hits=None, payload=None):
    return v.verify_created_draft(
        expected_product_id=WOO_ID,
        sent_payload=payload or sent_payload(),
        internal_product_id=IP_ID,
        remote=remote if remote is not None else remote_product(),
        sku_hits=hits if hits is not None else [{"id": WOO_ID, "sku": SKU}],
    )


def test_matching_preorder_draft_is_verified():
    result = check()
    assert result["status"] == v.VERIFIED, result["failed"]
    assert result["preorder"] is True


@pytest.mark.parametrize(
    "overrides, failed",
    [
        ({"status": "publish"}, "status_draft"),
        ({"regular_price": "120000"}, "prices_empty"),
        ({"sale_price": "99000"}, "prices_empty"),
        ({"price": "99000"}, "prices_empty"),
        ({"stock_quantity": 5}, "stock_quantity_zero"),
        ({"backorders": "no"}, "backorders_yes"),
        ({"manage_stock": False}, "manage_stock"),
        ({"sku": "OTHER"}, "sku"),
        ({"id": 1}, "woo_id"),
        ({"images": [{"id": 902}, {"id": 901}]}, "images"),
        ({"images": [{"id": 901}]}, "images"),
        ({"description": "<p>Khác hẳn.</p>"}, "description"),
        ({"short_description": "Khác."}, "short_description"),
        ({"name": "Sách khác"}, "name"),
        ({"meta_data": []}, "internal_product_link"),
    ],
)
def test_any_mismatch_fails(overrides, failed):
    result = check(remote=remote_product(**overrides))
    assert result["status"] == v.FAILED
    assert failed in result["failed"]


def test_missing_price_field_fails_closed():
    remote = remote_product()
    del remote["regular_price"]
    assert "prices_empty" in check(remote=remote)["failed"]


def test_unreadable_product_fails():
    result = v.verify_created_draft(
        expected_product_id=WOO_ID, sent_payload=sent_payload(), internal_product_id=IP_ID,
        remote=None, sku_hits=[{"id": WOO_ID, "sku": SKU}],
    )
    assert result["status"] == v.FAILED
    assert "product_read" in result["failed"]


@pytest.mark.parametrize(
    "hits",
    [
        None,  # SKU search unreadable
        [],  # product not found by SKU
        [{"id": WOO_ID, "sku": SKU}, {"id": 4000, "sku": SKU}],  # duplicate (e.g. trash)
    ],
)
def test_duplicate_or_unreadable_sku_fails(hits):
    result = v.verify_created_draft(
        expected_product_id=WOO_ID, sent_payload=sent_payload(), internal_product_id=IP_ID,
        remote=remote_product(), sku_hits=hits,
    )
    assert "no_duplicate_sku" in result["failed"]


def test_non_preorder_draft_checks_manage_stock_false():
    payload = sent_payload(preorder=False)
    remote = remote_product(manage_stock=False, stock_quantity=None, backorders="no")
    result = check(remote=remote, payload=payload)
    assert result["status"] == v.VERIFIED, result["failed"]
    assert result["preorder"] is False


class _Resp:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body
        self.text = ""

    def json(self) -> Any:
        return self._body


def _fake_get(product: dict[str, Any] | None, any_hits, trash_hits, calls: list):
    def fake(url, auth=None, params=None, headers=None, timeout=None):
        calls.append((url, dict(params or {})))
        if url.endswith(f"products/{WOO_ID}"):
            return _Resp(200, product) if product is not None else _Resp(500, None)
        status = (params or {}).get("status")
        return _Resp(200, any_hits if status == "any" else trash_hits)
    return fake


def _verify(monkeypatch, product, any_hits, trash_hits):
    calls: list = []
    monkeypatch.setattr(cwd.requests, "get", _fake_get(product, any_hits, trash_hits, calls))
    result = cwd.verify_created_draft_remotely(
        store_url="https://shop.example", api_version="wc/v3", consumer_key="k",
        consumer_secret="s", timeout_seconds=5, product={"internal_product_id": IP_ID},
        product_id=WOO_ID, sent_payload=sent_payload(),
    )
    return result, calls


def test_remote_reader_uses_edit_context_and_any_plus_trash(monkeypatch):
    result, calls = _verify(monkeypatch, remote_product(), [{"id": WOO_ID, "sku": SKU}], [])
    assert result["status"] == v.VERIFIED, result["failed"]
    assert calls[0][1] == {"context": "edit"}
    assert {c[1].get("status") for c in calls[1:]} == {"any", "trash"}


def test_remote_reader_detects_trashed_duplicate(monkeypatch):
    result, _ = _verify(monkeypatch, remote_product(), [{"id": WOO_ID, "sku": SKU}], [{"id": 3999, "sku": SKU}])
    assert "no_duplicate_sku" in result["failed"]


def test_remote_reader_read_failure_is_failed_not_raised(monkeypatch):
    result, _ = _verify(monkeypatch, None, [{"id": WOO_ID, "sku": SKU}], [])
    assert result["status"] == v.FAILED


def test_remote_reader_is_get_only():
    source = inspect.getsource(cwd.verify_created_draft_remotely) + inspect.getsource(cwd._woo_get)
    assert "requests.post" not in source and "requests.put" not in source and "requests.delete" not in source


def _sync_repo() -> FakeSupabaseRepository:
    return FakeSupabaseRepository({
        "woocommerce_product_syncs": [{
            "sync_id": "sync-1",
            "woocommerce_status": "DRAFT_CREATED",
            "woocommerce_product_id": WOO_ID,
            "response_payload": {"uploaded_media": [{"wordpress_media_id": 901}], "media_upload_completed": True},
        }],
    })


def test_verified_result_is_persisted_without_recovery():
    repo = _sync_repo()
    cwd.persist_post_create_verification(repo, "sync-1", check())
    row = repo.client.tables["woocommerce_product_syncs"][0]
    assert row["response_payload"]["post_create_verification"]["status"] == v.VERIFIED
    assert row["response_payload"]["uploaded_media"]  # existing progress kept
    assert "recovery_required" not in row["response_payload"]
    assert pipeline_state.derive_sync_recovery_state(row, {"woocommerce_status": "DRAFT_CREATED"}) is None


def test_failed_result_flags_recovery_and_stops_candidate():
    repo = _sync_repo()
    cwd.persist_post_create_verification(repo, "sync-1", check(remote=remote_product(regular_price="1")))
    row = repo.client.tables["woocommerce_product_syncs"][0]
    assert row["response_payload"]["recovery_required"] is True
    assert row["error_code"] == "POST_CREATE_VERIFICATION_FAILED"
    assert pipeline_state.derive_sync_recovery_state(row, {"woocommerce_status": "DRAFT_CREATED"}) == (
        "RECOVERY_REVIEW_REQUIRED", "REMOTE_CREATED_LOCAL_DIRTY",
    )


def test_main_runs_verification_after_local_finalization_and_never_retries():
    source = inspect.getsource(cwd.main)
    finalize = source.index("update_internal_product(")
    verify = source.index("verify_created_draft_remotely(")
    persist = source.index("persist_post_create_verification(")
    assert finalize < verify < persist
    # Exactly one create call in main: verification never re-creates.
    assert source.count("create_woocommerce_product(") == 1


def test_payload_is_not_mutated_by_verifier():
    payload = sent_payload()
    before = copy.deepcopy(payload)
    check(payload=payload)
    assert payload == before
