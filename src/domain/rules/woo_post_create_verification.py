"""Post-create verification of a newly created WooCommerce draft.

CLAUDE.md 6.3 (owner-adopted 2026-11-23, applied 2026-11-25): "Re-read the
created product, verify its Woo ID and draft fields, and persist the
confirmed synchronization result."

create_woocommerce_draft.py re-reads the product it just created
(GET products/<id>, context=edit) and searches the exact SKU (any + trash)
right after the POST. It then calls verify_created_draft() with:
  - the payload it sent,
  - the product row it created the product for,
  - the fresh remote product, and
  - the SKU search hits.

Every check compares the remote product with what this run sent, so the
verification never relies on the POST response. It fails closed: a
missing field counts as a mismatch, never as a pass.

Pure functions only; no I/O.
"""
from __future__ import annotations

import html
import re
from typing import Any, Iterable, Mapping, Sequence

from src.domain.rules import preorder_inventory_rules

VERIFIER_VERSION = "1.0.0"

VERIFIED = "VERIFIED"
FAILED = "FAILED"

INTERNAL_PRODUCT_META_KEY = "_tsyc_internal_product_id"

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def normalize_text(value: Any) -> str:
    """Markup-insensitive comparison form: tags removed, entities decoded,
    whitespace collapsed. WordPress may re-wrap or re-encode stored HTML
    without changing the words; it may not change the words."""
    text = html.unescape(_TAG_RE.sub(" ", str(value or "")))
    return _SPACE_RE.sub(" ", text).strip()


def _image_ids(images: Iterable[Mapping[str, Any]] | None) -> list[int]:
    ids: list[int] = []
    for image in images or []:
        try:
            ids.append(int(image.get("id")))
        except (TypeError, ValueError):
            ids.append(-1)
    return ids


def _meta_value(remote: Mapping[str, Any], key: str) -> str | None:
    for item in remote.get("meta_data") or []:
        if isinstance(item, Mapping) and item.get("key") == key:
            return str(item.get("value"))
    return None


def verify_created_draft(
    *,
    expected_product_id: int,
    sent_payload: Mapping[str, Any],
    internal_product_id: str,
    remote: Mapping[str, Any] | None,
    sku_hits: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Return {"status": VERIFIED|FAILED, "checks": {name: bool},
    "failed": [names], "verifier_version": ...}. sku_hits is the union of
    exact-SKU search results under status any and trash. None means the
    search could not be read, and the check fails."""
    remote = remote or {}
    sku = str(sent_payload.get("sku") or "")
    preorder = all(field in sent_payload for field in preorder_inventory_rules.INVENTORY_FIELDS)

    checks: dict[str, bool] = {
        "product_read": bool(remote),
        "woo_id": _safe_int(remote.get("id")) == int(expected_product_id),
        "sku": bool(sku) and str(remote.get("sku") or "") == sku,
        "internal_product_link": _meta_value(remote, INTERNAL_PRODUCT_META_KEY) == str(internal_product_id),
        "name": normalize_text(remote.get("name")) == normalize_text(sent_payload.get("name")),
        "status_draft": remote.get("status") == "draft",
        "prices_empty": all(
            field in remote and str(remote.get(field) or "") == ""
            for field in ("regular_price", "sale_price")
        ) and str(remote.get("price") or "") == "",
        "images": bool(sent_payload.get("images"))
        and _image_ids(remote.get("images")) == _image_ids(sent_payload.get("images")),
        "description": normalize_text(remote.get("description")) == normalize_text(sent_payload.get("description")),
        "short_description": normalize_text(remote.get("short_description"))
        == normalize_text(sent_payload.get("short_description")),
        "no_duplicate_sku": sku_hits is not None
        and [_safe_int(hit.get("id")) for hit in sku_hits if str(hit.get("sku") or "") == sku]
        == [int(expected_product_id)],
    }

    if preorder:
        checks["manage_stock"] = remote.get("manage_stock") is True
        checks["stock_quantity_zero"] = _safe_int(remote.get("stock_quantity")) == 0
        checks["backorders_yes"] = remote.get("backorders") == "yes"
    else:
        checks["manage_stock"] = remote.get("manage_stock") == sent_payload.get("manage_stock")

    failed = [name for name, passed in checks.items() if not passed]

    return {
        "status": VERIFIED if not failed else FAILED,
        "checks": checks,
        "failed": failed,
        "preorder": preorder,
        "verifier_version": VERIFIER_VERSION,
    }


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
