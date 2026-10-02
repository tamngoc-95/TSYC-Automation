"""Offline tests for scripts/update_woocommerce_draft_content.py.

Protects: draft-only updates, exact SKU identity, manual-edit protection,
description/short_description-only payload (never status/price), no retry
after an uncertain PUT, and post-update verification. No network.
"""
from __future__ import annotations

from typing import Any

import pytest
import requests

import update_woocommerce_draft_content as uwd

SKU = "TSYC-FB-HIST-2026-002-CAN-0011"
ORIGINAL = {
    "description": "<p>“X” hiện có tại Tiệm Sách Yêu Con.</p>\n<p>(Mô tả tham khảo từ nguồn FAHASA đã được xác minh.)</p>",
    "short_description": "<p>“X” là ấn phẩm đang có tại Tiệm Sách Yêu Con.</p>",
}
NEW = {
    "description": "<p>X là cuốn sách kinh điển về nghệ thuật giao tiếp.</p>",
    "short_description": "<p>Sách kinh điển về nghệ thuật giao tiếp.</p>",
}


def _remote(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": 101,
        "sku": SKU,
        "status": "draft",
        "regular_price": "",
        "sale_price": "",
        "price": "",
        **ORIGINAL,
    }
    row.update(overrides)
    return row


class FakeClient:
    def __init__(self, remote: dict[str, Any] | None, put_error: Exception | None = None, apply: bool = True):
        self.remote = remote
        self.put_error = put_error
        self.apply = apply
        self.puts: list[dict[str, str]] = []
        self.gets = 0

    def get(self, product_id):
        self.gets += 1
        return dict(self.remote) if self.remote else None

    def update(self, product_id, payload):
        if set(payload) != set(uwd.UPDATABLE_FIELDS):
            raise AssertionError("payload must contain only description/short_description")
        self.puts.append(payload)
        if self.apply:
            self.remote.update(payload)
        if self.put_error:
            raise self.put_error
        return dict(self.remote)


@pytest.fixture
def patched_local(monkeypatch):
    content = {"long_description": "X là cuốn sách kinh điển về nghệ thuật giao tiếp.", "short_description": "Sách kinh điển về nghệ thuật giao tiếp."}

    def fake_load(repository, product_code):
        return {
            "product": {"candidate_id": "c1", "product_code": product_code},
            "sync": {"woocommerce_product_id": 101, "product_sku": SKU, "request_payload": dict(ORIGINAL)},
            "content": content,
        }

    monkeypatch.setattr(uwd, "load_local_target", fake_load)
    monkeypatch.setattr(uwd, "build_update_payload", lambda content: dict(NEW))


@pytest.mark.parametrize(
    "remote, expected",
    [
        (None, uwd.REMOTE_MISSING),
        (_remote(sku="OTHER"), uwd.SKU_MISMATCH),
        (_remote(status="publish"), uwd.NOT_DRAFT),
        (_remote(status="trash"), uwd.NOT_DRAFT),
        (_remote(description="<p>Shop owner wrote this.</p>"), uwd.MANUAL_EDIT),
        (_remote(**NEW), uwd.NO_OP),
        (_remote(), None),
    ],
)
def test_classify_remote(remote, expected):
    assert uwd.classify_remote(remote, SKU, ORIGINAL, NEW) == expected


def test_comparable_text_ignores_markup_and_entities():
    assert uwd.comparable_text("<p>Ch&uacute;ng   ta</p>") == uwd.comparable_text("Chúng ta")


def test_published_product_is_never_written(patched_local):
    client = FakeClient(_remote(status="publish"))
    report = uwd.run_update(None, client, SKU, dry_run=False)
    assert report["result"] == uwd.NOT_DRAFT
    assert client.puts == []


def test_manual_edit_is_never_overwritten(patched_local):
    client = FakeClient(_remote(short_description="<p>Owner text.</p>"))
    report = uwd.run_update(None, client, SKU, dry_run=False)
    assert report["result"] == uwd.MANUAL_EDIT
    assert client.puts == []


def test_dry_run_writes_nothing(patched_local):
    client = FakeClient(_remote())
    assert uwd.run_update(None, client, SKU, dry_run=True)["result"] == uwd.DRY_RUN
    assert client.puts == []


