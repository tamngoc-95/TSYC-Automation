"""Automated tests for scripts/run_batch.py and scripts/pipeline_state.py.

Covers the Phase C P0 batch orchestrator: bounded allowlist enforcement,
dry-run read-only guarantees, idempotent single-next-stage dispatch, serial
execution, no-blind-retry stage-failure handling, the audit checkpoint
(including CLAUDE.md's accepted-warning policy), human gates, recovery
handling, and the WooCommerce draft-creation authorization gate.

Pure/offline: no live Supabase, no live WooCommerce, no subprocess actually
spawns .venv/Scripts/python.exe -- every test injects a stub
subprocess_runner and a FakeSupabaseRepository (Phase B testing
infrastructure).
"""

from __future__ import annotations

import subprocess
import types
from typing import Any, Callable
from unittest.mock import patch

import pytest

import run_batch
from pipeline_state import derive_candidate_state, load_candidate_bundle

from support.fake_supabase import FakeSupabaseRepository


CANDIDATE_ID = "22222222-2222-2222-2222-222222222222"
BATCH_ID = "11111111-1111-1111-1111-111111111111"
REFERENCE_ID = "33333333-3333-3333-3333-333333333333"
INTERNAL_PRODUCT_ID = "44444444-4444-4444-4444-444444444444"
SYNC_ID = "55555555-5555-5555-5555-555555555555"
CANDIDATE_CODE = "FB-2026-001-CAN-0001"
PRODUCT_CODE = f"TSYC-{CANDIDATE_CODE}"


# --------------------------------------------------------------------------
# Row builders
# --------------------------------------------------------------------------


def make_candidate(**overrides: Any) -> dict[str, Any]:
    row = {
        "candidate_id": CANDIDATE_ID,
        "candidate_code": CANDIDATE_CODE,
        "batch_id": BATCH_ID,
        "identity_status": "IDENTITY_PENDING",
        "workflow_status": "EXTRACTED",
    }
    row.update(overrides)
    return row


def make_reference(**overrides: Any) -> dict[str, Any]:
    row = {
        "reference_id": REFERENCE_ID,
        "candidate_id": CANDIDATE_ID,
        "match_decision": "MATCH",
        "source_url_id": "source-url-1",
    }
    row.update(overrides)
    return row


def make_internal_product(**overrides: Any) -> dict[str, Any]:
    row = {
        "internal_product_id": INTERNAL_PRODUCT_ID,
        "candidate_id": CANDIDATE_ID,
        "product_code": PRODUCT_CODE,
        "isbn": "9786041234567",
        "weight_grams": 250,
        "content_status": "APPROVED",
        "image_status": "APPROVED",
        "woocommerce_status": "NOT_CREATED",
    }
    row.update(overrides)
    return row


def make_sync(**overrides: Any) -> dict[str, Any]:
    row = {
        "sync_id": SYNC_ID,
        "internal_product_id": INTERNAL_PRODUCT_ID,
        "woocommerce_status": "DRAFT_CREATED",
        "woocommerce_product_id": 999,
        "response_payload": {},
    }
    row.update(overrides)
    return row


def make_repository(**tables: list[dict[str, Any]]) -> FakeSupabaseRepository:
    return FakeSupabaseRepository(tables=dict(tables))


def make_args(
    *,
    dry_run: bool = False,
    non_interactive: bool = True,
    allow_woo_draft: bool = False,
    batch_code: str | None = None,
    verbose: bool = False,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        dry_run=dry_run,
        non_interactive=non_interactive,
        allow_woo_draft=allow_woo_draft,
        batch_code=batch_code,
        verbose=verbose,
    )


def always_ready_preflight() -> "run_batch.preflight_pipeline.PreflightResult":
    """A stub preflight_runner that always reports ready -- keeps
    orchestrator tests fully offline (no live Supabase/WooCommerce) while
    still exercising the "preflight must pass before writes" gate."""
    return run_batch.preflight_pipeline.PreflightResult(checks=())


def always_blocked_preflight() -> "run_batch.preflight_pipeline.PreflightResult":
    """A stub preflight_runner that always reports a blocking failure."""
    blocking_check = run_batch.preflight_pipeline.PreflightCheck(
        code="SUPABASE_READ",
        status=run_batch.preflight_pipeline.CheckStatus.FAIL,
        message="simulated blocking failure",
    )
    return run_batch.preflight_pipeline.PreflightResult(checks=(blocking_check,))


