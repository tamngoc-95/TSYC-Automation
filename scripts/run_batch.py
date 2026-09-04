"""
Minimal recovery-aware batch orchestrator (TSYC Phase C, P0).

Processes an explicit, bounded allowlist of candidates through the existing
TSYC pipeline. This script never implements a pipeline stage itself -- it
only:

  1. derives current state (scripts/pipeline_state.py, read-only)
  2. decides the single next valid stage
  3. invokes the existing stage script as a subprocess
     (.venv/Scripts/python.exe scripts/<stage>.py ...)
  4. re-reads state to checkpoint the expected transition
  5. runs scripts/audit_pipeline_state.py as an integrity checkpoint
  6. stops at human gates, recovery states, or the first audit failure

Existing scripts remain the only writers. This script never publishes a
WooCommerce product, never changes a selling price, and never touches
.env/secrets/Git.

Usage:
    .venv/Scripts/python.exe scripts/run_batch.py \\
        --candidate-codes FB-2026-001-CAN-0001,FB-2026-001-CAN-0002 \\
        --max-candidates 8 \\
        --dry-run \\
        --non-interactive
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(PROJECT_ROOT))

from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain.decisions import Outcome  # noqa: E402
from src.repositories.supabase_repository import SupabaseRepository  # noqa: E402

import preflight_pipeline  # noqa: E402
from pipeline_state import (  # noqa: E402
    ACCEPTED_WARNING_CODES,
    CandidateState,
    derive_candidate_state,
    load_candidate_bundle,
)

configure_utf8_console()


SCRIPT_VERSION = "1.0.0"

# The repository virtual environment interpreter. CLAUDE.md is explicit:
# never fall back to system Python when .venv exists. Existence is checked
# lazily, only when a real subprocess call is about to be made (dry-run and
# fully-human-gated runs never need it, and tests inject their own runner).
PYTHON_EXE = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"

MAX_STAGES_PER_CANDIDATE = 10

TERMINAL_DERIVED_STATES = {"RECONCILED", "DUPLICATE_REJECTED"}

_AUDIT_ISSUE_LINE = re.compile(r"^\[\d+\]\s+(ERROR|WARNING)\s+\|\s+(\S+)\s*$")


class OrchestratorArgumentError(RuntimeError):
    """Raised for CLI/allowlist violations that must fail before any I/O."""


@dataclass(frozen=True)
class DispatchEntry:
    """One automatable next-stage mapping: derived state -> existing script."""

    script: str
    build_args: Callable[[CandidateState], list[str]]
    description: str


# Map each automatable derived state to exactly one existing script. Every
# invocation is built from --candidate-code / --product-code plus the
# script's own --non-interactive and (where the script has one) --confirm-*
# flag -- never any pricing, publish, or human-judgment argument the
# orchestrator itself is not authorized to decide.
AUTOMATABLE_DISPATCH: dict[str, DispatchEntry] = {
    "REFERENCE_REGISTERED": DispatchEntry(
        script="collect_reference_metadata.py",
        build_args=lambda state: [
            "--candidate-code",
            state.candidate_code,
            "--non-interactive",
        ],
        description=(
            "Collect metadata from the already-selected reference source."
        ),
    ),
    "REFERENCE_COLLECTED": DispatchEntry(
        script="match_candidate_identity.py",
        build_args=lambda state: [
            "--candidate-code",
            state.candidate_code,
            "--mode",
            "AUTO",
            "--non-interactive",
            "--confirm-save",
        ],
        description="Run automatic identity matching (AUTO mode only).",
    ),
    "IDENTITY_VERIFIED": DispatchEntry(
        script="create_internal_product.py",
        build_args=lambda state: [
            "--candidate-code",
            state.candidate_code,
            "--non-interactive",
            "--confirm-create",
        ],
        description="Create the internal product record.",
    ),
    "INTERNAL_PRODUCT_CREATED": DispatchEntry(
        script="prepare_product_content.py",
        build_args=lambda state: [
            "--product-code",
            state.product_code,
            "--action",
            "SAVE",
            "--non-interactive",
        ],
        description=(
            "Generate the conservative metadata-only content draft "
            "(never approves content)."
        ),
    ),
    "CONTENT_DRAFTED": DispatchEntry(
        script="prepare_product_content.py",
        build_args=lambda state: [
            "--product-code",
            state.product_code,
            "--action",
            "APPROVE",
            "--non-interactive",
        ],
        description=(
            "Attempt automatic content approval (CLAUDE.md 15.3): "
            "re-runs the deterministic content_rules checks and approves "
            "only when they all pass. When they do not, the script itself "
            "downgrades content_status to REVIEW_REQUIRED and exits 0 -- "
            "this candidate then stops at the resulting CONTENT_REVIEW_"
            "REQUIRED human gate on the next state read, without ever "
            "failing the stage or stopping any other candidate."
        ),
    ),
    "IMAGE_VALIDATED": DispatchEntry(
        script="check_draft_readiness.py",
        build_args=lambda state: [
            "--product-code",
            state.product_code,
            "--non-interactive",
            "--confirm-ready",
        ],
        description="Evaluate and mark WooCommerce draft readiness.",
    ),
    "DRAFT_CREATED": DispatchEntry(
        script="sync_woocommerce_product_status.py",
        build_args=lambda state: [
            "--product-code",
            state.product_code,
            "--non-interactive",
            "--confirm-sync",
        ],
        description=(
            "Reconcile local state with the remote WooCommerce product "
            "(read-only against WooCommerce; never creates or publishes)."
        ),
    ),
}

# READY_FOR_DRAFT is a human gate by default. It is only ever dispatched
# when the caller passes --allow-woo-draft on this exact bounded
# invocation -- see decide_action().
WOO_DRAFT_DISPATCH = DispatchEntry(
    script="create_woocommerce_draft.py",
    build_args=lambda state: [
        "--product-code",
        state.product_code,
        "--non-interactive",
        "--confirm-create",
    ],
    description=(
        "Create the WooCommerce draft product (explicitly authorized via "
        "--allow-woo-draft). Draft only -- never publishes, never sets a "
        "price."
    ),
)


@dataclass
class CandidateReport:
    """Everything the final summary table and counts need for one candidate."""

    candidate_code: str
    initial_state: str = ""
    final_state: str = ""
    actions_executed: list[str] = field(default_factory=list)
    human_gate: bool = False
    human_gate_reason: str | None = None
    recovery_state: str | None = None
    blocked: bool = False
    blocked_reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    result: str = ""
    # The canonical src.domain.decisions.Outcome vocabulary this
    # candidate's last derived state maps to (AUTO_PASS/AUTO_REJECT/
    # REVIEW_REQUIRED/BLOCKED) -- reporting only; `result` above (HUMAN_
    # GATE, STAGE_FAILED, DRY_RUN, ...) remains authoritative for actual
    # dispatch decisions. See pipeline_state.CandidateState.outcome.
    outcome: str = Outcome.AUTO_PASS
    outcome_reason: str | None = None
    # The last decide_action() call's (kind, description) for this
    # candidate -- captured for every run (dry-run and real) so
    # print_dry_run_plan() below has one source of truth instead of
    # re-deriving the decision a second time.
    next_action_kind: str = ""
    next_action_description: str = ""


def parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Process a bounded, explicit allowlist of candidates through "
            "the existing TSYC pipeline without duplicating any stage's "
            "business logic."
        )
    )

    parser.add_argument(
        "--candidate-code",
        action="append",
        dest="candidate_code",
        help="Process one exact candidate code. Repeatable.",
    )

    parser.add_argument(
        "--candidate-codes",
        help="Comma-separated exact candidate codes.",
    )

    parser.add_argument(
        "--batch-code",
        help=(
            "Optional: restrict the allowlist to candidates belonging to "
            "this exact batch. Any candidate outside the batch fails "
            "resolution for that candidate."
        ),
    )

    parser.add_argument(
        "--max-candidates",
        type=int,
        required=True,
        help="Hard upper bound on the number of candidates in this run.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Read-only. Derive state and print the next action for every "
            "candidate. Never invokes a writer script."
        ),
    )

    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help=(
            "Do not prompt for confirmation before invoking a writer "
            "stage. (Every invoked stage script is always called with its "
            "own --non-interactive flag regardless of this setting.)"
        ),
    )

    parser.add_argument(
        "--allow-woo-draft",
        action="store_true",
        help=(
            "Authorize create_woocommerce_draft.py for candidates in "
            "READY_FOR_DRAFT state on this exact bounded invocation. "
            "Without this flag, READY_FOR_DRAFT always stops as a human "
            "gate."
        ),
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "Print the full per-stage trace (derived state, dispatched "
            "script, subprocess tail) for every candidate. Without this "
            "flag only the final grouped batch result is printed -- the "
            "stable default operator experience."
        ),
    )

    return parser.parse_args(argv)


def resolve_allowlist(args: argparse.Namespace) -> list[str]:
    """
    Resolve the exact bounded candidate allowlist, or fail before any I/O.

    No implicit "all candidates" or "newest candidates" mode exists --
    only an explicit, repeatable/comma-separated list.
    """
    codes: list[str] = []

    if args.candidate_code:
        codes.extend(args.candidate_code)

    if args.candidate_codes:
        codes.extend(
            part.strip()
            for part in args.candidate_codes.split(",")
            if part.strip()
        )

    ordered: list[str] = []
    seen: set[str] = set()

    for code in codes:
        if code not in seen:
            seen.add(code)
            ordered.append(code)

    if not ordered:
        raise OrchestratorArgumentError(
            "No explicit candidate allowlist supplied. Pass "
            "--candidate-code (repeatable) or --candidate-codes. "
            "Implicit 'all candidates' / 'newest candidates' processing "
            "is not supported."
        )

    if args.max_candidates < 1:
        raise OrchestratorArgumentError(
            "--max-candidates must be at least 1."
        )

    if len(ordered) > args.max_candidates:
        raise OrchestratorArgumentError(
            f"Candidate count ({len(ordered)}) exceeds --max-candidates "
            f"({args.max_candidates}). Failing before any read or write."
        )

    return ordered


def get_batch_by_code(
    repository: SupabaseRepository,
    batch_code: str,
) -> dict[str, Any] | None:
    """Read-only lookup of one batch by its business code."""
    rows = (
        repository.client
        .table("batches")
        .select("*")
        .eq("batch_code", batch_code)
        .limit(1)
        .execute()
        .data
        or []
    )

    return rows[0] if rows else None


def parse_audit_output(stdout: str) -> tuple[int, list[str]]:
    """
    Parse scripts/audit_pipeline_state.py's printed issue list.

    Returns (error_count, warning_codes). Parsed from stdout rather than
    the exit code alone because PASS and PASS_WITH_WARNINGS both exit 0 --
    the actual warning codes must be inspected to decide whether they are
    all CLAUDE.md-accepted (ISBN_MISSING / WEIGHT_MISSING).
    """
    error_count = 0
    warning_codes: list[str] = []

    for line in (stdout or "").splitlines():
        match = _AUDIT_ISSUE_LINE.match(line.strip())

        if not match:
            continue

        severity, code = match.groups()

        if severity == "ERROR":
            error_count += 1
        else:
            warning_codes.append(code)

    return error_count, warning_codes


def default_subprocess_runner(argv: list[str]) -> subprocess.CompletedProcess:
    """Run one existing pipeline script with the repository virtual env."""
    if not PYTHON_EXE.exists():
        raise RuntimeError(
            "Repository virtual environment Python was not found at "
            f"{PYTHON_EXE}. Refusing to fall back to system Python."
        )

    return subprocess.run(
        argv,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def default_confirm(prompt: str) -> bool:
    """Interactive yes/no prompt used when --non-interactive is not set."""
    answer = input(prompt).strip().lower()
    return answer in ("y", "yes")


def build_argv(dispatch: DispatchEntry, state: CandidateState) -> list[str]:
    """Build the full subprocess argv for one dispatch entry."""
    return [
        str(PYTHON_EXE),
        str(SCRIPTS_DIR / dispatch.script),
        *dispatch.build_args(state),
    ]


def decide_action(
    state: CandidateState,
    allow_woo_draft: bool,
) -> tuple[str, DispatchEntry | None, str]:
    """
    Decide the single next valid action for one derived state.

    Returns (kind, dispatch_entry_or_None, description) where kind is one
    of "invoke", "human_gate", "terminal", "blocked".
    """
    if state.blocked:
        return ("blocked", None, state.blocked_reason or "Blocked.")

    if state.terminal:
        return (
            "terminal",
            None,
            f"{state.derived_state} -- already complete, no action required.",
        )

    if state.human_gate:
        if state.derived_state == "READY_FOR_DRAFT" and allow_woo_draft:
            return ("invoke", WOO_DRAFT_DISPATCH, WOO_DRAFT_DISPATCH.description)

        return (
            "human_gate",
            None,
            state.human_gate_reason or "Human review required.",
        )

    dispatch = AUTOMATABLE_DISPATCH.get(state.derived_state)

    if dispatch is not None:
        return ("invoke", dispatch, dispatch.description)

    # Safety net: an unmapped, non-gated, non-terminal state must never be
    # silently skipped as if nothing were needed. Surface it instead of
    # inventing a transition unsupported by the DB/script state.
    return (
        "human_gate",
        None,
        f"No automated dispatch is mapped for derived_state="
        f"{state.derived_state!r}. Manual review required.",
    )


def print_pre_block(
    candidate_code: str,
    state: CandidateState,
    next_stage_label: str,
    action_label: str,
    *,
    verbose: bool = True,
) -> None:
    if not verbose:
        return

    print()
    print(f"Candidate: {candidate_code}")
    print(f"Derived state: {state.derived_state}")
    print(f"Next stage: {next_stage_label}")
    print(f"Action: {action_label}")


def print_post_block(
    result_label: str,
    new_state_label: str,
    warnings: list[str],
    blocker: str | None,
    human_gate: bool,
    recovery_state: str | None,
    *,
    verbose: bool = True,
) -> None:
    if not verbose:
        return

    print(f"Result: {result_label}")
    print(f"New state: {new_state_label}")
    print(f"Warnings: {', '.join(warnings) if warnings else 'none'}")
    print(f"Blocker: {blocker or 'none'}")
    print(f"Human gate: {'yes' if human_gate else 'no'}")
    print(f"Recovery state: {recovery_state or 'none'}")


def _print_subprocess_tail(
    completed: subprocess.CompletedProcess,
    *,
    verbose: bool = True,
) -> None:
    """Print a short tail of a subprocess's output for operator visibility."""
    if not verbose:
        return

    for stream_name, text in (("stdout", completed.stdout), ("stderr", completed.stderr)):
        if not text:
            continue

        lines = text.strip().splitlines()
        tail = lines[-10:]

        print(f"  [{stream_name}] (last {len(tail)} of {len(lines)} lines)")

        for line in tail:
            print(f"    {line}")


