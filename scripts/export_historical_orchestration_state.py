#!/usr/bin/env python3
"""
Read-only state exporter for TSYC automation orchestration.

Queries current candidate state, classifies every candidate into exactly
one CLAUDE_AUTOMATION.md section 6 lane (src.domain.rules.lane_rules),
and writes the section 20 orchestration-state snapshot to
data/processed/orchestration/historical_state.json.

Does NOT mutate any business data: every read goes through
scripts/pipeline_state.py's bulk bundle loader (SELECT-only) plus the
existing read-only scripts/audit_pipeline_state.py and
scripts/preflight_pipeline.py. This script creates no candidates, writes
no content/image/identity/Woo state, sets no price, and never calls any
WooCommerce or WordPress endpoint.

Usage:
  .venv/Scripts/python.exe scripts/export_historical_orchestration_state.py --status
  .venv/Scripts/python.exe scripts/export_historical_orchestration_state.py --json
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
PYTHON_EXE = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

from dotenv import load_dotenv  # noqa: E402

from pipeline_state import (  # noqa: E402
    derive_candidate_state,
    load_all_candidate_bundles,
)
from preflight_pipeline import run_preflight  # noqa: E402
from run_batch import parse_audit_output  # noqa: E402
from src.domain.rules import lane_rules  # noqa: E402
from src.repositories.supabase_repository import SupabaseRepository  # noqa: E402

STATE_FILE = PROJECT_ROOT / "data" / "processed" / "orchestration" / "historical_state.json"

_LANE_TO_SCHEMA_FIELD = {
    lane_rules.READY_FOR_DRAFT: "ready_for_draft",
    lane_rules.MULTILINGUAL_CONTENT: "multilingual_content",
    lane_rules.FAST_TRACK: "fast_track",
    lane_rules.ENRICHMENT_NEEDED: "enrichment_needed",
    lane_rules.HUMAN_REVIEW: "human_review",
    lane_rules.RECOVERY_REVIEW: "recovery_review",
    lane_rules.CONFLICT: "conflict",
    lane_rules.TERMINAL: "terminal",
}


def _git_commit() -> str | None:
    """Best-effort current commit hash. Read-only; never fails the export."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout.strip() or None
    except Exception:
        return None


