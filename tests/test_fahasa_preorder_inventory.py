"""Fahasa pre-order inventory policy and existing-draft reconciliation.

Protects (2026-10-16): stock_quantity 0 + backorders "yes" only for TSYC
Fahasa pre-order products; stock_status never sent (WooCommerce derives
"onbackorder"); prices never sent; draft status preserved; published,
trashed, priced, owner-restocked and foreign products never written;
idempotent repeat; no retry after an uncertain PUT; post-write
reconciliation. Offline: no network, no Supabase.
"""
from __future__ import annotations

from typing import Any

import pytest
import requests

import apply_preorder_inventory as api
import create_woocommerce_draft as cwd
from src.domain.rules import preorder_inventory_rules as rules

PRODUCT_ID = "ip-1"
SKU = "TSYC-FAHASA-2026-002-CAN-0001"
WOO_ID = 4801
PREORDER_CANDIDATE = {
    "candidate_code": "FAHASA-2026-002-CAN-0001",
    "source_evidence": {"origin": "FAHASA_DISCOVERY", "supply_status": "PREORDER_PENDING_SUPPLY_CONFIRMATION"},
}
CREATED_PAYLOAD = {"status": "draft", "manage_stock": False}


def _remote(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": WOO_ID,
        "sku": SKU,
        "status": "draft",
        "name": "Bên Suối Bít Tai Nghe Gió",
        "slug": "ben-suoi",
        "description": "<p>Mô tả.</p>",
        "short_description": "<p>Ngắn.</p>",
        "catalog_visibility": "visible",
        "regular_price": "",
        "sale_price": "",
        "price": "",
        "manage_stock": False,
        "stock_quantity": None,
        "backorders": "no",
        "stock_status": "instock",
        "images": [{"id": 1}, {"id": 2}],
        "meta_data": [{"key": "_tsyc_internal_product_id", "value": PRODUCT_ID}],
    }
    row.update(overrides)
    return row


APPLIED = {"manage_stock": True, "stock_quantity": 0, "backorders": "yes", "stock_status": "onbackorder"}


# --- eligibility --------------------------------------------------------------


@pytest.mark.parametrize(
    "candidate, expected",
    [
        (PREORDER_CANDIDATE, True),
        ({**PREORDER_CANDIDATE, "candidate_code": "FB-2026-001-CAN-0001"}, False),
        ({**PREORDER_CANDIDATE, "candidate_code": "FB-HIST-2026-001-CAN-0001"}, False),
        ({**PREORDER_CANDIDATE, "source_evidence": {"origin": "FAHASA_DISCOVERY", "supply_status": "IN_STOCK"}}, False),
        ({**PREORDER_CANDIDATE, "source_evidence": {}}, False),
        (None, False),
    ],
)
def test_only_fahasa_preorder_candidates_are_eligible(candidate, expected):
    assert rules.is_fahasa_preorder_candidate(candidate) is expected


# --- creation payload ----------------------------------------------------------


def _payload(preorder: bool) -> dict[str, Any]:
    return cwd.build_woocommerce_payload(
        product={"internal_product_id": PRODUCT_ID, "candidate_id": "c1", "product_code": SKU, "author": "Văn Thành Lê"},
        content={"product_content_id": "pc1", "product_name": "Bên Suối", "long_description": "Mô tả.", "short_description": "Ngắn."},
        uploaded_media=[{"wordpress_media_id": 11}, {"wordpress_media_id": 12}],
        preorder=preorder,
    )


def test_preorder_draft_has_stock_zero_and_backorders_enabled():
    payload = _payload(preorder=True)
    assert payload["manage_stock"] is True
    assert payload["stock_quantity"] == 0
    assert payload["backorders"] == "yes"
    assert "stock_status" not in payload  # derived by WooCommerce
    assert payload["status"] == "draft"
    for field in ("regular_price", "sale_price", "price"):
        assert field not in payload


def test_non_preorder_draft_keeps_unmanaged_stock():
    payload = _payload(preorder=False)
    assert payload["manage_stock"] is False
    assert "stock_quantity" not in payload and "backorders" not in payload


def test_gallery_images_follow_main_image_in_order():
    assert _payload(preorder=True)["images"] == [{"id": 11}, {"id": 12}]


def test_inventory_payload_is_exactly_the_inventory_fields():
    assert rules.build_inventory_payload() == {"manage_stock": True, "stock_quantity": 0, "backorders": "yes"}


# --- existing-draft classification ----------------------------------------------


@pytest.mark.parametrize(
    "remote, expected",
    [
        (None, rules.REMOTE_MISSING),
        (_remote(id=9999), rules.REMOTE_MISSING),
        (_remote(sku="OTHER"), rules.SKU_MISMATCH),
        (_remote(status="publish"), rules.NOT_DRAFT),
        (_remote(status="trash"), rules.NOT_DRAFT),
        (_remote(status="pending"), rules.NOT_DRAFT),
        (_remote(meta_data=[]), rules.NOT_AUTOMATION_OWNED),
        (_remote(regular_price="10.99", price="10.99"), rules.OWNER_PRICED),
        (_remote(manage_stock=True, stock_quantity=3, backorders="no"), rules.OWNER_INVENTORY),
        (_remote(backorders="notify"), rules.OWNER_INVENTORY),
        (_remote(**APPLIED), rules.NO_OP),
        (_remote(), rules.ELIGIBLE),
    ],
)
def test_classify_inventory_target(remote, expected):
    assert rules.classify_inventory_target(remote, WOO_ID, SKU, PRODUCT_ID, CREATED_PAYLOAD) == expected