def process_one_candidate(
    candidate_code: str,
    args: argparse.Namespace,
    repository: SupabaseRepository,
    subprocess_runner: Callable[[list[str]], subprocess.CompletedProcess],
    confirm: Callable[[str], bool],
    expected_batch_id: str | None,
) -> CandidateReport:
    """Process one candidate through as many automatable stages as apply."""
    verbose = getattr(args, "verbose", False)
    report = CandidateReport(candidate_code=candidate_code)

    bundle = load_candidate_bundle(repository, candidate_code)

    if bundle is None:
        report.initial_state = "NOT_FOUND"
        report.final_state = "NOT_FOUND"
        report.result = "NOT_FOUND"
        print()
        print(f"Candidate: {candidate_code}")
        print("Result: NOT FOUND -- no product_candidates row for this code.")
        return report

    if expected_batch_id is not None:
        candidate_batch_id = bundle["candidate"].get("batch_id")

        if candidate_batch_id != expected_batch_id:
            report.initial_state = "BATCH_MISMATCH"
            report.final_state = "BATCH_MISMATCH"
            report.result = "BATCH_MISMATCH"
            print()
            print(f"Candidate: {candidate_code}")
            print(
                "Result: BATCH MISMATCH -- candidate does not belong to "
                f"--batch-code {args.batch_code}."
            )
            return report

    state = derive_candidate_state(bundle)
    report.initial_state = state.derived_state

    for _ in range(MAX_STAGES_PER_CANDIDATE):
        kind, dispatch, description = decide_action(state, args.allow_woo_draft)
        report.next_action_kind = kind
        report.next_action_description = description

        next_stage_label = (
            dispatch.script
            if dispatch is not None
            else f"(none -- {kind})"
        )

        print_pre_block(
            candidate_code, state, next_stage_label, description, verbose=verbose
        )

        if kind == "blocked":
            report.result = "BLOCKED"
            print_post_block(
                "BLOCKED",
                state.derived_state,
                state.warnings,
                state.blocked_reason,
                state.human_gate,
                state.recovery_state,
                verbose=verbose,
            )
            break

        if kind == "terminal":
            report.result = "TERMINAL"
            print_post_block(
                "ALREADY COMPLETE",
                state.derived_state,
                state.warnings,
                None,
                state.human_gate,
                state.recovery_state,
                verbose=verbose,
            )
            break

        if kind == "human_gate":
            report.result = "HUMAN_GATE"
            print_post_block(
                "STOPPED AT HUMAN GATE",
                state.derived_state,
                state.warnings,
                None,
                True,
                state.recovery_state,
                verbose=verbose,
            )
            break

        # kind == "invoke"
        assert dispatch is not None

        if args.dry_run:
            report.result = "DRY_RUN"
            print_post_block(
                "DRY-RUN (read-only; no writer invoked)",
                state.derived_state,
                state.warnings,
                None,
                state.human_gate,
                state.recovery_state,
                verbose=verbose,
            )
            break

        if not args.non_interactive:
            proceed = confirm(
                f"Proceed with {dispatch.script} for {candidate_code}? "
                "[y/N]: "
            )

            if not proceed:
                report.result = "SKIPPED_BY_USER"
                print_post_block(
                    "SKIPPED (not confirmed)",
                    state.derived_state,
                    state.warnings,
                    None,
                    state.human_gate,
                    state.recovery_state,
                    verbose=verbose,
                )
                break

        argv = build_argv(dispatch, state)

        if verbose:
            print(f"  Invoking: {' '.join(argv)}")

        completed = subprocess_runner(argv)
        _print_subprocess_tail(completed, verbose=verbose)

        if completed.returncode != 0:
            report.result = "STAGE_FAILED"
            print_post_block(
                f"STAGE FAILED (exit {completed.returncode}; no retry)",
                state.derived_state,
                state.warnings,
                None,
                state.human_gate,
                state.recovery_state,
                verbose=verbose,
            )
            break

        report.actions_executed.append(dispatch.script)

        new_bundle = load_candidate_bundle(repository, candidate_code)
        new_state = derive_candidate_state(new_bundle) if new_bundle else state

        if (
            new_state.derived_state == state.derived_state
            and new_state.blocked == state.blocked
        ):
            report.result = "STALLED"
            state = new_state
            print_post_block(
                "NO STATE CHANGE DETECTED (stopping this candidate)",
                new_state.derived_state,
                new_state.warnings,
                new_state.blocked_reason,
                new_state.human_gate,
                new_state.recovery_state,
                verbose=verbose,
            )
            break

        audit_argv = [
            str(PYTHON_EXE),
            str(SCRIPTS_DIR / "audit_pipeline_state.py"),
            "--candidate-code",
            candidate_code,
        ]

        if verbose:
            print(f"  Checkpoint audit: {' '.join(audit_argv)}")

        audit_completed = subprocess_runner(audit_argv)
        _print_subprocess_tail(audit_completed, verbose=verbose)

        error_count, warning_codes = parse_audit_output(audit_completed.stdout)
        unacceptable_warnings = [
            code for code in warning_codes if code not in ACCEPTED_WARNING_CODES
        ]

        if audit_completed.returncode != 0 or error_count or unacceptable_warnings:
            report.result = "AUDIT_FAILED"
            state = new_state
            print_post_block(
                "AUDIT CHECKPOINT FAILED (stopping ENTIRE BATCH)",
                new_state.derived_state,
                new_state.warnings,
                None,
                new_state.human_gate,
                new_state.recovery_state,
                verbose=verbose,
            )
            report.final_state = state.derived_state
            report.human_gate = state.human_gate
            report.human_gate_reason = state.human_gate_reason
            report.recovery_state = state.recovery_state
            report.blocked = state.blocked
            report.blocked_reason = state.blocked_reason
            report.warnings = state.warnings
            report.outcome = state.outcome
            report.outcome_reason = state.outcome_reason
            return report

        print_post_block(
            "SUCCESS (checkpoint audit passed)"
            + (
                f" -- accepted warnings: {', '.join(warning_codes)}"
                if warning_codes
                else ""
            ),
            new_state.derived_state,
            new_state.warnings,
            None,
            new_state.human_gate,
            new_state.recovery_state,
            verbose=verbose,
        )

        state = new_state
        report.result = "ADVANCED"
    else:
        report.result = "MAX_STAGES_REACHED"

    report.final_state = state.derived_state
    report.human_gate = state.human_gate
    report.human_gate_reason = state.human_gate_reason
    report.recovery_state = state.recovery_state
    report.blocked = state.blocked
    report.blocked_reason = state.blocked_reason
    report.warnings = state.warnings
    report.outcome = state.outcome
    report.outcome_reason = state.outcome_reason

    return report