def recording_runner(
    returncode: int = 0,
    stdout: str = "",
) -> tuple[Callable[[list[str]], subprocess.CompletedProcess], list[list[str]]]:
    """A subprocess_runner stub that records every argv and never mutates data."""
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess:
        calls.append(argv)
        return subprocess.CompletedProcess(
            args=argv,
            returncode=returncode,
            stdout=stdout,
            stderr="",
        )

    return runner, calls


def always_confirm(_prompt: str) -> bool:
    return True


# --------------------------------------------------------------------------
# 1. No candidate allowlist supplied => fail before any I/O
# --------------------------------------------------------------------------


def test_no_allowlist_fails_before_any_io():
    exit_code = run_batch.main(["--max-candidates", "5"])
    assert exit_code == 2


# --------------------------------------------------------------------------
# 2. Candidate count exceeds --max-candidates => fail before any I/O
# --------------------------------------------------------------------------


def test_allowlist_exceeding_max_candidates_fails():
    exit_code = run_batch.main(
        ["--candidate-codes", "A,B,C", "--max-candidates", "2"]
    )
    assert exit_code == 2


def test_max_candidates_below_one_fails():
    exit_code = run_batch.main(
        ["--candidate-code", "A", "--max-candidates", "0"]
    )
    assert exit_code == 2


# --------------------------------------------------------------------------
# 3. --dry-run never invokes writers (or the audit checkpoint)
# --------------------------------------------------------------------------


def test_dry_run_never_invokes_a_writer_script():
    repository = make_repository(
        product_candidates=[
            make_candidate(identity_status="IDENTITY_VERIFIED"),
        ],
        product_references=[make_reference()],
    )
    runner, calls = recording_runner()

    exit_code = run_batch.main(
        [
            "--candidate-code",
            CANDIDATE_CODE,
            "--max-candidates",
            "1",
            "--dry-run",
            "--non-interactive",
        ],
        repository=repository,
        subprocess_runner=runner,
        confirm=always_confirm,
    )

    assert exit_code == 0
    assert calls == []


# --------------------------------------------------------------------------
# 4. A completed stage is not repeated
# --------------------------------------------------------------------------


def test_completed_stage_is_not_repeated():
    """
    The candidate already has an internal_products row -- the mandatory
    gate before create_internal_product.py has already been satisfied and
    must not be re-run. The next dispatched script must be
    prepare_product_content.py, never create_internal_product.py again.
    """
    repository = make_repository(
        product_candidates=[
            make_candidate(identity_status="IDENTITY_VERIFIED"),
        ],
        product_references=[make_reference()],
        internal_products=[
            make_internal_product(content_status="PENDING"),
        ],
    )
    runner, calls = recording_runner()
    args = make_args()

    report = run_batch.process_one_candidate(
        CANDIDATE_CODE, args, repository, runner, always_confirm, None
    )

    assert len(calls) == 1
    assert calls[0][1].endswith("prepare_product_content.py")
    assert "create_internal_product.py" not in calls[0][1]
    assert report.initial_state == "INTERNAL_PRODUCT_CREATED"


# --------------------------------------------------------------------------
# 5. Exactly one next stage is chosen
# --------------------------------------------------------------------------


def test_exactly_one_next_stage_is_chosen():
    repository = make_repository(
        product_candidates=[
            make_candidate(identity_status="IDENTITY_VERIFIED"),
        ],
        product_references=[make_reference()],
    )
    bundle = load_candidate_bundle(repository, CANDIDATE_CODE)
    state = derive_candidate_state(bundle)

    kind, dispatch, _description = run_batch.decide_action(state, False)

    assert kind == "invoke"
    assert dispatch is run_batch.AUTOMATABLE_DISPATCH["IDENTITY_VERIFIED"]
    assert dispatch.script == "create_internal_product.py"


# --------------------------------------------------------------------------
# 6. Writer stages execute serially (never concurrently / never reordered)
# --------------------------------------------------------------------------


