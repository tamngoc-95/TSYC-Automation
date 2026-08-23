"""The shared deterministic-decision model for the TSYC rule engine.

Every business-rule module under src/domain/rules/ returns a
DecisionResult from this module. This is the one shared vocabulary
production scripts consume instead of independently reimplementing the
same identity/image/content/readiness decisions -- see
docs/TSYC_DECISION_MATRIX.md for the full, human-readable rule
specification this module's rule_codes are drawn from.

Architecture (CLAUDE.md's "Stable Automation Policy"):

    rule engine -> DecisionResult -> bounded writer -> re-read -> verify -> audit

Rule modules are pure functions: given plain dicts (the same shape the
Supabase repository layer already returns), they decide an outcome with
no I/O and no side effects. Writer scripts remain the only place that
persists anything -- a DecisionResult never writes to Supabase itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


class Outcome:
    """Canonical decision outcomes. A rule engine result is always
    exactly one of these four -- there is no fifth "maybe" outcome."""

    # Deterministic evidence supports proceeding automatically. No human
    # decision is required for this specific rule.
    AUTO_PASS = "AUTO_PASS"

    # Deterministic evidence supports refusing automatically (e.g. an
    # image that clearly does not match the product). Distinct from
    # REVIEW_REQUIRED: this is not ambiguous, it is a confirmed no.
    AUTO_REJECT = "AUTO_REJECT"

    # Evidence is ambiguous, conflicting, or otherwise insufficient for
    # a deterministic decision. A human must decide -- CLAUDE.md section
    # 5.2 "Stop only for true ambiguity".
    REVIEW_REQUIRED = "REVIEW_REQUIRED"

    # A structural/safety precondition is unmet (e.g. the candidate row
    # itself was not found, or a hard-deny condition applies). Distinct
    # from REVIEW_REQUIRED: this is not a business judgment call, it is
    # a precondition failure -- retrying the same rule with the same
    # inputs will always fail the same way until the precondition changes.
    BLOCKED = "BLOCKED"


ALL_OUTCOMES = frozenset(
    {
        Outcome.AUTO_PASS,
        Outcome.AUTO_REJECT,
        Outcome.REVIEW_REQUIRED,
        Outcome.BLOCKED,
    }
)

_EMPTY_EVIDENCE: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class DecisionResult:
    """The immutable result of one deterministic business-rule evaluation.

    Attributes:
        outcome: one of Outcome's four canonical values.
        rule_code: the stable, grep-able rule identifier documented in
            docs/TSYC_DECISION_MATRIX.md (e.g. "IDENTITY_EXACT_ISBN").
            Every production decision must carry one -- rule_code must
            never be repurposed for a different meaning once shipped;
            add a new code instead.
        reason: one human-readable sentence explaining the outcome, safe
            to surface in logs, audit output, or a review queue. Must
            never contain a fabricated fact -- only cite evidence the
            caller actually supplied.
        warnings: non-blocking observations (e.g. "ISBN is missing")
            that do not change the outcome but should still be recorded.
        evidence: the specific input values the decision was based on
            (e.g. {"isbn_match": True}), for audit/debugging -- never
            used to change behavior after the fact, purely descriptive.
        confidence: optional 0.0-1.0 score, only where a rule's own
            logic already produces one (e.g. title/author similarity).
            None when a rule's decision is a plain boolean match with no
            graded confidence to report.
    """

    outcome: str
    rule_code: str
    reason: str
    warnings: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=lambda: _EMPTY_EVIDENCE)
    confidence: float | None = None

    def __post_init__(self) -> None:
        if self.outcome not in ALL_OUTCOMES:
            raise ValueError(f"Unknown decision outcome: {self.outcome!r}")
        if not self.rule_code:
            raise ValueError("DecisionResult.rule_code must not be empty.")
        if not self.reason:
            raise ValueError("DecisionResult.reason must not be empty.")
        if self.confidence is not None and not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"confidence must be between 0.0 and 1.0, got {self.confidence!r}"
            )
        if not isinstance(self.evidence, Mapping):
            raise TypeError("DecisionResult.evidence must be a mapping.")

        # Freeze the two mutable-by-default fields so a caller cannot
        # mutate a DecisionResult after the fact through them.
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))

    @property
    def is_auto_pass(self) -> bool:
        return self.outcome == Outcome.AUTO_PASS

    @property
    def is_auto_reject(self) -> bool:
        return self.outcome == Outcome.AUTO_REJECT

    @property
    def requires_human(self) -> bool:
        """True for the two outcomes a bounded writer must stop for
        rather than persist automatically."""
        return self.outcome in (Outcome.REVIEW_REQUIRED, Outcome.BLOCKED)
