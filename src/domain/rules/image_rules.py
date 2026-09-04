"""Deterministic image-review rules.

Covers product_images.usage_rights_status / image_status /
is_selected_main_image / is_publish_eligible. Rights classification here
must always mirror an established TSYC policy, never a broad
generalization -- CLAUDE.md sections 14.3/14.4: do not assume every
Facebook-collected image is STORE_OWNED, and do not assume every
bookstore image is SUPPLIER_APPROVED without an established permission.

Rule codes implemented here:

    IMAGE_STORE_OWNED_EXACT               AUTO_PASS
    IMAGE_APPROVED_SUPPLIER_EXACT         AUTO_PASS
    IMAGE_APPROVED_PUBLISHER_EXACT        AUTO_PASS
    IMAGE_SINGLE_ELIGIBLE_MAIN            AUTO_PASS
    IMAGE_COMBO_FULL_SET                  AUTO_PASS
    IMAGE_MULTIPLE_EQUIVALENT_CANDIDATES  REVIEW_REQUIRED
    IMAGE_RIGHTS_UNKNOWN                  REVIEW_REQUIRED
    IMAGE_PRODUCT_MISMATCH                AUTO_REJECT (or AUTO_PASS/
                                           REVIEW_REQUIRED for the same
                                           check's other outcomes)

See docs/TSYC_DECISION_MATRIX.md for the full specification.
"""
from __future__ import annotations

from typing import Any, Sequence

from src.domain.decisions import DecisionResult, Outcome
from src.domain.image_status import ImageStatus
from src.domain.rights_status import PUBLISHABLE_RIGHTS_STATUSES, RightsStatus

# --- rule codes ----------------------------------------------------

IMAGE_STORE_OWNED_EXACT = "IMAGE_STORE_OWNED_EXACT"
IMAGE_APPROVED_SUPPLIER_EXACT = "IMAGE_APPROVED_SUPPLIER_EXACT"
IMAGE_APPROVED_PUBLISHER_EXACT = "IMAGE_APPROVED_PUBLISHER_EXACT"
IMAGE_SINGLE_ELIGIBLE_MAIN = "IMAGE_SINGLE_ELIGIBLE_MAIN"
IMAGE_COMBO_FULL_SET = "IMAGE_COMBO_FULL_SET"
IMAGE_MULTIPLE_EQUIVALENT_CANDIDATES = "IMAGE_MULTIPLE_EQUIVALENT_CANDIDATES"
IMAGE_RIGHTS_UNKNOWN = "IMAGE_RIGHTS_UNKNOWN"
IMAGE_PRODUCT_MISMATCH = "IMAGE_PRODUCT_MISMATCH"
IMAGE_GROUP_OWNERSHIP_UNAMBIGUOUS = "IMAGE_GROUP_OWNERSHIP_UNAMBIGUOUS"
IMAGE_GROUP_OWNERSHIP_AMBIGUOUS = "IMAGE_GROUP_OWNERSHIP_AMBIGUOUS"
IMAGE_CAPABILITY_UNAVAILABLE = "IMAGE_CAPABILITY_UNAVAILABLE"

_RIGHTS_RULE_CODE = {
    RightsStatus.STORE_OWNED: IMAGE_STORE_OWNED_EXACT,
    RightsStatus.SUPPLIER_APPROVED: IMAGE_APPROVED_SUPPLIER_EXACT,
    RightsStatus.PUBLISHER_APPROVED: IMAGE_APPROVED_PUBLISHER_EXACT,
}


# --- rights classification -------------------------------------------------


