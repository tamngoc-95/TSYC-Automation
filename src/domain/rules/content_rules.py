"""Deterministic content-validation rules.

Covers product_contents.content_status (src.domain.content_status.
ContentStatus). CLAUDE.md section 15: content must be based only on
verified data, must never contain internal workflow instructions, and
default generated drafts must be customer-facing. REVISE is a
prepare_product_content.py --action, not a content_status value -- see
src.domain.content_status's module docstring.

Rule codes implemented here:

    CONTENT_VERIFIED_FACTS_ONLY        AUTO_PASS
    CONTENT_MISSING_OPTIONAL_METADATA  AUTO_PASS (non-blocking)
    CONTENT_SAFE_APPROVAL              AUTO_PASS / REVIEW_REQUIRED / BLOCKED
    CONTENT_INTERNAL_BOILERPLATE       AUTO_PASS / REVIEW_REQUIRED
    CONTENT_UNSUPPORTED_CLAIM          REVIEW_REQUIRED
    CONTENT_REFERENCE_CONFLICT         REVIEW_REQUIRED

See docs/TSYC_DECISION_MATRIX.md for the full specification.
"""
from __future__ import annotations

import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from src.domain.decisions import DecisionResult, Outcome
from src.domain.identity_status import MatchDecision
from src.domain.reference_sources import REFERENCE_SOURCE_PRIORITY
from src.domain.rules.identity_rules import reference_business_conflict_reason

# --- rule codes ----------------------------------------------------

CONTENT_VERIFIED_FACTS_ONLY = "CONTENT_VERIFIED_FACTS_ONLY"
CONTENT_MISSING_OPTIONAL_METADATA = "CONTENT_MISSING_OPTIONAL_METADATA"
CONTENT_INTERNAL_BOILERPLATE = "CONTENT_INTERNAL_BOILERPLATE"
CONTENT_UNSUPPORTED_CLAIM = "CONTENT_UNSUPPORTED_CLAIM"
CONTENT_REFERENCE_CONFLICT = "CONTENT_REFERENCE_CONFLICT"
CONTENT_SAFE_APPROVAL = "CONTENT_SAFE_APPROVAL"
# Historical-migration draft-safe content auto-enrichment (CLAUDE.md
# section 6.2/15) -- see select_historical_draft_safe_content_reference().
CONTENT_REFERENCE_DRAFT_SAFE_SELECTED = "CONTENT_REFERENCE_DRAFT_SAFE_SELECTED"
CONTENT_REFERENCE_NONE_DRAFT_SAFE = "CONTENT_REFERENCE_NONE_DRAFT_SAFE"

# A reference description shorter than this is treated as too thin to
# meaningfully identify the product -- CLAUDE.md 15.3 "description cannot
# identify the product meaningfully" is a human-review reason, not
# something this function silently accepts.
_MIN_USABLE_DESCRIPTION_LENGTH = 40

_DRAFT_SAFE_MATCH_DECISIONS = (
    MatchDecision.MATCH,
    MatchDecision.POSSIBLE_MATCH,
    MatchDecision.MANUAL_REVIEW,
)

_MATCH_DECISION_RANK = MappingProxyType(
    {
        MatchDecision.MATCH: 0,
        MatchDecision.POSSIBLE_MATCH: 1,
        MatchDecision.MANUAL_REVIEW: 2,
    }
)

# CLAUDE.md section 15.1's exact forbidden examples, plus close variants.
# Deliberately conservative substring/regex matching -- a false positive
# here just means one extra REVISE cycle, never a silently-shipped
# internal note reaching a customer.
_INTERNAL_WORKFLOW_PATTERNS = (
    re.compile(r"manager (?:must|should) review", re.IGNORECASE),
    re.compile(r"pending (?:manager|admin|staff) review", re.IGNORECASE),
    re.compile(r"should be completed later", re.IGNORECASE),
    re.compile(r"to be (?:filled|completed|updated) later", re.IGNORECASE),
    re.compile(r"\bTODO\b"),
    re.compile(r"\bFIXME\b"),
    re.compile(r"placeholder text", re.IGNORECASE),
)

CUSTOMER_FACING_FIELDS = (
    "short_description",
    "long_description",
    "author_summary",
    "product_details",
    "seo_title",
    "seo_description",
)


def find_internal_workflow_language(
    content: Mapping[str, Any],
    fields: Sequence[str] = CUSTOMER_FACING_FIELDS,
) -> dict[str, str]:
    """Return {field_name: matched_phrase} for every customer-facing
    field that contains internal workflow language."""
    findings: dict[str, str] = {}
    for field in fields:
        value = content.get(field)
        if not value:
            continue
        for pattern in _INTERNAL_WORKFLOW_PATTERNS:
            match = pattern.search(str(value))
            if match:
                findings[field] = match.group(0)
                break
    return findings