def print_detailed_summary(reports: list[CandidateReport], requested: int) -> None:
    """Print the full per-candidate table and batch-level counts.

    Only printed with --verbose -- the stable default operator experience
    is print_grouped_summary() below (CLAUDE.md section 26 / Phase 6:
    the operator should not have to read a wide per-stage table to learn
    which candidates need a decision)."""
    print()
    print("=" * 100)
    print("BATCH SUMMARY")
    print("=" * 100)
    print(
        f"{'Candidate':<30} {'Initial state':<22} {'Actions executed':<45} "
        f"{'Final state':<22} {'Human gate':<11} {'Recovery':<14} "
        f"{'Result':<18} {'Outcome'}"
    )

    for report in reports:
        actions = (
            " -> ".join(report.actions_executed)
            if report.actions_executed
            else "(none)"
        )

        print(
            f"{report.candidate_code:<30} {report.initial_state:<22} "
            f"{actions:<45} {report.final_state:<22} "
            f"{('yes' if report.human_gate else 'no'):<11} "
            f"{(report.recovery_state or '-'):<14} "
            f"{report.result:<18} {report.outcome}"
        )

    processed = [r for r in reports if r.result != "NOT_PROCESSED"]

    advanced = sum(1 for r in processed if r.actions_executed)
    already_terminal = sum(
        1
        for r in processed
        if r.initial_state in TERMINAL_DERIVED_STATES and not r.actions_executed
    )
    blocked_by_human_gate = sum(1 for r in processed if r.result == "HUMAN_GATE")
    in_recovery = sum(1 for r in processed if r.recovery_state is not None)
    stage_failures = sum(1 for r in processed if r.result == "STAGE_FAILED")
    audit_failures = sum(1 for r in processed if r.result == "AUDIT_FAILED")
    structurally_blocked = sum(1 for r in processed if r.result == "BLOCKED")
    not_found = sum(1 for r in processed if r.result in ("NOT_FOUND", "BATCH_MISMATCH"))

    print()
    print(f"Candidates requested: {requested}")
    print(f"Candidates processed: {len(processed)}")
    print(f"Candidates advanced: {advanced}")
    print(f"Candidates already terminal: {already_terminal}")
    print(f"Candidates blocked by human gate: {blocked_by_human_gate}")
    print(f"Candidates in recovery: {in_recovery}")
    print(f"Candidates structurally blocked: {structurally_blocked}")
    print(f"Candidates unresolved (not found / batch mismatch): {not_found}")
    print(f"Stage failures: {stage_failures}")
    print(f"Audit failures: {audit_failures}")


