"""
Automated tests for scripts/import_historical_facebook_candidates_remaining.py.

Covers only the pure, offline-testable selection logic
(compute_remaining_rows / import_key_for_row) -- the part that replaces
FB-HIST-2026-001/002's hand-authored SELECTED_CANDIDATES allowlist with a
computed one. The I/O-performing helpers (get_or_create_batch,
get_or_create_source_url, ...) are copied unchanged from
import_historical_facebook_candidates.py, which already has production
history; this file focuses on the new behavior only.

Pure/offline: no live Supabase, no network, no filesystem CSV read --
every test builds its preview rows as plain dicts in memory.
"""

from __future__ import annotations

from typing import Any

import import_historical_facebook_candidates_remaining as ihcr


def make_row(**overrides: Any) -> dict[str, str]:
    row = {
        "record_id": "2000",
        "date": "2025-01-01",
        "final_outcome": "AUTO_PASS",
        "extraction_source": "CLAUDE_SEMANTIC",
        "candidate_index": "1",
        "title_raw": "Một Cuốn Sách",
        "title_normalized": "một cuốn sách",
        "candidate_type": "SINGLE_BOOK",
        "evidence_text": "Một cuốn sách hay",
        "confidence": "0.9",
        "completeness_status": "COMPLETE",
        "provider": "claude",
        "model": "claude-opus-5",
        "prompt_version": "v1",
        "schema_version": "v1",
        "review_reason_codes": "",
        "non_book_hints": "",
        "local_media_count": "0",
        "local_media_paths": "",
        "cleaned_text_hash": "abc123",
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------------
# 1. import_key_for_row is exactly the format every historical importer
# writes to source_evidence.historical_import_key.
# --------------------------------------------------------------------------


def test_import_key_matches_hand_selected_batch_format():
    row = make_row(
        record_id="1269",
        title_normalized="Thần đồng đất Việt",
        candidate_type="SINGLE_BOOK",
    )

    assert ihcr.import_key_for_row(row) == "1269:thần đồng đất việt:SINGLE_BOOK"


# --------------------------------------------------------------------------
# 2. compute_remaining_rows only selects AUTO_PASS rows
# --------------------------------------------------------------------------


def test_only_auto_pass_rows_are_eligible():
    rows = [
        make_row(record_id="1", final_outcome="AUTO_PASS"),
        make_row(record_id="2", final_outcome="REVIEW_REQUIRED"),
    ]

    remaining = ihcr.compute_remaining_rows(rows, already_imported_keys=set())

    assert [r["record_id"] for r in remaining] == ["1"]


# --------------------------------------------------------------------------
# 3. Already-imported rows (by historical_import_key) are excluded
# --------------------------------------------------------------------------


def test_already_imported_rows_are_excluded():
    rows = [
        make_row(record_id="1"),
        make_row(record_id="2"),
    ]
    already_imported = {ihcr.import_key_for_row(rows[0])}

    remaining = ihcr.compute_remaining_rows(rows, already_imported_keys=already_imported)

    assert [r["record_id"] for r in remaining] == ["2"]


def test_rerun_is_a_no_op_when_everything_is_already_imported():
    rows = [make_row(record_id="1"), make_row(record_id="2")]
    already_imported = {ihcr.import_key_for_row(r) for r in rows}

    remaining = ihcr.compute_remaining_rows(rows, already_imported_keys=already_imported)

    assert remaining == []


# --------------------------------------------------------------------------
# 4. Forbidden record IDs are permanently excluded, regardless of import
# history -- #1483 and #1038 must never be importable by this script.
# --------------------------------------------------------------------------


def test_forbidden_record_ids_are_never_selected():
    rows = [
        make_row(record_id="1483"),
        make_row(record_id="1038"),
        make_row(record_id="2000"),
    ]

    remaining = ihcr.compute_remaining_rows(rows, already_imported_keys=set())

    assert [r["record_id"] for r in remaining] == ["2000"]


# --------------------------------------------------------------------------
# 5. Rows missing a candidate_index, or with an unrecognized
# candidate_type, are never eligible (mirrors resolve_selected_rows'
# hard-coded validation in the hand-selected batches).
# --------------------------------------------------------------------------


def test_rows_without_candidate_index_are_excluded():
    rows = [make_row(record_id="1", candidate_index="")]

    remaining = ihcr.compute_remaining_rows(rows, already_imported_keys=set())

    assert remaining == []


def test_unrecognized_candidate_type_is_excluded():
    rows = [make_row(record_id="1", candidate_type="ACTIVITY_PRODUCT")]

    remaining = ihcr.compute_remaining_rows(rows, already_imported_keys=set())

    assert remaining == []


# --------------------------------------------------------------------------
# 6. Deterministic, reproducible ordering across reruns
# --------------------------------------------------------------------------


def test_remaining_rows_are_sorted_by_record_id_then_candidate_index():
    rows = [
        make_row(record_id="50", candidate_index="2"),
        make_row(record_id="10", candidate_index="1"),
        make_row(record_id="50", candidate_index="1"),
    ]

    remaining = ihcr.compute_remaining_rows(rows, already_imported_keys=set())

    assert [(r["record_id"], r["candidate_index"]) for r in remaining] == [
        ("10", "1"),
        ("50", "1"),
        ("50", "2"),
    ]


# --------------------------------------------------------------------------
# 7. --max-import caps the selection without changing its order
# --------------------------------------------------------------------------


def test_max_import_caps_the_selection():
    rows = [make_row(record_id=str(n)) for n in range(1, 11)]

    remaining = ihcr.compute_remaining_rows(
        rows, already_imported_keys=set(), max_import=3
    )

    assert [r["record_id"] for r in remaining] == ["1", "2", "3"]


def test_max_import_none_returns_every_eligible_row():
    rows = [make_row(record_id=str(n)) for n in range(1, 6)]

    remaining = ihcr.compute_remaining_rows(rows, already_imported_keys=set())

    assert len(remaining) == 5
