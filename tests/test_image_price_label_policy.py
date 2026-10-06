"""Regression tests: historical shop photos with a visible selling price
never become PRIMARY/GALLERY automatically (shop owner instruction
2026-10-02). Evidence-based detection only (no OCR in this repository)."""
from __future__ import annotations

from typing import Any

import pytest

from pipeline_state import derive_candidate_state, load_candidate_bundle
from src.domain.rules import image_price_rules

from support.fake_supabase import FakeSupabaseRepository

CANDIDATE_ID = "c0ffee00-0000-0000-0000-000000000001"
CODE = "FB-HIST-2026-IMG-001-CAN-0006"
OWN_MEDIA = "your_facebook_activity/posts/media/Shop_1/4094979990815503.jpg"


# --- text detection ----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "5,99€ visible",
        "9,99 € visible",
        "15.99 EUR visible",
        "4-volume combo, 24€/combo visible",
        "23,99",
        "€ 12 sticker",
        "17-volume full-series combo, 4,99€/cuốn, 55€/17 cuốn visible",
    ],
)
def test_price_mentions_are_detected(text):
    assert image_price_rules.text_mentions_price(text)


@pytest.mark.parametrize(
    "text",
    [
        "cover photographed",
        "cover photographed, level label 'Cấp độ 4-1 / Bạn bè của tôi'",
        "cover photographed, 'Dành cho trẻ em từ 3 tuổi trở lên', 5-9 tuổi",
        "tập 1 & 2, NXB Kim Đồng",
        "4 combo boxes photographed",
        None,
    ],
)
def test_non_price_evidence_is_not_flagged(text):
    assert not image_price_rules.text_mentions_price(text)


def test_explicit_visual_verdict_wins_over_evidence_text():
    assert (
        image_price_rules.candidate_price_label_status(
            {"price_label_visible": False, "evidence_text": "5,99€ visible"}
        )
        == image_price_rules.PRICE_LABEL_EXPLICITLY_ABSENT
    )
    assert (
        image_price_rules.candidate_price_label_status(
            {"price_label_visible": True, "evidence_text": "cover photographed"}
        )
        == image_price_rules.PRICE_LABEL_VISIBLE
    )
    assert (
        image_price_rules.candidate_price_label_status({"evidence_text": "cover photographed"})
        == image_price_rules.PRICE_LABEL_NOT_RECORDED
    )


def test_only_own_export_images_carry_the_candidate_verdict():
    candidate = {
        "source_evidence": {"evidence_text": "5,99€ visible", "local_media_paths": [OWN_MEDIA]},
    }
    own = {"source_url": f"facebook-export-media://{OWN_MEDIA}"}
    reference = {"source_url": "https://cdn.fahasa.com/x.jpg"}
    other_post = {"source_url": "facebook-export-media://your_facebook_activity/posts/media/Shop_1/other.jpg"}

    assert image_price_rules.image_has_price_label(own, candidate)
    assert not image_price_rules.image_has_price_label(reference, candidate)
    assert not image_price_rules.image_has_price_label(other_post, candidate)


# --- pipeline routing -------------------------------------------------------------


def _candidate(evidence_text: str) -> dict[str, Any]:
    return {
        "candidate_id": CANDIDATE_ID,
        "candidate_code": CODE,
        "raw_page_id": None,
        "candidate_type": "SINGLE_BOOK",
        "identity_status": "IDENTITY_PENDING",
        "extracted_title": "Tớ Có Một Tình Bạn Muốn Cho Thuê",
        "source_evidence": {"evidence_text": evidence_text, "local_media_paths": [OWN_MEDIA]},
    }


def _product() -> dict[str, Any]:
    return {
        "internal_product_id": "ip-1",
        "candidate_id": CANDIDATE_ID,
        "product_code": f"TSYC-{CODE}",
        "isbn": None,
        "weight_grams": None,
        "content_status": "APPROVED",
        "image_status": "PENDING",
        "woocommerce_status": "NOT_CREATED",
    }


def _own_image(**overrides: Any) -> dict[str, Any]:
    row = {
        "image_id": "img-own",
        "candidate_id": CANDIDATE_ID,
        "source_type": "FACEBOOK",
        "source_url": f"facebook-export-media://{OWN_MEDIA}",
        "usage_rights_status": "RIGHTS_UNKNOWN",
        "reference_id": None,
        "image_role": "FRONT_COVER",
        "created_at": "2026-10-02T10:00:00+00:00",
    }
    row.update(overrides)
    return row


def _reference(**overrides: Any) -> dict[str, Any]:
    row = {
        "reference_id": "ref-fahasa",
        "candidate_id": CANDIDATE_ID,
        "source_type": "FAHASA",
        "source_url_id": "su-1",
        "match_decision": "POSSIBLE_MATCH",
        "reference_title": "Tớ Có Một Tình Bạn Muốn Cho Thuê",
        "reference_image_url": "https://cdn.fahasa.com/cover.jpg",
    }
    row.update(overrides)
    return row


def _state(candidate, images, references=()):
    repository = FakeSupabaseRepository(
        tables={
            "product_candidates": [candidate],
            "internal_products": [_product()],
            "product_images": list(images),
            "product_references": list(references),
        }
    )
    return derive_candidate_state(load_candidate_bundle(repository, CODE))


def test_price_labelled_only_image_without_reference_is_enrichment_item():
    state = _state(_candidate("5,99€ visible"), [_own_image()])

    assert state.derived_state == "IMAGE_PRICE_LABEL_REPLACEMENT_NEEDED"
    assert state.blocked is True
    assert state.human_gate is False
    assert state.auto_main_image_id is None


def test_price_labelled_only_image_routes_to_reference_image_fallback():
    state = _state(_candidate("5,99€ visible"), [_own_image()], [_reference()])

    assert state.derived_state == "IMAGE_REFERENCE_FALLBACK_PENDING_HISTORICAL"
    assert state.auto_reference_id == "ref-fahasa"


def test_price_free_reference_image_becomes_primary_and_price_photo_is_not_gallery():
    reference_image = {
        "image_id": "img-ref",
        "candidate_id": CANDIDATE_ID,
        "source_type": "FAHASA",
        "source_url": "https://cdn.fahasa.com/cover.jpg",
        "usage_rights_status": "SUPPLIER_APPROVED",
        "reference_id": "ref-fahasa",
        "image_role": None,
        "created_at": "2026-10-02T11:00:00+00:00",
    }
    state = _state(_candidate("5,99€ visible"), [_own_image(), reference_image], [_reference()])

    assert state.derived_state == "IMAGE_APPROVAL_PENDING_HISTORICAL"
    assert state.auto_main_image_id == "img-ref"
    assert state.auto_gallery_images == ()


def test_unrecorded_price_keeps_existing_behavior():
    """No price in evidence -> unchanged pre-existing selection (unknown is
    never treated as a rejection)."""
    state = _state(_candidate("cover photographed"), [_own_image()])

    assert state.derived_state == "IMAGE_APPROVAL_PENDING_HISTORICAL"
    assert state.auto_main_image_id == "img-own"
    assert state.auto_rights_status == "STORE_OWNED"