def test_writer_stages_execute_serially_in_allowlist_order():
    other_candidate_id = "66666666-6666-6666-6666-666666666666"
    other_code = "FB-2026-001-CAN-0002"

    repository = make_repository(
        product_candidates=[
            make_candidate(identity_status="IDENTITY_VERIFIED"),
            make_candidate(
                candidate_id=other_candidate_id,
                candidate_code=other_code,
                identity_status="IDENTITY_VERIFIED",
            ),
        ],
        product_references=[
            make_reference(),
            make_reference(
                reference_id="reference-2",
                candidate_id=other_candidate_id,
            ),
        ],
    )
    runner, calls = recording_runner()

    exit_code = run_batch.main(
        [
            "--candidate-code",
            CANDIDATE_CODE,
            "--candidate-code",
            other_code,
            "--max-candidates",
            "2",
            "--non-interactive",
        ],
        repository=repository,
        subprocess_runner=runner,
        confirm=always_confirm,
        preflight_runner=always_ready_preflight,
    )

    assert exit_code == 0
    # Both candidates dispatched create_internal_product.py exactly once,
    # strictly in allowlist order -- candidate 1 fully finished (its one
    # call happened) before candidate 2's call was made.
    assert len(calls) == 2
    assert calls[0][-3] == CANDIDATE_CODE
    assert calls[1][-3] == other_code


# --------------------------------------------------------------------------
# 7. A non-zero stage result stops that candidate immediately (no retry)
# --------------------------------------------------------------------------


def test_nonzero_stage_result_stops_candidate_without_retry():
    repository = make_repository(
        product_candidates=[
            make_candidate(identity_status="IDENTITY_VERIFIED"),
        ],
        product_references=[make_reference()],
    )
    runner, calls = recording_runner(returncode=1, stdout="boom")
    args = make_args()

    report = run_batch.process_one_candidate(
        CANDIDATE_CODE, args, repository, runner, always_confirm, None
    )

    assert report.result == "STAGE_FAILED"
    assert len(calls) == 1  # stopped immediately, no blind retry


# --------------------------------------------------------------------------
# 8. Audit error stops the entire batch
# --------------------------------------------------------------------------


def test_audit_error_stops_entire_batch():
    other_code = "FB-2026-001-CAN-0002"
    other_candidate_id = "77777777-7777-7777-7777-777777777777"

    repository = make_repository(
        product_candidates=[
            make_candidate(identity_status="IDENTITY_VERIFIED"),
            make_candidate(
                candidate_id=other_candidate_id,
                candidate_code=other_code,
                identity_status="IDENTITY_VERIFIED",
            ),
        ],
        product_references=[
            make_reference(),
            make_reference(
                reference_id="reference-2",
                candidate_id=other_candidate_id,
            ),
        ],
        internal_products=[
            make_internal_product(content_status="PENDING"),
        ],
    )

    def runner(argv: list[str]) -> subprocess.CompletedProcess:
        script = argv[1]

        if script.endswith("prepare_product_content.py"):
            # Simulate the real script's write: content moves PENDING -> DRAFTED.
            product = repository.client.tables["internal_products"][0]
            product["content_status"] = "DRAFTED"
            repository.client.tables.setdefault(
                "product_contents", []
            ).append(
                {
                    "product_content_id": "content-1",
                    "internal_product_id": INTERNAL_PRODUCT_ID,
                    "content_language": "vi",
                    "content_status": "DRAFTED",
                }
            )
            return subprocess.CompletedProcess(argv, 0, "", "")

        if script.endswith("audit_pipeline_state.py"):
            return subprocess.CompletedProcess(
                argv,
                1,
                "[1] ERROR | CONTENT_STATUS_MISMATCH\nEntity: x\nDetails: y\n",
                "",
            )

        return subprocess.CompletedProcess(argv, 0, "", "")

    exit_code = run_batch.main(
        [
            "--candidate-code",
            CANDIDATE_CODE,
            "--candidate-code",
            other_code,
            "--max-candidates",
            "2",
            "--non-interactive",
        ],
        repository=repository,
        subprocess_runner=runner,
        confirm=always_confirm,
        preflight_runner=always_ready_preflight,
    )

    assert exit_code == 1


# --------------------------------------------------------------------------
# 9. Accepted warnings (ISBN/weight) may continue past the audit checkpoint
# --------------------------------------------------------------------------


