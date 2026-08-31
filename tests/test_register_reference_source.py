"""
Regression tests for scripts/register_reference_source.py's --batch-code
generalization.

Before this change, BATCH_CODE was a hardcoded module constant
("FB-2026-001", the live Facebook collection batch), so the registrar
could never resolve a candidate from any other batch -- including the
historical batches (FB-HIST-2026-001, FB-HIST-2026-002,
FB-HIST-2026-AUTOIMPORT) created by the historical import pipeline.

This file protects the generalization: an optional --batch-code that
(a) defaults to the exact previous hardcoded value, so omitting it is
byte-for-byte equivalent to the old hardcoded behavior, (b) is honored
for candidate/batch resolution when supplied, (c) never silently falls
back to the default batch when the requested one is missing or the
candidate isn't in it, and (d) leaves reference-source idempotency
(get_or_create_source_url) unchanged.

Fully offline: FakeSupabaseRepository only, no live Supabase/network.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

import register_reference_source as rrs
from support.fake_supabase import FakeSupabaseRepository


def make_batch(batch_id: str, batch_code: str) -> dict[str, Any]:
    return {"batch_id": batch_id, "batch_code": batch_code}


def make_candidate(
    candidate_id: str,
    candidate_code: str,
    batch_id: str,
    workflow_status: str = "EXTRACTED",
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "candidate_code": candidate_code,
        "candidate_type": "SINGLE_BOOK",
        "extracted_title": "Một Cuốn Sách",
        "extracted_author": None,
        "possible_isbn": None,
        "identity_status": "IDENTITY_PENDING",
        "workflow_status": workflow_status,
        "review_required": True,
        "batch_id": batch_id,
    }


def make_repository() -> FakeSupabaseRepository:
    return FakeSupabaseRepository(
        tables={
            "batches": [
                make_batch("batch-live", rrs.DEFAULT_BATCH_CODE),
                make_batch("batch-hist-001", "FB-HIST-2026-001"),
                make_batch("batch-hist-002", "FB-HIST-2026-002"),
            ],
            "product_candidates": [
                make_candidate("cand-live-1", "FB-2026-001-CAN-0001", "batch-live"),
                make_candidate(
                    "cand-hist-001-1",
                    "FB-HIST-2026-001-CAN-0001",
                    "batch-hist-001",
                ),
                make_candidate(
                    "cand-hist-002-1",
                    "FB-HIST-2026-002-CAN-0001",
                    "batch-hist-002",
                ),
            ],
        }
    )


# ---------------------------------------------------------------------------
# 1. No --batch-code supplied resolves exactly as before against the
#    hardcoded live batch value.
# ---------------------------------------------------------------------------


def test_batch_code_defaults_to_the_previous_hardcoded_live_batch(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "register_reference_source.py",
            "--candidate-code",
            "FB-2026-001-CAN-0001",
            "--source-type",
            "FAHASA",
            "--source-url",
            "https://www.fahasa.com/example.html",
            "--confirm-register",
            "--non-interactive",
        ],
    )

    args = rrs.parse_arguments()

    assert args.batch_code == "FB-2026-001"
    assert args.batch_code == rrs.DEFAULT_BATCH_CODE


def test_omitted_batch_code_resolves_the_same_live_candidate_as_before():
    repository = make_repository()

    batch = rrs.get_batch(repository, rrs.DEFAULT_BATCH_CODE)
    candidates = rrs.get_candidates(repository, batch["batch_id"])
    candidate = rrs.resolve_candidate(
        candidates=candidates,
        candidate_code="FB-2026-001-CAN-0001",
        candidate_id=None,
        non_interactive=True,
    )

    assert candidate["candidate_id"] == "cand-live-1"


# ---------------------------------------------------------------------------
# 2 & 3. An explicit historical --batch-code resolves a candidate from
#    that historical batch.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "batch_code,batch_id,candidate_code,candidate_id",
    [
        (
            "FB-HIST-2026-001",
            "batch-hist-001",
            "FB-HIST-2026-001-CAN-0001",
            "cand-hist-001-1",
        ),
        (
            "FB-HIST-2026-002",
            "batch-hist-002",
            "FB-HIST-2026-002-CAN-0001",
            "cand-hist-002-1",
        ),
    ],
)
def test_explicit_historical_batch_code_resolves_that_batchs_candidate(
    batch_code, batch_id, candidate_code, candidate_id
):
    repository = make_repository()

    batch = rrs.get_batch(repository, batch_code)
    assert batch["batch_id"] == batch_id

    candidates = rrs.get_candidates(repository, batch["batch_id"])
    candidate = rrs.resolve_candidate(
        candidates=candidates,
        candidate_code=candidate_code,
        candidate_id=None,
        non_interactive=True,
    )

    assert candidate["candidate_id"] == candidate_id


# ---------------------------------------------------------------------------
# 4. A candidate that exists, but in a different batch than requested,
#    is rejected clearly -- never silently resolved from another batch.
# ---------------------------------------------------------------------------


def test_candidate_belonging_to_another_batch_is_rejected_clearly():
    repository = make_repository()

    # FB-HIST-2026-001-CAN-0001 lives in batch-hist-001, not batch-hist-002.
    batch = rrs.get_batch(repository, "FB-HIST-2026-002")
    candidates = rrs.get_candidates(repository, batch["batch_id"])

    with pytest.raises(RuntimeError, match="did not resolve to exactly one record"):
        rrs.resolve_candidate(
            candidates=candidates,
            candidate_code="FB-HIST-2026-001-CAN-0001",
            candidate_id=None,
            non_interactive=True,
        )


# ---------------------------------------------------------------------------
# 5. A nonexistent batch code is rejected clearly -- never silently falls
#    back to the default live batch.
# ---------------------------------------------------------------------------


def test_nonexistent_batch_code_is_rejected_without_falling_back_to_default():
    repository = make_repository()

    with pytest.raises(RuntimeError, match="Batch was not found: FB-HIST-2099-999"):
        rrs.get_batch(repository, "FB-HIST-2099-999")


# ---------------------------------------------------------------------------
# 6. Existing reference-source idempotency behavior is unchanged: the same
#    (batch, source_type, source_url) reuses the existing source_urls row
#    instead of inserting a duplicate, for a historical batch exactly as
#    it already did for the live batch.
# ---------------------------------------------------------------------------


def test_reference_source_idempotency_is_unchanged_for_a_historical_batch():
    repository = make_repository()
    batch = rrs.get_batch(repository, "FB-HIST-2026-001")

    registration = {
        "source_type": "FAHASA",
        "source_name": "Fahasa",
        "source_url": "https://www.fahasa.com/example-book.html",
        "is_authorized": True,
    }

    first_source, first_created = rrs.get_or_create_source_url(
        repository=repository,
        batch_id=batch["batch_id"],
        registration=registration,
    )
    second_source, second_created = rrs.get_or_create_source_url(
        repository=repository,
        batch_id=batch["batch_id"],
        registration=registration,
    )

    assert first_created is True
    assert second_created is False
    assert first_source["source_url_id"] == second_source["source_url_id"]
    assert (
        len(repository.client.tables["source_urls"]) == 1
    ), "a second call with the same (batch, source_type, source_url) must reuse, not duplicate"
