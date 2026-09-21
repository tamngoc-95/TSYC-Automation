"""
Regression tests for pipeline_state.py::_derive_pre_product_state's
reference-source selection check.

Discovered during a bounded historical pilot (2026-09-21): 3 FB-HIST-*
candidates were reported REFERENCE_REGISTERED ("safe to auto-invoke
collect_reference_metadata.py") even though their sole selected
candidate_reference_sources row had already permanently failed to crawl
(discovery_status == "FAILED", e.g. "No metadata parser is available for
domain: ..."). collect_reference_metadata.py's own
select_next_reference_queue_item() requires discovery_status == "SELECTED"
(plus source_urls.crawl_status == "PENDING") and raises RuntimeError for
anything else, so run_batch.py kept redispatching the same doomed stage
every run without ever surfacing it for review.

The fix makes derive_candidate_state() check discovery_status the same
way the collector does, so a FAILED discovery can no longer masquerade as
REFERENCE_REGISTERED -- it now routes to the same historical draft-safe /
human-gate outcomes as "no reference found", instead of an infinite
STAGE_FAILED loop.
"""
from __future__ import annotations

from typing import Any

from pipeline_state import derive_candidate_state, load_candidate_bundle

from support.fake_supabase import FakeSupabaseRepository


HIST_CANDIDATE_ID = "77777777-7777-7777-7777-777777770001"
HIST_CANDIDATE_CODE = "FB-HIST-2026-REFCRAWL-CAN-0001"
LIVE_CANDIDATE_ID = "77777777-7777-7777-7777-777777770002"
LIVE_CANDIDATE_CODE = "FB-2026-001-CAN-0099"


def _candidate(**overrides: Any) -> dict[str, Any]:
    row = {
        "candidate_id": HIST_CANDIDATE_ID,
        "candidate_code": HIST_CANDIDATE_CODE,
        "raw_page_id": None,
        "candidate_type": "SINGLE_BOOK",
        "identity_status": "IDENTITY_PENDING",
        "extracted_title": "Tuyển Tập Kịch",
        "verified_title": None,
        "review_required": True,
        "review_reason": None,
        "conflict_fields": [],
        "identity_confidence": None,
        "source_evidence": {},
    }
    row.update(overrides)
    return row


def _discovery(**overrides: Any) -> dict[str, Any]:
    row = {
        "discovery_id": "discovery-1",
        "candidate_id": HIST_CANDIDATE_ID,
        "source_url_id": "source-url-1",
        "discovery_status": "SELECTED",
        "is_selected_for_crawl": True,
    }
    row.update(overrides)
    return row


def _repository(
    candidate: dict[str, Any],
    discovery_sources: list[dict[str, Any]],
) -> FakeSupabaseRepository:
    return FakeSupabaseRepository(
        tables={
            "product_candidates": [candidate],
            "product_references": [],
            "candidate_reference_sources": discovery_sources,
            "product_images": [],
        }
    )


def test_historical_candidate_with_failed_crawl_no_longer_reports_reference_registered():
    """The exact bug: a selected-but-FAILED discovery must not be
    reported as REFERENCE_REGISTERED (it is not collectible -- the
    collector would immediately raise RuntimeError). It should proceed
    draft-safe with unverified identity instead, matching the "no
    reference found" outcome, not loop forever."""
    candidate = _candidate()
    discovery = _discovery(discovery_status="FAILED")
    repository = _repository(candidate, [discovery])

    bundle = load_candidate_bundle(repository, HIST_CANDIDATE_CODE)
    state = derive_candidate_state(bundle)

    assert state.derived_state == "IDENTITY_PENDING_HISTORICAL_DRAFT_SAFE"
    assert state.human_gate is False


def test_historical_candidate_with_still_selected_crawl_is_unaffected():
    """A discovery that is genuinely still SELECTED (never attempted, or
    attempted but not yet marked FAILED) must keep reporting
    REFERENCE_REGISTERED exactly as before -- this is the normal,
    working path and must not regress."""
    candidate = _candidate()
    discovery = _discovery(discovery_status="SELECTED")
    repository = _repository(candidate, [discovery])

    bundle = load_candidate_bundle(repository, HIST_CANDIDATE_CODE)
    state = derive_candidate_state(bundle)

    assert state.derived_state == "REFERENCE_REGISTERED"
    assert state.human_gate is False


def test_live_candidate_with_failed_crawl_stops_at_human_gate():
    """The same FAILED-discovery shape on a live (non-historical)
    candidate must stop for human review (EXTRACTED, human_gate=True)
    instead of silently proceeding or looping -- CLAUDE.md 5.2/9.2:
    only historical candidates get the draft-safe enrichment-only
    fallback."""
    candidate = _candidate(
        candidate_id=LIVE_CANDIDATE_ID,
        candidate_code=LIVE_CANDIDATE_CODE,
    )
    discovery = _discovery(
        candidate_id=LIVE_CANDIDATE_ID,
        discovery_status="FAILED",
    )
    repository = _repository(candidate, [discovery])

    bundle = load_candidate_bundle(repository, LIVE_CANDIDATE_CODE)
    state = derive_candidate_state(bundle)

    assert state.derived_state == "EXTRACTED"
    assert state.human_gate is True
    assert "failed to crawl" in (state.human_gate_reason or "")


def test_live_candidate_with_still_selected_crawl_is_unaffected():
    """Live-candidate normal working path must also be unchanged."""
    candidate = _candidate(
        candidate_id=LIVE_CANDIDATE_ID,
        candidate_code=LIVE_CANDIDATE_CODE,
    )
    discovery = _discovery(
        candidate_id=LIVE_CANDIDATE_ID,
        discovery_status="SELECTED",
    )
    repository = _repository(candidate, [discovery])

    bundle = load_candidate_bundle(repository, LIVE_CANDIDATE_CODE)
    state = derive_candidate_state(bundle)

    assert state.derived_state == "REFERENCE_REGISTERED"
    assert state.human_gate is False


def test_historical_candidate_no_discovery_at_all_is_unaffected():
    """No candidate_reference_sources row whatsoever must still fall
    through to the pre-existing "no reference found" draft-safe path --
    unrelated to the FAILED-discovery fix, must not regress."""
    candidate = _candidate()
    repository = _repository(candidate, [])

    bundle = load_candidate_bundle(repository, HIST_CANDIDATE_CODE)
    state = derive_candidate_state(bundle)

    assert state.derived_state == "IDENTITY_PENDING_HISTORICAL_DRAFT_SAFE"
    assert state.human_gate is False
