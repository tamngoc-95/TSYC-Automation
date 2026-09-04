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
    IMAGE_REFERENCE_SELECTED              AUTO_PASS
    IMAGE_REFERENCE_TIE_BREAK_SELECTED    AUTO_PASS
    IMAGE_REFERENCE_CONFLICT              REVIEW_REQUIRED
    IMAGE_REFERENCE_IDENTITY_CONFLICT     REVIEW_REQUIRED
    IMAGE_REFERENCE_NONE_USABLE           BLOCKED

See docs/TSYC_DECISION_MATRIX.md for the full specification.
"""
from __future__ import annotations

from typing import Any, Sequence

from src.domain.decisions import DecisionResult, Outcome
from src.domain.identity_status import MatchDecision
from src.domain.image_status import ImageStatus
from src.domain.reference_sources import REFERENCE_SOURCE_PRIORITY
from src.domain.rights_status import PUBLISHABLE_RIGHTS_STATUSES, RightsStatus
from src.domain.rules.identity_rules import (
    calculate_similarity,
    looks_like_valid_isbn,
    normalize_isbn,
    normalize_text,
    publishers_conflict,
)

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
IMAGE_REFERENCE_SELECTED = "IMAGE_REFERENCE_SELECTED"
IMAGE_REFERENCE_TIE_BREAK_SELECTED = "IMAGE_REFERENCE_TIE_BREAK_SELECTED"
IMAGE_REFERENCE_CONFLICT = "IMAGE_REFERENCE_CONFLICT"
IMAGE_REFERENCE_IDENTITY_CONFLICT = "IMAGE_REFERENCE_IDENTITY_CONFLICT"
IMAGE_REFERENCE_NONE_USABLE = "IMAGE_REFERENCE_NONE_USABLE"

# Mirrors identity_rules.evaluate_single_reference_identity()'s own
# title_similarity < 0.60 "too different" cutoff (IDENTITY_CONFIRMED_NO_
# MATCH) -- reused rather than reinvented so "materially different title"
# means the same thing everywhere in the codebase.
_TITLE_MATERIALLY_DIFFERENT_THRESHOLD = 0.60
# A publisher disagreement alone is common noise between two
# independently-crawled reference pages (imprint naming, missing field,
# etc.); only treated as a real edition conflict when the titles are not
# already a near-exact match.
_TITLE_NEAR_EXACT_THRESHOLD = 0.90

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


# --- preferred image-reference selection -----------------------------
#
# scripts/download_bookstore_product_image.py used to refuse outright
# whenever a candidate carried more than one MATCH product_reference
# (e.g. both a BOOKSTORE and a FAHASA row -- exactly the case for every
# TSYC historical candidate, since collect_reference_metadata.py
# registers both when both are available). That was a safe default but
# not a real decision: it never actually ranked references, it just
# stopped. This function is the real decision, reused by any caller
# that needs to pick one MATCH reference's image out of several.


def _reference_identity_conflict_reason(
    candidate: dict[str, Any],
    reference: dict[str, Any],
) -> str | None:
    """
    Return a human-readable conflict reason if `reference` looks like a
    different edition/product than the candidate's own verified
    identity, or None if it is safe to use as an automatic image source.

    Deliberately stricter than identity verification itself: CLAUDE.md
    section 9.3 allows a candidate to stay IDENTITY_VERIFIED despite an
    ISBN/edition difference (edition metadata and identity are separate
    concerns). But showing a different edition's cover art as the
    product's main image is a real presentational mismatch even when
    identity itself is unaffected -- so image-reference selection applies
    the stricter check this function encodes, on top of (never instead
    of) the candidate already being IDENTITY_VERIFIED with a MATCH
    reference.
    """
    candidate_isbn_raw = (
        candidate.get("verified_isbn") or candidate.get("possible_isbn")
    )
    reference_isbn_raw = reference.get("reference_isbn")

    if (
        looks_like_valid_isbn(candidate_isbn_raw)
        and looks_like_valid_isbn(reference_isbn_raw)
        and normalize_isbn(candidate_isbn_raw) != normalize_isbn(reference_isbn_raw)
    ):
        return (
            f"Reference ISBN {reference_isbn_raw!r} conflicts with the "
            f"candidate's verified ISBN {candidate_isbn_raw!r} (different "
            "edition)."
        )

    candidate_title = candidate.get("verified_title") or candidate.get(
        "extracted_title"
    )
    reference_title = reference.get("reference_title")
    title_similarity = calculate_similarity(candidate_title, reference_title)

    if reference_title and title_similarity < _TITLE_MATERIALLY_DIFFERENT_THRESHOLD:
        return (
            f"Reference title {reference_title!r} is materially different "
            f"from the candidate's verified title {candidate_title!r} "
            f"(similarity {title_similarity})."
        )

    candidate_publisher = candidate.get("verified_publisher")
    reference_publisher = reference.get("reference_publisher")

    if (
        candidate_publisher
        and reference_publisher
        and title_similarity < _TITLE_NEAR_EXACT_THRESHOLD
        and publishers_conflict([candidate_publisher, reference_publisher])
    ):
        return (
            f"Reference publisher {reference_publisher!r} conflicts with "
            f"the candidate's verified publisher {candidate_publisher!r}, "
            "and title similarity is not near-exact."
        )

    return None


def select_preferred_image_reference(
    candidate: dict[str, Any],
    match_references: Sequence[dict[str, Any]],
) -> DecisionResult:
    """
    Deterministically pick exactly one MATCH product_reference to source
    an image download from, out of possibly several persisted for one
    candidate.

    Ranking is the single canonical
    src.domain.reference_sources.REFERENCE_SOURCE_PRIORITY order
    (CLAUDE.md section 8.1: PUBLISHER > AUTHORIZED_SUPPLIER > BOOKSTORE >
    FAHASA > FACEBOOK > OTHER) -- never a locally invented order, and
    never the first row in whatever order the database happened to
    return them.

    evidence["reference_id"] carries the selected reference on
    AUTO_PASS. Every other outcome selects nothing; the caller must not
    download an image.
    """
    usable = [
        reference
        for reference in match_references
        if reference.get("match_decision") == MatchDecision.MATCH
        and reference.get("source_url_id")
        and reference.get("source_type") in REFERENCE_SOURCE_PRIORITY
    ]

    if not usable:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=IMAGE_REFERENCE_NONE_USABLE,
            reason=(
                "No MATCH product_reference has both a recognized "
                "source_type and a registered source_url_id -- there is "
                "no usable image source for this candidate."
            ),
            evidence={"total_references": len(match_references)},
        )

    # Rank by the canonical priority of each reference's own source_type
    # -- never by a source_priority integer read back from the row,
    # which could be stale relative to REFERENCE_SOURCE_PRIORITY. This
    # is a stricter invariant than the code it replaces, not a weaker
    # one: it makes the canonical mapping the single source of truth
    # instead of trusting a persisted copy of it.
    best_priority = min(
        REFERENCE_SOURCE_PRIORITY[reference["source_type"]] for reference in usable
    )
    top_tier = [
        reference
        for reference in usable
        if REFERENCE_SOURCE_PRIORITY[reference["source_type"]] == best_priority
    ]

    tie_break_reason: str | None = None

    if len(top_tier) > 1:
        # Same-priority tie (e.g. two BOOKSTORE MATCH references): only
        # resolve automatically when every tied reference agrees on
        # edition evidence -- same ISBN or same normalized title, and no
        # publisher conflict. Any material disagreement among
        # same-priority references must stop for review, never be
        # broken by picking whichever row came back first.
        isbns = {
            normalize_isbn(reference.get("reference_isbn"))
            for reference in top_tier
            if looks_like_valid_isbn(reference.get("reference_isbn"))
        }
        titles = {
            normalize_text(reference.get("reference_title"))
            for reference in top_tier
            if reference.get("reference_title")
        }
        # More than one distinct valid ISBN among the tied references is
        # a real, explicit edition conflict -- it must win over a title
        # match rather than be silently outvoted by one. An identical
        # normalized title only stands in as tie-break evidence when no
        # ISBN is available to check at all (isbns is empty), never when
        # ISBNs actively disagree.
        isbn_conflict = len(isbns) > 1
        same_isbn = len(isbns) == 1
        same_title = bool(titles) and len(titles) == 1
        no_publisher_conflict = not publishers_conflict(
            [reference.get("reference_publisher") for reference in top_tier]
        )
        agrees_on_edition = same_isbn or (not isbns and same_title)

        if isbn_conflict or not (agrees_on_edition and no_publisher_conflict):
            return DecisionResult(
                outcome=Outcome.REVIEW_REQUIRED,
                rule_code=IMAGE_REFERENCE_CONFLICT,
                reason=(
                    f"{len(top_tier)} MATCH references share the highest "
                    f"source priority ({best_priority}) but do not agree "
                    "on edition evidence (ISBN/title/publisher). "
                    "Resolve manually before selecting an image source."
                ),
                evidence={
                    "tied_reference_ids": [
                        reference.get("reference_id") for reference in top_tier
                    ],
                    "source_priority": best_priority,
                },
            )

        selected = top_tier[0]
        tie_break_reason = (
            f"{len(top_tier)} MATCH references share the highest source "
            f"priority ({best_priority}) but agree on edition evidence "
            "(same ISBN or same normalized title, no publisher "
            "conflict); selected deterministically."
        )
    else:
        selected = top_tier[0]

    identity_conflict_reason = _reference_identity_conflict_reason(
        candidate, selected
    )

    if identity_conflict_reason:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=IMAGE_REFERENCE_IDENTITY_CONFLICT,
            reason=(
                "The selected reference conflicts with the candidate's "
                f"verified identity: {identity_conflict_reason}"
            ),
            evidence={
                "reference_id": selected.get("reference_id"),
                "source_type": selected.get("source_type"),
            },
        )

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=(
            IMAGE_REFERENCE_TIE_BREAK_SELECTED
            if tie_break_reason
            else IMAGE_REFERENCE_SELECTED
        ),
        reason=tie_break_reason
        or (
            f"Selected the highest-priority MATCH reference "
            f"(source_type={selected['source_type']!r}, priority="
            f"{best_priority}); no other reference shares that priority."
        ),
        evidence={
            "reference_id": selected.get("reference_id"),
            "source_type": selected.get("source_type"),
            "source_priority": best_priority,
        },
    )
