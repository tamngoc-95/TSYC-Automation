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

from types import MappingProxyType
from typing import Any, Mapping, Sequence

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
    reference_business_conflict_reason,
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
# Historical multi-image ownership resolver (image-derived FB-HIST
# candidates only) -- see resolve_historical_candidate_images() and
# select_primary_candidate_image() below.
IMAGE_OWNERSHIP_RESOLVED_EXCLUSIVE = "IMAGE_OWNERSHIP_RESOLVED_EXCLUSIVE"
IMAGE_OWNERSHIP_RESOLVED_MULTI_PRODUCT = "IMAGE_OWNERSHIP_RESOLVED_MULTI_PRODUCT"
IMAGE_OWNERSHIP_PROVENANCE_MISSING = "IMAGE_OWNERSHIP_PROVENANCE_MISSING"
IMAGE_OWNERSHIP_CONTRADICTORY = "IMAGE_OWNERSHIP_CONTRADICTORY"
IMAGE_PRIMARY_SELECTED = "IMAGE_PRIMARY_SELECTED"
IMAGE_REFERENCE_SELECTED = "IMAGE_REFERENCE_SELECTED"
IMAGE_REFERENCE_TIE_BREAK_SELECTED = "IMAGE_REFERENCE_TIE_BREAK_SELECTED"
IMAGE_REFERENCE_CONFLICT = "IMAGE_REFERENCE_CONFLICT"
IMAGE_REFERENCE_IDENTITY_CONFLICT = "IMAGE_REFERENCE_IDENTITY_CONFLICT"
IMAGE_REFERENCE_NONE_USABLE = "IMAGE_REFERENCE_NONE_USABLE"
# Historical-migration draft-safe image-reference fallback (CLAUDE.md
# section 6.2/8.1/14.7) -- see is_historical_reference_image_draft_safe()
# and select_historical_draft_safe_image_reference() below.
IMAGE_REFERENCE_DRAFT_SAFE_SELECTED = "IMAGE_REFERENCE_DRAFT_SAFE_SELECTED"
IMAGE_REFERENCE_NONE_DRAFT_SAFE = "IMAGE_REFERENCE_NONE_DRAFT_SAFE"

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

# Historical-migration image-rights policy (explicit shop-owner business
# authorization, 2026-09-04 -- see CLAUDE.md section 14.3/14.4 and the
# TSYC decision matrix for the full policy text). Canonical mapping from
# an already-approved reference source_type to the rights status it
# authorizes: PUBLISHER maps to PUBLISHER_APPROVED; AUTHORIZED_SUPPLIER/
# BOOKSTORE/FAHASA all map to SUPPLIER_APPROVED -- the same mapping this
# project's own production precedent already used (100% of BOOKSTORE-
# sourced images classified SUPPLIER_APPROVED to date). A source_type
# not in this mapping (an unconfigured/unknown domain, or OTHER) is
# never auto-authorized by this policy.
APPROVED_REFERENCE_SOURCE_RIGHTS = MappingProxyType(
    {
        "PUBLISHER": RightsStatus.PUBLISHER_APPROVED,
        "AUTHORIZED_SUPPLIER": RightsStatus.SUPPLIER_APPROVED,
        "BOOKSTORE": RightsStatus.SUPPLIER_APPROVED,
        "FAHASA": RightsStatus.SUPPLIER_APPROVED,
    }
)


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


def evaluate_historical_image_rights_policy(
    *,
    is_own_facebook_export: bool = False,
    reference_source_type: str | None = None,
) -> DecisionResult:
    """
    Determine rights classification for one image under the shop
    owner's explicit historical-migration image-rights policy: an image
    from the shop's own Facebook export is STORE_OWNED; an image
    downloaded from an already-approved reference source type
    (APPROVED_REFERENCE_SOURCE_RIGHTS) is authorized at the rights
    status that mapping names. Neither case requires a separate
    per-image human rights approval.

    Delegates the actual AUTO_PASS/REVIEW_REQUIRED decision to
    evaluate_rights_classification() unchanged -- this function's only
    job is to decide policy_established from the two facts the historical
    pipeline actually has (which export the image came from, which
    reference source_type it was downloaded from), never to duplicate
    that function's own classification logic.

    An unconfigured/unknown domain or source_type (reference_source_type
    not in APPROVED_REFERENCE_SOURCE_RIGHTS, and is_own_facebook_export
    False) always falls through to RIGHTS_UNKNOWN / REVIEW_REQUIRED --
    CLAUDE.md 14.3/14.4's "do not assume every image is approved" still
    applies to anything outside this exact, configured allowlist.

    Never a substitute for the separate, still-mandatory candidate-
    relevance/ownership check (evaluate_image_product_match): a
    pre-authorized rights basis does not mean the image is confirmed to
    actually depict this candidate.
    """
    if is_own_facebook_export:
        return evaluate_rights_classification(
            rights_status=RightsStatus.STORE_OWNED,
            policy_established=True,
        )

    approved_rights = APPROVED_REFERENCE_SOURCE_RIGHTS.get(reference_source_type or "")

    if approved_rights is not None:
        return evaluate_rights_classification(
            rights_status=approved_rights,
            policy_established=True,
        )

    return evaluate_rights_classification(
        rights_status=RightsStatus.RIGHTS_UNKNOWN,
        policy_established=False,
    )