def test_accepted_warnings_continue_past_audit_checkpoint():
    repository = make_repository(
        product_candidates=[
            make_candidate(identity_status="IDENTITY_VERIFIED"),
        ],
        product_references=[make_reference()],
        internal_products=[
            make_internal_product(content_status="PENDING"),
        ],
    )

    def runner(argv: list[str]) -> subprocess.CompletedProcess:
        script = argv[1]

        if script.endswith("prepare_product_content.py"):
            repository.client.tables["internal_products"][0][
                "content_status"
            ] = "DRAFTED"
            return subprocess.CompletedProcess(argv, 0, "", "")

        if script.endswith("audit_pipeline_state.py"):
            return subprocess.CompletedProcess(
                argv,
                0,
                "[1] WARNING | ISBN_MISSING\nEntity: x\nDetails: y\n"
                "[2] WARNING | WEIGHT_MISSING\nEntity: x\nDetails: y\n",
                "",
            )

        return subprocess.CompletedProcess(argv, 0, "", "")

    args = make_args()
    report = run_batch.process_one_candidate(
        CANDIDATE_CODE, args, repository, runner, always_confirm, None
    )

    assert report.result != "AUDIT_FAILED"
    assert report.actions_executed == ["prepare_product_content.py"]
    # Content is now DRAFTED -- a human gate for approval, reached only
    # because the batch was allowed to continue past accepted warnings.
    assert report.final_state == "CONTENT_DRAFTED"
    assert report.human_gate is True


def test_unaccepted_warning_stops_the_batch():
    repository = make_repository(
        product_candidates=[
            make_candidate(identity_status="IDENTITY_VERIFIED"),
        ],
        product_references=[make_reference()],
        internal_products=[
            make_internal_product(content_status="PENDING"),
        ],
    )

    def runner(argv: list[str]) -> subprocess.CompletedProcess:
        script = argv[1]

        if script.endswith("prepare_product_content.py"):
            repository.client.tables["internal_products"][0][
                "content_status"
            ] = "DRAFTED"
            return subprocess.CompletedProcess(argv, 0, "", "")

        if script.endswith("audit_pipeline_state.py"):
            return subprocess.CompletedProcess(
                argv,
                0,
                "[1] WARNING | APPROVED_AT_MISSING\nEntity: x\nDetails: y\n",
                "",
            )

        return subprocess.CompletedProcess(argv, 0, "", "")

    args = make_args()
    report = run_batch.process_one_candidate(
        CANDIDATE_CODE, args, repository, runner, always_confirm, None
    )

    assert report.result == "AUDIT_FAILED"


# --------------------------------------------------------------------------
# 10. A human gate stops the candidate without invoking any writer
# --------------------------------------------------------------------------


def test_human_gate_stops_candidate_without_invoking_writer():
    repository = make_repository(
        product_candidates=[make_candidate(identity_status="IDENTITY_PENDING")],
    )
    runner, calls = recording_runner()
    args = make_args()

    report = run_batch.process_one_candidate(
        CANDIDATE_CODE, args, repository, runner, always_confirm, None
    )

    assert report.result == "HUMAN_GATE"
    assert report.initial_state == "EXTRACTED"
    assert calls == []


# --------------------------------------------------------------------------
# 11. recovery_required prevents any WooCommerce draft retry
# --------------------------------------------------------------------------


def test_recovery_required_prevents_woo_draft_retry():
    repository = make_repository(
        product_candidates=[make_candidate(identity_status="IDENTITY_VERIFIED")],
        product_references=[make_reference()],
        internal_products=[
            make_internal_product(woocommerce_status="DRAFT_CREATED"),
        ],
        woocommerce_product_syncs=[
            make_sync(
                response_payload={"recovery_required": True},
            ),
        ],
    )
    runner, calls = recording_runner()
    # Even with explicit authorization, recovery must still stop -- the
    # derived state is not READY_FOR_DRAFT, so --allow-woo-draft is moot.
    args = make_args(allow_woo_draft=True)

    report = run_batch.process_one_candidate(
        CANDIDATE_CODE, args, repository, runner, always_confirm, None
    )

    assert report.result == "HUMAN_GATE"
    assert report.recovery_state == "REMOTE_CREATED_LOCAL_DIRTY"
    assert calls == []


# --------------------------------------------------------------------------
# 12. READY_FOR_DRAFT without --allow-woo-draft stops as a human gate
# --------------------------------------------------------------------------


