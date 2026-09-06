"""
Regression tests for scripts/audit_pipeline_state.py's audit_woocommerce()
REMOTE_WOO_ID_WITH_LOCAL_STATUS_MISMATCH check.

Confirmed real incident (2026-09-06): FB-HIST-2026-AUTOIMPORT-CAN-0040 was
legitimately synchronized by scripts/sync_woocommerce_product_status.py
from a remote "publish" status into internal_products.woocommerce_status =
PUBLISHED (that script's own SUPPORTED_REMOTE_STATUS_MAPPING maps remote
"publish" to the canonical PUBLISHED status -- see
scripts/sync_woocommerce_product_status.py). The audit rule previously
only accepted DRAFT_CREATED as a valid local status once a WooCommerce
product id existed, so this fully-reconciled, non-automation-caused
publish tripped a false ERROR and blocked scripts/preflight_pipeline.py.

The fix imports LOCAL_STATUSES_VALID_WITH_REMOTE_ID from
sync_woocommerce_product_status.py (derived from that module's own
SUPPORTED_REMOTE_STATUS_MAPPING, never a second hand-maintained list) so
the audit rule and the sync writer can never silently diverge again.

Pure-function, fully offline: audit_woocommerce() takes plain lists, no
Supabase/network access.
"""

from __future__ import annotations

from typing import Any

import audit_pipeline_state as audit
from sync_woocommerce_product_status import LOCAL_STATUSES_VALID_WITH_REMOTE_ID
from src.domain.woocommerce_status import WooCommerceStatus


def _product(
    internal_product_id: str,
    woocommerce_status: str,
    product_code: str | None = None,
) -> dict[str, Any]:
    return {
        "internal_product_id": internal_product_id,
        "product_code": product_code or f"TSYC-TEST-{internal_product_id}",
        "woocommerce_status": woocommerce_status,
    }


def _sync(
    internal_product_id: str,
    woocommerce_product_id: int | None = 1234,
    response_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "internal_product_id": internal_product_id,
        "woocommerce_product_id": woocommerce_product_id,
        "response_payload": response_payload,
    }


def _codes(issues: list[dict[str, str]]) -> list[str]:
    return [issue["code"] for issue in issues]


def test_canonical_synced_statuses_include_draft_created_and_published():
    """Derived from SUPPORTED_REMOTE_STATUS_MAPPING, not hand-duplicated --
    exactly the two statuses the task calls out, no more, no less."""
    assert LOCAL_STATUSES_VALID_WITH_REMOTE_ID == frozenset(
        {WooCommerceStatus.DRAFT_CREATED, WooCommerceStatus.PUBLISHED}
    )


def test_draft_created_with_remote_id_is_not_flagged():
    """1: DRAFT_CREATED + Woo id => no error."""
    products = [_product("p1", WooCommerceStatus.DRAFT_CREATED)]
    syncs = [_sync("p1")]

    issues: list[dict[str, str]] = []
    audit.audit_woocommerce(products, syncs, issues)

    assert "REMOTE_WOO_ID_WITH_LOCAL_STATUS_MISMATCH" not in _codes(issues)
    assert issues == []


def test_published_with_remote_id_is_not_flagged():
    """2: PUBLISHED + Woo id => no error (the confirmed CAN-0040 case:
    sync_woocommerce_product_status.py legitimately wrote PUBLISHED after
    observing a remote "publish" status it never caused)."""
    products = [_product("p2", WooCommerceStatus.PUBLISHED)]
    syncs = [_sync("p2", woocommerce_product_id=3798)]

    issues: list[dict[str, str]] = []
    audit.audit_woocommerce(products, syncs, issues)

    assert "REMOTE_WOO_ID_WITH_LOCAL_STATUS_MISMATCH" not in _codes(issues)
    assert issues == []


def test_ready_for_draft_with_remote_id_is_still_an_error():
    """3: READY_FOR_DRAFT + Woo id => error. A remote id must never
    coexist with a pre-draft local status -- the fix must not widen the
    accepted set beyond the two canonical synced statuses."""
    products = [_product("p3", WooCommerceStatus.READY_FOR_DRAFT)]
    syncs = [_sync("p3")]

    issues: list[dict[str, str]] = []
    audit.audit_woocommerce(products, syncs, issues)

    mismatch_issues = [
        issue
        for issue in issues
        if issue["code"] == "REMOTE_WOO_ID_WITH_LOCAL_STATUS_MISMATCH"
    ]
    assert len(mismatch_issues) == 1
    assert mismatch_issues[0]["severity"] == "ERROR"
    assert mismatch_issues[0]["entity"] == products[0]["product_code"]


def test_recovery_required_sync_behavior_is_preserved():
    """4: a FAILED/recovery sync (response_payload.recovery_required=True)
    with a remote id still raises WOO_RECOVERY_REQUIRED exactly as before
    -- this fix touches only the local-status allowlist, never the
    recovery check. FAILED is also not a canonical synced status, so the
    mismatch check still fires too (a recovery product's local status was
    never PUBLISHED/DRAFT_CREATED-clean by definition)."""
    products = [_product("p4", WooCommerceStatus.FAILED)]
    syncs = [
        _sync(
            "p4",
            woocommerce_product_id=42,
            response_payload={"recovery_required": True},
        )
    ]

    issues: list[dict[str, str]] = []
    audit.audit_woocommerce(products, syncs, issues)

    codes = _codes(issues)
    assert "WOO_RECOVERY_REQUIRED" in codes
    assert "REMOTE_WOO_ID_WITH_LOCAL_STATUS_MISMATCH" in codes


def test_unrelated_status_with_remote_id_is_not_falsely_accepted():
    """5: no false acceptance of an unrelated status -- NOT_CREATED
    (a product that should never have a remote id at all) still errors."""
    products = [_product("p5", WooCommerceStatus.NOT_CREATED)]
    syncs = [_sync("p5", woocommerce_product_id=99)]

    issues: list[dict[str, str]] = []
    audit.audit_woocommerce(products, syncs, issues)

    assert "REMOTE_WOO_ID_WITH_LOCAL_STATUS_MISMATCH" in _codes(issues)


def test_draft_created_without_any_remote_id_check_is_unaffected():
    """Unrelated check DRAFT_CREATED_WITHOUT_SINGLE_REMOTE_ID must keep
    firing exactly as before -- this fix must not weaken it."""
    products = [_product("p6", WooCommerceStatus.DRAFT_CREATED)]
    syncs: list[dict[str, Any]] = []

    issues: list[dict[str, str]] = []
    audit.audit_woocommerce(products, syncs, issues)

    assert "DRAFT_CREATED_WITHOUT_SINGLE_REMOTE_ID" in _codes(issues)
    assert "REMOTE_WOO_ID_WITH_LOCAL_STATUS_MISMATCH" not in _codes(issues)
