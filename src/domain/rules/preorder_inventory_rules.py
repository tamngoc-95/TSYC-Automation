"""WooCommerce inventory policy for TSYC Fahasa pre-order products.

2026-10-16 owner request. Scope: ONLY a product whose candidate is a
Fahasa discovery (candidate_code FAHASA-..., origin FAHASA_DISCOVERY) still
recorded with supply status PREORDER_PENDING_SUPPLY_CONFIRMATION -- the
product is not in stock and supply is not confirmed. Every other product
keeps the existing manage_stock=false creation default.

Inventory sent (and nothing else):
    manage_stock   = true
    stock_quantity = 0
    backorders     = "yes"

stock_status is never sent. With managed stock WooCommerce derives it
(WC_Product_Data_Store_CPT / wc_update_product_stock_status): quantity <=
woocommerce_notify_no_stock_amount (0 on this store, read 2026-10-16) and
backorders allowed -> "onbackorder". Sending it as well could only
disagree with WooCommerce's own calculation. Observed on the live store
2026-10-16: the owner's own pre-order products carry exactly
manage_stock=true, stock_quantity=0, backorders="yes" -> "onbackorder".

What this policy never does: set or change a price (a product without a
price is not purchasable, so checkout stays closed until the owner prices
and publishes it), change status, promise a delivery date or stock
availability (no date/availability field or text is written), or touch
a product the owner has published, trashed, priced, or re-stocked.
No pre-order plugin is active on the store (plugin list read 2026-10-16),
so there is no plugin meta to keep consistent with.

Pure functions only.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

from src.domain.candidate_origin import FAHASA_DISCOVERY_ORIGIN, is_fahasa_discovery_candidate_code

PREORDER_SUPPLY_STATUS = "PREORDER_PENDING_SUPPLY_CONFIRMATION"

PREORDER_INVENTORY = MappingProxyType({"manage_stock": True, "stock_quantity": 0, "backorders": "yes"})
INVENTORY_FIELDS = tuple(PREORDER_INVENTORY)
EXPECTED_STOCK_STATUS = "onbackorder"

PRICE_FIELDS = ("regular_price", "sale_price", "price")
# Never sent with inventory; verified unchanged after every inventory PUT.
PRESERVED_FIELDS = PRICE_FIELDS + ("status", "sku", "name", "slug", "description", "short_description", "catalog_visibility")

# Classification codes.
ELIGIBLE = "ELIGIBLE"
NO_OP = "NO_OP"
NOT_PREORDER_PRODUCT = "NOT_FAHASA_PREORDER_PRODUCT"
REMOTE_MISSING = "REMOTE_MISSING"
SKU_MISMATCH = "REMOTE_SKU_MISMATCH"
NOT_DRAFT = "REMOTE_NOT_DRAFT"
NOT_AUTOMATION_OWNED = "REMOTE_NOT_AUTOMATION_OWNED"
OWNER_PRICED = "OWNER_MAINTAINED_PRICE"
OWNER_INVENTORY = "OWNER_MAINTAINED_INVENTORY"


def is_fahasa_preorder_candidate(candidate: Mapping[str, Any] | None) -> bool:
    """True only for a Fahasa-discovered candidate still pending supply."""
    if not candidate or not is_fahasa_discovery_candidate_code(candidate.get("candidate_code")):
        return False
    evidence = candidate.get("source_evidence") or {}
    return (
        isinstance(evidence, Mapping)
        and evidence.get("origin") == FAHASA_DISCOVERY_ORIGIN
        and evidence.get("supply_status") == PREORDER_SUPPLY_STATUS
    )


def build_inventory_payload() -> dict[str, Any]:
    """Exactly the inventory fields -- never a price, status or stock_status."""
    payload = dict(PREORDER_INVENTORY)
    assert set(payload) == set(INVENTORY_FIELDS)
    return payload


def _quantity(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def inventory_matches_policy(remote: Mapping[str, Any]) -> bool:
    return (
        remote.get("manage_stock") is True
        and _quantity(remote.get("stock_quantity")) == 0
        and remote.get("backorders") == "yes"
    )


def inventory_is_creation_default(remote: Mapping[str, Any], original_payload: Mapping[str, Any] | None) -> bool:
    """True when the remote inventory is still what TSYC created: the
    original payload's manage_stock=false (WooCommerce then stores no
    quantity and backorders "no")."""
    sent_manage_stock = (original_payload or {}).get("manage_stock", False)
    return (
        sent_manage_stock is False
        and remote.get("manage_stock") is False
        and _quantity(remote.get("stock_quantity")) in (None, 0)
        and remote.get("backorders") in (None, "", "no")
    )


def _remote_meta(remote: Mapping[str, Any], key: str) -> str | None:
    for item in remote.get("meta_data") or []:
        if isinstance(item, Mapping) and item.get("key") == key:
            return str(item.get("value"))
    return None


def classify_inventory_target(
    remote: Mapping[str, Any] | None,
    expected_product_id: int | str,
    expected_sku: str,
    internal_product_id: str,
    original_payload: Mapping[str, Any] | None,
) -> str:
    """
    ELIGIBLE only for an automation-owned, unpriced DRAFT whose inventory
    is still TSYC's creation default. NO_OP when the policy is already in
    place. Anything else is a stop code and the product is left as is:
    published/trashed/pending/private, SKU or id mismatch, not created by
    TSYC, a price entered by the owner, or inventory the owner changed.
    """
    if not remote or str(remote.get("id")) != str(expected_product_id):
        return REMOTE_MISSING
    if str(remote.get("sku") or "") != expected_sku:
        return SKU_MISMATCH
    if remote.get("status") != "draft":
        return NOT_DRAFT
    if _remote_meta(remote, "_tsyc_internal_product_id") != str(internal_product_id):
        return NOT_AUTOMATION_OWNED
    if any(str(remote.get(field) or "").strip() for field in ("regular_price", "sale_price")):
        return OWNER_PRICED
    if inventory_matches_policy(remote):
        return NO_OP
    if inventory_is_creation_default(remote, original_payload):
        return ELIGIBLE
    return OWNER_INVENTORY


def verify_inventory_after(before: Mapping[str, Any], after: Mapping[str, Any] | None) -> list[str]:
    """Problems after an inventory PUT (empty = verified)."""
    if not after:
        return ["remote product could not be re-read"]
    problems = [
        f"{field} changed"
        for field in PRESERVED_FIELDS
        if (before.get(field) or "") != (after.get(field) or "")
    ]
    if [image.get("id") for image in before.get("images") or []] != [image.get("id") for image in after.get("images") or []]:
        problems.append("images changed")
    if not inventory_matches_policy(after):
        problems.append(
            "inventory not applied "
            f"(manage_stock={after.get('manage_stock')!r}, stock_quantity={after.get('stock_quantity')!r}, "
            f"backorders={after.get('backorders')!r})"
        )
    if after.get("stock_status") != EXPECTED_STOCK_STATUS:
        problems.append(f"stock_status is {after.get('stock_status')!r}, expected {EXPECTED_STOCK_STATUS!r}")
    return problems
