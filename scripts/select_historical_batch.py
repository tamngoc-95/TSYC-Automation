"""
Read-only historical-candidate batch selector (TSYC).

Identifies which FB-HIST-* candidates are safe to progress right now and
which ones need individual attention, without duplicating any decision
logic: it reuses scripts/pipeline_state.py's derive_candidate_state() and
scripts/run_batch.py's decide_action() exactly as run_batch.py itself
does, and only buckets their existing output.

Buckets (each candidate lands in exactly one):

  AUTOMATABLE_NOW   -- decide_action() returns "invoke": an existing
                       sanctioned dispatch is ready to run for this
                       candidate right now.
  RECOVERY_REVIEW   -- state.recovery_state is set. Needs individual
                       recovery review (e.g. scripts/recover_woocommerce_
                       remote_loss.py / clear_woocommerce_sync_recovery.py)
                       -- never batch-blocking for other candidates.
  CONFLICT          -- derived_state == IDENTITY_CONFLICT. Needs a human
                       identity decision (CLAUDE.md 5.2/9.2); never
                       force-resolved automatically.
  HUMAN_REVIEW      -- any other human gate or structural blocker (e.g.
                       image/content ambiguity, an unmapped derived
                       state).
  TERMINAL          -- already RECONCILED / DUPLICATE_REJECTED. No action
                       needed.

This script performs reads only. It never invokes a writer stage and
never calls scripts/run_batch.py -- for AUTOMATABLE_NOW candidates it
prints the exact run_batch.py invocation an operator can run next. That
remains a separate, explicit step: CLAUDE.md section 19.1 requires an
explicit, bounded candidate allowlist for any actual production write,
and this selector's automatic identification of "what's safe" is not a
substitute for that explicit step.

Usage:
    .venv/Scripts/python.exe scripts/select_historical_batch.py \\
        --limit 25 \\
        [--batch-code FB-HIST-2026-IMG-007] \\
        [--candidate-code FB-HIST-... [--candidate-code ...]] \\
        [--candidate-codes FB-HIST-...,FB-HIST-...]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.repositories.supabase_repository import SupabaseRepository  # noqa: E402

from create_internal_product import is_historical_candidate_code  # noqa: E402
from pipeline_state import derive_candidate_state, load_candidate_bundle  # noqa: E402
from run_batch import decide_action, get_batch_by_code  # noqa: E402

configure_utf8_console()


SCRIPT_VERSION = "1.0.0"

DEFAULT_LIMIT = 25

BUCKET_ORDER = (
    "AUTOMATABLE_NOW",
    "HUMAN_REVIEW",
    "RECOVERY_REVIEW",
    "CONFLICT",
    "TERMINAL",
    "NOT_FOUND",
)


class SelectorArgumentError(RuntimeError):
    """Raised for CLI/scope problems that must fail before any I/O."""


@dataclass(frozen=True)
class SelectionEntry:
    """One historical candidate's bucketed classification."""

    candidate_code: str
    bucket: str
    derived_state: str
    reason: str
    dispatch_script: str | None = None


def classify_bundle(
    candidate_code: str,
    bundle: dict[str, Any] | None,
) -> SelectionEntry:
    """Bucket one candidate from its already-loaded bundle (or None).

    Calls derive_candidate_state()/decide_action() exactly as run_batch.py
    does for real dispatch -- this function only reads their output and
    picks a bucket; it never re-implements the decision itself.
    allow_woo_draft is always False here: identification is not a Woo
    draft authorization (CLAUDE.md section 6/6.2), it merely reports that
    a candidate has already reached the pre-authorized historical
    READY_FOR_DRAFT_HISTORICAL dispatch.
    """
    if bundle is None:
        return SelectionEntry(
            candidate_code,
            "NOT_FOUND",
            "NOT_FOUND",
            "No product_candidates row for this code.",
        )

    state = derive_candidate_state(bundle)
    kind, dispatch, description = decide_action(state, allow_woo_draft=False)

    if state.recovery_state is not None:
        bucket = "RECOVERY_REVIEW"
        reason = (
            state.human_gate_reason
            or f"Recovery state: {state.recovery_state}."
        )
    elif state.derived_state == "IDENTITY_CONFLICT":
        bucket = "CONFLICT"
        reason = state.human_gate_reason or description
    elif kind == "terminal":
        bucket = "TERMINAL"
        reason = description
    elif kind == "invoke":
        bucket = "AUTOMATABLE_NOW"
        reason = description
    else:
        # kind in ("human_gate", "blocked") -- any other structural
        # blocker or judgment gate that is not a recovery condition and
        # not a confirmed identity conflict.
        bucket = "HUMAN_REVIEW"
        reason = state.human_gate_reason or state.blocked_reason or description

    return SelectionEntry(
        candidate_code,
        bucket,
        state.derived_state,
        reason,
        dispatch.script if dispatch is not None else None,
    )