def classify_historical_image_rights(
    image: Mapping[str, Any],
    reference_by_id: Mapping[str, dict[str, Any]] | None = None,
) -> DecisionResult:
    """
    Per-image (not per-candidate) rights classification for an FB-HIST
    candidate -- CLAUDE.md section 6.2/14.7: STORE_OWNED/PUBLISHER_
    APPROVED/SUPPLIER_APPROVED apply per image based on that image's own
    provenance, never gated on how many images the candidate happens to
    carry. Main-image *selection* (choosing which of possibly several
    rights-eligible images becomes the one selected main image) remains
    a completely separate decision -- see evaluate_main_image_selection.

    Provenance is never inferred beyond what the writer scripts
    themselves already record:
      - usage_rights_status already publishable -> kept as-is.
      - no reference_id and source_type == "FACEBOOK" -> this candidate's
        own Facebook export (upload_facebook_images_to_supabase.py is the
        only writer of FACEBOOK-sourced product_images rows and never
        sets reference_id) -> STORE_OWNED.
      - reference_id set -> the linked reference's source_type, mapped
        through APPROVED_REFERENCE_SOURCE_RIGHTS (download_bookstore_
        product_image.py always writes source_type = the selected
        reference's own source_type).
      - anything else (no reference_id and source_type != "FACEBOOK", or
        a reference_id with no resolvable reference row in
        reference_by_id) -> RIGHTS_UNKNOWN, exactly as before -- an
        unrecognized/unconfigured provenance is never auto-approved.
    """
    existing_rights = image.get("usage_rights_status")

    if existing_rights in PUBLISHABLE_RIGHTS_STATUSES:
        return evaluate_rights_classification(
            rights_status=existing_rights,
            policy_established=True,
        )

    reference_id = image.get("reference_id")

    if not reference_id:
        if image.get("source_type") == "FACEBOOK":
            return evaluate_historical_image_rights_policy(
                is_own_facebook_export=True,
            )

        return evaluate_rights_classification(
            rights_status=RightsStatus.RIGHTS_UNKNOWN,
            policy_established=False,
        )

    reference = (reference_by_id or {}).get(str(reference_id))
    reference_source_type = reference.get("source_type") if reference else None

    return evaluate_historical_image_rights_policy(
        reference_source_type=reference_source_type,
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


# --- historical multi-image ownership resolver --------------------------
#
# evaluate_historical_image_ownership() above stays completely unmodified
# and remains the first check every caller runs -- a candidate with zero
# siblings is unaffected by anything below. The functions in this section
# are only ever consulted as a *second*, stricter step, for the case
# evaluate_historical_image_ownership() itself cannot resolve (siblings
# exist): they ask whether persisted, candidate-specific provenance can
# resolve ownership anyway, instead of falling straight to a human gate.
#
# Image-derived historical batches (import_image_derived_historical_
# candidates*.py) write each candidate's own exact source image path(s)
# into that candidate's own source_evidence.local_media_paths at import
# time (extraction_source="MANUAL_VISUAL_REVIEW"), with a persisted
# evidence_text explaining the visual basis for that specific mapping
# (e.g. "5,99EUR visible, NXB Kim Dong"). That is real, persisted,
# candidate-specific evidence -- CLAUDE.md section 11 requires "explicit
# candidate mapping", not "no candidate shares this post at all". A
# candidate imported through any other pathway (the original CSV-based
# historical import, extraction_source e.g. "CLAUDE_SEMANTIC") never
# carries this per-candidate mapping and is never resolved here -- it
# keeps exactly today's human gate.

_IMAGE_DERIVED_EXTRACTION_SOURCE = "MANUAL_VISUAL_REVIEW"


def resolve_historical_candidate_images(
    candidate_source_evidence: Mapping[str, Any],
    sibling_source_evidence: Sequence[Mapping[str, Any]],
) -> DecisionResult:
    """
    Deterministically resolve whether this candidate's own persisted
    source_evidence.local_media_paths can be safely associated to it,
    even though at least one sibling candidate shares its source post.

    Ownership is never inferred from post membership, position, filename
    similarity, or guessing -- only from exact, persisted evidence:
      - this candidate's own extraction_source is the image-derived
        MANUAL_VISUAL_REVIEW pathway, and it carries at least one
        persisted local_media_paths entry (its own exact image(s));
      - for every sibling that also names one of those same exact
        paths (a legitimate multi-product photo, CLAUDE.md section 11's
        "explicit candidate mapping" case), that sibling was imported
        through the same MANUAL_VISUAL_REVIEW pathway *and* both sides
        carry their own non-empty evidence_text distinguishing the
        sellable units. Anything less (a non-image-derived sibling
        naming the same path, or either side missing its distinguishing
        evidence_text) is treated as contradictory, never auto-resolved.

    AUTO_PASS evidence carries "owned_local_media_paths" -- the exact
    paths the caller may treat as this candidate's own for extraction/
    upload. This function never decides image rights or main-image
    selection; see classify_historical_image_rights and
    select_primary_candidate_image for those separate decisions.
    """
    extraction_source = candidate_source_evidence.get("extraction_source")
    own_paths = list(candidate_source_evidence.get("local_media_paths") or [])

    if extraction_source != _IMAGE_DERIVED_EXTRACTION_SOURCE or not own_paths:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=IMAGE_OWNERSHIP_PROVENANCE_MISSING,
            reason=(
                "This candidate's own image provenance is not an "
                "explicit image-derived (MANUAL_VISUAL_REVIEW) mapping "
                "with at least one persisted local media path -- "
                "ownership cannot be resolved from persisted evidence "
                "alone while its source post is shared."
            ),
            evidence={
                "extraction_source": extraction_source,
                "own_path_count": len(own_paths),
            },
        )

    own_evidence_text = candidate_source_evidence.get("evidence_text")
    own_path_set = set(own_paths)
    multi_product = False

    for sibling_evidence in sibling_source_evidence:
        sibling_paths = set(sibling_evidence.get("local_media_paths") or [])
        overlap = own_path_set & sibling_paths

        if not overlap:
            # This sibling names none of this candidate's own exact
            # paths -- it has no bearing on this candidate's ownership.
            continue

        sibling_source = sibling_evidence.get("extraction_source")
        sibling_evidence_text = sibling_evidence.get("evidence_text")

        if (
            sibling_source != _IMAGE_DERIVED_EXTRACTION_SOURCE
            or not sibling_evidence_text
            or not own_evidence_text
        ):
            return DecisionResult(
                outcome=Outcome.REVIEW_REQUIRED,
                rule_code=IMAGE_OWNERSHIP_CONTRADICTORY,
                reason=(
                    "A sibling candidate from the same source post names "
                    "an overlapping image path without matching "
                    "image-derived provenance and persisted evidence "
                    "text on both sides -- resolve manually."
                ),
                evidence={"overlapping_paths": tuple(sorted(overlap))},
            )

        # Both sides are image-derived and each carries its own
        # persisted evidence_text distinguishing the sellable units --
        # CLAUDE.md section 11's "explicit candidate mapping" for a
        # legitimate multi-product photo.
        multi_product = True

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=(
            IMAGE_OWNERSHIP_RESOLVED_MULTI_PRODUCT
            if multi_product
            else IMAGE_OWNERSHIP_RESOLVED_EXCLUSIVE
        ),
        reason=(
            "This candidate's own image-derived (MANUAL_VISUAL_REVIEW) "
            "provenance persists exact, candidate-specific image "
            "path(s); any sibling naming the same path also carries "
            "matching image-derived provenance and distinguishing "
            "evidence text."
            if multi_product
            else "This candidate's own image-derived (MANUAL_VISUAL_"
            "REVIEW) provenance persists exact image path(s) that no "
            "sibling sharing the source post also claims."
        ),
        evidence={"owned_local_media_paths": tuple(own_paths)},
    )


