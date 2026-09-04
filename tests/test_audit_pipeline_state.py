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


def _candidate(
    candidate_id: str,
    identity_status: str,
    candidate_code: str | None = None,
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "candidate_code": candidate_code or f"TEST-{candidate_id}",
        "identity_status": identity_status,
    }


def _product(
    candidate_id: str,
    primary_reference_id: str | None,
    product_code: str | None = None,
) -> dict[str, Any]:
    return {
        "internal_product_id": f"ip-{candidate_id}",
        "product_code": product_code or f"TSYC-TEST-{candidate_id}",
        "candidate_id": candidate_id,
        "primary_reference_id": primary_reference_id,
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


# ---------------------------------------------------------------------
# Historical draft-safe primary-reference carve-out (CLAUDE.md 6.2/9.4/13)
#
# create_internal_product.py deliberately creates an FB-HIST internal
# product with a POSSIBLE_MATCH/MANUAL_REVIEW primary reference (or none
# at all) when no MATCH reference exists -- these must be warnings, not
# audit ERRORs, or scripts/run_batch.py stops the entire batch on the
# very first historical candidate it processes (confirmed in production:
# FB-HIST-2026-001-CAN-0001, 2026-09-04).
# ---------------------------------------------------------------------


def test_historical_candidate_with_possible_match_primary_reference_is_warning():
    candidate = _candidate("h1", "IDENTITY_PENDING", "FB-HIST-2026-001-CAN-0001")
    reference = _reference("h1", "POSSIBLE_MATCH", "ref-h1")
    product = _product("h1", "ref-h1")
    issues: list[dict[str, str]] = []

    audit.audit_references(
        candidates=[candidate],
        references=[reference],
        products=[product],
        issues=issues,
    )

    assert "PRIMARY_REFERENCE_NOT_MATCHED" not in _issue_codes(issues)
    (issue,) = [
        i for i in issues if i["code"] == "PRIMARY_REFERENCE_NOT_MATCHED_HISTORICAL"
    ]
    assert issue["severity"] == "WARNING"
    assert issue["entity"] == product["product_code"]


def test_historical_candidate_with_manual_review_primary_reference_is_warning():
    candidate = _candidate("h1b", "IDENTITY_PENDING", "FB-HIST-2026-AUTOIMPORT-CAN-0002")
    reference = _reference("h1b", "MANUAL_REVIEW", "ref-h1b")
    product = _product("h1b", "ref-h1b")
    issues: list[dict[str, str]] = []

    audit.audit_references(
        candidates=[candidate],
        references=[reference],
        products=[product],
        issues=issues,
    )

    assert "PRIMARY_REFERENCE_NOT_MATCHED" not in _issue_codes(issues)
    assert "PRIMARY_REFERENCE_NOT_MATCHED_HISTORICAL" in _issue_codes(issues)


def test_historical_candidate_with_no_reference_at_all_is_warning_not_error():
    candidate = _candidate("h2", "IDENTITY_PENDING", "FB-HIST-2026-001-CAN-0002")
    product = _product("h2", None)
    issues: list[dict[str, str]] = []

    audit.audit_references(
        candidates=[candidate], references=[], products=[product], issues=issues
    )

    assert "PRIMARY_REFERENCE_MISSING" not in _issue_codes(issues)
    (issue,) = [
        i for i in issues if i["code"] == "PRIMARY_REFERENCE_MISSING_HISTORICAL"
    ]
    assert issue["severity"] == "WARNING"


def test_historical_candidate_no_match_decision_at_all_is_still_error():
    """A reference with match_decision=None (never evaluated) is not the
    accepted historical enrichment set (POSSIBLE_MATCH/MANUAL_REVIEW) --
    stays an ERROR even for a historical candidate."""
    candidate = _candidate("h3", "IDENTITY_PENDING", "FB-HIST-2026-001-CAN-0003")
    reference = _reference("h3", None, "ref-h3")
    product = _product("h3", "ref-h3")
    issues: list[dict[str, str]] = []

    audit.audit_references(
        candidates=[candidate],
        references=[reference],
        products=[product],
        issues=issues,
    )

    assert "PRIMARY_REFERENCE_NOT_MATCHED" in _issue_codes(issues)
    assert "PRIMARY_REFERENCE_NOT_MATCHED_HISTORICAL" not in _issue_codes(issues)


def test_live_candidate_primary_reference_not_matched_stays_error():
    """Non-historical (FB-2026-*) behavior is completely unchanged: a
    primary reference without match_decision=MATCH is still a hard
    ERROR."""
    candidate = _candidate("l1", "IDENTITY_VERIFIED", "FB-2026-001-CAN-0001")
    reference = _reference("l1", "POSSIBLE_MATCH", "ref-l1")
    product = _product("l1", "ref-l1")
    issues: list[dict[str, str]] = []

    audit.audit_references(
        candidates=[candidate],
        references=[reference],
        products=[product],
        issues=issues,
    )

    assert "PRIMARY_REFERENCE_NOT_MATCHED" in _issue_codes(issues)
    (issue,) = [i for i in issues if i["code"] == "PRIMARY_REFERENCE_NOT_MATCHED"]
    assert issue["severity"] == "ERROR"


def test_live_candidate_with_no_primary_reference_stays_error():
    candidate = _candidate("l2", "IDENTITY_VERIFIED", "FB-2026-001-CAN-0002")
    product = _product("l2", None)
    issues: list[dict[str, str]] = []

    audit.audit_references(
        candidates=[candidate], references=[], products=[product], issues=issues
    )

    (issue,) = [i for i in issues if i["code"] == "PRIMARY_REFERENCE_MISSING"]
    assert issue["severity"] == "ERROR"


def test_historical_candidate_reference_candidate_mismatch_stays_error():
    """A primary reference pointing at a different candidate is a real
    data-integrity bug, not an enrichment shape -- stays an ERROR even
    for a historical candidate."""
    candidate = _candidate("h4", "IDENTITY_PENDING", "FB-HIST-2026-001-CAN-0004")
    reference = _reference("someone-else", "POSSIBLE_MATCH", "ref-h4")
    product = _product("h4", "ref-h4")
    issues: list[dict[str, str]] = []

    audit.audit_references(
        candidates=[candidate],
        references=[reference],
        products=[product],
        issues=issues,
    )

    assert "PRIMARY_REFERENCE_CANDIDATE_MISMATCH" in _issue_codes(issues)