# The six named result groups from CLAUDE.md's stable operating mode /
# Phase 6, plus three internal catch-alls: RECONCILED for a candidate that
# reached full terminal completion (pipeline_state.py's "RECONCILED"
# derived state), NOT_PROCESSED for candidates the batch never reached
# after an earlier AUDIT_FAILED stop, and OTHER for any report shape none
# of the above describe -- kept so a candidate can never silently vanish
# from the summary.
GROUP_ORDER = (
    "READY_FOR_DRAFT",
    "REVIEW_REQUIRED",
    "BLOCKED",
    "AUTO_REJECTED",
    "DRAFT_CREATED",
    "RECOVERY_REQUIRED",
    "RECONCILED",
    "OTHER",
    "NOT_PROCESSED",
)

# result values that mean "a dispatched writer or the audit checkpoint
# itself failed" -- checked before READY_FOR_DRAFT/DRAFT_CREATED so that a
# failed create_woocommerce_draft.py / sync_woocommerce_product_status.py
# attempt is never reported as if it were merely awaiting authorization or
# already reconciled (final_state does not change on a failed dispatch --
# only report.result does).
_FAILURE_RESULTS = (
    "BLOCKED",
    "STAGE_FAILED",
    "AUDIT_FAILED",
    "NOT_FOUND",
    "BATCH_MISMATCH",
)