def select_primary_candidate_image(
    eligible_images: Sequence[tuple[Mapping[str, Any], str]],
) -> DecisionResult:
    """
    Deterministically choose exactly one PRIMARY image out of one or more
    images already confirmed eligible (validated-or-validatable,
    publishable rights) for the *same* candidate.

    `eligible_images` entries are (image_row, rights_status) pairs, in
    the exact shape scripts/pipeline_state.py already builds. This
    function only decides which single image is PRIMARY when several are
    already known to all belong to this one candidate -- it never
    decides *whether* an image belongs to this candidate (see
    resolve_historical_candidate_images for that separate question), and
    it is never itself a source of REVIEW_REQUIRED when eligible_images
    is non-empty: CLAUDE.md 14.5's "exactly one eligible image" is about
    ownership ambiguity, not about which of several already-confirmed,
    equally-owned images looks best as the cover.

    Selection order:
      1. an image already carrying image_role == "FRONT_COVER" (an
         explicit persisted primary/cover marker);
      2. otherwise, the earliest inserted image (created_at, then
         image_id as a stable tie-break) -- deterministic inventory
         order. No other per-image "confidence" or "shows candidate
         alone" field is persisted anywhere in product_images today, so
         this function only ever ranks by what is actually recorded.

    All remaining images become the GALLERY, in the same deterministic
    order, preserved in evidence["gallery"].
    """
    if not eligible_images:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=IMAGE_RIGHTS_UNKNOWN,
            reason="No eligible image exists to select a primary from.",
            evidence={"eligible_count": 0},
        )

    def _sort_key(pair: tuple[Mapping[str, Any], str]) -> tuple[int, str, str]:
        image, _rights_status = pair
        is_explicit_cover = image.get("image_role") == "FRONT_COVER"
        return (
            0 if is_explicit_cover else 1,
            str(image.get("created_at") or ""),
            str(image.get("image_id") or ""),
        )

    ordered = sorted(eligible_images, key=_sort_key)
    primary_image, primary_rights_status = ordered[0]
    gallery = ordered[1:]

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=IMAGE_PRIMARY_SELECTED,
        reason=(
            f"Selected one deterministic primary image out of "
            f"{len(eligible_images)} eligible image(s) already "
            "confirmed to belong to this candidate; the remaining "
            f"{len(gallery)} become gallery image(s)."
        ),
        evidence={
            "primary_image_id": primary_image.get("image_id"),
            "primary_rights_status": primary_rights_status,
            "gallery": tuple(
                (image.get("image_id"), rights_status)
                for image, rights_status in gallery
            ),
        },
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


