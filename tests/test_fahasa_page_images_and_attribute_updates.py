"""Fahasa supplementary images and Woo attribute updates (2026-10-16).

Protects: multiple Fahasa page images are recorded with provenance and a
role, deduplicated across CDN hosts; sibling-volume covers are blocked and
gallery/preview images, although owner-authorized (CLAUDE.md 14.8), are not
usable until stored and visually confirmed; the draft content updater
syncs the author attribute but never over an owner's attribute edit, and
never touches images. Offline.
"""
from __future__ import annotations

from typing import Any

import pytest

import update_woocommerce_draft_content as uwd
from src.domain.decisions import Outcome
from src.domain.rules import image_rules

COVER = "https://cdn1.fahasa.com/media/catalog/product/b/i/biet_doi_cho_bay_bia.jpg"
SUPPLEMENTARY = "https://cdn1.fahasa.com/media/catalog/product/b/i/biet_doi_cho_bay_bia_1.jpg"
SIBLING = "https://cdn1.fahasa.com/media/catalog/product/h/o/ho_ng_t_b_1_.jpg"


def test_page_images_are_classified_with_provenance_and_deduplicated():
    entries = image_rules.classify_fahasa_page_images(
        COVER,
        [
            COVER.replace("cdn1", "cdn0") + "?v=2",  # same asset, other CDN host
            SUPPLEMENTARY,
            SIBLING,
            "https://cdn1.fahasa.com/skin/frontend/ma_vanese/fahasa/images/logo.png",  # not a product asset
            "https://example.com/media/catalog/product/x.jpg",  # not Fahasa
            SUPPLEMENTARY,
        ],
        [SIBLING],
    )
    assert [(entry["role"], entry["url"]) for entry in entries] == [
        (image_rules.FAHASA_IMAGE_ROLE_COVER, COVER),
        (image_rules.FAHASA_IMAGE_ROLE_SUPPLEMENTARY, SUPPLEMENTARY),
        (image_rules.FAHASA_IMAGE_ROLE_OTHER_PRODUCT, SIBLING),
    ]
    assert all(entry["asset_path"].startswith("/media/catalog/product/") for entry in entries)


def test_sibling_volume_cover_is_blocked():
    decision = image_rules.evaluate_fahasa_supplementary_image(
        {"url": SIBLING, "asset_path": "/x", "role": image_rules.FAHASA_IMAGE_ROLE_OTHER_PRODUCT}
    )
    assert decision.outcome == Outcome.BLOCKED
    assert decision.rule_code == image_rules.IMAGE_FAHASA_OTHER_PRODUCT


def test_unstored_supplementary_image_is_not_yet_usable():
    # Rights are owner-authorized (CLAUDE.md 14.8), but relevance is proven
    # only on the stored, visually reviewed image.
    decision = image_rules.evaluate_fahasa_supplementary_image(
        {"url": SUPPLEMENTARY, "asset_path": "/y", "role": image_rules.FAHASA_IMAGE_ROLE_SUPPLEMENTARY}
    )
    assert decision.outcome == Outcome.REVIEW_REQUIRED
    assert decision.rule_code == image_rules.IMAGE_FAHASA_SUPPLEMENTARY_NOT_AUTHORIZED
    assert decision.evidence["url"] == SUPPLEMENTARY  # provenance preserved
    assert "rights_status" not in decision.evidence


def test_image_authorization_scope_covers_all_product_images():
    scope = image_rules.FAHASA_IMAGE_AUTHORIZATION["scope"].lower()
    for kind in ("cover", "gallery", "interior", "preview"):
        assert kind in scope
    assert image_rules.FAHASA_IMAGE_AUTHORIZATION["rights_status"] == "SUPPLIER_APPROVED"


# --- updater: attributes protected like the text fields ---------------------------

SKU = "TSYC-FAHASA-2026-002-CAN-0001"
ORIGINAL = {"description": "<p>Cũ.</p>", "short_description": "<p>Ngắn.</p>"}
AUTHOR_ATTRIBUTE = [{"name": "Tác giả", "position": 0, "visible": True, "variation": False, "options": ["Văn Thành Lê"]}]
NEW = {"description": "<p>Mới.</p>", "short_description": "<p>Ngắn.</p>", "attributes": AUTHOR_ATTRIBUTE}


def _remote(**overrides: Any) -> dict[str, Any]:
    row = {"id": 101, "sku": SKU, "status": "draft", "attributes": [], "images": [{"id": 7}], **ORIGINAL}
    row.update(overrides)
    return row


def test_attributes_are_built_only_with_the_local_product():
    content = {"long_description": "Mô tả.", "short_description": "Ngắn.", "product_details": "Tác giả: Văn Thành Lê"}
    assert set(uwd.build_update_payload(content)) == {"description", "short_description"}
    payload = uwd.build_update_payload(content, {"author": "Văn Thành Lê"})
    assert payload["attributes"][0]["options"] == ["Văn Thành Lê"]
    assert not {"images", "status", "regular_price", "sale_price", "price", "stock_quantity"} & set(payload)


@pytest.mark.parametrize(
    "remote, expected",
    [
        (_remote(), None),  # attributes still what TSYC sent (none)
        (_remote(description=NEW["description"], attributes=AUTHOR_ATTRIBUTE), uwd.NO_OP),
        (_remote(attributes=[{"name": "Tác giả", "options": ["Tên do chủ tiệm sửa"]}]), uwd.MANUAL_EDIT),
    ],
)
def test_owner_attribute_edit_is_protected(remote, expected):
    assert uwd.classify_remote(remote, SKU, ORIGINAL, NEW) == expected


def test_verification_detects_unapplied_attributes_and_image_changes():
    before = _remote()
    applied = _remote(description=NEW["description"], attributes=AUTHOR_ATTRIBUTE)
    assert uwd.verify_after_update(before, applied, NEW, SKU) == []
    assert "attributes not applied" in uwd.verify_after_update(before, _remote(description=NEW["description"]), NEW, SKU)
    assert "images changed" in uwd.verify_after_update(before, {**applied, "images": []}, NEW, SKU)
