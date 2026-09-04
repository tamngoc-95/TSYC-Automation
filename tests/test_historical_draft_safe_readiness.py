"""Golden tests E, F, J for the historical-migration draft-safe readiness
gate (explicit shop-owner business authorization, 2026-09-04).

Covers src.domain.rules.readiness_rules.evaluate_historical_draft_safe_
readiness(): (E) title + one usable image + approved content + no
reference is draft-safe; (F) missing ISBN/author/publisher/weight is
warning-only, never a blocker; (J) a confirmed sellable-unit (identity)
conflict still blocks. Pure functions, no live Supabase/network.
"""
from __future__ import annotations

from src.domain.decisions import Outcome
from src.domain.rules import readiness_rules as rules


def make_minimal_historical_product(**overrides):
    """Deliberately sparse: none of ISBN/author/publisher/weight/
    dimensions/page_count are set -- CLAUDE.md 2.2/9.3, the historical
    contract must never require these."""
    product = {
        "internal_product_id": "product-1",
        "candidate_id": "candidate-1",
        "content_status": "APPROVED",
        "pricing_status": "PENDING",
    }
    product.update(overrides)
    return product


def make_historical_candidate(**overrides):
    candidate = {
        "candidate_id": "candidate-1",
        "candidate_code": "FB-HIST-2026-002-CAN-0099",
        "candidate_type": "BOOK_SINGLE",
        "identity_status": "IDENTITY_PENDING",
        "verified_title": None,
        "extracted_title": "Combo 4 cuốn truyện của Thomas Harris",
    }
    candidate.update(overrides)
    return candidate


def make_approved_content(**overrides):
    content = {"product_content_id": "content-1", "content_status": "APPROVED"}
    content.update(overrides)
    return content


def make_usable_image(**overrides):
    image = {
        "image_id": "image-1",
        "image_status": "VALIDATED",
        "is_publish_eligible": True,
        "usage_rights_status": "STORE_OWNED",
    }
    image.update(overrides)
    return image


# --- E. title + image + content + no reference is draft-safe --------------


