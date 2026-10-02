"""Confirmed remote removal (deleted / trashed) of a TSYC-linked
WooCommerce product -- an intentional, terminal remote state.

Background (2026-10-02): products the shop owner permanently deleted
(31) or moved to trash (16) directly in WooCommerce were left locally as
FAILED / UNSUPPORTED_REMOTE_STATUS and surfaced as generic audit ERRORs
(REMOTE_WOO_ID_WITH_LOCAL_STATUS_MISMATCH) and RECOVERY_REVIEW items. A
confirmed removal is not an uncertain remote state: it is the shop
owner's decision, and the product must NOT be recreated automatically.

Representation (no schema change -- both woocommerce_status columns keep
their canonical CHECK-constrained values):

  woocommerce_product_syncs
    woocommerce_status     = FAILED (the attempt's linked product is gone)
    woocommerce_product_id = KEPT (historical provenance; also makes
                             create_woocommerce_draft.py refuse to create)
    error_code             = REMOTE_PRODUCT_DELETED | REMOTE_PRODUCT_TRASHED
    response_payload[REMOVAL_MARKER_KEY] = evidence record (below)
  internal_products
    woocommerce_status     = FAILED
    product_metadata[REMOVAL_MARKER_KEY] = the same evidence record

pipeline_state derives REMOTE_REMOVED (terminal, no human gate, never
dispatched) and the audit reports a WARNING instead of the mismatch ERROR
-- but only when the marker is present AND consistent with the row (same
woocommerce_product_id, no active recovery_required flag).

Confirmation rules (evaluate_remote_removal), never a guess:
  REMOTE_DELETED  GET-by-id is a definite 404, AND an exact-SKU search
                  with status=any AND with status=trash both return zero
                  products.
  REMOTE_TRASHED  GET-by-id returns the same id + same SKU with
                  status == "trash", AND an exact-SKU status=any search
                  returns no other (live) product.
Any error, other status code, SKU mismatch, or live duplicate -> None:
the product stays in the normal recovery path.

Pure functions only.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

REMOTE_DELETED = "REMOTE_DELETED"
REMOTE_TRASHED = "REMOTE_TRASHED"
REMOTE_REMOVAL_STATES = frozenset({REMOTE_DELETED, REMOTE_TRASHED})

REMOVAL_MARKER_KEY = "remote_removal"

REMOVAL_ERROR_CODES = {
    REMOTE_DELETED: "REMOTE_PRODUCT_DELETED",
    REMOTE_TRASHED: "REMOTE_PRODUCT_TRASHED",
}

# Derived pipeline state for a confirmed removal (pipeline_state.py).
DERIVED_STATE_REMOTE_REMOVED = "REMOTE_REMOVED"


def evaluate_remote_removal(
    woocommerce_product_id: int | str,
    expected_sku: str,
    get_http_status: int | None,
    get_body: Mapping[str, Any] | None,
    sku_any_matches: Sequence[Mapping[str, Any]] | None,
    sku_trash_matches: Sequence[Mapping[str, Any]] | None,
) -> tuple[str | None, str]:
    """(state, reason). state is None unless removal is confirmed.
    A None match list means that lookup failed/was uncertain."""
    if not expected_sku:
        return None, "No stored SKU; remote identity cannot be confirmed."

    if sku_any_matches is None:
        return None, "Exact-SKU search (status=any) was uncertain."

    live_others = [
        match for match in sku_any_matches
        if str(match.get("id")) != str(woocommerce_product_id)
    ]
    if live_others:
        return None, (
            "Another live WooCommerce product carries this SKU "
            f"(ids {[m.get('id') for m in live_others]}); reconcile, never assume removal."
        )

    if get_http_status == 404:
        if sku_any_matches:
            return None, "GET-by-id is 404 but an exact-SKU search still finds the product."
        if sku_trash_matches is None:
            return None, "Exact-SKU search (status=trash) was uncertain."
        if sku_trash_matches:
            return None, "GET-by-id is 404 but the SKU is found in trash."
        return REMOTE_DELETED, (
            "GET-by-id returned 404 and exact-SKU searches (any, trash) found no product."
        )

    if get_http_status == 200 and isinstance(get_body, Mapping):
        if str(get_body.get("id")) != str(woocommerce_product_id):
            return None, "GET-by-id returned a different product id."
        if str(get_body.get("sku") or "") != expected_sku:
            return None, "Remote SKU does not match the stored SKU."
        if get_body.get("status") == "trash":
            return REMOTE_TRASHED, (
                "Remote product (same id and SKU) has status 'trash' and no live "
                "product carries the SKU."
            )
        return None, f"Remote product is not removed (status {get_body.get('status')!r})."

    return None, f"GET-by-id result is uncertain (HTTP {get_http_status})."


def build_removal_marker(
    state: str,
    woocommerce_product_id: int | str,
    sku: str,
    reason: str,
    checked_at: str,
    checked_by: str,
    remote_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The evidence record stored on both rows."""
    if state not in REMOTE_REMOVAL_STATES:
        raise ValueError(f"Not a remote-removal state: {state!r}")
    marker: dict[str, Any] = {
        "state": state,
        "woocommerce_product_id": int(woocommerce_product_id),
        "sku": sku,
        "reason": reason,
        "confirmed_at": checked_at,
        "confirmed_by": checked_by,
        "intentional": True,
        "recreate": False,
    }
    if remote_snapshot:
        marker["remote_snapshot"] = {
            key: remote_snapshot.get(key)
            for key in ("id", "sku", "status", "name", "date_modified_gmt")
        }
    return marker


def confirmed_remote_removal(sync: Mapping[str, Any] | None) -> str | None:
    """REMOTE_DELETED / REMOTE_TRASHED when the sync row carries a
    consistent removal marker; otherwise None."""
    if not sync or not sync.get("woocommerce_product_id"):
        return None
    payload = sync.get("response_payload")
    if not isinstance(payload, Mapping):
        return None
    if payload.get("recovery_required") is True:
        return None
    marker = payload.get(REMOVAL_MARKER_KEY)
    if not isinstance(marker, Mapping):
        return None
    state = marker.get("state")
    if state not in REMOTE_REMOVAL_STATES:
        return None
    if str(marker.get("woocommerce_product_id")) != str(sync.get("woocommerce_product_id")):
        return None
    return state