def test_ready_for_draft_without_authorization_stops():
    repository = make_repository(
        product_candidates=[make_candidate(identity_status="IDENTITY_VERIFIED")],
        product_references=[make_reference()],
        internal_products=[
            make_internal_product(woocommerce_status="READY_FOR_DRAFT"),
        ],
    )
    runner, calls = recording_runner()
    args = make_args(allow_woo_draft=False)

    report = run_batch.process_one_candidate(
        CANDIDATE_CODE, args, repository, runner, always_confirm, None
    )

    assert report.result == "HUMAN_GATE"
    assert report.initial_state == "READY_FOR_DRAFT"
    assert calls == []


# --------------------------------------------------------------------------
# 13. READY_FOR_DRAFT with bounded explicit authorization delegates to
#     create_woocommerce_draft.py
# --------------------------------------------------------------------------


def test_ready_for_draft_with_authorization_delegates_to_draft_creation():
    repository = make_repository(
        product_candidates=[make_candidate(identity_status="IDENTITY_VERIFIED")],
        product_references=[make_reference()],
        internal_products=[
            make_internal_product(woocommerce_status="READY_FOR_DRAFT"),
        ],
    )
    runner, calls = recording_runner()
    args = make_args(allow_woo_draft=True)

    run_batch.process_one_candidate(
        CANDIDATE_CODE, args, repository, runner, always_confirm, None
    )

    assert len(calls) == 1
    assert calls[0][1].endswith("create_woocommerce_draft.py")
    assert "--product-code" in calls[0]
    assert PRODUCT_CODE in calls[0]
    assert "--confirm-create" in calls[0]
    assert "--non-interactive" in calls[0]


# --------------------------------------------------------------------------
# 14. DRAFT_CREATED delegates to reconciliation, not creation
# --------------------------------------------------------------------------


def test_draft_created_delegates_to_reconciliation_not_creation():
    repository = make_repository(
        product_candidates=[make_candidate(identity_status="IDENTITY_VERIFIED")],
        product_references=[make_reference()],
        internal_products=[
            make_internal_product(woocommerce_status="DRAFT_CREATED"),
        ],
        woocommerce_product_syncs=[make_sync()],
    )
    runner, calls = recording_runner()
    # Authorization must not matter here -- DRAFT_CREATED is not
    # READY_FOR_DRAFT, so create_woocommerce_draft.py must never be called.
    args = make_args(allow_woo_draft=True)

    run_batch.process_one_candidate(
        CANDIDATE_CODE, args, repository, runner, always_confirm, None
    )

    assert len(calls) == 1
    assert calls[0][1].endswith("sync_woocommerce_product_status.py")
    assert "create_woocommerce_draft.py" not in calls[0][1]


# --------------------------------------------------------------------------
# 15. No publish/price action exists anywhere in the dispatch table
# --------------------------------------------------------------------------


FORBIDDEN_SUBSTRINGS = (
    "publish",
    "PUBLISH",
    "price",
    "PRICE",
    "regular_price",
    "sale_price",
)


def test_no_publish_or_price_action_in_dispatch_table():
    sample_state = types.SimpleNamespace(
        candidate_code=CANDIDATE_CODE,
        product_code=PRODUCT_CODE,
    )

    all_entries = list(run_batch.AUTOMATABLE_DISPATCH.values()) + [
        run_batch.WOO_DRAFT_DISPATCH
    ]

    assert len(all_entries) >= 6

    for entry in all_entries:
        assert "publish" not in entry.script.lower()

        built_args = entry.build_args(sample_state)

        for arg in built_args:
            for forbidden in FORBIDDEN_SUBSTRINGS:
                assert forbidden not in arg, (
                    f"{entry.script} arg {arg!r} contains forbidden "
                    f"substring {forbidden!r}"
                )


# --------------------------------------------------------------------------
# 16. default_subprocess_runner decodes child stdout/stderr as UTF-8
#
# Regression coverage for the Windows encoding defect found during the
# FB-2026-001-CAN-0009/0010/0011 orchestrator pilot: capture_output=True +
# text=True with no explicit encoding decodes child output using the
# Windows legacy codepage (cp1252), which mangled Vietnamese titles and
# crashed a subprocess reader thread with UnicodeDecodeError on byte 0x90.
# Production DB data was unaffected -- this was a console-capture defect
# only. Fix: pass encoding="utf-8", errors="replace" explicitly.
# --------------------------------------------------------------------------