def test_e_title_image_content_no_reference_is_draft_safe():
    result = rules.evaluate_historical_draft_safe_readiness(
        product=make_minimal_historical_product(),
        candidate=make_historical_candidate(),
        approved_content=make_approved_content(),
        selected_images=[make_usable_image()],
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.HISTORICAL_DRAFT_SAFE


def test_e_unverified_identity_alone_never_blocks():
    """No MATCH reference at all -- identity_status stays IDENTITY_PENDING
    -- must not block; only IDENTITY_CONFLICT blocks (test J)."""
    result = rules.evaluate_historical_draft_safe_readiness(
        product=make_minimal_historical_product(),
        candidate=make_historical_candidate(identity_status="IDENTITY_PENDING"),
        approved_content=make_approved_content(),
        selected_images=[make_usable_image()],
    )

    assert result.outcome == Outcome.AUTO_PASS


# --- F. missing ISBN/author/publisher/weight is warning-only --------------


def test_f_missing_isbn_author_publisher_weight_are_warnings_only():
    result = rules.evaluate_historical_draft_safe_readiness(
        product=make_minimal_historical_product(
            isbn=None,
            author=None,
            publisher=None,
            weight_grams=None,
            length_cm=None,
            width_cm=None,
            height_cm=None,
            page_count=None,
        ),
        candidate=make_historical_candidate(),
        approved_content=make_approved_content(),
        selected_images=[make_usable_image()],
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert "ISBN is missing." in result.warnings
    assert "Author is missing." in result.warnings
    assert "Publisher is missing." in result.warnings
    assert "Product weight is missing." in result.warnings
    assert "Product dimensions are missing." in result.warnings
    assert "Page count is missing." in result.warnings


def test_f_pricing_pending_is_also_warning_only():
    result = rules.evaluate_historical_draft_safe_readiness(
        product=make_minimal_historical_product(pricing_status="PENDING"),
        candidate=make_historical_candidate(),
        approved_content=make_approved_content(),
        selected_images=[make_usable_image()],
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert any("Pricing is not approved" in w for w in result.warnings)


def test_f_full_metadata_produces_no_warnings_except_identity():
    result = rules.evaluate_historical_draft_safe_readiness(
        product=make_minimal_historical_product(
            isbn="9786041234567",
            author="Thomas Harris",
            publisher="NXB Test",
            weight_grams=250,
            length_cm=20,
            width_cm=14,
            height_cm=1,
            page_count=120,
            pricing_status="APPROVED",
        ),
        candidate=make_historical_candidate(),
        approved_content=make_approved_content(),
        selected_images=[make_usable_image()],
    )

    assert result.outcome == Outcome.AUTO_PASS
    # Identity is still not IDENTITY_VERIFIED in this fixture, so exactly
    # one warning (identity) is expected, and none of the metadata ones.
    assert result.warnings == (
        "Identity is not fully verified; historical draft-safe "
        "policy applied instead.",
    )


def test_f_identity_verified_drops_the_identity_warning_too():
    result = rules.evaluate_historical_draft_safe_readiness(
        product=make_minimal_historical_product(),
        candidate=make_historical_candidate(identity_status="IDENTITY_VERIFIED"),
        approved_content=make_approved_content(),
        selected_images=[make_usable_image()],
    )

    assert result.outcome == Outcome.AUTO_PASS
    identity_warnings = [w for w in result.warnings if "not fully verified" in w]
    assert identity_warnings == []


# --- J. a confirmed sellable-unit (identity) conflict still blocks --------


def test_j_identity_conflict_blocks_even_with_everything_else_ready():
    result = rules.evaluate_historical_draft_safe_readiness(
        product=make_minimal_historical_product(),
        candidate=make_historical_candidate(identity_status="IDENTITY_CONFLICT"),
        approved_content=make_approved_content(),
        selected_images=[make_usable_image()],
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert any("confirmed identity conflict" in b for b in result.evidence["blockers"])


def test_j_missing_title_blocks():
    result = rules.evaluate_historical_draft_safe_readiness(
        product=make_minimal_historical_product(),
        candidate=make_historical_candidate(verified_title=None, extracted_title=""),
        approved_content=make_approved_content(),
        selected_images=[make_usable_image()],
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert any("meaningful product title" in b for b in result.evidence["blockers"])


def test_j_missing_candidate_type_blocks():
    result = rules.evaluate_historical_draft_safe_readiness(
        product=make_minimal_historical_product(),
        candidate=make_historical_candidate(candidate_type=None),
        approved_content=make_approved_content(),
        selected_images=[make_usable_image()],
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert any("sellable-unit shape" in b for b in result.evidence["blockers"])


def test_j_unapproved_content_still_blocks():
    result = rules.evaluate_historical_draft_safe_readiness(
        product=make_minimal_historical_product(content_status="DRAFTED"),
        candidate=make_historical_candidate(),
        approved_content=None,
        selected_images=[make_usable_image()],
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED


def test_j_unusable_image_still_blocks():
    result = rules.evaluate_historical_draft_safe_readiness(
        product=make_minimal_historical_product(),
        candidate=make_historical_candidate(),
        approved_content=make_approved_content(),
        selected_images=[make_usable_image(usage_rights_status="RIGHTS_UNKNOWN")],
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED


def test_j_recovery_required_still_blocks():
    result = rules.evaluate_historical_draft_safe_readiness(
        product=make_minimal_historical_product(),
        candidate=make_historical_candidate(),
        approved_content=make_approved_content(),
        selected_images=[make_usable_image()],
        recovery_required=True,
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED


def test_j_existing_woo_sync_still_blocks():
    result = rules.evaluate_historical_draft_safe_readiness(
        product=make_minimal_historical_product(),
        candidate=make_historical_candidate(),
        approved_content=make_approved_content(),
        selected_images=[make_usable_image()],
        has_created_woo_sync=True,
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
