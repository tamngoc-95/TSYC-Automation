"""
Regression tests for download_bookstore_product_image.py's approved
source-priority fallback (CLAUDE.md section 8.1): when the highest-
priority reference's image is dead, the next eligible reference is
tried instead of failing the candidate outright.

No live Supabase/Playwright dependency -- FakeSupabaseRepository backs
every read, and resolve_preferred_match_reference() itself never
launches a browser (only main()'s retry loop does, which these tests
do not exercise).
"""
from __future__ import annotations

from typing import Any

import pytest

from download_bookstore_product_image import (
    ImageSourceUnusableError,
    resolve_preferred_match_reference,
)

from support.fake_supabase import FakeSupabaseRepository


LIVE_CANDIDATE_ID = "55555555-5555-5555-5555-555555555501"
LIVE_CANDIDATE_CODE = "FB-2026-001-CAN-0099"
HIST_CANDIDATE_ID = "55555555-5555-5555-5555-555555555502"
HIST_CANDIDATE_CODE = "FB-HIST-2026-FALLBACK-CAN-0001"


def _live_candidate(**overrides: Any) -> dict[str, Any]:
    row = {
        "candidate_id": LIVE_CANDIDATE_ID,
        "candidate_code": LIVE_CANDIDATE_CODE,
        "candidate_type": "SINGLE_BOOK",
        "identity_status": "IDENTITY_VERIFIED",
        "extracted_title": "Thép Đã Tôi Thế Đấy",
        "verified_title": "Thép Đã Tôi Thế Đấy",
        "verified_author": None,
        "verified_isbn": None,
        "verified_publisher": None,
        "possible_isbn": None,
    }
    row.update(overrides)
    return row


def _hist_candidate(**overrides: Any) -> dict[str, Any]:
    row = {
        "candidate_id": HIST_CANDIDATE_ID,
        "candidate_code": HIST_CANDIDATE_CODE,
        "candidate_type": "SINGLE_BOOK",
        "identity_status": "IDENTITY_PENDING",
        "extracted_title": "Ngựa Ô Yêu Dấu",
        "verified_title": "Ngựa Ô Yêu Dấu",
        "verified_author": None,
        "verified_isbn": None,
        "verified_publisher": None,
        "possible_isbn": None,
    }
    row.update(overrides)
    return row


def _reference(**overrides: Any) -> dict[str, Any]:
    row = {
        "reference_id": "ref-1",
        "candidate_id": LIVE_CANDIDATE_ID,
        "source_type": "BOOKSTORE",
        "source_url_id": "source-url-1",
        "match_decision": "MATCH",
        "reference_title": "Thép Đã Tôi Thế Đấy",
        "reference_isbn": None,
        "reference_author": None,
        "reference_publisher": None,
        "reference_image_url": "https://dinhtibooks.com.vn/thep-da-toi-the-day.jpg",
    }
    row.update(overrides)
    return row


def _repository(candidate: dict[str, Any], references: list[dict[str, Any]]) -> FakeSupabaseRepository:
    return FakeSupabaseRepository(
        tables={
            "product_candidates": [candidate],
            "product_references": references,
        }
    )


def test_live_candidate_excludes_already_tried_reference_and_picks_next():
    """Two MATCH references, BOOKSTORE (higher priority) and FAHASA;
    excluding the BOOKSTORE one (its image was dead) returns the FAHASA
    reference instead of raising."""
    candidate = _live_candidate()
    bookstore_ref = _reference(
        reference_id="ref-bookstore",
        source_type="BOOKSTORE",
    )
    fahasa_ref = _reference(
        reference_id="ref-fahasa",
        source_type="FAHASA",
        reference_image_url="https://cdn1.fahasa.com/thep-da-toi-the-day.jpg",
    )
    repository = _repository(candidate, [bookstore_ref, fahasa_ref])

    first = resolve_preferred_match_reference(repository=repository, candidate=candidate)
    assert first["reference_id"] == "ref-bookstore"

    second = resolve_preferred_match_reference(
        repository=repository,
        candidate=candidate,
        exclude_reference_ids=frozenset({"ref-bookstore"}),
    )
    assert second["reference_id"] == "ref-fahasa"


def test_live_candidate_raises_once_all_references_excluded():
    """Excluding every MATCH reference must fail closed -- never invent
    or silently reuse an already-tried source."""
    candidate = _live_candidate()
    bookstore_ref = _reference(reference_id="ref-bookstore", source_type="BOOKSTORE")
    repository = _repository(candidate, [bookstore_ref])

    with pytest.raises(RuntimeError, match="No MATCH product_reference exists"):
        resolve_preferred_match_reference(
            repository=repository,
            candidate=candidate,
            exclude_reference_ids=frozenset({"ref-bookstore"}),
        )