def classify_report_group(report: CandidateReport) -> str:
    """Map one CandidateReport onto exactly one of GROUP_ORDER's buckets."""
    if report.result == "NOT_PROCESSED":
        return "NOT_PROCESSED"

    if report.blocked or report.result in _FAILURE_RESULTS:
        return "BLOCKED"

    if report.recovery_state is not None:
        return "RECOVERY_REQUIRED"

    if report.final_state == "READY_FOR_DRAFT" and report.human_gate:
        return "READY_FOR_DRAFT"

    if report.final_state == "DRAFT_CREATED":
        return "DRAFT_CREATED"

    if report.final_state == "DUPLICATE_REJECTED":
        return "AUTO_REJECTED"

    if report.final_state == "RECONCILED":
        return "RECONCILED"

    if report.human_gate:
        return "REVIEW_REQUIRED"

    return "OTHER"


def print_grouped_summary(reports: list[CandidateReport], requested: int) -> None:
    """Print the stable default batch result: candidates grouped into
    READY_FOR_DRAFT / REVIEW_REQUIRED / BLOCKED / AUTO_REJECTED /
    DRAFT_CREATED / RECOVERY_REQUIRED, with a short candidate / derived
    state / reason line for each REVIEW_REQUIRED or BLOCKED candidate --
    no per-stage trace, no wide table (see --verbose for that).

    Candidates already in READY_FOR_DRAFT are never listed as needing
    review here -- they get their own group plus the bounded Woo
    authorization request printed by print_woo_approval_request().
    """
    groups: dict[str, list[CandidateReport]] = {name: [] for name in GROUP_ORDER}

    for report in reports:
        groups[classify_report_group(report)].append(report)

    print()
    print("=" * 78)
    print("BATCH RESULT")
    print("=" * 78)

    for name in GROUP_ORDER:
        members = groups[name]

        if not members:
            continue

        print()
        print(f"{name}: {len(members)}")

        for report in members:
            if name in ("REVIEW_REQUIRED", "BLOCKED"):
                reason = report.human_gate_reason or report.blocked_reason or report.result
                print()
                print(report.candidate_code)
                print(report.final_state)
                print(reason)
            elif name == "RECOVERY_REQUIRED":
                print(f"{report.candidate_code}  ({report.recovery_state})")
            else:
                print(report.candidate_code)

    print()
    print(f"Candidates requested: {requested}")
    print(f"Candidates processed: {sum(len(v) for k, v in groups.items() if k != 'NOT_PROCESSED')}")


