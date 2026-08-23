"""Offline tests for src/domain/rules/image_rules.py.

No live Supabase/WooCommerce/Facebook dependency -- pure functions
operating on plain dicts.
"""
from __future__ import annotations

from src.domain.decisions import Outcome
from src.domain.rights_status import RightsStatus
from src.domain.rules import image_rules as rules


# --- evaluate_rights_classification -------------------------------------


def test_store_owned_with_established_policy_auto_passes():
    result = rules.evaluate_rights_classification(
        rights_status=RightsStatus.STORE_OWNED,
        policy_established=True,
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.IMAGE_STORE_OWNED_EXACT


def test_store_owned_without_established_policy_requires_review():
    """CLAUDE.md section 14.3: do not assume every Facebook-posted
    image is STORE_OWNED."""
    result = rules.evaluate_rights_classification(
        rights_status=RightsStatus.STORE_OWNED,
        policy_established=False,
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.IMAGE_RIGHTS_UNKNOWN


def test_supplier_approved_with_established_policy_auto_passes():
    result = rules.evaluate_rights_classification(
        rights_status=RightsStatus.SUPPLIER_APPROVED,
        policy_established=True,
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.IMAGE_APPROVED_SUPPLIER_EXACT


def test_supplier_approved_without_policy_requires_review():
    """CLAUDE.md section 14.4: do not generalize arbitrary bookstore
    images to SUPPLIER_APPROVED without an established permission."""
    result = rules.evaluate_rights_classification(
        rights_status=RightsStatus.SUPPLIER_APPROVED,
        policy_established=False,
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED


def test_publisher_approved_with_established_policy_auto_passes():
    result = rules.evaluate_rights_classification(
        rights_status=RightsStatus.PUBLISHER_APPROVED,
        policy_established=True,
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.IMAGE_APPROVED_PUBLISHER_EXACT


def test_rights_unknown_requires_review():
    result = rules.evaluate_rights_classification(
        rights_status=RightsStatus.RIGHTS_UNKNOWN,
        policy_established=False,
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.IMAGE_RIGHTS_UNKNOWN


def test_reference_only_requires_review():
    result = rules.evaluate_rights_classification(
        rights_status=RightsStatus.REFERENCE_ONLY,
        policy_established=True,
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.IMAGE_RIGHTS_UNKNOWN


def test_none_rights_status_requires_review():
    result = rules.evaluate_rights_classification(
        rights_status=None,
        policy_established=True,
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED


# --- evaluate_main_image_selection --------------------------------------


def make_image(**overrides):
    image = {
        "image_id": "image-1",
        "image_status": "VALIDATED",
        "usage_rights_status": RightsStatus.STORE_OWNED,
        "image_role": "FRONT_COVER",
    }
    image.update(overrides)
    return image


def test_single_eligible_main_auto_selects():
    images = [make_image(image_id="image-1")]

    result = rules.evaluate_main_image_selection(images)

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.IMAGE_SINGLE_ELIGIBLE_MAIN
    assert result.evidence["selected_image_id"] == "image-1"


def test_multiple_equivalent_candidates_requires_review():
    images = [
        make_image(image_id="image-1"),
        make_image(image_id="image-2"),
    ]

    result = rules.evaluate_main_image_selection(images)

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.IMAGE_MULTIPLE_EQUIVALENT_CANDIDATES
    assert result.evidence["eligible_count"] == 2


def test_no_eligible_images_requires_review():
    images = [make_image(image_status="PENDING")]

    result = rules.evaluate_main_image_selection(images)

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.IMAGE_RIGHTS_UNKNOWN


def test_non_publishable_rights_excludes_image_from_eligibility():
    images = [make_image(usage_rights_status=RightsStatus.RIGHTS_UNKNOWN)]

    result = rules.evaluate_main_image_selection(images)

    assert result.outcome == Outcome.REVIEW_REQUIRED


def test_combo_full_set_auto_selects():
    images = [
        make_image(image_id="combo-1", image_role="COMBO_IMAGE"),
        # A single-volume cover must never be eligible as the combo
        # main image, even though it is otherwise validated/publishable.
        make_image(image_id="single-1", image_role="FRONT_COVER"),
    ]

    result = rules.evaluate_main_image_selection(images, is_combo=True)

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.IMAGE_COMBO_FULL_SET
    assert result.evidence["selected_image_id"] == "combo-1"


def test_combo_without_combo_image_requires_review():
    """CLAUDE.md section 14.6: a single-volume cover must never be
    selected as the main image for a multi-volume combo."""
    images = [make_image(image_id="single-1", image_role="FRONT_COVER")]

    result = rules.evaluate_main_image_selection(images, is_combo=True)

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.evidence["eligible_count"] == 0


def test_combo_multiple_combo_images_requires_review():
    images = [
        make_image(image_id="combo-1", image_role="COMBO_IMAGE"),
        make_image(image_id="combo-2", image_role="COMBO_IMAGE"),
    ]

    result = rules.evaluate_main_image_selection(images, is_combo=True)

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.IMAGE_MULTIPLE_EQUIVALENT_CANDIDATES


# --- evaluate_image_product_match ---------------------------------------


def test_confirmed_mismatch_auto_rejects():
    result = rules.evaluate_image_product_match(
        image_id="image-1",
        matches_product=False,
        mismatch_reason="Cover shows a different title entirely.",
    )

    assert result.outcome == Outcome.AUTO_REJECT
    assert result.rule_code == rules.IMAGE_PRODUCT_MISMATCH


def test_confirmed_match_auto_passes():
    result = rules.evaluate_image_product_match(
        image_id="image-1",
        matches_product=True,
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.IMAGE_PRODUCT_MISMATCH


def test_undetermined_match_requires_review():
    result = rules.evaluate_image_product_match(
        image_id="image-1",
        matches_product=None,
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.IMAGE_PRODUCT_MISMATCH
