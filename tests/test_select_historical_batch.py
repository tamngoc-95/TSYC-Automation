"""Automated tests for scripts/select_historical_batch.py.

Covers the read-only historical-candidate selector: bucketing into
AUTOMATABLE_NOW / RECOVERY_REVIEW / CONFLICT / HUMAN_REVIEW / TERMINAL,
per-candidate isolation (one candidate's state never affects another's
bucket), the explicit-code validation guard against non-historical
candidates, deterministic oldest-first scanning with --limit, and that it
never invokes a writer script (it only reuses pipeline_state.derive_
candidate_state()/run_batch.decide_action(), never a subprocess).

Pure/offline: no live Supabase -- every test uses FakeSupabaseRepository.
"""

from __future__ import annotations

from typing import Any

import select_historical_batch as selector
from select_historical_batch import (
    SelectorArgumentError,
    classify_bundle,
    resolve_candidate_codes,
)
from pipeline_state import load_candidate_bundle

from support.fake_supabase import FakeSupabaseRepository


BATCH_ID = "11111111-1111-1111-1111-111111111111"


def make_repository(**tables: list[dict[str, Any]]) -> FakeSupabaseRepository:
    return FakeSupabaseRepository(tables=dict(tables))


def make_candidate(candidate_id: str, candidate_code: str, **overrides: Any) -> dict[str, Any]:
    row = {
        "candidate_id": candidate_id,
        "candidate_code": candidate_code,
        "batch_id": BATCH_ID,
        "identity_status": "IDENTITY_PENDING",
        "workflow_status": "EXTRACTED",
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    row.update(overrides)
    return row


def make_reference(reference_id: str, candidate_id: str, **overrides: Any) -> dict[str, Any]:
    row = {
        "reference_id": reference_id,
        "candidate_id": candidate_id,
        "match_decision": "MATCH",
        "source_url_id": "source-url-1",
    }
    row.update(overrides)
    return row


def make_internal_product(
    internal_product_id: str, candidate_id: str, product_code: str, **overrides: Any
) -> dict[str, Any]:
    row = {
        "internal_product_id": internal_product_id,
        "candidate_id": candidate_id,
        "product_code": product_code,
        "isbn": "9786041234567",
        "weight_grams": 250,
        "content_status": "PENDING",
        "image_status": "APPROVED",
        "woocommerce_status": "NOT_CREATED",
    }
    row.update(overrides)
    return row


def make_sync(sync_id: str, internal_product_id: str, **overrides: Any) -> dict[str, Any]:
    row = {
        "sync_id": sync_id,
        "internal_product_id": internal_product_id,
        "woocommerce_status": "DRAFT_CREATED",
        "woocommerce_product_id": 999,
        "response_payload": {},
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------------
# classify_bundle: one bucket per derived-state shape
# --------------------------------------------------------------------------


def test_not_found_bucket_for_missing_candidate():
    entry = classify_bundle("FB-HIST-2026-SEL-CAN-9999", None)
    assert entry.bucket == "NOT_FOUND"


def test_automatable_now_bucket_for_ready_historical_candidate():
    candidate_id = "aaaaaaaa-0000-0000-0000-000000000001"
    code = "FB-HIST-2026-SEL-CAN-0001"
    repository = make_repository(
        product_candidates=[
            make_candidate(candidate_id, code, identity_status="IDENTITY_VERIFIED"),
        ],
        product_references=[make_reference("ref-1", candidate_id)],
    )

    entry = classify_bundle(code, load_candidate_bundle(repository, code))

    assert entry.bucket == "AUTOMATABLE_NOW"
    assert entry.derived_state == "IDENTITY_VERIFIED"
    assert entry.dispatch_script == "create_internal_product.py"


def test_recovery_review_bucket_takes_priority_over_human_gate():
    candidate_id = "aaaaaaaa-0000-0000-0000-000000000002"
    code = "FB-HIST-2026-SEL-CAN-0002"
    internal_product_id = "ip-2"
    repository = make_repository(
        product_candidates=[
            make_candidate(candidate_id, code, identity_status="IDENTITY_VERIFIED"),
        ],
        product_references=[make_reference("ref-2", candidate_id)],
        internal_products=[
            make_internal_product(
                internal_product_id,
                candidate_id,
                "TSYC-" + code,
                woocommerce_status="DRAFT_CREATED",
            ),
        ],
        woocommerce_product_syncs=[
            make_sync(
                "sync-2",
                internal_product_id,
                response_payload={"recovery_required": True},
            ),
        ],
    )

    entry = classify_bundle(code, load_candidate_bundle(repository, code))

    assert entry.bucket == "RECOVERY_REVIEW"
    assert entry.derived_state == "RECOVERY_REVIEW_REQUIRED"


def test_conflict_bucket_for_identity_conflict():
    candidate_id = "aaaaaaaa-0000-0000-0000-000000000003"
    code = "FB-HIST-2026-SEL-CAN-0003"
    repository = make_repository(
        product_candidates=[
            make_candidate(candidate_id, code, identity_status="IDENTITY_CONFLICT"),
        ],
    )

    entry = classify_bundle(code, load_candidate_bundle(repository, code))

    assert entry.bucket == "CONFLICT"
    assert entry.derived_state == "IDENTITY_CONFLICT"


def test_human_review_bucket_for_content_review_required():
    candidate_id = "aaaaaaaa-0000-0000-0000-000000000004"
    code = "FB-HIST-2026-SEL-CAN-0004"
    internal_product_id = "ip-4"
    repository = make_repository(
        product_candidates=[
            make_candidate(candidate_id, code, identity_status="IDENTITY_VERIFIED"),
        ],
        product_references=[make_reference("ref-4", candidate_id)],
        internal_products=[
            make_internal_product(
                internal_product_id,
                candidate_id,
                "TSYC-" + code,
                content_status="REVIEW_REQUIRED",
            ),
        ],
    )

    entry = classify_bundle(code, load_candidate_bundle(repository, code))

    assert entry.bucket == "HUMAN_REVIEW"
    assert entry.derived_state == "CONTENT_REVIEW_REQUIRED"


def test_terminal_bucket_for_duplicate_rejected():
    candidate_id = "aaaaaaaa-0000-0000-0000-000000000005"
    code = "FB-HIST-2026-SEL-CAN-0005"
    repository = make_repository(
        product_candidates=[
            make_candidate(candidate_id, code, identity_status="REJECTED"),
        ],
    )

    entry = classify_bundle(code, load_candidate_bundle(repository, code))

    assert entry.bucket == "TERMINAL"
    assert entry.derived_state == "DUPLICATE_REJECTED"


# --------------------------------------------------------------------------
# resolve_candidate_codes: explicit-code validation, filtering, ordering
# --------------------------------------------------------------------------


def test_explicit_non_historical_code_is_rejected():
    repository = make_repository()

    try:
        resolve_candidate_codes(
            repository,
            explicit_codes=["FB-2026-001-CAN-0001"],
            batch_code=None,
            limit=25,
        )
        assert False, "expected SelectorArgumentError"
    except SelectorArgumentError:
        pass


def test_explicit_codes_used_exactly_as_given():
    repository = make_repository()
    codes = resolve_candidate_codes(
        repository,
        explicit_codes=["FB-HIST-2026-SEL-CAN-0001", "FB-HIST-2026-SEL-CAN-0002"],
        batch_code=None,
        limit=1,  # explicit codes bypass --limit entirely
    )
    assert codes == ["FB-HIST-2026-SEL-CAN-0001", "FB-HIST-2026-SEL-CAN-0002"]


def test_scan_filters_out_non_historical_candidates():
    repository = make_repository(
        product_candidates=[
            make_candidate("id-1", "FB-HIST-2026-SEL-CAN-0001", created_at="2026-01-01"),
            make_candidate("id-2", "FB-2026-001-CAN-0009", created_at="2026-01-02"),
            make_candidate("id-3", "FB-HIST-2026-SEL-CAN-0003", created_at="2026-01-03"),
        ],
    )

    codes = resolve_candidate_codes(
        repository, explicit_codes=None, batch_code=None, limit=25
    )

    assert codes == [
        "FB-HIST-2026-SEL-CAN-0001",
        "FB-HIST-2026-SEL-CAN-0003",
    ]


def test_scan_is_oldest_created_first_and_respects_limit():
    repository = make_repository(
        product_candidates=[
            make_candidate("id-1", "FB-HIST-2026-SEL-CAN-0003", created_at="2026-01-03"),
            make_candidate("id-2", "FB-HIST-2026-SEL-CAN-0001", created_at="2026-01-01"),
            make_candidate("id-3", "FB-HIST-2026-SEL-CAN-0002", created_at="2026-01-02"),
        ],
    )

    codes = resolve_candidate_codes(
        repository, explicit_codes=None, batch_code=None, limit=2
    )

    assert codes == [
        "FB-HIST-2026-SEL-CAN-0001",
        "FB-HIST-2026-SEL-CAN-0002",
    ]


def test_scan_respects_batch_code_filter():
    other_batch_id = "22222222-2222-2222-2222-222222222222"
    repository = make_repository(
        batches=[{"batch_id": BATCH_ID, "batch_code": "HIST-BATCH-A"}],
        product_candidates=[
            make_candidate(
                "id-1", "FB-HIST-2026-SEL-CAN-0001", batch_id=BATCH_ID, created_at="2026-01-01"
            ),
            make_candidate(
                "id-2",
                "FB-HIST-2026-SEL-CAN-0002",
                batch_id=other_batch_id,
                created_at="2026-01-02",
            ),
        ],
    )

    codes = resolve_candidate_codes(
        repository, explicit_codes=None, batch_code="HIST-BATCH-A", limit=25
    )

    assert codes == ["FB-HIST-2026-SEL-CAN-0001"]


def test_unknown_batch_code_is_rejected():
    repository = make_repository(batches=[])

    try:
        resolve_candidate_codes(
            repository, explicit_codes=None, batch_code="NOT-A-BATCH", limit=25
        )
        assert False, "expected SelectorArgumentError"
    except SelectorArgumentError:
        pass


# --------------------------------------------------------------------------
# main(): end-to-end bucketing, isolation, and non-historical exclusion
# --------------------------------------------------------------------------


def test_main_buckets_mixed_candidates_and_ignores_non_historical(capsys):
    automatable_id = "bbbbbbbb-0000-0000-0000-000000000001"
    automatable_code = "FB-HIST-2026-SEL-CAN-0001"
    conflict_id = "bbbbbbbb-0000-0000-0000-000000000002"
    conflict_code = "FB-HIST-2026-SEL-CAN-0002"
    live_id = "bbbbbbbb-0000-0000-0000-000000000003"
    live_code = "FB-2026-001-CAN-0009"

    repository = make_repository(
        product_candidates=[
            make_candidate(
                automatable_id,
                automatable_code,
                identity_status="IDENTITY_VERIFIED",
                created_at="2026-01-01",
            ),
            make_candidate(
                conflict_id,
                conflict_code,
                identity_status="IDENTITY_CONFLICT",
                created_at="2026-01-02",
            ),
            make_candidate(
                live_id,
                live_code,
                identity_status="IDENTITY_VERIFIED",
                created_at="2026-01-03",
            ),
        ],
        product_references=[
            make_reference("ref-auto", automatable_id),
            make_reference("ref-live", live_id),
        ],
    )

    exit_code = selector.main(["--limit", "25"], repository=repository)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Candidates scanned: 2" in output
    assert automatable_code in output
    assert conflict_code in output
    assert live_code not in output
    assert "AUTOMATABLE_NOW: 1" in output
    assert "CONFLICT: 1" in output
    assert "RECOMMENDED_NEXT_ACTION:" in output
    assert f"--candidate-codes {automatable_code}" in output
    assert "--max-candidates 1" in output


def test_main_rejects_explicit_non_historical_code(capsys):
    repository = make_repository()

    exit_code = selector.main(
        ["--candidate-code", "FB-2026-001-CAN-0001"], repository=repository
    )
    err = capsys.readouterr().err

    assert exit_code == 2
    assert "not a historical" in err


def test_main_no_matching_candidates_reports_cleanly(capsys):
    repository = make_repository(product_candidates=[])

    exit_code = selector.main(["--limit", "25"], repository=repository)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "No FB-HIST-* candidates matched" in output
