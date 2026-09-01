"""
Regression tests for scripts/audit_pipeline_state.py's identity-reference
invariant (VERIFIED_IDENTITY_WITHOUT_MATCH_REFERENCE) -- the check that
caught the confirmed FB-HIST-2026 incident (5 candidates, 2026-08-31):
identity_status=IDENTITY_VERIFIED persisted with zero product_references
rows carrying match_decision=MATCH.

Pure-function, fully offline: audit_references() takes plain lists, no
Supabase/network access.
"""

from __future__ import annotations

from typing import Any

import audit_pipeline_state as audit


def _candidate(candidate_id: str, identity_status: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "candidate_code": f"TEST-{candidate_id}",
        "identity_status": identity_status,
    }


def _reference(
    candidate_id: str,
    match_decision: str | None,
    reference_id: str = "ref-1",
) -> dict[str, Any]:
    return {
        "reference_id": reference_id,
        "candidate_id": candidate_id,
        "match_decision": match_decision,
    }


def _issue_codes(issues: list[dict[str, str]]) -> list[str]:
    return [issue["code"] for issue in issues]


def test_deliberately_invalid_verified_without_match_fixture_is_detected():
    """The exact FB-HIST-2026 shape: IDENTITY_VERIFIED with references
    that exist but are all POSSIBLE_MATCH, never MATCH."""
    candidate = _candidate("cand-1", "IDENTITY_VERIFIED")
    references = [
        _reference("cand-1", "POSSIBLE_MATCH", "ref-1"),
        _reference("cand-1", "POSSIBLE_MATCH", "ref-2"),
    ]
    issues: list[dict[str, str]] = []

    audit.audit_references(
        candidates=[candidate], references=references, products=[], issues=issues
    )

    assert "VERIFIED_IDENTITY_WITHOUT_MATCH_REFERENCE" in _issue_codes(issues)
    (issue,) = [
        i for i in issues if i["code"] == "VERIFIED_IDENTITY_WITHOUT_MATCH_REFERENCE"
    ]
    assert issue["severity"] == "ERROR"
    assert issue["entity"] == "TEST-cand-1"


def test_verified_with_zero_references_at_all_is_also_detected():
    candidate = _candidate("cand-2", "IDENTITY_VERIFIED")
    issues: list[dict[str, str]] = []

    audit.audit_references(
        candidates=[candidate], references=[], products=[], issues=issues
    )

    assert "VERIFIED_IDENTITY_WITHOUT_MATCH_REFERENCE" in _issue_codes(issues)


def test_genuinely_verified_candidate_with_a_match_reference_is_clean():
    candidate = _candidate("cand-3", "IDENTITY_VERIFIED")
    references = [
        _reference("cand-3", "MATCH", "ref-1"),
        _reference("cand-3", "POSSIBLE_MATCH", "ref-2"),
    ]
    issues: list[dict[str, str]] = []

    audit.audit_references(
        candidates=[candidate], references=references, products=[], issues=issues
    )

    assert "VERIFIED_IDENTITY_WITHOUT_MATCH_REFERENCE" not in _issue_codes(issues)


def test_non_verified_candidate_with_no_match_reference_is_not_flagged():
    """This check is scoped to IDENTITY_VERIFIED only -- an ordinary
    IDENTITY_PENDING candidate with only POSSIBLE_MATCH references is a
    completely normal, unfinished state, not an audit error."""
    candidate = _candidate("cand-4", "IDENTITY_PENDING")
    references = [_reference("cand-4", "POSSIBLE_MATCH", "ref-1")]
    issues: list[dict[str, str]] = []

    audit.audit_references(
        candidates=[candidate], references=references, products=[], issues=issues
    )

    assert "VERIFIED_IDENTITY_WITHOUT_MATCH_REFERENCE" not in _issue_codes(issues)
