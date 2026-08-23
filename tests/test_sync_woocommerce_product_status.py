"""Regression tests for scripts/sync_woocommerce_product_status.py's
remote-status reconciliation.

Covers the fix for the drift found during the src.domain centralization:
map_remote_status() must never produce a value outside either
woocommerce_status column's CHECK constraint, and an unsupported/
ambiguous remote status must never be silently written into either
column at all -- it must leave both untouched and flag the internal
product for review instead (mark_reconciliation_anomaly()).

Pure/offline: no live WooCommerce, no live Supabase, no network calls.
get_woocommerce_product() (the one function that makes a real HTTP
request) is monkeypatched wherever a full synchronize_one_product() run
is exercised.
"""
from __future__ import annotations

from typing import Any

import pytest

import sync_woocommerce_product_status as sync_mod

from src.domain.woocommerce_status import (
    ALL_WOOCOMMERCE_STATUSES,
    ALL_WOOCOMMERCE_SYNC_STATUSES,
    WooCommerceStatus,
    WooCommerceSyncStatus,
)
from support.fake_supabase import FakeSupabaseRepository


INTERNAL_PRODUCT_ID = "internal-product-1"
SYNC_ID = "sync-1"
WC_PRODUCT_ID = 555
PRODUCT_SKU = "TSYC-FB-2026-001-CAN-0001"


# --- map_remote_status(): pure function, no I/O -----------------------------

SUPPORTED_CASES = [
    ("draft", WooCommerceSyncStatus.DRAFT_CREATED, WooCommerceStatus.DRAFT_CREATED),
    ("publish", WooCommerceSyncStatus.DRAFT_CREATED, WooCommerceStatus.PUBLISHED),
]

# Every remote status this reconciliation workflow deliberately does NOT
# have a confirmed mapping for -- ambiguous WooCommerce/WordPress
# lifecycle stages (pending, private, trash), a possible-but-unlisted
# future WordPress status, a totally unrecognized string, the
# empty/missing cases, a unicode value, and -- specifically -- strings
# that collide with a *local* WooCommerceStatus/WooCommerceSyncStatus
# member name (e.g. "FAILED", "NOT_CREATED") without actually being a
# real WooCommerce remote status; the lookup is a plain case-sensitive
# dict .get() against the lowercase keys "draft"/"publish", so these
# must be safe misses just like any other unrecognized string.
UNSUPPORTED_STATUSES = [
    "pending",
    "private",
    "trash",
    "future",
    "auto-draft",
    "some-unrecognized-custom-status",
    "",
    None,
    "Đã lưu trữ",
    "FAILED",
    "NOT_CREATED",
    "DRAFT_CREATED",
    "PUBLISHED",
    "READY_TO_PUBLISH",
]


@pytest.mark.parametrize(
    "remote_status,expected_sync_status,expected_internal_status",
    SUPPORTED_CASES,
)
def test_map_remote_status_supported_returns_canonical_pair(
    remote_status, expected_sync_status, expected_internal_status
):
    result = sync_mod.map_remote_status(remote_status)

    assert result.supported is True
    assert result.local_sync_status == expected_sync_status
    assert result.internal_status == expected_internal_status
    assert result.remote_status == remote_status

    # The hard guarantee: whatever is returned is always a real member of
    # the column it will be written to.
    assert result.local_sync_status in ALL_WOOCOMMERCE_SYNC_STATUSES
    assert result.internal_status in ALL_WOOCOMMERCE_STATUSES


@pytest.mark.parametrize("remote_status", UNSUPPORTED_STATUSES)
def test_map_remote_status_unsupported_returns_no_value(remote_status):
    result = sync_mod.map_remote_status(remote_status)

    assert result.supported is False
    assert result.local_sync_status is None
    assert result.internal_status is None
    assert result.remote_status == remote_status


def test_no_supported_mapping_ever_yields_a_non_canonical_value():
    """Every value SUPPORTED_REMOTE_STATUS_MAPPING can possibly produce
    must be canonical -- checked exhaustively, not just for the two
    cases above, so a future addition to the mapping cannot silently
    reintroduce the original bug."""
    for local_sync_status, internal_status in sync_mod.SUPPORTED_REMOTE_STATUS_MAPPING.values():
        assert local_sync_status in ALL_WOOCOMMERCE_SYNC_STATUSES
        assert internal_status in ALL_WOOCOMMERCE_STATUSES


