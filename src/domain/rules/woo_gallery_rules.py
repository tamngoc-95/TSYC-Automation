"""Gallery enrichment of existing TSYC WooCommerce drafts (CLAUDE.md 14.8.4).

A draft may receive its approved Fahasa gallery images only while its
images are still exactly the set TSYC itself last applied: any image the
owner added, removed or reordered (a different featured image) means the
gallery is owner-curated and is never touched. The only field ever sent is
"images" -- never status, prices, inventory or text -- and the re-read must
prove that everything else is unchanged.

Pure functions only.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

ELIGIBLE = "ELIGIBLE"
REMOTE_NOT_FOUND = "REMOTE_NOT_FOUND"
REMOTE_IDENTITY_MISMATCH = "REMOTE_IDENTITY_MISMATCH"
NOT_A_DRAFT = "NOT_A_DRAFT"
NOT_TSYC_PRODUCT = "NOT_TSYC_PRODUCT"
OWNER_CURATED_IMAGES = "OWNER_CURATED_IMAGES"

TSYC_META_KEY = "_tsyc_internal_product_id"
GALLERY_FIELDS = ("images",)
# Fields the enrichment must leave exactly as they were.
PRESERVED_FIELDS = (
    "status",
    "sku",
    "name",
    "description",
    "short_description",
    "regular_price",
    "sale_price",
    "price",
    "manage_stock",
    "stock_quantity",
    "backorders",
    "stock_status",
    "attributes",
)


def remote_image_ids(product: Mapping[str, Any] | None) -> list[int]:
    return [int(image["id"]) for image in (product or {}).get("images") or [] if image.get("id")]


def tsyc_applied_media_ids(response_payload: Mapping[str, Any] | None) -> list[int]:
    """The media ids TSYC last applied to the remote product, in order:
    the recorded enrichment, else the media uploaded at draft creation."""
    payload = response_payload or {}
    applied = payload.get("applied_image_media_ids")
    if isinstance(applied, list) and applied:
        return [int(media_id) for media_id in applied]
    return [
        int(media["wordpress_media_id"])
        for media in payload.get("uploaded_media") or []
        if isinstance(media, Mapping) and media.get("wordpress_media_id")
    ]


def _meta_value(product: Mapping[str, Any], key: str) -> Any:
    for item in product.get("meta_data") or []:
        if isinstance(item, Mapping) and item.get("key") == key:
            return item.get("value")
    return None


def classify_gallery_target(
    remote: Mapping[str, Any] | None,
    product_id: int | str,
    sku: str,
    internal_product_id: str,
    applied_media_ids: Sequence[int],
) -> str:
    """ELIGIBLE, or the stop code that isolates this product."""
    if not remote:
        return REMOTE_NOT_FOUND
    if str(remote.get("id")) != str(product_id) or str(remote.get("sku") or "") != str(sku):
        return REMOTE_IDENTITY_MISMATCH
    if remote.get("status") != "draft":
        return NOT_A_DRAFT
    if str(_meta_value(remote, TSYC_META_KEY) or "") != str(internal_product_id):
        return NOT_TSYC_PRODUCT
    if not applied_media_ids or remote_image_ids(remote) != [int(media_id) for media_id in applied_media_ids]:
        return OWNER_CURATED_IMAGES
    return ELIGIBLE


def build_gallery_payload(media_ids: Sequence[int]) -> dict[str, Any]:
    """Exactly {"images": [...]} -- featured image first."""
    payload = {"images": [{"id": int(media_id)} for media_id in media_ids]}
    assert set(payload) == set(GALLERY_FIELDS)
    return payload


def verify_gallery_after(
    before: Mapping[str, Any],
    after: Mapping[str, Any] | None,
    expected_media_ids: Sequence[int],
) -> list[str]:
    """Problems found on the re-read; [] when the gallery is applied and
    nothing else changed."""
    if not after:
        return ["product not found on re-read"]
    problems = []
    if remote_image_ids(after) != [int(media_id) for media_id in expected_media_ids]:
        problems.append(f"images are {remote_image_ids(after)}, expected {list(expected_media_ids)}")
    if after.get("status") != "draft":
        problems.append(f"status is {after.get('status')!r}, expected 'draft'")
    for field in PRESERVED_FIELDS:
        if before.get(field) != after.get(field):
            problems.append(f"{field} changed")
    return problems