def _run_audit() -> dict[str, Any]:
    """Invoke scripts/audit_pipeline_state.py exactly as run_batch.py and
    preflight_pipeline.py already do, and parse its result with
    parse_audit_output() -- the same authoritative parser those two
    scripts use, so this exporter never carries a second copy of the
    audit-output format."""
    try:
        completed = subprocess.run(
            [str(PYTHON_EXE), str(SCRIPTS_DIR / "audit_pipeline_state.py")],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as error:
        return {
            "status": "FAIL",
            "errors": None,
            "warnings": None,
            "detail": f"Could not run audit_pipeline_state.py: {type(error).__name__}: {error}",
        }

    error_count, warning_codes = parse_audit_output(completed.stdout)

    if error_count:
        status = "FAIL"
    elif warning_codes:
        status = "PASS_WITH_WARNINGS"
    else:
        status = "PASS"

    return {
        "status": status,
        "errors": error_count,
        "warnings": len(warning_codes),
    }


def _run_preflight() -> dict[str, Any]:
    """In-process call into preflight_pipeline.run_preflight() -- the same
    function scripts/run_batch.py already calls before any production
    write. Read-only."""
    try:
        result = run_preflight()
    except Exception as error:
        return {"status": f"PREFLIGHT_ERROR: {type(error).__name__}: {error}"}

    return {"status": "READY_FOR_BATCH" if result.ready else "NOT_READY_FOR_BATCH"}


def _classify_candidates(
    repository: SupabaseRepository,
) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Bulk-load every candidate bundle and classify each into exactly one
    lane. Returns (lane_counts keyed by schema field name, candidate_codes
    keyed by schema field name) -- the latter kept small (codes only) so
    the caller can attach a bounded example list without re-deriving
    anything."""
    bundles = load_all_candidate_bundles(repository)

    lane_counts: dict[str, int] = {field: 0 for field in _LANE_TO_SCHEMA_FIELD.values()}
    lane_codes: dict[str, list[str]] = {field: [] for field in _LANE_TO_SCHEMA_FIELD.values()}

    for candidate_code, bundle in bundles.items():
        state = derive_candidate_state(bundle)
        multilingual_ready = lane_rules.has_multilingual_content(bundle["contents"])
        lane = lane_rules.classify_lane(state, multilingual_ready=multilingual_ready)
        field = _LANE_TO_SCHEMA_FIELD[lane]
        lane_counts[field] += 1
        lane_codes[field].append(candidate_code)

    return lane_counts, lane_codes


def _historical_recovery_backlog(repository: SupabaseRepository) -> int:
    """Count raw Facebook source posts that have not yet been turned into
    any product_candidates row at all -- a fact about raw_pages, not
    about one candidate's derived state (CLAUDE_AUTOMATION.md section 6,
    LANE I). Two bulk reads, no per-row round trips."""
    raw_pages = (
        repository.client.table("raw_pages")
        .select("raw_page_id, page_type")
        .execute()
        .data
        or []
    )
    candidates = (
        repository.client.table("product_candidates")
        .select("raw_page_id")
        .execute()
        .data
        or []
    )

    linked_raw_page_ids = {
        str(candidate["raw_page_id"])
        for candidate in candidates
        if candidate.get("raw_page_id")
    }

    return sum(
        1
        for raw_page in raw_pages
        if raw_page.get("page_type") == "FACEBOOK_POST"
        and str(raw_page.get("raw_page_id")) not in linked_raw_page_ids
    )


def _next_recommended_action(
    *,
    audit: dict[str, Any],
    preflight: dict[str, Any],
    lane_counts: dict[str, int],
    historical_recovery_backlog: int,
) -> str:
    """CLAUDE_AUTOMATION.md section 16 (global stop conditions) and
    section 23 (automatic next-action rule), in that order: a global stop
    condition always outranks the lane-priority decision tree."""
    if audit.get("status") == "FAIL":
        return "RESOLVE_AUDIT_ERROR"

    if preflight.get("status") == "NOT_READY_FOR_BATCH":
        return "RESOLVE_PREFLIGHT_BLOCKER"

    if lane_counts["ready_for_draft"] > 0:
        return "PRIORITIZE_WOO_DRAFT_AUTHORIZATION"

    if lane_counts["multilingual_content"] > 0:
        return "PRIORITIZE_MULTILINGUAL_COMPLETION"

    if lane_counts["fast_track"] > 0:
        return "RUN_NEXT_FAST_TRACK_BATCH"

    if lane_counts["enrichment_needed"] > 0:
        return "RUN_DETERMINISTIC_ENRICHMENT"

    if lane_counts["human_review"] > 0:
        return "PREPARE_HUMAN_REVIEW_QUEUE"

    if historical_recovery_backlog > 0:
        return "RESUME_HISTORICAL_RECOVERY"

    return "NO_ACTION_NEEDED"


def export_state(output_format: str = "status") -> dict[str, Any]:
    """
    Export current orchestration state.

    Args:
        output_format: 'status' for a human summary, 'json' for raw JSON.

    Returns:
        dict with the state snapshot (CLAUDE_AUTOMATION.md section 20
        contract).
    """
    load_dotenv(PROJECT_ROOT / ".env")

    repository = SupabaseRepository()

    lane_counts, lane_codes = _classify_candidates(repository)
    historical_recovery_backlog = _historical_recovery_backlog(repository)
    audit = _run_audit()
    preflight = _run_preflight()

    state = {
        "schema_version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "repository_commit": _git_commit(),
        "ready_for_draft": lane_counts["ready_for_draft"],
        "multilingual_content": lane_counts["multilingual_content"],
        "fast_track": lane_counts["fast_track"],
        "enrichment_needed": lane_counts["enrichment_needed"],
        "human_review": lane_counts["human_review"],
        "recovery_review": lane_counts["recovery_review"],
        "conflict": lane_counts["conflict"],
        "terminal": lane_counts["terminal"],
        "historical_recovery_backlog": historical_recovery_backlog,
        "latest_execution": {
            "execution_id": None,
            "result": None,
            "candidate_codes": [],
            "attempted": 0,
            "progressed": 0,
            "unchanged": 0,
            "human_gated": 0,
            "failed": 0,
        },
        "audit": {
            "status": audit.get("status"),
            "errors": audit.get("errors"),
            "warnings": audit.get("warnings"),
        },
        "preflight": {
            "status": preflight.get("status"),
        },
        "safety": {
            "price_writes": 0,
            "publish_actions": 0,
            "outside_allowlist_touched": 0,
            "non_historical_touched": 0,
        },
        "next_recommended_action": _next_recommended_action(
            audit=audit,
            preflight=preflight,
            lane_counts=lane_counts,
            historical_recovery_backlog=historical_recovery_backlog,
        ),
        # Not part of the section 20 contract; kept as a bounded debugging
        # aid (candidate codes per lane, not persisted-as-authoritative
        # state) so a human/Cowork reading the snapshot does not have to
        # re-run a query just to see who is in each lane.
        "_lane_candidate_codes": lane_codes,
    }

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, ensure_ascii=False)

    if output_format == "json":
        print(json.dumps(state, indent=2, ensure_ascii=False))
    else:  # status
        print(f"State Export: {state['updated_at']}")
        print(f"  Ready for draft:      {state['ready_for_draft']}")
        print(f"  Multilingual content: {state['multilingual_content']}")
        print(f"  Fast track:           {state['fast_track']}")
        print(f"  Enrichment needed:    {state['enrichment_needed']}")
        print(f"  Human review:         {state['human_review']}")
        print(f"  Recovery review:      {state['recovery_review']}")
        print(f"  Conflict:             {state['conflict']}")
        print(f"  Terminal:             {state['terminal']}")
        print(f"  Historical backlog:   {state['historical_recovery_backlog']}")
        print(f"  Audit:                {state['audit']['status']}")
        print(f"  Preflight:            {state['preflight']['status']}")
        print(f"  Next action:          {state['next_recommended_action']}")

    return state


if __name__ == "__main__":
    format_arg = sys.argv[1] if len(sys.argv) > 1 else "--status"
    output_format = "json" if format_arg == "--json" else "status"
    export_state(output_format)
