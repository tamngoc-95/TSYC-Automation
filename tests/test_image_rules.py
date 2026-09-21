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


# --- evaluate_historical_image_capability -------------------------------
# (TSYC pipeline stabilization Phase 3: FB-HIST image ingestion)


def test_historical_capability_available_auto_passes():
    result = rules.evaluate_historical_image_capability(
        available=True,
        reason="Facebook export archive found: facebook-export.zip.",
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.IMAGE_CAPABILITY_UNAVAILABLE


def test_historical_capability_unavailable_is_blocked_not_review():
    """A missing export archive is an environmental precondition, not a
    business judgment call -- BLOCKED, not REVIEW_REQUIRED."""
    result = rules.evaluate_historical_image_capability(
        available=False,
        reason="Historical image ingestion capability is unavailable: no "
        "Facebook export archive was found.",
    )

    assert result.outcome == Outcome.BLOCKED
    assert result.rule_code == rules.IMAGE_CAPABILITY_UNAVAILABLE


# --- evaluate_historical_image_ownership --------------------------------


def test_historical_ownership_unambiguous_when_no_siblings():
    result = rules.evaluate_historical_image_ownership([])

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.IMAGE_GROUP_OWNERSHIP_UNAMBIGUOUS


def test_historical_ownership_ambiguous_when_siblings_share_post():
    """CLAUDE.md section 11: a multi-book Facebook post must never have
    its images silently attached to one candidate."""
    result = rules.evaluate_historical_image_ownership(
        ["FB-HIST-2026-001-CAN-0002", "FB-HIST-2026-001-CAN-0003"]
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.IMAGE_GROUP_OWNERSHIP_AMBIGUOUS
    assert "FB-HIST-2026-001-CAN-0002" in result.reason
    assert "FB-HIST-2026-001-CAN-0003" in result.reason


# --- resolve_historical_candidate_images ---------------------------------


def _manual_visual_review_evidence(
    paths: list[str], evidence_text: str | None = "5,99EUR visible, NXB Kim Dong"
) -> dict:
    return {
        "extraction_source": "MANUAL_VISUAL_REVIEW",
        "local_media_paths": paths,
        "evidence_text": evidence_text,
    }


def test_resolve_returns_provenance_missing_when_not_image_derived():
    """A candidate imported through any pathway other than the image-
    derived MANUAL_VISUAL_REVIEW importer (e.g. the original CLAUDE_
    SEMANTIC-based historical import) is never resolved here -- it keeps
    exactly today's human gate."""
    result = rules.resolve_historical_candidate_images(
        candidate_source_evidence={
            "extraction_source": "CLAUDE_SEMANTIC",
            "local_media_paths": ["x/1.jpg"],
        },
        sibling_source_evidence=[],
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.IMAGE_OWNERSHIP_PROVENANCE_MISSING


def test_resolve_returns_provenance_missing_when_no_local_media_paths():
    result = rules.resolve_historical_candidate_images(
        candidate_source_evidence={
            "extraction_source": "MANUAL_VISUAL_REVIEW",
            "local_media_paths": [],
        },
        sibling_source_evidence=[],
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.IMAGE_OWNERSHIP_PROVENANCE_MISSING


def test_resolve_exclusive_when_own_path_does_not_overlap_sibling():
    """Two image-derived siblings sharing a source post but each naming
    its own distinct exact image path -- CLAUDE.md section 11's
    'explicit candidate mapping' is already persisted, so this is not
    ambiguous even though the post is shared."""
    result = rules.resolve_historical_candidate_images(
        candidate_source_evidence=_manual_visual_review_evidence(["x/1.jpg"]),
        sibling_source_evidence=[
            _manual_visual_review_evidence(["x/2.jpg"]),
        ],
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.IMAGE_OWNERSHIP_RESOLVED_EXCLUSIVE
    assert result.evidence["owned_local_media_paths"] == ("x/1.jpg",)


def test_resolve_multi_product_when_both_sides_name_same_path_with_evidence():
    """A legitimate multi-product photo (e.g. a flat-lay showing several
    titles): both candidates explicitly name the same path and both
    carry their own persisted evidence_text -- allowed."""
    result = rules.resolve_historical_candidate_images(
        candidate_source_evidence=_manual_visual_review_evidence(
            ["x/flatlay.jpg"], evidence_text="Title A, left position"
        ),
        sibling_source_evidence=[
            _manual_visual_review_evidence(
                ["x/flatlay.jpg"], evidence_text="Title B, right position"
            ),
        ],
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.IMAGE_OWNERSHIP_RESOLVED_MULTI_PRODUCT


def test_resolve_contradictory_when_sibling_shares_path_without_evidence():
    """The same path is claimed by a sibling that lacks its own
    evidence_text -- the sharing was never actually explained, so it is
    not safe to auto-resolve."""
    result = rules.resolve_historical_candidate_images(
        candidate_source_evidence=_manual_visual_review_evidence(
            ["x/flatlay.jpg"], evidence_text="Title A"
        ),
        sibling_source_evidence=[
            _manual_visual_review_evidence(["x/flatlay.jpg"], evidence_text=None),
        ],
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.IMAGE_OWNERSHIP_CONTRADICTORY


def test_resolve_contradictory_when_sibling_shares_path_via_different_pathway():
    """The exact same case this task's original blocker exhibited: two
    candidates from the older CLAUDE_SEMANTIC pathway both nominally
    name the same single image with no distinguishing evidence -- never
    auto-resolved, even though this candidate's own side happens to be
    image-derived."""
    result = rules.resolve_historical_candidate_images(
        candidate_source_evidence=_manual_visual_review_evidence(
            ["x/shared.jpg"], evidence_text="Title A"
        ),
        sibling_source_evidence=[
            {
                "extraction_source": "CLAUDE_SEMANTIC",
                "local_media_paths": ["x/shared.jpg"],
            },
        ],
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.IMAGE_OWNERSHIP_CONTRADICTORY


def test_resolve_ignores_sibling_with_no_overlapping_path():
    """A sibling that names a path unrelated to this candidate's own has
    no bearing on this candidate's ownership at all."""
    result = rules.resolve_historical_candidate_images(
        candidate_source_evidence=_manual_visual_review_evidence(["x/1.jpg"]),
        sibling_source_evidence=[
            {"extraction_source": "CLAUDE_SEMANTIC", "local_media_paths": ["x/9.jpg"]},
        ],
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.IMAGE_OWNERSHIP_RESOLVED_EXCLUSIVE


# --- select_primary_candidate_image ---------------------------------------


def test_select_primary_returns_review_required_when_no_eligible_images():
    result = rules.select_primary_candidate_image([])

    assert result.outcome == Outcome.REVIEW_REQUIRED


def test_select_primary_with_single_image():
    image = {"image_id": "img-1", "created_at": "2026-01-01T00:00:00Z"}

    result = rules.select_primary_candidate_image([(image, "STORE_OWNED")])

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.IMAGE_PRIMARY_SELECTED
    assert result.evidence["primary_image_id"] == "img-1"
    assert result.evidence["primary_rights_status"] == "STORE_OWNED"
    assert result.evidence["gallery"] == ()


def test_select_primary_prefers_explicit_front_cover_marker():
    early = {"image_id": "img-early", "created_at": "2026-01-01T00:00:00Z"}
    cover = {
        "image_id": "img-cover",
        "created_at": "2026-01-02T00:00:00Z",
        "image_role": "FRONT_COVER",
    }

    result = rules.select_primary_candidate_image(
        [(early, "STORE_OWNED"), (cover, "SUPPLIER_APPROVED")]
    )

    assert result.evidence["primary_image_id"] == "img-cover"
    assert result.evidence["primary_rights_status"] == "SUPPLIER_APPROVED"
    assert result.evidence["gallery"] == (("img-early", "STORE_OWNED"),)


def test_select_primary_falls_back_to_deterministic_created_at_order():
    later = {"image_id": "img-b", "created_at": "2026-01-02T00:00:00Z"}
    earlier = {"image_id": "img-a", "created_at": "2026-01-01T00:00:00Z"}

    result = rules.select_primary_candidate_image(
        [(later, "STORE_OWNED"), (earlier, "STORE_OWNED")]
    )

    assert result.evidence["primary_image_id"] == "img-a"
    assert result.evidence["gallery"] == (("img-b", "STORE_OWNED"),)


def test_select_primary_gallery_preserves_order_for_three_images():
    a = {"image_id": "img-a", "created_at": "2026-01-01T00:00:00Z"}
    b = {"image_id": "img-b", "created_at": "2026-01-02T00:00:00Z"}
    c = {"image_id": "img-c", "created_at": "2026-01-03T00:00:00Z"}

    result = rules.select_primary_candidate_image(
        [(c, "STORE_OWNED"), (a, "STORE_OWNED"), (b, "SUPPLIER_APPROVED")]
    )

    assert result.evidence["primary_image_id"] == "img-a"
    assert result.evidence["gallery"] == (
        ("img-b", "SUPPLIER_APPROVED"),
        ("img-c", "STORE_OWNED"),
    )