# --- historical draft-safe image-reference fallback (CLAUDE.md 6.2/8.1) --
#
# select_preferred_image_reference() above stays completely unmodified and
# is still the only function download_bookstore_product_image.py uses for
# a live (non-historical) candidate: match_decision==MATCH is still
# required, in full, for every FB-2026-* candidate.
#
# The functions below are FB-HIST-only and deliberately separate rather
# than a relaxation of select_preferred_image_reference() itself, so live
# behavior can never be affected by this policy. Neither function performs
# an is_historical_candidate_code() check itself -- these are pure
# decision functions and should not need the candidate_code string at all;
# callers (pipeline_state.py, download_bookstore_product_image.py) are
# responsible for only ever reaching them for an FB-HIST candidate.

_DRAFT_SAFE_MATCH_DECISIONS = (
    MatchDecision.MATCH,
    MatchDecision.POSSIBLE_MATCH,
    MatchDecision.MANUAL_REVIEW,
)

# Tie-break preference among same-source-priority draft-safe references:
# a real MATCH still outranks a POSSIBLE_MATCH/MANUAL_REVIEW of the same
# source priority, which in turn outranks a MANUAL_REVIEW. Never used to
# invent a ranking among different source priorities -- source priority
# (REFERENCE_SOURCE_PRIORITY) is always decided first.
_MATCH_DECISION_RANK = MappingProxyType(
    {
        MatchDecision.MATCH: 0,
        MatchDecision.POSSIBLE_MATCH: 1,
        MatchDecision.MANUAL_REVIEW: 2,
    }
)


