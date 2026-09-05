"""
Regression tests for the FB-HIST "identity insufficient evidence"
recovery in pipeline_state.py::_derive_pre_product_state (CLAUDE.md
section 6.2/9.4): a historical candidate whose sole remaining reference
was already evaluated by match_candidate_identity.py and found
insufficient evidence (no active conflict -- e.g. a missing author,
never an invented one) must not stay stuck at REFERENCE_COLLECTED
forever just because match_decision was never written. Live candidates
keep the exact original behavior.
"""
from __future__ import annotations

from typing import Any

import pytest

from pipeline_state import derive_candidate_state, load_candidate_bundle

from support.fake_supabase import FakeSupabaseRepository


HIST_CANDIDATE_ID = "66666666-6666-6666-6666-666666666601"
HIST_CANDIDATE_CODE = "FB-HIST-2026-EVIDENCE-CAN-0001"
LIVE_CANDIDATE_ID = "66666666-6666-6666-6666-666666666602"
LIVE_CANDIDATE_CODE = "FB-2026-001-CAN-0088"


def _candidate(**overrides: Any) -> dict[str, Any]:
    row = {
        "candidate_id": HIST_CANDIDATE_ID,
        "candidate_code": HIST_CANDIDATE_CODE,
        "raw_page_id": None,
        "candidate_type": "SINGLE_BOOK",
        "identity_status": "IDENTITY_PENDING",
        "extracted_title": "Trên Đường Băng",
        "verified_title": None,
        "review_required": True,
        "review_reason": "Title matches strongly, but author data is missing.",
        "conflict_fields": [],
        "identity_confidence": "0.8500",
        "source_evidence": {"decision_fingerprint": "abc123"},
    }
    row.update(overrides)
    return row


def _reference(**overrides: Any) -> dict[str, Any]:
    row = {
        "reference_id": "ref-1",
        "candidate_id": HIST_CANDIDATE_ID,
        "match_decision": None,
        "source_type": "FAHASA",
        "source_url_id": "source-url-1",
    }
    row.update(overrides)
    return row


def _repository(candidate: dict[str, Any], references: list[dict[str, Any]]) -> FakeSupabaseRepository:
    return FakeSupabaseRepository(
        tables={
            "product_candidates": [candidate],
            "product_references": references,
            "candidate_reference_sources": [],
            "product_images": [],
        }
    )


def test_historical_candidate_evaluated_insufficient_evidence_progresses():
    """Fingerprint stamped (already evaluated), conflict_fields empty
    (no genuine conflict), match_decision still None -- must progress to
    IDENTITY_PENDING_HISTORICAL_DRAFT_SAFE, not stay REFERENCE_COLLECTED."""
    candidate = _candidate()
    reference = _reference()
    repository = _repository(candidate, [reference])

    bundle = load_candidate_bundle(repository, HIST_CANDIDATE_CODE)
    state = derive_candidate_state(bundle)

    assert state.derived_state == "IDENTITY_PENDING_HISTORICAL_DRAFT_SAFE"
    assert state.human_gate is False


def test_historical_candidate_never_evaluated_stays_reference_collected():
    """No decision_fingerprint at all -- match_candidate_identity.py has
    never actually run for this candidate. Must NOT be treated as
    "evaluated, insufficient evidence" -- stays REFERENCE_COLLECTED so
    the orchestrator still dispatches match_candidate_identity.py."""
    candidate = _candidate(source_evidence={})
    reference = _reference()
    repository = _repository(candidate, [reference])

    bundle = load_candidate_bundle(repository, HIST_CANDIDATE_CODE)
    state = derive_candidate_state(bundle)

    assert state.derived_state == "REFERENCE_COLLECTED"


def test_historical_candidate_with_genuine_conflict_field_stays_reference_collected():
    """A stamped fingerprint alone is not sufficient -- a real conflict
    signal (e.g. author) must still block, not be silently waved through
    as "just missing evidence"."""
    candidate = _candidate(conflict_fields=["author"])
    reference = _reference()
    repository = _repository(candidate, [reference])

    bundle = load_candidate_bundle(repository, HIST_CANDIDATE_CODE)
    state = derive_candidate_state(bundle)

    assert state.derived_state == "REFERENCE_COLLECTED"


def test_historical_candidate_identity_status_and_author_are_never_touched():
    """Pure derivation -- no write ever happens, and nothing invents the
    missing author. identity_status stays exactly IDENTITY_PENDING."""
    candidate = _candidate()
    reference = _reference()
    repository = _repository(candidate, [reference])

    bundle = load_candidate_bundle(repository, HIST_CANDIDATE_CODE)
    derive_candidate_state(bundle)

    stored = repository.client.tables["product_candidates"][0]
    assert stored["identity_status"] == "IDENTITY_PENDING"
    assert "verified_author" not in stored or stored.get("verified_author") is None
    stored_reference = repository.client.tables["product_references"][0]
    assert stored_reference["match_decision"] is None


def test_live_candidate_same_shape_is_completely_unaffected():
    """The exact same fingerprint/no-conflict/unresolved-reference shape
    on a live (non-historical) candidate must still stop at
    REFERENCE_COLLECTED -- this recovery is FB-HIST-only."""
    candidate = _candidate(
        candidate_id=LIVE_CANDIDATE_ID,
        candidate_code=LIVE_CANDIDATE_CODE,
    )
    reference = _reference(candidate_id=LIVE_CANDIDATE_ID)
    repository = _repository(candidate, [reference])

    bundle = load_candidate_bundle(repository, LIVE_CANDIDATE_CODE)
    state = derive_candidate_state(bundle)

    assert state.derived_state == "REFERENCE_COLLECTED"