# --- synchronize_one_product(): full write-path integration, fake DB -------


def make_internal_product(**overrides) -> dict[str, Any]:
    product = {
        "internal_product_id": INTERNAL_PRODUCT_ID,
        "candidate_id": "candidate-1",
        "product_code": PRODUCT_SKU,
        "title": "Some Book",
        "pricing_status": "PENDING",
        "content_status": "APPROVED",
        "image_status": "APPROVED",
        "woocommerce_status": WooCommerceStatus.DRAFT_CREATED,
        "review_required": False,
        "review_reason": None,
        "product_metadata": {},
    }
    product.update(overrides)
    return product


def make_sync_record(**overrides) -> dict[str, Any]:
    record = {
        "sync_id": SYNC_ID,
        "internal_product_id": INTERNAL_PRODUCT_ID,
        "woocommerce_product_id": WC_PRODUCT_ID,
        "woocommerce_status": WooCommerceSyncStatus.DRAFT_CREATED,
        "product_sku": PRODUCT_SKU,
        "product_name": "Some Book",
        "response_payload": {},
        "error_code": None,
        "error_message": None,
    }
    record.update(overrides)
    return record


def make_repository(
    internal_product: dict[str, Any],
    sync_record: dict[str, Any],
) -> FakeSupabaseRepository:
    return FakeSupabaseRepository(
        {
            "internal_products": [internal_product],
            "woocommerce_product_syncs": [sync_record],
        }
    )


def run_sync(
    monkeypatch: pytest.MonkeyPatch,
    repository: FakeSupabaseRepository,
    sync_record: dict[str, Any],
    remote_status: str | None,
    http_status: int = 200,
) -> bool:
    remote_payload = (
        {"message": "Invalid ID."}
        if http_status == 404
        else {
            "id": WC_PRODUCT_ID,
            "sku": PRODUCT_SKU,
            "name": "Some Book",
            "status": remote_status,
            "permalink": "https://example.com/product/some-book",
        }
    )

    def fake_get_woocommerce_product(**kwargs):
        return http_status, remote_payload

    monkeypatch.setattr(
        sync_mod, "get_woocommerce_product", fake_get_woocommerce_product
    )

    return sync_mod.synchronize_one_product(
        repository=repository,
        sync_record=sync_record,
        store_url="https://example.com",
        api_version="wc/v3",
        consumer_key="ck_test",
        consumer_secret="cs_test",
        timeout_seconds=5,
    )


def test_supported_draft_status_writes_canonical_values(monkeypatch):
    internal_product = make_internal_product(woocommerce_status="NOT_CREATED")
    sync_record = make_sync_record(woocommerce_status=WooCommerceSyncStatus.PENDING)
    repository = make_repository(internal_product, sync_record)

    result = run_sync(monkeypatch, repository, sync_record, "draft")

    assert result is True
    updated_sync = repository.client.tables["woocommerce_product_syncs"][0]
    updated_internal = repository.client.tables["internal_products"][0]

    assert updated_sync["woocommerce_status"] == WooCommerceSyncStatus.DRAFT_CREATED
    assert updated_internal["woocommerce_status"] == WooCommerceStatus.DRAFT_CREATED
    assert updated_internal["review_required"] is False


def test_supported_publish_status_writes_canonical_values(monkeypatch):
    internal_product = make_internal_product(
        woocommerce_status=WooCommerceStatus.DRAFT_CREATED
    )
    sync_record = make_sync_record(
        woocommerce_status=WooCommerceSyncStatus.DRAFT_CREATED
    )
    repository = make_repository(internal_product, sync_record)

    result = run_sync(monkeypatch, repository, sync_record, "publish")

    assert result is True
    updated_sync = repository.client.tables["woocommerce_product_syncs"][0]
    updated_internal = repository.client.tables["internal_products"][0]

    # The sync *attempt* itself remains DRAFT_CREATED -- "publish" is a
    # later lifecycle stage of the same already-successful attempt, not
    # a new/different sync outcome.
    assert updated_sync["woocommerce_status"] == WooCommerceSyncStatus.DRAFT_CREATED
    assert updated_internal["woocommerce_status"] == WooCommerceStatus.PUBLISHED
    assert updated_internal["review_required"] is False