def is_historical_reference_image_draft_safe(
    candidate: dict[str, Any],
    reference: dict[str, Any],
) -> DecisionResult:
    """
    FB-HIST-only draft-safe image-reference eligibility (CLAUDE.md
    section 6.2/8.1/14.7).

    Deliberately looser than select_preferred_image_reference()'s live-
    pipeline match_decision==MATCH requirement: a POSSIBLE_MATCH or
    MANUAL_REVIEW reference may be used as an image fallback source for a
    historical draft, but only when every one of these holds:
      - candidate has a meaningful title (verified_title or
        extracted_title, non-empty)
      - candidate has a known candidate_type (clear sellable unit)
      - reference has a usable image URL (reference_image_url)
      - reference source_type maps to a publishable historical rights
        status (APPROVED_REFERENCE_SOURCE_RIGHTS: PUBLISHER/
        AUTHORIZED_SUPPLIER/BOOKSTORE/FAHASA only)
      - no ISBN/title/publisher/author/sellable-unit conflict
        (identity_rules.reference_business_conflict_reason)

    Never requires IDENTITY_VERIFIED, never requires match_decision==
    MATCH, never requires a second independent reference (CLAUDE.md
    section 6.2's explicit relaxations). Never relabels the reference's
    own match_decision -- this is image eligibility only; identity_status
    and match_decision are read here, never written.
    """
    candidate_title = candidate.get("verified_title") or candidate.get(
        "extracted_title"
    )

    if not candidate_title or not str(candidate_title).strip():
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=IMAGE_REFERENCE_NONE_DRAFT_SAFE,
            reason="Candidate has no meaningful title.",
        )

    if not candidate.get("candidate_type"):
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=IMAGE_REFERENCE_NONE_DRAFT_SAFE,
            reason="Candidate type (sellable-unit shape) is unknown.",
        )

    if not reference.get("reference_image_url"):
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=IMAGE_REFERENCE_NONE_DRAFT_SAFE,
            reason="Reference has no usable image URL.",
            evidence={"reference_id": reference.get("reference_id")},
        )

    reference_source_type = reference.get("source_type")
    rights_decision = evaluate_historical_image_rights_policy(
        reference_source_type=reference_source_type,
    )

    if rights_decision.outcome != Outcome.AUTO_PASS:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=IMAGE_RIGHTS_UNKNOWN,
            reason=(
                f"Reference source_type {reference_source_type!r} does "
                "not map to a publishable historical rights status."
            ),
            evidence={
                "reference_id": reference.get("reference_id"),
                "reference_source_type": reference_source_type,
            },
        )

    conflict_reason = reference_business_conflict_reason(candidate, reference)

    if conflict_reason:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=IMAGE_REFERENCE_IDENTITY_CONFLICT,
            reason=conflict_reason,
            evidence={"reference_id": reference.get("reference_id")},
        )

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=IMAGE_REFERENCE_DRAFT_SAFE_SELECTED,
        reason=(
            "Reference is draft-safe: approved source type, publishable "
            "historical rights mapping, no identity/sellable-unit "
            "conflict."
        ),
        evidence={
            "reference_id": reference.get("reference_id"),
            "source_type": reference_source_type,
            "rights_status": rights_decision.evidence.get("rights_status"),
            "match_decision": reference.get("match_decision"),
        },
    )


def select_historical_draft_safe_image_reference(
    candidate: dict[str, Any],
    references: Sequence[dict[str, Any]],
) -> DecisionResult:
    """
    FB-HIST-only: deterministically pick one draft-safe reference to
    source an image fallback from, out of a candidate's references,
    without requiring match_decision==MATCH -- see
    is_historical_reference_image_draft_safe().

    Ranking reuses the exact same canonical source-priority order as
    select_preferred_image_reference() (REFERENCE_SOURCE_PRIORITY,
    PUBLISHER > AUTHORIZED_SUPPLIER > BOOKSTORE > FAHASA); a same-
    priority tie prefers a real MATCH over POSSIBLE_MATCH/MANUAL_REVIEW,
    then falls back to list order (deterministic, since callers always
    pass the same rows in the same order). No image-selection or write
    ever happens here -- evidence["reference_id"] is the only output a
    caller acts on.
    """
    candidates_for_selection = [
        reference
        for reference in references
        if reference.get("match_decision") in _DRAFT_SAFE_MATCH_DECISIONS
        and reference.get("source_type") in REFERENCE_SOURCE_PRIORITY
    ]

    evaluated = [
        (reference, is_historical_reference_image_draft_safe(candidate, reference))
        for reference in candidates_for_selection
    ]

    passing = [
        (reference, decision)
        for reference, decision in evaluated
        if decision.outcome == Outcome.AUTO_PASS
    ]

    if not passing:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=IMAGE_REFERENCE_NONE_DRAFT_SAFE,
            reason=(
                "No reference is draft-safe for an image fallback "
                f"(checked {len(candidates_for_selection)} candidate "
                "reference(s))."
            ),
            evidence={"checked_count": len(candidates_for_selection)},
        )

    best_priority = min(
        REFERENCE_SOURCE_PRIORITY[reference["source_type"]]
        for reference, _ in passing
    )
    top_tier = [
        (reference, decision)
        for reference, decision in passing
        if REFERENCE_SOURCE_PRIORITY[reference["source_type"]] == best_priority
    ]
    top_tier.sort(
        key=lambda pair: _MATCH_DECISION_RANK.get(
            pair[0].get("match_decision"), 3
        )
    )

    selected_reference, selected_decision = top_tier[0]

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=selected_decision.rule_code,
        reason=selected_decision.reason,
        evidence=selected_decision.evidence,
    )