def test_update_sends_only_content_fields_and_verifies(patched_local):
    client = FakeClient(_remote())
    logs: list[dict[str, Any]] = []
    report = uwd.run_update(None, client, SKU, dry_run=False, log=lambda **kw: logs.append(kw))
    assert report["result"] == uwd.UPDATED
    assert client.puts == [NEW]
    assert client.remote["status"] == "draft"
    assert logs and logs[0]["error_details"]["previous_description"] == ORIGINAL["description"]


def test_uncertain_put_is_not_retried_and_reconciled(patched_local):
    client = FakeClient(_remote(), put_error=requests.Timeout("timeout"), apply=False)
    report = uwd.run_update(None, client, SKU, dry_run=False)
    assert report["result"] == uwd.UNCERTAIN
    assert len(client.puts) == 1  # never retried


def test_uncertain_put_that_actually_applied_is_verified(patched_local):
    client = FakeClient(_remote(), put_error=requests.ConnectionError("reset"), apply=True)
    report = uwd.run_update(None, client, SKU, dry_run=False)
    assert report["result"] == uwd.UPDATED
    assert len(client.puts) == 1


def test_price_change_after_update_fails_verification():
    before = _remote()
    after = _remote(regular_price="100", **NEW)
    problems = uwd.verify_after_update(before, after, NEW, SKU)
    assert "regular_price changed" in problems


@pytest.fixture
def patched_local_published(monkeypatch, patched_local):
    original_load = uwd.load_local_target

    def fake_load(repository, product_code):
        local = original_load(repository, product_code)
        local["product"]["woocommerce_status"] = "PUBLISHED"
        return local

    monkeypatch.setattr(uwd, "load_local_target", fake_load)


def test_published_requires_explicit_authorization_and_local_published():
    assert uwd.allowed_remote_statuses(False, "PUBLISHED") == {"draft"}
    assert uwd.allowed_remote_statuses(True, "DRAFT_CREATED") == {"draft"}
    assert uwd.allowed_remote_statuses(True, "PUBLISHED") == {"draft", "publish"}


def test_published_update_preserves_status_and_prices(patched_local_published):
    client = FakeClient(_remote(status="publish", regular_price="120000", price="120000"))
    report = uwd.run_update(None, client, SKU, dry_run=False, allow_published=True)
    assert report["result"] == uwd.UPDATED
    assert client.puts == [NEW]  # never status, never a price field
    assert client.remote["status"] == "publish"
    assert client.remote["regular_price"] == "120000"


def test_published_without_flag_is_never_written(patched_local_published):
    client = FakeClient(_remote(status="publish"))
    report = uwd.run_update(None, client, SKU, dry_run=False, allow_published=False)
    assert report["result"] == uwd.NOT_DRAFT
    assert client.puts == []


def test_published_manual_edit_is_never_overwritten(patched_local_published):
    client = FakeClient(_remote(status="publish", description="<p>Owner rewrote this.</p>"))
    report = uwd.run_update(None, client, SKU, dry_run=False, allow_published=True)
    assert report["result"] == uwd.MANUAL_EDIT
    assert client.puts == []


def test_trash_is_never_written_even_with_published_flag(patched_local_published):
    client = FakeClient(_remote(status="trash"))
    report = uwd.run_update(None, client, SKU, dry_run=False, allow_published=True)
    assert report["result"] == uwd.NOT_DRAFT
    assert client.puts == []


def test_status_or_store_field_change_fails_verification():
    before = _remote(status="publish", name="Book", stock_status="instock")
    assert "status is 'draft', expected unchanged 'publish'" in uwd.verify_after_update(
        before, _remote(status="draft", name="Book", stock_status="instock", **NEW), NEW, SKU
    )
    assert "stock_status changed" in uwd.verify_after_update(
        before, _remote(status="publish", name="Book", stock_status="outofstock", **NEW), NEW, SKU
    )


def test_remote_id_mismatch_is_treated_as_missing():
    assert uwd.classify_remote(_remote(id=999), SKU, ORIGINAL, NEW, expected_product_id=101) == uwd.REMOTE_MISSING


def test_real_payload_builder_contains_only_content_fields():
    payload = uwd.build_update_payload(
        {"long_description": "Nội dung.", "short_description": "Ngắn.", "author_summary": None, "product_details": None}
    )
    assert set(payload) == {"description", "short_description"}
    assert not any(field in payload for field in ("status", "regular_price", "sale_price", "price"))
