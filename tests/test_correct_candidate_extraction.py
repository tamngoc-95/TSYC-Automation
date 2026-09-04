"""Regression tests for scripts/correct_candidate_extraction.py's
run_correction() -- the extracted, testable core of main().

Covers the generalized state guard: the original EXTRACTED/
IDENTITY_PENDING path (unchanged), the new IDENTITY_CONFLICT
title-only-recovery exception, refusal for IDENTITY_VERIFIED, and
idempotency/audit-history behavior on rerun.

Fully offline: FakeSupabaseRepository only, no live Supabase, no network.
"""

from __future__ import annotations

import pytest

import correct_candidate_extraction as cce
from src.domain.identity_status import IdentityStatus
from support.fake_supabase import FakeSupabaseRepository

CANDIDATE_ID = "cand-1"
RAW_PAGE_ID = "page-1"
CANDIDATE_CODE = "FB-HIST-2026-002-CAN-0023"

# The real CAN-0023 fixture shape: a leading price/preorder announcement
# clause, then the actual combo product description.
CLEANED_TEXT = (
    "Em nhận preorder sách mới 70€/ Combo 4 cuốn truyện của Thomas Harris\n"
    "Thanh lý truyện cũ 40€/ combo 4 cuốn (Có sẵn)\n"
    "Có bán lẻ tập ạ"
)

BAD_STORED_TITLE = "Em nhận preorder sách mới 70€/ Combo 4 cuốn truyện"


def make_candidate(
    *,
    workflow_status: str,
    identity_status: str,
    extracted_title: str = BAD_STORED_TITLE,
    extracted_author: str | None = None,
) -> dict:
    return {
        "candidate_id": CANDIDATE_ID,
        "batch_id": "batch-1",
        "candidate_code": CANDIDATE_CODE,
        "candidate_type": "BOOK_COMBO",
        "raw_page_id": RAW_PAGE_ID,
        "source_url_id": "source-1",
        "extracted_title": extracted_title,
        "extracted_author": extracted_author,
        "possible_isbn": None,
        "extraction_confidence": 0.8,
        "identity_status": identity_status,
        "workflow_status": workflow_status,
        "review_required": True,
        "source_evidence": {},
    }


def make_repository(candidate: dict, *, references: list[dict] | None = None) -> FakeSupabaseRepository:
    return FakeSupabaseRepository(
        tables={
            "product_candidates": [candidate],
            "raw_pages": [
                {
                    "raw_page_id": RAW_PAGE_ID,
                    "page_url": "facebook-export://x",
                    "cleaning_status": "CLEANED",
                    "cleaning_method": "RULE_BASED",
                    "cleaned_at": "2026-09-04T00:00:00Z",
                    "cleaned_text": CLEANED_TEXT,
                }
            ],
            "product_references": references or [],
            "process_logs": [],
        }
    )


def get_candidate_row(repository: FakeSupabaseRepository) -> dict:
    rows = [
        row
        for row in repository.client.tables["product_candidates"]
        if row["candidate_id"] == CANDIDATE_ID
    ]
    assert len(rows) == 1
    return rows[0]


# ---------------------------------------------------------------------------
# Original EXTRACTED / IDENTITY_PENDING path -- unchanged
# ---------------------------------------------------------------------------


def test_extracted_pending_candidate_still_corrects_normally():
    candidate = make_candidate(
        workflow_status="EXTRACTED",
        identity_status=IdentityStatus.IDENTITY_PENDING,
    )
    repository = make_repository(candidate)

    result = cce.run_correction(
        repository=repository,
        candidate_code=CANDIDATE_CODE,
        confirm_correct=True,
        non_interactive=True,
    )

    assert result is not None
    assert result["extracted_title"] == "Combo 4 cuốn truyện"


# ---------------------------------------------------------------------------
# 7 (integration). IDENTITY_CONFLICT caused only by title mismatch can
# enter the correction path.
# ---------------------------------------------------------------------------


def test_title_only_identity_conflict_candidate_can_be_corrected():
    candidate = make_candidate(
        workflow_status=IdentityStatus.IDENTITY_CONFLICT,
        identity_status=IdentityStatus.IDENTITY_CONFLICT,
    )
    references = [
        {
            "reference_id": "ref-1",
            "candidate_id": CANDIDATE_ID,
            "reference_title": "Combo Sách Tiểu Thuyết Nổi Tiếng Của Thomas Harris",
            "reference_author": None,
            "reference_isbn": None,
            "reference_publisher": None,
            "match_decision": "NO_MATCH",
        }
    ]
    repository = make_repository(candidate, references=references)

    result = cce.run_correction(
        repository=repository,
        candidate_code=CANDIDATE_CODE,
        confirm_correct=True,
        non_interactive=True,
    )

    assert result is not None
    assert result["extracted_title"] == "Combo 4 cuốn truyện"

    stored = get_candidate_row(repository)
    corrections = stored["source_evidence"]["corrections"]
    assert len(corrections) == 1
    assert corrections[0]["reason"] == cce.TITLE_ONLY_CONFLICT_CORRECTION_REASON


