"""
Historical Facebook image ingestion -- chains extraction and upload.

Thin, non-duplicating orchestration over the two already-sanctioned
historical image scripts:
    scripts/extract_historical_facebook_images.py  (local cache only,
        no Supabase writes)
    scripts/upload_facebook_images_to_supabase.py   (the only script
        that inserts product_images rows)

Why this script exists: scripts/run_batch.py's orchestrator dispatches
exactly one script per derived pipeline state, then re-derives state and
checks whether it actually changed. extract_historical_facebook_images.py
alone never changes any Supabase-visible state (it only writes local
files), so it cannot be a dispatch target by itself -- the orchestrator
would see no state change and stop that candidate as STALLED before the
image was ever uploaded. This script performs no business-rule decisions
and no direct Supabase writes of its own; it only runs the two existing
scripts in sequence, exactly as an operator would run them by hand, so a
single dispatch advances a historical candidate's image-ingest stage in
one step (pipeline_state.py's IMAGE_INGEST_PENDING_HISTORICAL state).

Safety: identical to running the two scripts manually. Ownership
ambiguity (a shared multi-candidate Facebook post) and Facebook-export-
archive availability are both re-checked by
extract_historical_facebook_images.py itself; this script does not
duplicate or weaken either check.

Usage:
    .venv/Scripts/python.exe scripts/ingest_historical_images.py \\
        --candidate-code FB-HIST-2026-001-CAN-0001 \\
        --non-interactive --confirm-ingest
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.cli_bootstrap import configure_utf8_console  # noqa: E402

configure_utf8_console()


SCRIPT_VERSION = "1.0.0"
DEFAULT_BATCH_CODE = "FB-2026-001"

# The repository virtual environment interpreter. CLAUDE.md is explicit:
# never fall back to system Python when .venv exists.
PYTHON_EXE = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Chain extract_historical_facebook_images.py and "
            "upload_facebook_images_to_supabase.py for one historical "
            "candidate -- the single dispatch scripts/run_batch.py runs "
            "for the IMAGE_INGEST_PENDING_HISTORICAL state."
        )
    )
    parser.add_argument(
        "--candidate-code",
        required=True,
        help="Exact product candidate code.",
    )
    parser.add_argument(
        "--batch-code",
        default=DEFAULT_BATCH_CODE,
        help=(
            "Local cache batch subdirectory under data/raw/facebook-images/. "
            f"Default: {DEFAULT_BATCH_CODE} (passed identically to both "
            "stage scripts)."
        ),
    )
    parser.add_argument(
        "--confirm-ingest",
        action="store_true",
        help="Confirm extraction and upload without an interactive prompt.",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Disable input prompts. Requires --confirm-ingest.",
    )
    return parser.parse_args(argv)


def run_stage(
    argv: list[str],
    *,
    subprocess_runner=None,
) -> subprocess.CompletedProcess:
    """Run one stage script with the repository virtual env."""
    if subprocess_runner is not None:
        return subprocess_runner(argv)

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


def build_extract_argv(candidate_code: str, batch_code: str) -> list[str]:
    return [
        str(PYTHON_EXE),
        str(PROJECT_ROOT / "scripts" / "extract_historical_facebook_images.py"),
        "--candidate-code",
        candidate_code,
        "--batch-code",
        batch_code,
        "--non-interactive",
        "--confirm-extract",
    ]


def build_upload_argv(candidate_code: str, batch_code: str) -> list[str]:
    return [
        str(PYTHON_EXE),
        str(PROJECT_ROOT / "scripts" / "upload_facebook_images_to_supabase.py"),
        "--candidate-code",
        candidate_code,
        "--batch-code",
        batch_code,
        "--images",
        "ALL",
        "--non-interactive",
        "--confirm-upload",
    ]


def main(argv: list[str] | None = None, *, subprocess_runner=None) -> int:
    args = parse_arguments(argv)

    if args.non_interactive and not args.confirm_ingest:
        print(
            "Error: --non-interactive requires --confirm-ingest.",
            file=sys.stderr,
        )
        return 2

    if not args.confirm_ingest:
        print(
            "Error: --confirm-ingest is required. This script never "
            "prompts interactively itself -- run "
            "extract_historical_facebook_images.py and "
            "upload_facebook_images_to_supabase.py directly for manual, "
            "interactive review.",
            file=sys.stderr,
        )
        return 2

    print("=" * 78)
    print("TSYC HISTORICAL IMAGE INGESTION (extract + upload)")
    print("=" * 78)
    print(f"Version: {SCRIPT_VERSION}")
    print(f"Candidate code: {args.candidate_code}")

    extract_argv = build_extract_argv(args.candidate_code, args.batch_code)
    print()
    print(f"[1/2] {' '.join(extract_argv)}")
    extracted = run_stage(extract_argv, subprocess_runner=subprocess_runner)

    if extracted.stdout:
        print(extracted.stdout)
    if extracted.stderr:
        print(extracted.stderr, file=sys.stderr)

    if extracted.returncode != 0:
        print()
        print("Result: EXTRACTION_FAILED")
        return extracted.returncode

    upload_argv = build_upload_argv(args.candidate_code, args.batch_code)
    print()
    print(f"[2/2] {' '.join(upload_argv)}")
    uploaded = run_stage(upload_argv, subprocess_runner=subprocess_runner)

    if uploaded.stdout:
        print(uploaded.stdout)
    if uploaded.stderr:
        print(uploaded.stderr, file=sys.stderr)

    if uploaded.returncode != 0:
        print()
        print("Result: UPLOAD_FAILED")
        return uploaded.returncode

    print()
    print("Result: INGEST_COMPLETE")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        print("Historical image ingestion was cancelled.")
        sys.exit(130)
    except Exception as error:
        print()
        print("Historical image ingestion failed.")
        print(f"Error type: {type(error).__name__}")
        print(f"Error details: {error}")
        sys.exit(1)