def resolve_candidate_codes(
    repository: SupabaseRepository,
    *,
    explicit_codes: list[str] | None,
    batch_code: str | None,
    limit: int,
) -> list[str]:
    """Resolve the bounded, deterministically-ordered FB-HIST-* candidate
    codes to evaluate.

    Explicit codes (if given) are used exactly as passed, validated as
    historical. Otherwise this reads every product_candidates row
    (id/code/created_at/batch_id only), filters to FB-HIST-* in Python
    (the same prefix check is_historical_candidate_code() callers use
    elsewhere -- not a wildcard query operator), sorts oldest-created-
    first (CLAUDE.md section 11: never an arbitrary "newest" selection),
    and caps at --limit.
    """
    if explicit_codes:
        for code in explicit_codes:
            if not is_historical_candidate_code(code):
                raise SelectorArgumentError(
                    f"{code!r} is not a historical (FB-HIST-*) candidate "
                    "code. This selector is scoped to historical "
                    "candidates only."
                )

        return explicit_codes

    expected_batch_id: str | None = None

    if batch_code:
        batch = get_batch_by_code(repository, batch_code)

        if batch is None:
            raise SelectorArgumentError(
                f"--batch-code {batch_code} does not match any batch."
            )

        expected_batch_id = batch.get("batch_id")

    rows = (
        repository.client
        .table("product_candidates")
        .select("candidate_id, candidate_code, created_at, batch_id")
        .order("created_at")
        .execute()
        .data
        or []
    )

    codes: list[str] = []

    for row in rows:
        code = row.get("candidate_code")

        if not code or not is_historical_candidate_code(code):
            continue

        if expected_batch_id is not None and row.get("batch_id") != expected_batch_id:
            continue

        codes.append(code)

    return codes[:limit]


def parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only: identify which FB-HIST-* historical candidates "
            "are safe to progress right now through the existing "
            "sanctioned run_batch.py dispatch, and which ones need "
            "individual attention (recovery, conflict, or other human "
            "review). Never invokes a writer stage and never calls "
            "run_batch.py itself."
        )
    )

    parser.add_argument(
        "--candidate-code",
        action="append",
        dest="candidate_code",
        help="Evaluate exactly one FB-HIST-* candidate code. Repeatable.",
    )

    parser.add_argument(
        "--candidate-codes",
        help="Comma-separated exact FB-HIST-* candidate codes.",
    )

    parser.add_argument(
        "--batch-code",
        help="Restrict the scan to one batch's historical candidates.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=(
            "Maximum number of historical candidates to scan when no "
            f"explicit codes are given (default {DEFAULT_LIMIT})."
        ),
    )

    return parser.parse_args(argv)


def print_report(codes: list[str], entries: list[SelectionEntry]) -> None:
    print(f"Candidates scanned: {len(codes)}")

    buckets: dict[str, list[SelectionEntry]] = {name: [] for name in BUCKET_ORDER}

    for entry in entries:
        buckets[entry.bucket].append(entry)

    for name in BUCKET_ORDER:
        members = buckets[name]

        if not members:
            continue

        print()
        print(f"{name}: {len(members)}")

        for entry in members:
            print(f"  {entry.candidate_code}  ({entry.derived_state})  {entry.reason}")

    automatable = buckets["AUTOMATABLE_NOW"]

    print()
    print("=" * 100)

    if automatable:
        codes_arg = ",".join(entry.candidate_code for entry in automatable)
        print("RECOMMENDED_NEXT_ACTION:")
        print(
            "  .venv/Scripts/python.exe scripts/run_batch.py "
            f"--candidate-codes {codes_arg} "
            f"--max-candidates {len(automatable)} --non-interactive"
        )
        print(
            "  (Explicit bounded allowlist per CLAUDE.md 19.1 -- this "
            "selector never invokes run_batch.py itself.)"
        )
    else:
        print(
            "RECOMMENDED_NEXT_ACTION: none -- no historical candidate in "
            "this scan is currently automatable."
        )


def main(
    argv: list[str] | None = None,
    *,
    repository: SupabaseRepository | None = None,
) -> int:
    args = parse_arguments(argv)

    if args.limit < 1:
        print("Error: --limit must be at least 1.", file=sys.stderr)
        return 2

    explicit_codes: list[str] = []

    if args.candidate_code:
        explicit_codes.extend(args.candidate_code)

    if args.candidate_codes:
        explicit_codes.extend(
            part.strip()
            for part in args.candidate_codes.split(",")
            if part.strip()
        )

    ordered_explicit: list[str] = []
    seen: set[str] = set()

    for code in explicit_codes:
        if code not in seen:
            seen.add(code)
            ordered_explicit.append(code)

    print("=" * 100)
    print(f"TSYC HISTORICAL BATCH SELECTOR (v{SCRIPT_VERSION}) -- read-only")
    print("=" * 100)

    if repository is None:
        repository = SupabaseRepository()

    try:
        codes = resolve_candidate_codes(
            repository,
            explicit_codes=ordered_explicit or None,
            batch_code=args.batch_code,
            limit=args.limit,
        )
    except SelectorArgumentError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2

    if not codes:
        print("No FB-HIST-* candidates matched the given scope.")
        return 0

    entries = [
        classify_bundle(code, load_candidate_bundle(repository, code))
        for code in codes
    ]

    print_report(codes, entries)

    return 0


if __name__ == "__main__":
    sys.exit(main())