def evaluate_internal_boilerplate(content: Mapping[str, Any]) -> DecisionResult:
    """
    CLAUDE.md section 15.1: customer-facing content must never contain
    internal workflow instructions ("manager must review this before
    publishing", "pending manager review", "this description should be
    completed later", ...). Any match routes to REVIEW_REQUIRED with the
    exact offending field/phrase named, so prepare_product_content.py's
    REVISE workflow has an exact deterministic target -- CLAUDE.md
    section 15.2 ("deterministic revise, validate again").
    """
    findings = find_internal_workflow_language(content)

    if findings:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=CONTENT_INTERNAL_BOILERPLATE,
            reason=(
                "Customer-facing content contains internal workflow "
                "language in: " + ", ".join(sorted(findings))
            ),
            evidence={"findings": findings},
        )

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=CONTENT_INTERNAL_BOILERPLATE,
        reason="No internal workflow language found in customer-facing content.",
        evidence={"findings": {}},
    )


def evaluate_unsupported_claims(
    claimed_facts: Mapping[str, Any],
    verifiable_facts: Mapping[str, Any],
) -> DecisionResult:
    """
    REVIEW_REQUIRED when content asserts a fact that is not traceable to
    verified internal_product/reference data.

    claimed_facts and verifiable_facts are keyed the same way (e.g.
    {"author": "...", "publisher": "..."}); a claimed value that is
    present, non-empty, and differs from the corresponding verifiable
    value (when one exists) is treated as unsupported. A claimed fact
    with no corresponding verifiable key at all is also unsupported --
    this function never assumes an un-cross-checked claim is safe.
    """
    unsupported: dict[str, Any] = {}
    for field, claimed_value in claimed_facts.items():
        if not claimed_value:
            continue
        verifiable_value = verifiable_facts.get(field)
        if not verifiable_value:
            unsupported[field] = claimed_value
        elif (
            str(claimed_value).strip().lower()
            != str(verifiable_value).strip().lower()
        ):
            unsupported[field] = claimed_value

    if unsupported:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=CONTENT_UNSUPPORTED_CLAIM,
            reason=(
                "Content claims facts not traceable to verified data: "
                + ", ".join(sorted(unsupported))
            ),
            evidence={"unsupported_fields": unsupported},
        )

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=CONTENT_VERIFIED_FACTS_ONLY,
        reason="All claimed facts are traceable to verified data.",
        evidence={"checked_fields": tuple(sorted(claimed_facts))},
    )


def evaluate_reference_conflict(
    conflicting_fields: Sequence[str],
) -> DecisionResult:
    """
    REVIEW_REQUIRED when verified references disagree on a fact content
    would need to state as settled -- CLAUDE.md section 15.3 "verified
    references conflict".
    """
    if conflicting_fields:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=CONTENT_REFERENCE_CONFLICT,
            reason=(
                "Verified references conflict on: "
                + ", ".join(sorted(conflicting_fields))
            ),
            evidence={"conflicting_fields": tuple(conflicting_fields)},
        )

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=CONTENT_VERIFIED_FACTS_ONLY,
        reason="No reference conflicts affect this content.",
        evidence={"conflicting_fields": ()},
    )


def evaluate_optional_metadata(missing_fields: Sequence[str]) -> DecisionResult:
    """
    Non-blocking: missing optional metadata (ISBN, weight, dimensions,
    page count, ...) is a warning, never a reason to withhold automatic
    content approval -- CLAUDE.md section 2.2.
    """
    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=CONTENT_MISSING_OPTIONAL_METADATA,
        reason=(
            "Optional metadata is missing but non-blocking: "
            + ", ".join(sorted(missing_fields))
            if missing_fields
            else "No optional metadata is missing."
        ),
        warnings=tuple(f"{field} is missing" for field in missing_fields),
        evidence={"missing_fields": tuple(missing_fields)},
    )