# CLAUDE.md Phase 6: one hop's worth of prediction only -- the derived
# state immediately after the *single* dispatched action succeeds, not a
# multi-stage simulation (a dry run performs no I/O, so it cannot know
# whether a later automated check will actually pass). Where a script's
# own deterministic validation can go either way, both branches are
# named rather than picking one.
_EXPECTED_NEXT_STATE_AFTER_ACTION: dict[str, str] = {
    "REFERENCE_REGISTERED": "REFERENCE_COLLECTED",
    "REFERENCE_COLLECTED": (
        "IDENTITY_VERIFIED (or IDENTITY_PENDING/IDENTITY_CONFLICT if "
        "evidence is inconclusive)"
    ),
    "IDENTITY_VERIFIED": "INTERNAL_PRODUCT_CREATED",
    "INTERNAL_PRODUCT_CREATED": "CONTENT_DRAFTED",
    "CONTENT_DRAFTED": (
        "CONTENT_APPROVED (or CONTENT_REVIEW_REQUIRED if automatic "
        "approval checks do not all pass)"
    ),
    "IMAGE_VALIDATED": "READY_FOR_DRAFT",
    "DRAFT_CREATED": "RECONCILED",
}


def print_dry_run_plan(reports: list[CandidateReport], requested: int) -> None:
    """CLAUDE.md Phase 6: the stable dry-run/read-only planning report.

    Per candidate: CURRENT_STAGE / NEXT_SAFE_ACTION / BLOCKER /
    EXPECTED_FINAL_STATE -- so an operator never has to manually invoke
    scripts/pipeline_state.py or an individual stage script just to see
    what would happen next. Read-only: reuses the exact decision
    (report.next_action_kind/description) --dry-run already computed;
    invokes no writer and performs no additional I/O.
    """
    print()
    print("=" * 78)
    print("DRY-RUN PLAN (read-only; no writer was invoked)")
    print("=" * 78)

    planned = 0

    for report in reports:
        if report.result == "NOT_PROCESSED":
            continue

        planned += 1

        if report.result in ("NOT_FOUND", "BATCH_MISMATCH"):
            print()
            print(f"CANDIDATE: {report.candidate_code}")
            print(f"CURRENT_STAGE: {report.result}")
            print("NEXT_SAFE_ACTION: none -- candidate could not be resolved")
            print(f"BLOCKER: {report.result}")
            print(f"EXPECTED_FINAL_STATE: {report.result}")
            continue

        current_stage = report.initial_state
        kind = report.next_action_kind
        blocker = report.human_gate_reason or report.blocked_reason or "none"

        if kind == "invoke":
            next_action = report.next_action_description
            expected_final_state = _EXPECTED_NEXT_STATE_AFTER_ACTION.get(
                current_stage,
                "(next milestone not pre-mapped -- re-run after this "
                "stage to confirm)",
            )
        elif kind == "terminal":
            next_action = "none -- already complete"
            expected_final_state = current_stage
        elif kind == "blocked":
            next_action = "none -- structural precondition unmet"
            expected_final_state = current_stage
        else:  # "human_gate"
            next_action = "none -- requires human decision"
            expected_final_state = current_stage

        print()
        print(f"CANDIDATE: {report.candidate_code}")
        print(f"CURRENT_STAGE: {current_stage}")
        print(f"NEXT_SAFE_ACTION: {next_action}")
        print(f"BLOCKER: {blocker}")
        print(f"EXPECTED_FINAL_STATE: {expected_final_state}")

    print()
    print(f"Candidates requested: {requested}")
    print(f"Candidates planned: {planned}")