# ---------------------------------------------------------------------------
# 8/9/10 (integration). A non-title-only conflict cannot use recovery.
# ---------------------------------------------------------------------------


def test_isbn_conflict_candidate_is_refused():
    candidate = make_candidate(
        workflow_status=IdentityStatus.IDENTITY_CONFLICT,
        identity_status=IdentityStatus.IDENTITY_CONFLICT,
    )
    candidate["possible_isbn"] = "9786045123456"
    references = [
        {
            "reference_id": "ref-1",
            "candidate_id": CANDIDATE_ID,
            "reference_title": "Combo Sách Tiểu Thuyết Nổi Tiếng Của Thomas Harris",
            "reference_author": None,
            "reference_isbn": "9786049999999",
            "reference_publisher": None,
            "match_decision": "NO_MATCH",
        }
    ]
    repository = make_repository(candidate, references=references)

    with pytest.raises(RuntimeError, match="NOT title-only"):
        cce.run_correction(
            repository=repository,
            candidate_code=CANDIDATE_CODE,
            confirm_correct=True,
            non_interactive=True,
        )

    stored = get_candidate_row(repository)
    assert stored["extracted_title"] == BAD_STORED_TITLE
    assert "corrections" not in stored["source_evidence"]


# ---------------------------------------------------------------------------
# 11. A VERIFIED candidate cannot be corrected
# ---------------------------------------------------------------------------


def test_verified_candidate_is_refused():
    candidate = make_candidate(
        workflow_status=IdentityStatus.IDENTITY_VERIFIED,
        identity_status=IdentityStatus.IDENTITY_VERIFIED,
    )
    repository = make_repository(candidate)

    with pytest.raises(RuntimeError, match="Refusing to correct"):
        cce.run_correction(
            repository=repository,
            candidate_code=CANDIDATE_CODE,
            confirm_correct=True,
            non_interactive=True,
        )

    stored = get_candidate_row(repository)
    assert stored["extracted_title"] == BAD_STORED_TITLE


# ---------------------------------------------------------------------------
# 12/13. Idempotency + audit/history records the correction exactly once
# ---------------------------------------------------------------------------


def test_rerun_after_correction_is_idempotent_and_history_recorded_once():
    candidate = make_candidate(
        workflow_status=IdentityStatus.IDENTITY_CONFLICT,
        identity_status=IdentityStatus.IDENTITY_CONFLICT,
    )
    references = [
        {
            "reference_id": "ref-1",
            "candidate_id": CANDIDATE_ID,
            "reference_title": "Combo Sách Tiểu Thuyết Nổi Tiếng Của Thomas Harris",
            "reference_author": None,
            "reference_isbn": None,
            "reference_publisher": None,
            "match_decision": "NO_MATCH",
        }
    ]
    repository = make_repository(candidate, references=references)

    first = cce.run_correction(
        repository=repository,
        candidate_code=CANDIDATE_CODE,
        confirm_correct=True,
        non_interactive=True,
    )
    assert first is not None

    process_logs_after_first = list(repository.client.tables["process_logs"])
    assert len(process_logs_after_first) == 1

    # Rerun: the candidate is now at IDENTITY_CONFLICT again in this
    # fixture (nothing in this script rewrites identity_status -- that
    # is match_candidate_identity.py's job), so it re-enters the same
    # conflict-recovery branch; the re-derived title now matches the
    # already-corrected stored title exactly, so this must be a no-op.
    stored = get_candidate_row(repository)
    stored["identity_status"] = IdentityStatus.IDENTITY_CONFLICT
    stored["workflow_status"] = IdentityStatus.IDENTITY_CONFLICT

    second = cce.run_correction(
        repository=repository,
        candidate_code=CANDIDATE_CODE,
        confirm_correct=True,
        non_interactive=True,
    )

    assert second is None  # no-op: title already matches

    final = get_candidate_row(repository)
    corrections = final["source_evidence"]["corrections"]
    assert len(corrections) == 1  # not duplicated

    process_logs_after_second = list(repository.client.tables["process_logs"])
    assert len(process_logs_after_second) == 1  # not duplicated