def test_default_subprocess_runner_passes_utf8_encoding_and_replace_errors():
    """subprocess.run must be called with deterministic UTF-8 decoding,
    never the platform-default locale encoding."""
    with patch("run_batch.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["dummy"], returncode=0, stdout="", stderr=""
        )

        run_batch.default_subprocess_runner(["dummy", "arg"])

        assert mock_run.call_count == 1
        _args, kwargs = mock_run.call_args
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        # Unrelated capture behavior must remain unchanged by the fix.
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["cwd"] == run_batch.PROJECT_ROOT


def test_default_subprocess_runner_vietnamese_stdout_roundtrips(monkeypatch):
    """A real child process writing UTF-8 Vietnamese text to stdout must be
    decoded back to the exact original string, not mojibake."""
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    title = "Giáo Dục Giới Tính - Không Phải Lỗi Của Con"

    result = run_batch.default_subprocess_runner(
        [str(run_batch.PYTHON_EXE), "-c", f"print({title!r})"]
    )

    assert result.returncode == 0
    assert result.stdout.strip() == title


def test_default_subprocess_runner_malformed_output_cannot_crash_capture():
    """Invalid UTF-8 bytes in child output (e.g. byte 0x90, which broke
    cp1252 decoding during the pilot) must never raise UnicodeDecodeError
    or crash the orchestrator's log capture -- they must be replaced."""
    result = run_batch.default_subprocess_runner(
        [
            str(run_batch.PYTHON_EXE),
            "-c",
            "import sys; sys.stdout.buffer.write(bytes([0x90, 0x41, 0x42])); "
            "sys.stdout.buffer.flush()",
        ]
    )

    assert result.returncode == 0
    # The malformed byte becomes U+FFFD; the well-formed ASCII around it
    # survives intact -- no exception, no dropped output.
    assert "�" in result.stdout
    assert "AB" in result.stdout


def test_default_subprocess_runner_return_code_unchanged():
    result = run_batch.default_subprocess_runner(
        [str(run_batch.PYTHON_EXE), "-c", "import sys; sys.exit(7)"]
    )

    assert result.returncode == 7