def test_verification_requires_onbackorder_and_unchanged_store_fields():
    before = _remote()
    assert rules.verify_inventory_after(before, _remote(**APPLIED)) == []
    assert any("stock_status" in p for p in rules.verify_inventory_after(before, _remote(**{**APPLIED, "stock_status": "instock"})))
    assert "regular_price changed" in rules.verify_inventory_after(before, _remote(**APPLIED, regular_price="9.99"))
    assert "status changed" in rules.verify_inventory_after(before, _remote(**{**APPLIED, "status": "publish"}))
    assert "images changed" in rules.verify_inventory_after(before, _remote(**APPLIED, images=[{"id": 3}]))


# --- end-to-end apply run (fake Woo, fake local) ----------------------------------


class FakeWoo:
    def __init__(self, remote: dict[str, Any] | None, put_error: Exception | None = None, apply: bool = True):
        self.remote = remote
        self.put_error = put_error
        self.apply = apply
        self.puts: list[dict[str, Any]] = []

    def get(self, product_id):
        return dict(self.remote) if self.remote else None

    def update_inventory(self, product_id, payload):
        assert set(payload) == set(rules.INVENTORY_FIELDS), "only inventory fields may be sent"
        self.puts.append(payload)
        if self.apply:
            self.remote.update(payload)
            # WooCommerce derives stock_status for managed stock.
            self.remote["stock_status"] = "onbackorder"
        if self.put_error:
            raise self.put_error
        return dict(self.remote)


@pytest.fixture
def local(monkeypatch):
    def fake_load(repository, product_code):
        return {
            "product": {"internal_product_id": PRODUCT_ID, "candidate_id": "c1", "product_code": product_code},
            "candidate": PREORDER_CANDIDATE,
            "sync": {"woocommerce_product_id": WOO_ID, "product_sku": SKU, "request_payload": dict(CREATED_PAYLOAD)},
        }

    monkeypatch.setattr(api, "load_local_target", fake_load)


def test_apply_updates_eligible_draft_and_verifies(local):
    woo = FakeWoo(_remote())
    logs: list[dict[str, Any]] = []
    report = api.run_apply(None, woo, SKU, dry_run=False, log=lambda **kw: logs.append(kw))
    assert report["result"] == api.UPDATED
    assert woo.puts == [{"manage_stock": True, "stock_quantity": 0, "backorders": "yes"}]
    assert woo.remote["status"] == "draft" and woo.remote["regular_price"] == ""
    assert report["stock_status"] == "onbackorder"
    assert logs and logs[0]["error_details"]["previous_inventory"]["backorders"] == "no"


def test_apply_is_idempotent(local):
    woo = FakeWoo(_remote())
    assert api.run_apply(None, woo, SKU, dry_run=False)["result"] == api.UPDATED
    assert api.run_apply(None, woo, SKU, dry_run=False)["result"] == rules.NO_OP
    assert len(woo.puts) == 1


def test_dry_run_writes_nothing(local):
    woo = FakeWoo(_remote())
    assert api.run_apply(None, woo, SKU, dry_run=True)["result"] == api.DRY_RUN
    assert woo.puts == []


@pytest.mark.parametrize(
    "remote, expected",
    [
        (_remote(status="publish", regular_price="10.99"), rules.NOT_DRAFT),
        (_remote(regular_price="10.99"), rules.OWNER_PRICED),
        (_remote(manage_stock=True, stock_quantity=1, backorders="yes", stock_status="instock"), rules.OWNER_INVENTORY),
    ],
)
def test_owner_maintained_products_are_never_written(local, remote, expected):
    woo = FakeWoo(remote)
    assert api.run_apply(None, woo, SKU, dry_run=False)["result"] == expected
    assert woo.puts == []


def test_uncertain_put_is_not_retried_and_is_reconciled(local):
    woo = FakeWoo(_remote(), put_error=requests.Timeout("timeout"), apply=False)
    report = api.run_apply(None, woo, SKU, dry_run=False)
    assert report["result"] == api.UNCERTAIN
    assert len(woo.puts) == 1


def test_uncertain_put_that_applied_is_verified_on_reread(local):
    woo = FakeWoo(_remote(), put_error=requests.ConnectionError("reset"), apply=True)
    assert api.run_apply(None, woo, SKU, dry_run=False)["result"] == api.UPDATED
    assert len(woo.puts) == 1


def test_non_preorder_product_is_isolated_locally(monkeypatch):
    def fake_load(repository, product_code):
        raise RuntimeError(f"{rules.NOT_PREORDER_PRODUCT}: candidate is not a Fahasa pre-order candidate.")

    monkeypatch.setattr(api, "load_local_target", fake_load)
    woo = FakeWoo(_remote())
    report = api.run_apply(None, woo, "TSYC-FB-2026-001-CAN-0001", dry_run=False)
    assert report["result"] == api.LOCAL_NOT_READY and rules.NOT_PREORDER_PRODUCT in report["detail"]
    assert woo.puts == []


def test_client_refuses_any_non_inventory_field():
    client = api.WooInventoryClient("https://example.com", "wc/v3", "k", "s", 5)
    with pytest.raises(RuntimeError, match="Refusing"):
        client.update_inventory(WOO_ID, {**rules.build_inventory_payload(), "regular_price": "1"})


def test_cli_requires_bounded_exact_targets():
    with pytest.raises(SystemExit):
        api.parse_arguments(["--product-code", SKU, "--product-code", SKU, "--max-products", "2"])
    with pytest.raises(SystemExit):
        api.parse_arguments(["--product-code", "A", "--product-code", "B", "--max-products", "1"])
    args = api.parse_arguments(["--product-code", SKU, "--max-products", "1"])
    assert args.confirm_update is False  # dry run by default