def test_historical_candidate_fallback_excludes_and_picks_next():
    """FB-HIST candidate, no MATCH reference -- two POSSIBLE_MATCH/
    MANUAL_REVIEW references (BOOKSTORE, FAHASA); excluding the
    BOOKSTORE one falls through to the FAHASA one."""
    candidate = _hist_candidate()
    bookstore_ref = _reference(
        reference_id="ref-h-bookstore",
        candidate_id=HIST_CANDIDATE_ID,
        source_type="BOOKSTORE",
        match_decision="POSSIBLE_MATCH",
        reference_title="Ngựa Ô Yêu Dấu",
        reference_image_url="https://dinhtibooks.com.vn/ngua-o-yeu-dau.jpg",
    )
    fahasa_ref = _reference(
        reference_id="ref-h-fahasa",
        candidate_id=HIST_CANDIDATE_ID,
        source_type="FAHASA",
        match_decision="MANUAL_REVIEW",
        reference_title="Ngựa Ô Yêu Dấu (Tái Bản 2020)",
        reference_image_url="https://cdn1.fahasa.com/ngua-o-yeu-dau.jpg",
    )
    repository = _repository(candidate, [bookstore_ref, fahasa_ref])

    first = resolve_preferred_match_reference(repository=repository, candidate=candidate)
    assert first["reference_id"] == "ref-h-bookstore"

    second = resolve_preferred_match_reference(
        repository=repository,
        candidate=candidate,
        exclude_reference_ids=frozenset({"ref-h-bookstore"}),
    )
    assert second["reference_id"] == "ref-h-fahasa"


def test_historical_candidate_raises_once_all_references_excluded():
    candidate = _hist_candidate()
    bookstore_ref = _reference(
        reference_id="ref-h-bookstore",
        candidate_id=HIST_CANDIDATE_ID,
        source_type="BOOKSTORE",
        match_decision="POSSIBLE_MATCH",
        reference_title="Ngựa Ô Yêu Dấu",
    )
    repository = _repository(candidate, [bookstore_ref])

    with pytest.raises(RuntimeError, match="No product_reference exists"):
        resolve_preferred_match_reference(
            repository=repository,
            candidate=candidate,
            exclude_reference_ids=frozenset({"ref-h-bookstore"}),
        )


def test_exclusion_never_touches_match_decision_or_identity_status():
    """The exclude/retry mechanism is a pure selection filter -- it must
    never write anything. FakeSupabaseRepository raises on any
    unexpected table mutation attempt would surface here if the function
    tried one; simply asserting the rows are unchanged is the direct
    proof CLAUDE.md section 9 (image sourcing and identity verification
    are separate concerns) requires."""
    candidate = _hist_candidate()
    bookstore_ref = _reference(
        reference_id="ref-h-bookstore",
        candidate_id=HIST_CANDIDATE_ID,
        source_type="BOOKSTORE",
        match_decision="POSSIBLE_MATCH",
        reference_title="Ngựa Ô Yêu Dấu",
    )
    fahasa_ref = _reference(
        reference_id="ref-h-fahasa",
        candidate_id=HIST_CANDIDATE_ID,
        source_type="FAHASA",
        match_decision="MANUAL_REVIEW",
        reference_title="Ngựa Ô Yêu Dấu (Tái Bản 2020)",
        reference_image_url="https://cdn1.fahasa.com/ngua-o-yeu-dau.jpg",
    )
    repository = _repository(candidate, [bookstore_ref, fahasa_ref])

    resolve_preferred_match_reference(
        repository=repository,
        candidate=candidate,
        exclude_reference_ids=frozenset({"ref-h-bookstore"}),
    )

    stored_candidate = repository.client.tables["product_candidates"][0]
    assert stored_candidate["identity_status"] == "IDENTITY_PENDING"

    for reference in repository.client.tables["product_references"]:
        if reference["reference_id"] == "ref-h-bookstore":
            assert reference["match_decision"] == "POSSIBLE_MATCH"
        if reference["reference_id"] == "ref-h-fahasa":
            assert reference["match_decision"] == "MANUAL_REVIEW"


def test_image_source_unusable_error_is_a_runtime_error():
    """Sanity contract check: main()'s retry loop and every other caller
    treats ImageSourceUnusableError as a RuntimeError subclass."""
    assert issubclass(ImageSourceUnusableError, RuntimeError)