def print_woo_approval_request(
    reports: list[CandidateReport],
    allow_woo_draft: bool,
) -> None:
    """Print the single bounded Woo draft authorization request required
    by CLAUDE.md section 6 whenever candidates reached READY_FOR_DRAFT on
    this run without --allow-woo-draft. Never creates a draft itself --
    printing only. Asks once for the whole bounded batch, never once per
    candidate."""
    if allow_woo_draft:
        return

    pending = [
        report for report in reports if classify_report_group(report) == "READY_FOR_DRAFT"
    ]

    if not pending:
        return

    print()
    print("=" * 78)
    print(f"READY_FOR_DRAFT: {len(pending)}")
    print()
    print("Candidates:")

    for report in pending:
        print(report.candidate_code)

    print()
    print("Woo draft creation requires one bounded human authorization.")
    print(
        "Re-run with --allow-woo-draft and the exact same "
        "--candidate-code(s)/--candidate-codes to authorize exactly "
        "these candidates. No other candidate is authorized by this "
        "request."
    )


def main(
    argv: list[str] | None = None,
    *,
    repository: SupabaseRepository | None = None,
    subprocess_runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
    confirm: Callable[[str], bool] | None = None,
    preflight_runner: Callable[[], preflight_pipeline.PreflightResult] | None = None,
) -> int:
    """Run the batch orchestrator. Returns a process exit code.

    `preflight_runner` is injectable (Phase 5's "reusable Python
    function", not a recursive shell-out): production leaves it unset and
    gets a real scripts/preflight_pipeline.run_preflight() call reusing
    this run's own `repository`; tests inject a stub so the orchestrator
    test suite stays fully offline. Skipped entirely for --dry-run, which
    remains usable for diagnostics even when live connectivity (e.g. Woo)
    is unavailable -- --dry-run never invokes a writer regardless.
    """
    args = parse_arguments(argv)

    try:
        allowlist = resolve_allowlist(args)
    except OrchestratorArgumentError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2

    print("=" * 100)
    print(f"TSYC BATCH ORCHESTRATOR (v{SCRIPT_VERSION})")
    print("=" * 100)
    print(f"Requested candidates: {', '.join(allowlist)}")
    print(f"Max candidates: {args.max_candidates}")
    print(f"Dry run: {args.dry_run}")
    print(f"Non-interactive: {args.non_interactive}")
    print(f"Allow Woo draft creation: {args.allow_woo_draft}")

    if repository is None:
        repository = SupabaseRepository()

    if subprocess_runner is None:
        subprocess_runner = default_subprocess_runner

    if confirm is None:
        confirm = default_confirm

    if not args.dry_run:
        if preflight_runner is None:
            active_repository = repository

            def preflight_runner() -> preflight_pipeline.PreflightResult:
                return preflight_pipeline.run_preflight(repository=active_repository)

        preflight_result = preflight_runner()
        preflight_pipeline.print_report(preflight_result)

        if not preflight_result.ready:
            print(
                "Error: preflight reported a blocking failure -- refusing "
                "to start production writes. Resolve the blocker(s) above "
                "(or use --dry-run for a read-only diagnostic run) and "
                "try again.",
                file=sys.stderr,
            )
            return 2

    expected_batch_id: str | None = None

    if args.batch_code:
        batch = get_batch_by_code(repository, args.batch_code)

        if batch is None:
            print(
                f"Error: --batch-code {args.batch_code} does not match any "
                "batch.",
                file=sys.stderr,
            )
            return 2

        expected_batch_id = batch.get("batch_id")

    reports: list[CandidateReport] = []
    batch_stopped = False

    for candidate_code in allowlist:
        if batch_stopped:
            reports.append(
                CandidateReport(
                    candidate_code=candidate_code,
                    initial_state="NOT_PROCESSED",
                    final_state="NOT_PROCESSED",
                    result="NOT_PROCESSED",
                )
            )
            continue

        report = process_one_candidate(
            candidate_code,
            args,
            repository,
            subprocess_runner,
            confirm,
            expected_batch_id,
        )
        reports.append(report)

        if report.result == "AUDIT_FAILED":
            batch_stopped = True

    if args.verbose:
        print_detailed_summary(reports, len(allowlist))

    if args.dry_run:
        print_dry_run_plan(reports, len(allowlist))

    print_grouped_summary(reports, len(allowlist))
    print_woo_approval_request(reports, args.allow_woo_draft)

    hard_failure = any(
        report.result
        in (
            "STAGE_FAILED",
            "AUDIT_FAILED",
            "NOT_FOUND",
            "BATCH_MISMATCH",
            "BLOCKED",
        )
        for report in reports
    )

    return 1 if hard_failure else 0


if __name__ == "__main__":
    sys.exit(main())