def evaluate_rights_classification(
    rights_status: str | None,
    policy_established: bool,
) -> DecisionResult:
    """
    Classify one image's usage-rights status.

    AUTO_PASS only for STORE_OWNED/SUPPLIER_APPROVED/PUBLISHER_APPROVED
    *and* only when the caller confirms an established TSYC policy
    basis exists for that classification (e.g. an exact TSYC Facebook
    post the shop itself photographed, or a specific publisher/supplier
    permission on file) -- CLAUDE.md sections 14.3/14.4. This function
    never infers policy_established on its own; a caller that cannot
    point to an established basis must pass policy_established=False,
    which routes to REVIEW_REQUIRED regardless of the rights_status
    value -- it is never generalized from the source type alone.
    """
    if rights_status is None or rights_status == RightsStatus.RIGHTS_UNKNOWN:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=IMAGE_RIGHTS_UNKNOWN,
            reason="Image usage rights are unknown or unclassified.",
            evidence={"rights_status": rights_status},
        )

    if rights_status == RightsStatus.REFERENCE_ONLY:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=IMAGE_RIGHTS_UNKNOWN,
            reason="Image is reference-only and is not publish eligible; "
            "confirm an alternative publishable image or an established "
            "rights basis before proceeding.",
            evidence={"rights_status": rights_status},
        )

    rule_code = _RIGHTS_RULE_CODE.get(rights_status)
    if rule_code is None:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=IMAGE_RIGHTS_UNKNOWN,
            reason=f"Unrecognized usage-rights status: {rights_status!r}.",
            evidence={"rights_status": rights_status},
        )

    if not policy_established:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=IMAGE_RIGHTS_UNKNOWN,
            reason=(
                f"{rights_status} requires an established TSYC policy "
                "basis (exact shop-photographed post, or a specific "
                "publisher/supplier permission) -- none was confirmed."
            ),
            evidence={"rights_status": rights_status},
        )

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=rule_code,
        reason=f"{rights_status} classification has an established policy basis.",
        evidence={"rights_status": rights_status},
    )


# --- main-image selection -----------------------------------------------


def evaluate_main_image_selection(
    candidates: Sequence[dict[str, Any]],
    is_combo: bool = False,
) -> DecisionResult:
    """
    Decide whether exactly one eligible main image can be auto-selected.

    `candidates` should already be narrowed to images confirmed to
    visually represent the product (this function only decides the
    *cardinality* question -- exactly one, none, or several equally
    plausible -- never whether an image visually matches the product;
    see evaluate_image_product_match for that). Each candidate must be
    image_status=VALIDATED and carry a publishable usage_rights_status.

    For a combo/set candidate (is_combo=True), only images explicitly
    marked as representing the complete set (image_role ==
    "COMBO_IMAGE") are eligible -- CLAUDE.md section 14.6: a
    single-volume cover must never be selected as the main image for a
    multi-volume combo.
    """
    eligible = [
        image
        for image in candidates
        if image.get("image_status") == ImageStatus.VALIDATED
        and image.get("usage_rights_status") in PUBLISHABLE_RIGHTS_STATUSES
    ]

    if is_combo:
        eligible = [
            image for image in eligible if image.get("image_role") == "COMBO_IMAGE"
        ]

    if not eligible:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=IMAGE_RIGHTS_UNKNOWN,
            reason=(
                "No image representing the complete combo/set is eligible."
                if is_combo
                else "No validated, publishable image is eligible."
            ),
            evidence={"eligible_count": 0, "is_combo": is_combo},
        )

    if len(eligible) > 1:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=IMAGE_MULTIPLE_EQUIVALENT_CANDIDATES,
            reason=(
                f"{len(eligible)} equally eligible images exist; automatic "
                "selection requires subjective judgment."
            ),
            evidence={
                "eligible_count": len(eligible),
                "eligible_image_ids": [
                    image.get("image_id") for image in eligible
                ],
                "is_combo": is_combo,
            },
        )

    selected = eligible[0]
    rule_code = IMAGE_COMBO_FULL_SET if is_combo else IMAGE_SINGLE_ELIGIBLE_MAIN

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=rule_code,
        reason=(
            "Exactly one image represents the complete combo/set and "
            "is eligible."
            if is_combo
            else "Exactly one validated, publishable image is eligible "
            "as the main image."
        ),
        evidence={
            "eligible_count": 1,
            "selected_image_id": selected.get("image_id"),
            "is_combo": is_combo,
        },
    )


