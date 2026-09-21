"""
Offline tests for review_product_images.py's PRIMARY + GALLERY image
approval (historical multi-image ownership resolver, Phases 5-6).

validate_gallery_request() is pure. approve_main_and_gallery_images() is
exercised against FakeSupabaseRepository -- no live Supabase dependency.
"""

from __future__ import annotations

import pytest

import review_product_images as review
from src.domain.image_status import ImageStatus

from support.fake_supabase import FakeSupabaseRepository


CANDIDATE_ID = "cand-1"


def _images() -> list[dict]:
    return [
        {"image_id": "img-main", "candidate_id": CANDIDATE_ID},
        {"image_id": "img-gallery-1", "candidate_id": CANDIDATE_ID},
        {"image_id": "img-gallery-2", "candidate_id": CANDIDATE_ID},
        {"image_id": "img-rejected", "candidate_id": CANDIDATE_ID},
    ]


# --- validate_gallery_request ---------------------------------------------


def test_validate_gallery_request_returns_ordered_pairs():
    pairs = review.validate_gallery_request(
        images=_images(),
        main_image_id="img-main",
        gallery_image_ids=["img-gallery-1", "img-gallery-2"],
        gallery_rights_statuses=["STORE_OWNED", "SUPPLIER_APPROVED"],
    )

    assert pairs == [
        ("img-gallery-1", "STORE_OWNED"),
        ("img-gallery-2", "SUPPLIER_APPROVED"),
    ]


def test_validate_gallery_request_empty_when_no_gallery_images():
    pairs = review.validate_gallery_request(
        images=_images(),
        main_image_id="img-main",
        gallery_image_ids=[],
        gallery_rights_statuses=[],
    )

    assert pairs == []


def test_validate_gallery_request_rejects_mismatched_counts():
    with pytest.raises(RuntimeError, match="same number of times"):
        review.validate_gallery_request(
            images=_images(),
            main_image_id="img-main",
            gallery_image_ids=["img-gallery-1"],
            gallery_rights_statuses=["STORE_OWNED", "SUPPLIER_APPROVED"],
        )


def test_validate_gallery_request_rejects_main_image_repeated_as_gallery():
    with pytest.raises(RuntimeError, match="distinct from the main image"):
        review.validate_gallery_request(
            images=_images(),
            main_image_id="img-main",
            gallery_image_ids=["img-main"],
            gallery_rights_statuses=["STORE_OWNED"],
        )


def test_validate_gallery_request_rejects_duplicate_gallery_ids():
    with pytest.raises(RuntimeError, match="distinct"):
        review.validate_gallery_request(
            images=_images(),
            main_image_id="img-main",
            gallery_image_ids=["img-gallery-1", "img-gallery-1"],
            gallery_rights_statuses=["STORE_OWNED", "STORE_OWNED"],
        )


def test_validate_gallery_request_rejects_unknown_image_id():
    with pytest.raises(RuntimeError, match="not linked to this candidate"):
        review.validate_gallery_request(
            images=_images(),
            main_image_id="img-main",
            gallery_image_ids=["img-not-a-real-image"],
            gallery_rights_statuses=["STORE_OWNED"],
        )


def test_validate_gallery_request_rejects_unestablished_rights():
    """A gallery image is never published under a rights basis the main
    image itself would not clear either."""
    with pytest.raises(RuntimeError, match="IMAGE_RIGHTS_UNKNOWN"):
        review.validate_gallery_request(
            images=_images(),
            main_image_id="img-main",
            gallery_image_ids=["img-gallery-1"],
            gallery_rights_statuses=["RIGHTS_UNKNOWN"],
        )


# --- approve_main_and_gallery_images ---------------------------------------


def _repository_with_images(images: list[dict]) -> FakeSupabaseRepository:
    return FakeSupabaseRepository(
        tables={"product_images": [dict(image) for image in images]}
    )


def test_approve_main_and_gallery_marks_primary_and_gallery_publish_eligible():
    repository = _repository_with_images(_images())

    review.approve_main_and_gallery_images(
        repository=repository,
        candidate_id=CANDIDATE_ID,
        images=_images(),
        main_image_id="img-main",
        main_role="FRONT_COVER",
        rights_status="STORE_OWNED",
        gallery_images=[
            ("img-gallery-1", "STORE_OWNED"),
            ("img-gallery-2", "SUPPLIER_APPROVED"),
        ],
    )

    rows = {
        row["image_id"]: row
        for row in repository.client.tables["product_images"]
    }

    main_row = rows["img-main"]
    assert main_row["is_selected_main_image"] is True
    assert main_row["is_main_image_candidate"] is True
    assert main_row["is_publish_eligible"] is True
    assert main_row["image_status"] == ImageStatus.VALIDATED
    assert main_row["usage_rights_status"] == "STORE_OWNED"

    for gallery_id, expected_rights in (
        ("img-gallery-1", "STORE_OWNED"),
        ("img-gallery-2", "SUPPLIER_APPROVED"),
    ):
        gallery_row = rows[gallery_id]
        assert gallery_row["is_selected_main_image"] is False
        assert gallery_row["is_main_image_candidate"] is False
        assert gallery_row["is_publish_eligible"] is True
        assert gallery_row["image_status"] == ImageStatus.VALIDATED
        assert gallery_row["usage_rights_status"] == expected_rights

    # Never part of this approval -- demoted, exactly like
    # approve_main_image() already demotes every non-selected image.
    rejected_row = rows["img-rejected"]
    assert rejected_row["is_selected_main_image"] is False
    assert rejected_row["is_publish_eligible"] is False


def test_approve_main_and_gallery_with_empty_gallery_matches_approve_main_image():
    """With no gallery images, behavior must be identical to the
    pre-existing single-image approve_main_image() path."""
    repository = _repository_with_images(_images())

    review.approve_main_and_gallery_images(
        repository=repository,
        candidate_id=CANDIDATE_ID,
        images=_images(),
        main_image_id="img-main",
        main_role="FRONT_COVER",
        rights_status="STORE_OWNED",
        gallery_images=[],
    )

    rows = {
        row["image_id"]: row
        for row in repository.client.tables["product_images"]
    }

    assert rows["img-main"]["is_selected_main_image"] is True

    for other_id in ("img-gallery-1", "img-gallery-2", "img-rejected"):
        assert rows[other_id]["is_selected_main_image"] is False
        assert rows[other_id]["is_publish_eligible"] is False


def test_approve_main_and_gallery_raises_when_main_image_missing():
    repository = _repository_with_images(
        [image for image in _images() if image["image_id"] != "img-main"]
    )

    with pytest.raises(RuntimeError, match="exactly one image"):
        review.approve_main_and_gallery_images(
            repository=repository,
            candidate_id=CANDIDATE_ID,
            images=_images(),
            main_image_id="img-main",
            main_role="FRONT_COVER",
            rights_status="STORE_OWNED",
            gallery_images=[],
        )
