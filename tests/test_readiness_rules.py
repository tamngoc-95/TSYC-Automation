"""Offline tests for src/domain/rules/readiness_rules.py.

No live Supabase/WooCommerce/Facebook dependency -- pure functions
operating on plain dicts.
"""
from __future__ import annotations

from src.domain.decisions import Outcome
from src.domain.rules import readiness_rules as rules


def make_ready_product(**overrides):
    product = {
        "internal_product_id": "product-1",
        "candidate_id": "candidate-1",
        "isbn": "9786041234567",
        "weight_grams": 250,
        "length_cm": 20,
        "width_cm": 14,
        "height_cm": 1,
        "page_count": 120,
        "content_status": "APPROVED",
        "image_status": "APPROVED",
        "pricing_status": "APPROVED",
    }
    product.update(overrides)
    return product


def make_ready_candidate(**overrides):
    candidate = {"candidate_id": "candidate-1", "identity_status": "IDENTITY_VERIFIED"}
    candidate.update(overrides)
    return candidate


def make_ready_content(**overrides):
    content = {"product_content_id": "content-1", "content_status": "APPROVED"}
    content.update(overrides)
    return content


def make_ready_image(**overrides):
    image = {
        "image_id": "image-1",
        "image_status": "VALIDATED",
        "is_publish_eligible": True,
        "usage_rights_status": "STORE_OWNED",
    }
    image.update(overrides)
    return image


def test_full_readiness_auto_passes():
    result = rules.evaluate_readiness(
        product=make_ready_product(),
        candidate=make_ready_candidate(),
        approved_content=make_ready_content(),
        selected_images=[make_ready_image()],
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.READY_FOR_DRAFT
    assert result.warnings == ()


def test_pricing_pending_is_non_blocking():
    """CLAUDE.md section 2.5/16: pricing may remain PENDING without
    blocking WooCommerce draft creation."""
    result = rules.evaluate_readiness(
        product=make_ready_product(pricing_status="PENDING"),
        candidate=make_ready_candidate(),
        approved_content=make_ready_content(),
        selected_images=[make_ready_image()],
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert any("Pricing" in warning for warning in result.warnings)


def test_missing_isbn_and_weight_are_non_blocking_warnings():
    result = rules.evaluate_readiness(
        product=make_ready_product(isbn=None, weight_grams=None),
        candidate=make_ready_candidate(),
        approved_content=make_ready_content(),
        selected_images=[make_ready_image()],
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert "ISBN is missing." in result.warnings
    assert "Product weight is missing." in result.warnings


def test_missing_dimensions_and_page_count_are_non_blocking_warnings():
    result = rules.evaluate_readiness(
        product=make_ready_product(
            length_cm=None, width_cm=None, height_cm=None, page_count=None
        ),
        candidate=make_ready_candidate(),
        approved_content=make_ready_content(),
        selected_images=[make_ready_image()],
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert "Product dimensions are missing." in result.warnings
    assert "Page count is missing." in result.warnings


def test_candidate_not_found_is_blocked():
    result = rules.evaluate_readiness(
        product=make_ready_product(),
        candidate=None,
        approved_content=make_ready_content(),
        selected_images=[make_ready_image()],
    )

    assert result.outcome == Outcome.BLOCKED
    assert result.rule_code == rules.READY_FOR_DRAFT


def test_identity_not_verified_requires_review():
    result = rules.evaluate_readiness(
        product=make_ready_product(),
        candidate=make_ready_candidate(identity_status="IDENTITY_PENDING"),
        approved_content=make_ready_content(),
        selected_images=[make_ready_image()],
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert "identity is not verified" in result.reason.lower()


def test_content_not_approved_requires_review():
    result = rules.evaluate_readiness(
        product=make_ready_product(content_status="DRAFTED"),
        candidate=make_ready_candidate(),
        approved_content=make_ready_content(),
        selected_images=[make_ready_image()],
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED


def test_missing_approved_content_row_requires_review():
    result = rules.evaluate_readiness(
        product=make_ready_product(),
        candidate=make_ready_candidate(),
        approved_content=None,
        selected_images=[make_ready_image()],
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED


def test_image_status_not_approved_requires_review():
    result = rules.evaluate_readiness(
        product=make_ready_product(image_status="PENDING"),
        candidate=make_ready_candidate(),
        approved_content=make_ready_content(),
        selected_images=[make_ready_image()],
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED


def test_zero_selected_images_requires_review():
    result = rules.evaluate_readiness(
        product=make_ready_product(),
        candidate=make_ready_candidate(),
        approved_content=make_ready_content(),
        selected_images=[],
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert "Exactly one selected main image" in result.reason


def test_multiple_selected_images_requires_review():
    result = rules.evaluate_readiness(
        product=make_ready_product(),
        candidate=make_ready_candidate(),
        approved_content=make_ready_content(),
        selected_images=[
            make_ready_image(image_id="image-1"),
            make_ready_image(image_id="image-2"),
        ],
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert "Exactly one selected main image" in result.reason


def test_selected_image_not_validated_requires_review():
    result = rules.evaluate_readiness(
        product=make_ready_product(),
        candidate=make_ready_candidate(),
        approved_content=make_ready_content(),
        selected_images=[make_ready_image(image_status="PENDING")],
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED


def test_selected_image_not_publish_eligible_requires_review():
    result = rules.evaluate_readiness(
        product=make_ready_product(),
        candidate=make_ready_candidate(),
        approved_content=make_ready_content(),
        selected_images=[make_ready_image(is_publish_eligible=False)],
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED


def test_selected_image_non_publishable_rights_requires_review():
    result = rules.evaluate_readiness(
        product=make_ready_product(),
        candidate=make_ready_candidate(),
        approved_content=make_ready_content(),
        selected_images=[make_ready_image(usage_rights_status="RIGHTS_UNKNOWN")],
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert "usage rights" in result.reason.lower()


def test_recovery_required_blocks_readiness():
    result = rules.evaluate_readiness(
        product=make_ready_product(),
        candidate=make_ready_candidate(),
        approved_content=make_ready_content(),
        selected_images=[make_ready_image()],
        recovery_required=True,
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert "recovery" in result.reason.lower()


def test_existing_created_woo_sync_blocks_readiness():
    """READY_FOR_DRAFT must not be re-granted for a product that
    already has a created WooCommerce sync -- no duplicate draft."""
    result = rules.evaluate_readiness(
        product=make_ready_product(),
        candidate=make_ready_candidate(),
        approved_content=make_ready_content(),
        selected_images=[make_ready_image()],
        has_created_woo_sync=True,
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert "already exists" in result.reason.lower()


def test_multiple_blockers_are_all_reported():
    result = rules.evaluate_readiness(
        product=make_ready_product(content_status="DRAFTED"),
        candidate=make_ready_candidate(identity_status="IDENTITY_PENDING"),
        approved_content=None,
        selected_images=[],
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert "identity" in result.reason.lower()
    assert "content" in result.reason.lower()
    assert "main image" in result.reason.lower()