def evaluate_safe_approval(
    is_first_draft: bool,
    is_generic_safe_draft: bool,
    checks: Sequence[DecisionResult],
) -> DecisionResult:
    """
    The single gate prepare_product_content.py's --action APPROVE must
    pass through: content_status may only become APPROVED automatically
    once every other content rule result in `checks` (boilerplate,
    unsupported claims, reference conflicts, ...) has AUTO_PASSed, AND
    the content is not still the untouched, metadata-only generated
    draft (mirrors the existing is_generic_safe_draft() safety check).

    Never itself decides what "verified"/"boilerplate"/"unsupported"
    mean -- it only aggregates results the other rules already computed,
    so approval can never silently skip a check that was never run.
    """
    if is_first_draft:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=CONTENT_SAFE_APPROVAL,
            reason="Content has no prior saved draft to approve; save "
            "and enrich it first.",
        )

    if is_generic_safe_draft:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=CONTENT_SAFE_APPROVAL,
            reason="Content is still the generic metadata-only safe "
            "draft; it must be enriched from verified source material "
            "before approval.",
        )

    failing = [check for check in checks if not check.is_auto_pass]

    if failing:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=CONTENT_SAFE_APPROVAL,
            reason=(
                "Content cannot be approved automatically: "
                + "; ".join(f"{c.rule_code}: {c.reason}" for c in failing)
            ),
            evidence={"failing_rule_codes": tuple(c.rule_code for c in failing)},
        )

    all_warnings = tuple(warning for check in checks for warning in check.warnings)

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=CONTENT_SAFE_APPROVAL,
        reason="All content validation rules passed; content may be "
        "approved automatically.",
        warnings=all_warnings,
        evidence={"passed_rule_codes": tuple(c.rule_code for c in checks)},
    )


# --- historical draft-safe content auto-enrichment (CLAUDE.md 6.2/15) ------
#
# For an FB-HIST candidate whose content is still the generic metadata-
# only safe draft (prepare_product_content.py's is_generic_safe_draft()),
# normal enrichment should not require human review when a reference
# already carries verified, non-conflicting descriptive text -- CLAUDE.md
# section 2.2/15.1: content must be based only on verified data, never
# invented. This never applies to a live (non-historical) candidate; no
# caller in this codebase reaches it for one.


def select_historical_draft_safe_content_reference(
    candidate: dict[str, Any],
    references: Sequence[dict[str, Any]],
) -> DecisionResult:
    """
    FB-HIST-only: deterministically pick one reference whose
    reference_description can safely enrich this candidate's generic
    content draft, without requiring match_decision==MATCH.

    A reference is eligible only when all of these hold:
      - match_decision is MATCH, POSSIBLE_MATCH, or MANUAL_REVIEW
      - source_type is a recognized reference source
        (REFERENCE_SOURCE_PRIORITY)
      - reference_description exists and is at least
        _MIN_USABLE_DESCRIPTION_LENGTH characters (not too thin to
        meaningfully identify the product)
      - no ISBN/title/publisher/author/sellable-unit conflict with the
        candidate (identity_rules.reference_business_conflict_reason) --
        the same shared check image_rules.
        is_historical_reference_image_draft_safe uses, so "source
        evidence conflicts" and "wrong sellable unit" are refused
        identically for images and content.

    Ranking mirrors image_rules.select_historical_draft_safe_image_
    reference(): canonical source priority first, then a same-priority
    tie prefers a real MATCH over POSSIBLE_MATCH/MANUAL_REVIEW.

    Never invents a description -- evidence["reference_description"] is
    always the exact, unmodified text already collected and stored by
    collect_reference_metadata.py from an approved source. Never decides
    match_decision or identity_status.
    """
    candidates_for_selection = [
        reference
        for reference in references
        if reference.get("match_decision") in _DRAFT_SAFE_MATCH_DECISIONS
        and reference.get("source_type") in REFERENCE_SOURCE_PRIORITY
        and len(str(reference.get("reference_description") or "").strip())
        >= _MIN_USABLE_DESCRIPTION_LENGTH
    ]

    passing: list[tuple[dict[str, Any], str | None]] = []

    for reference in candidates_for_selection:
        conflict_reason = reference_business_conflict_reason(candidate, reference)

        if conflict_reason is None:
            passing.append((reference, None))

    if not passing:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=CONTENT_REFERENCE_NONE_DRAFT_SAFE,
            reason=(
                "No reference has a usable, non-conflicting description "
                f"for auto-enrichment (checked {len(candidates_for_selection)} "
                "candidate reference(s) with a description)."
            ),
            evidence={"checked_count": len(candidates_for_selection)},
        )

    best_priority = min(
        REFERENCE_SOURCE_PRIORITY[reference["source_type"]]
        for reference, _ in passing
    )
    top_tier = [
        (reference, _)
        for reference, _ in passing
        if REFERENCE_SOURCE_PRIORITY[reference["source_type"]] == best_priority
    ]
    top_tier.sort(
        key=lambda pair: _MATCH_DECISION_RANK.get(pair[0].get("match_decision"), 3)
    )

    selected_reference = top_tier[0][0]

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=CONTENT_REFERENCE_DRAFT_SAFE_SELECTED,
        reason=(
            "Reference is draft-safe for content enrichment: recognized "
            "source, usable description, no identity/sellable-unit "
            "conflict."
        ),
        evidence={
            "reference_id": selected_reference.get("reference_id"),
            "source_type": selected_reference.get("source_type"),
            "reference_description": selected_reference.get(
                "reference_description"
            ),
            "match_decision": selected_reference.get("match_decision"),
        },
    )
