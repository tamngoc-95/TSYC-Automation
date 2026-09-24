"""
Read-only operational-lane classifier (CLAUDE_AUTOMATION.md section 6).

Maps an already-derived scripts.pipeline_state.CandidateState (plus a
multilingual-completeness signal, when relevant) onto exactly one of the
lane constants CLAUDE_AUTOMATION.md section 6/18 defines, so the Cowork
controller can classify the backlog (section 18 STEP 2) and choose the
next safe action (STEP 3) without a second copy of pipeline_state.py's
own state-machine business logic.

Pure function: no I/O, no decisions beyond relabeling fields
pipeline_state.py already computed. It never weakens, widens, or
re-evaluates a human_gate/blocked/recovery_state pipeline_state.py
already set -- scripts.pipeline_state.derive_candidate_state() remains
the single place that decides whether a candidate needs a human or
recovery gate (CLAUDE.md section 5).

HISTORICAL_RECOVERY_BACKLOG is deliberately not something classify_lane()
can return: it counts raw historical source posts that have not yet been
turned into any product_candidates row at all, which is a fact about
raw_pages, not about one candidate's derived state. See
scripts/export_historical_orchestration_state.py for that count.
"""

from __future__ import annotations

from typing import Any, Protocol

READY_FOR_DRAFT = "READY_FOR_DRAFT"
MULTILINGUAL_CONTENT = "MULTILINGUAL_CONTENT"
FAST_TRACK = "FAST_TRACK"
ENRICHMENT_NEEDED = "ENRICHMENT_NEEDED"
HUMAN_REVIEW = "HUMAN_REVIEW"
RECOVERY_REVIEW = "RECOVERY_REVIEW"
CONFLICT = "CONFLICT"
TERMINAL = "TERMINAL"
HISTORICAL_RECOVERY_BACKLOG = "HISTORICAL_RECOVERY_BACKLOG"

# The full nine-lane vocabulary CLAUDE_AUTOMATION.md names. classify_lane()
# only ever returns one of the first eight; HISTORICAL_RECOVERY_BACKLOG is
# listed here only so callers can validate/iterate the complete set.
LANES = (
    READY_FOR_DRAFT,
    MULTILINGUAL_CONTENT,
    FAST_TRACK,
    ENRICHMENT_NEEDED,
    HUMAN_REVIEW,
    RECOVERY_REVIEW,
    CONFLICT,
    TERMINAL,
    HISTORICAL_RECOVERY_BACKLOG,
)

_READY_FOR_DRAFT_DERIVED_STATES = {"READY_FOR_DRAFT", "READY_FOR_DRAFT_HISTORICAL"}

# derived_state values that need no further Fast Track action even though
# scripts.pipeline_state.CandidateState.terminal is only set True for a
# subset of them (RECONCILED, DUPLICATE_REJECTED) -- DRAFT_CREATED sits
# between Woo draft creation and the separate reconciliation step
# (CLAUDE.md pipeline stage list) and is likewise not Fast Track work.
_TERMINAL_DERIVED_STATES = {"RECONCILED", "DRAFT_CREATED", "DUPLICATE_REJECTED"}

# Matches src.domain.content_status.InternalProductContentStatus.APPROVED's
# value without importing it, so this module stays dependency-free and
# directly unit-testable with plain dicts (same design choice
# scripts/pipeline_state.py documents for itself).
_CONTENT_APPROVED = "APPROVED"


class _CandidateStateLike(Protocol):
    """Structural shape classify_lane() needs. Matches
    scripts.pipeline_state.CandidateState's public fields without
    importing that module: scripts/ sits one layer above
    src/domain/rules/, and importing it here would invert that
    dependency direction."""

    derived_state: str
    recovery_state: str | None
    human_gate: bool
    terminal: bool
    blocked: bool
    product_code: str | None


def is_ready_for_draft_state(derived_state: str) -> bool:
    """True for the derived states that would dispatch Woo draft creation."""
    return derived_state in _READY_FOR_DRAFT_DERIVED_STATES


def has_multilingual_content(contents: list[dict[str, Any]]) -> bool:
    """True once both an 'en' and a 'de' product_contents row exist with
    content_status == APPROVED. Reads only rows that already exist in the
    schema (product_contents.content_language IN ('vi', 'de', 'en')) --
    this never invents a translation or a status."""
    approved_languages = {
        content.get("content_language")
        for content in contents
        if content.get("content_status") == _CONTENT_APPROVED
    }
    return "en" in approved_languages and "de" in approved_languages


def classify_lane(
    state: _CandidateStateLike,
    *,
    multilingual_ready: bool = True,
) -> str:
    """
    Classify one already-derived candidate state into exactly one
    CLAUDE_AUTOMATION.md lane (section 6). Precedence (highest first):

    1. RECOVERY_REVIEW -- state.recovery_state is set (never blind-retry
       an uncertain Woo operation, CLAUDE.md 2.6/18).
    2. CONFLICT -- state.derived_state == IDENTITY_CONFLICT.
    3. READY_FOR_DRAFT -- state.derived_state is
       READY_FOR_DRAFT(_HISTORICAL), regardless of state.human_gate (the
       Woo-draft human gate is the single required business approval,
       CLAUDE.md section 6 -- it lands in this lane, not HUMAN_REVIEW) --
       but only once multilingual content is ready (APPROVED en AND de,
       CLAUDE_AUTOMATION.md section 5 Priority 1). A READY_FOR_DRAFT
       state without it is MULTILINGUAL_CONTENT instead, never
       READY_FOR_DRAFT.
    4. TERMINAL -- state.terminal, or a derived_state that needs no
       further Fast Track action (RECONCILED/DRAFT_CREATED/
       DUPLICATE_REJECTED).
    5. HUMAN_REVIEW -- state.human_gate (every other human gate
       pipeline_state.py already decided).
    6. ENRICHMENT_NEEDED -- state.blocked (a structural/deterministic
       blocker, never a judgment call).
    7. MULTILINGUAL_CONTENT -- an internal product exists
       (state.product_code) but multilingual_ready is False.
    8. FAST_TRACK -- none of the above: the candidate can advance through
       its next deterministic stage unattended.
    """
    if state.recovery_state is not None:
        return RECOVERY_REVIEW

    if state.derived_state == "IDENTITY_CONFLICT":
        return CONFLICT

    if state.derived_state in _READY_FOR_DRAFT_DERIVED_STATES:
        if not multilingual_ready:
            return MULTILINGUAL_CONTENT
        return READY_FOR_DRAFT

    if state.terminal or state.derived_state in _TERMINAL_DERIVED_STATES:
        return TERMINAL

    if state.human_gate:
        return HUMAN_REVIEW

    if state.blocked:
        return ENRICHMENT_NEEDED

    if state.product_code and not multilingual_ready:
        return MULTILINGUAL_CONTENT

    return FAST_TRACK