def test_default_subprocess_runner_stdout_stderr_capture_unchanged():
    result = run_batch.default_subprocess_runner(
        [
            str(run_batch.PYTHON_EXE),
            "-c",
            "import sys; print('out-line'); print('err-line', file=sys.stderr)",
        ]
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "out-line"
    assert result.stderr.strip() == "err-line"


# --------------------------------------------------------------------------
# 17. Preflight integration (Phase 5): a blocking preflight failure prevents
# any writer from running; --dry-run skips preflight entirely.
# --------------------------------------------------------------------------


def test_preflight_failure_prevents_writers():
    repository = make_repository(
        product_candidates=[make_candidate(identity_status="IDENTITY_VERIFIED")],
        product_references=[make_reference()],
    )
    runner, calls = recording_runner()

    exit_code = run_batch.main(
        ["--candidate-code", CANDIDATE_CODE, "--max-candidates", "1", "--non-interactive"],
        repository=repository,
        subprocess_runner=runner,
        confirm=always_confirm,
        preflight_runner=always_blocked_preflight,
    )

    assert exit_code == 2
    assert calls == []  # no writer, no audit checkpoint subprocess was ever invoked


def test_default_preflight_runner_reuses_the_batchs_own_repository(monkeypatch):
    """When no preflight_runner is injected, main() must build its own
    default closure that calls preflight_pipeline.run_preflight() reusing
    this run's own repository -- not construct a second live connection."""
    repository = make_repository(
        product_candidates=[make_candidate(identity_status="IDENTITY_VERIFIED")],
        product_references=[make_reference()],
    )
    runner, calls = recording_runner()
    seen_repositories = []

    def fake_run_preflight(*, repository):
        seen_repositories.append(repository)
        return run_batch.preflight_pipeline.PreflightResult(checks=())

    monkeypatch.setattr(
        run_batch.preflight_pipeline, "run_preflight", fake_run_preflight
    )

    exit_code = run_batch.main(
        ["--candidate-code", CANDIDATE_CODE, "--max-candidates", "1", "--non-interactive"],
        repository=repository,
        subprocess_runner=runner,
        confirm=always_confirm,
    )

    assert exit_code == 0
    assert seen_repositories == [repository]


def test_dry_run_skips_preflight_entirely():
    """--dry-run must remain usable for diagnostics even when preflight
    would otherwise report a blocking failure (CLAUDE.md Phase 5)."""
    repository = make_repository(
        product_candidates=[make_candidate(identity_status="IDENTITY_VERIFIED")],
        product_references=[make_reference()],
    )
    runner, calls = recording_runner()

    def exploding_preflight_runner():
        raise AssertionError("preflight must not run at all in --dry-run mode")

    exit_code = run_batch.main(
        [
            "--candidate-code",
            CANDIDATE_CODE,
            "--max-candidates",
            "1",
            "--dry-run",
            "--non-interactive",
        ],
        repository=repository,
        subprocess_runner=runner,
        confirm=always_confirm,
        preflight_runner=exploding_preflight_runner,
    )

    assert exit_code == 0
    assert calls == []


# --------------------------------------------------------------------------
# 18. One REVIEW_REQUIRED (human-gated) candidate does not stop other,
# independent candidates in the same bounded batch.
# --------------------------------------------------------------------------


def test_one_human_gated_candidate_does_not_stop_independent_candidates():
    other_code = "FB-2026-001-CAN-0002"
    other_candidate_id = "88888888-8888-8888-8888-888888888888"

    repository = make_repository(
        product_candidates=[
            # First candidate: no identity evidence yet -> human gate.
            make_candidate(identity_status="IDENTITY_PENDING"),
            # Second candidate: fully clear to advance automatically.
            make_candidate(
                candidate_id=other_candidate_id,
                candidate_code=other_code,
                identity_status="IDENTITY_VERIFIED",
            ),
        ],
        product_references=[
            make_reference(
                reference_id="reference-2",
                candidate_id=other_candidate_id,
            ),
        ],
    )
    runner, calls = recording_runner()

    exit_code = run_batch.main(
        [
            "--candidate-code",
            CANDIDATE_CODE,
            "--candidate-code",
            other_code,
            "--max-candidates",
            "2",
            "--non-interactive",
        ],
        repository=repository,
        subprocess_runner=runner,
        confirm=always_confirm,
        preflight_runner=always_ready_preflight,
    )

    assert exit_code == 0
    # The human-gated candidate never invoked a writer, but the
    # independent second candidate still advanced.
    assert len(calls) == 1
    assert calls[0][-3] == other_code


# --------------------------------------------------------------------------
# 19. Final grouped summary (Phase 6): candidates classify into exactly one
# of the named result groups, and a human-gated candidate's short reason
# is printed.
# --------------------------------------------------------------------------


def test_classify_report_group_covers_the_named_groups():
    ready_report = run_batch.CandidateReport(
        candidate_code="CAN-READY",
        final_state="READY_FOR_DRAFT",
        human_gate=True,
    )
    review_report = run_batch.CandidateReport(
        candidate_code="CAN-REVIEW",
        final_state="CONTENT_DRAFTED",
        human_gate=True,
        human_gate_reason="Content awaits human approval.",
    )
    blocked_report = run_batch.CandidateReport(
        candidate_code="CAN-BLOCKED",
        result="STAGE_FAILED",
    )
    rejected_report = run_batch.CandidateReport(
        candidate_code="CAN-REJECTED",
        final_state="DUPLICATE_REJECTED",
    )
    draft_created_report = run_batch.CandidateReport(
        candidate_code="CAN-DRAFTED",
        final_state="DRAFT_CREATED",
    )
    recovery_report = run_batch.CandidateReport(
        candidate_code="CAN-RECOVERY",
        recovery_state="CREATE_RESULT_UNCERTAIN",
    )

    assert run_batch.classify_report_group(ready_report) == "READY_FOR_DRAFT"
    assert run_batch.classify_report_group(review_report) == "REVIEW_REQUIRED"
    assert run_batch.classify_report_group(blocked_report) == "BLOCKED"
    assert run_batch.classify_report_group(rejected_report) == "AUTO_REJECTED"
    assert run_batch.classify_report_group(draft_created_report) == "DRAFT_CREATED"
    assert run_batch.classify_report_group(recovery_report) == "RECOVERY_REQUIRED"


def test_failed_woo_draft_creation_is_blocked_not_ready_for_draft():
    """Regression: a candidate still shows final_state="READY_FOR_DRAFT"
    (state is only reassigned after a *successful* dispatch+audit) even
    when create_woocommerce_draft.py itself failed -- report.result is
    "STAGE_FAILED" in that case, and that must win over the READY_FOR_DRAFT
    final_state so a failed authorized Woo draft attempt is never reported
    as if it were merely awaiting authorization."""
    failed_after_authorization = run_batch.CandidateReport(
        candidate_code="CAN-WOO-FAILED",
        final_state="READY_FOR_DRAFT",
        human_gate=True,
        result="STAGE_FAILED",
    )

    assert run_batch.classify_report_group(failed_after_authorization) == "BLOCKED"


def test_failed_reconciliation_is_blocked_not_draft_created():
    failed_reconciliation = run_batch.CandidateReport(
        candidate_code="CAN-SYNC-FAILED",
        final_state="DRAFT_CREATED",
        result="STAGE_FAILED",
    )

    assert run_batch.classify_report_group(failed_reconciliation) == "BLOCKED"


def test_reconciled_terminal_state_gets_its_own_group():
    reconciled_report = run_batch.CandidateReport(
        candidate_code="CAN-DONE",
        final_state="RECONCILED",
        result="TERMINAL",
    )

    assert run_batch.classify_report_group(reconciled_report) == "RECONCILED"


def test_grouped_summary_prints_reason_for_review_required(capsys):
    review_report = run_batch.CandidateReport(
        candidate_code="CAN-0021",
        final_state="CONTENT_DRAFTED",
        human_gate=True,
        human_gate_reason="Content draft awaits human approval.",
    )

    run_batch.print_grouped_summary([review_report], requested=1)

    output = capsys.readouterr().out
    assert "REVIEW_REQUIRED: 1" in output
    assert "CAN-0021" in output
    assert "Content draft awaits human approval." in output


def test_grouped_summary_never_lists_ready_for_draft_under_review_required(capsys):
    ready_report = run_batch.CandidateReport(
        candidate_code="CAN-READY",
        final_state="READY_FOR_DRAFT",
        human_gate=True,
        human_gate_reason="WooCommerce draft creation is a human gate by default.",
    )

    run_batch.print_grouped_summary([ready_report], requested=1)

    output = capsys.readouterr().out
    assert "READY_FOR_DRAFT: 1" in output
    assert "REVIEW_REQUIRED" not in output


# --------------------------------------------------------------------------
# 20. Bounded Woo authorization request (Phase 7): lists exactly the
# READY_FOR_DRAFT candidates from this bounded run, nothing else, and is
# suppressed once --allow-woo-draft was actually supplied.
# --------------------------------------------------------------------------


def test_woo_approval_request_lists_only_ready_for_draft_candidates(capsys):
    ready_report = run_batch.CandidateReport(
        candidate_code="CAN-READY",
        final_state="READY_FOR_DRAFT",
        human_gate=True,
    )
    other_report = run_batch.CandidateReport(
        candidate_code="CAN-OTHER",
        final_state="DRAFT_CREATED",
    )

    run_batch.print_woo_approval_request([ready_report, other_report], allow_woo_draft=False)

    output = capsys.readouterr().out
    assert "READY_FOR_DRAFT: 1" in output
    assert "CAN-READY" in output
    assert "CAN-OTHER" not in output
    assert "bounded human authorization" in output


def test_woo_approval_request_suppressed_when_already_authorized(capsys):
    ready_report = run_batch.CandidateReport(
        candidate_code="CAN-READY",
        final_state="READY_FOR_DRAFT",
        human_gate=True,
    )

    run_batch.print_woo_approval_request([ready_report], allow_woo_draft=True)

    output = capsys.readouterr().out
    assert output == ""


def test_woo_approval_request_silent_when_nothing_ready(capsys):
    other_report = run_batch.CandidateReport(
        candidate_code="CAN-OTHER",
        final_state="DRAFT_CREATED",
    )

    run_batch.print_woo_approval_request([other_report], allow_woo_draft=False)

    output = capsys.readouterr().out
    assert output == ""