@pytest.mark.parametrize("remote_status", UNSUPPORTED_STATUSES)
def test_unsupported_status_never_writes_invalid_enum_and_flags_review(
    monkeypatch, remote_status
):
    internal_product = make_internal_product(
        woocommerce_status=WooCommerceStatus.DRAFT_CREATED
    )
    sync_record = make_sync_record(
        woocommerce_status=WooCommerceSyncStatus.DRAFT_CREATED
    )
    repository = make_repository(internal_product, sync_record)

    result = run_sync(monkeypatch, repository, sync_record, remote_status)

    assert result is False

    updated_sync = repository.client.tables["woocommerce_product_syncs"][0]
    updated_internal = repository.client.tables["internal_products"][0]

    # Both woocommerce_status columns must be left EXACTLY as they were --
    # never overwritten with a guessed or invalid value.
    assert updated_sync["woocommerce_status"] == WooCommerceSyncStatus.DRAFT_CREATED
    assert updated_internal["woocommerce_status"] == WooCommerceStatus.DRAFT_CREATED

    # And, as a hard invariant independent of the above: whatever ended up
    # in either column is always a canonical member of that column's enum.
    assert updated_sync["woocommerce_status"] in ALL_WOOCOMMERCE_SYNC_STATUSES
    assert updated_internal["woocommerce_status"] in ALL_WOOCOMMERCE_STATUSES

    assert updated_internal["review_required"] is True
    assert updated_internal["review_reason"] is not None
    assert repr(remote_status) in updated_internal["review_reason"]

    assert updated_sync["error_code"] == "UNSUPPORTED_REMOTE_STATUS"
    assert updated_sync["error_message"] is not None


def test_unsupported_status_does_not_touch_pricing_or_unrelated_fields(
    monkeypatch,
):
    """The anomaly path must only ever touch review_required/review_reason
    (plus bookkeeping timestamps) on internal_products -- never pricing,
    content, image, or metadata_status fields."""
    internal_product = make_internal_product(
        pricing_status="APPROVED",
        content_status="APPROVED",
        image_status="APPROVED",
    )
    sync_record = make_sync_record()
    repository = make_repository(internal_product, sync_record)

    run_sync(monkeypatch, repository, sync_record, "private")

    updated_internal = repository.client.tables["internal_products"][0]
    assert updated_internal["pricing_status"] == "APPROVED"
    assert updated_internal["content_status"] == "APPROVED"
    assert updated_internal["image_status"] == "APPROVED"


def test_404_missing_product_writes_failed_not_a_missing_literal(monkeypatch):
    internal_product = make_internal_product(
        woocommerce_status=WooCommerceStatus.DRAFT_CREATED
    )
    sync_record = make_sync_record(
        woocommerce_status=WooCommerceSyncStatus.DRAFT_CREATED
    )
    repository = make_repository(internal_product, sync_record)

    result = run_sync(monkeypatch, repository, sync_record, None, http_status=404)

    assert result is False

    updated_sync = repository.client.tables["woocommerce_product_syncs"][0]
    updated_internal = repository.client.tables["internal_products"][0]

    # The previous behavior wrote the literal string "MISSING" here, which
    # is not a member of woocommerce_product_syncs.woocommerce_status's
    # CHECK constraint. A 404 unambiguously means this sync's remote
    # target is gone -- FAILED, not a guess.
    assert updated_sync["woocommerce_status"] == WooCommerceSyncStatus.FAILED
    assert updated_sync["woocommerce_status"] in ALL_WOOCOMMERCE_SYNC_STATUSES

    assert updated_internal["woocommerce_status"] == WooCommerceStatus.FAILED
    assert updated_internal["woocommerce_status"] in ALL_WOOCOMMERCE_STATUSES
    assert updated_internal["review_required"] is True