# --- historical (FB-HIST) image sourcing --------------------------------


def evaluate_historical_image_capability(
    available: bool,
    reason: str,
) -> DecisionResult:
    """
    Whether the historical image extraction capability (the gitignored
    Facebook export archive -- see
    src.services.historical_image_extraction) is usable right now.

    BLOCKED, not REVIEW_REQUIRED: a missing/unreadable export archive is
    an environmental precondition, not a business judgment call -- the
    same candidate will fail the same way on every retry until the
    archive is made available, exactly like Outcome.BLOCKED's contract.
    """
    if not available:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=IMAGE_CAPABILITY_UNAVAILABLE,
            reason=reason,
            evidence={"available": False},
        )

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=IMAGE_CAPABILITY_UNAVAILABLE,
        reason=reason,
        evidence={"available": True},
    )


def evaluate_historical_image_ownership(
    sibling_candidate_codes: Sequence[str],
) -> DecisionResult:
    """
    Decide whether a historical candidate's source Facebook post is
    shared with any other candidate.

    CLAUDE.md section 11: "For multi-book Facebook posts: ... do not
    silently attach all shared-post images to the newest candidate;
    explicit candidate mapping is required where image ownership is
    ambiguous." A historical post that produced more than one
    product_candidates row (this candidate plus at least one sibling
    sharing the same raw_page_id) is exactly that case -- images must
    never be auto-associated to one of them; a human must map each image
    to its correct candidate first.
    """
    if sibling_candidate_codes:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=IMAGE_GROUP_OWNERSHIP_AMBIGUOUS,
            reason=(
                "This candidate's source Facebook post also produced "
                f"{len(sibling_candidate_codes)} other candidate(s) "
                f"({', '.join(sorted(sibling_candidate_codes))}). Images "
                "cannot be auto-associated to one candidate from a "
                "shared multi-product post -- assign each image to its "
                "correct candidate explicitly before ingestion."
            ),
            evidence={"sibling_candidate_codes": tuple(sorted(sibling_candidate_codes))},
        )

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=IMAGE_GROUP_OWNERSHIP_UNAMBIGUOUS,
        reason=(
            "This candidate is the sole product extracted from its source "
            "Facebook post; its local media can be associated to it "
            "unambiguously."
        ),
        evidence={"sibling_candidate_codes": ()},
    )


# --- product-match check ------------------------------------------------


def evaluate_image_product_match(
    image_id: str | None,
    matches_product: bool | None,
    mismatch_reason: str | None = None,
) -> DecisionResult:
    """
    Confirm (or reject) that one image represents the product/candidate
    it is linked to.

    Visual comparison itself is out of scope for this rule engine --
    matches_product is a caller-supplied verdict from whatever upstream
    process determined it (a human reviewer, or a deterministic
    same-source-page linkage). None means "not yet determined", which
    is REVIEW_REQUIRED rather than a silent pass or reject; this
    function never guesses at a match.
    """
    evidence = {"image_id": image_id, "matches_product": matches_product}

    if matches_product is False:
        return DecisionResult(
            outcome=Outcome.AUTO_REJECT,
            rule_code=IMAGE_PRODUCT_MISMATCH,
            reason=mismatch_reason or "Image does not match the linked product.",
            evidence=evidence,
        )

    if matches_product is True:
        return DecisionResult(
            outcome=Outcome.AUTO_PASS,
            rule_code=IMAGE_PRODUCT_MISMATCH,
            reason="Image was confirmed to match the linked product.",
            evidence=evidence,
        )

    return DecisionResult(
        outcome=Outcome.REVIEW_REQUIRED,
        rule_code=IMAGE_PRODUCT_MISMATCH,
        reason="Whether this image matches the linked product has not "
        "been determined.",
        evidence=evidence,
    )
