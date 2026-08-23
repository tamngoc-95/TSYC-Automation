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
from typing import Any, Mapping, Sequence

from src.domain.decisions import DecisionResult, Outcome

# --- rule codes ----------------------------------------------------

CONTENT_VERIFIED_FACTS_ONLY = "CONTENT_VERIFIED_FACTS_ONLY"
CONTENT_MISSING_OPTIONAL_METADATA = "CONTENT_MISSING_OPTIONAL_METADATA"
CONTENT_INTERNAL_BOILERPLATE = "CONTENT_INTERNAL_BOILERPLATE"
CONTENT_UNSUPPORTED_CLAIM = "CONTENT_UNSUPPORTED_CLAIM"
CONTENT_REFERENCE_CONFLICT = "CONTENT_REFERENCE_CONFLICT"
CONTENT_SAFE_APPROVAL = "CONTENT_SAFE_APPROVAL"

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
