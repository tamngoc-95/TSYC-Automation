"""
TSYC Facebook permalink clipboard-ingestion helper.

The operator's only action is clicking "Copy link" on a post in the
authorized TSYC Facebook Group. This script reads that clipboard text
locally (Windows clipboard API via ctypes -- no new dependency, no
browser, no cookies/auth state/passwords ever touched) and, only when it
is already a valid, exact TSYC group permalink, hands it to the single
shared ingestion service:

    src.services.source_ingestion.ingest_facebook_post_url()

This script never browses, scrapes, or scrolls Facebook, and never
discovers posts on its own -- it only reacts to a URL the operator
already copied. All validation/normalization/dedupe/registration logic
lives in source_ingestion.py; this script does not reimplement any of
it.

Usage (read the clipboard once):
    .venv/Scripts/python.exe scripts/watch_facebook_clipboard.py \\
        --once --batch-code FB-2026-001

Usage (watch for clipboard changes until Ctrl+C):
    .venv/Scripts/python.exe scripts/watch_facebook_clipboard.py \\
        --watch --batch-code FB-2026-001 --max-sources 5

Optional auto-continue (ingest, then run the existing exact-source
collector and cleaner for that one URL, then deterministic automatic
candidate extraction, then the bounded orchestrator for exactly the
candidate codes just created -- requires --max-candidates; never passes
--allow-woo-draft, so this chain never crosses the WooCommerce
human-approval gate):
    .venv/Scripts/python.exe scripts/watch_facebook_clipboard.py \\
        --once --batch-code FB-2026-001 --process --max-candidates 5

Privacy: raw clipboard content is never printed, logged, or persisted.
Only the normalized permalink -- and only after it has passed strict
validation -- ever appears in output or process_logs. An invalid or
unrelated clipboard value is reported with a generic, content-free
message.
"""

from __future__ import annotations

import argparse
import ctypes
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.repositories.supabase_repository import SupabaseRepository  # noqa: E402
from src.services.source_ingestion import (  # noqa: E402
    IngestOutcome,
    ingest_facebook_post_url,
)

# Reuse the orchestrator's own PYTHON_EXE/subprocess conventions (repo
# venv only, UTF-8-safe capture) instead of reimplementing them here.
from run_batch import PYTHON_EXE, default_subprocess_runner  # noqa: E402

configure_utf8_console()


SCRIPT_VERSION = "1.1.0"

MIN_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_POLL_INTERVAL_SECONDS = 2.0

# Exit code returned when ingestion itself succeeded but a requested
# --process auto-continue chain failed for at least one source. Distinct
# from 2 (an argument/preflight error, before any I/O) -- this means real
# work was attempted and a downstream stage did not complete cleanly, so
# the OS-level exit code must not read as full success.
PROCESS_CHAIN_FAILURE_EXIT_CODE = 1

# Windows clipboard format for plain Unicode text (CF_UNICODETEXT).
_CF_UNICODETEXT = 13


class WatcherArgumentError(RuntimeError):
    """Raised for CLI/bounding violations that must fail before any I/O."""


def read_clipboard_text() -> str | None:
    """Read plain-text clipboard content via the Windows clipboard API.

    Returns None if the clipboard holds no plain text (e.g. an image or
    file selection) or cannot be opened right now (another process is
    briefly holding it). Never reads browser cookies, auth state,
    passwords, or any other browser/file data -- only the OS clipboard's
    own plain-text slot.
    """
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    if not user32.OpenClipboard(None):
        return None

    try:
        handle = user32.GetClipboardData(_CF_UNICODETEXT)

        if not handle:
            return None

        locked = kernel32.GlobalLock(handle)

        if not locked:
            return None

        try:
            text = ctypes.wstring_at(locked)
        finally:
            kernel32.GlobalUnlock(handle)

        return text

    finally:
        user32.CloseClipboard()


def process_clipboard_value(
    text: str | None,
    *,
    repository: Any,
    batch_id: str,
    last_seen: str | None,
) -> tuple[str | None, IngestOutcome | None]:
    """Decide whether one clipboard reading should be ingested.

    Pure decision core, independent of the real clipboard/polling loop,
    so it is directly unit-testable. Returns (new_last_seen, outcome).
    outcome is None when there is nothing new to report (empty
    clipboard, or unchanged since the last observed value -- required
    duplicate-clipboard protection: the same still-copied link is never
    re-ingested on every poll tick).
    """
    if not text:
        return last_seen, None

    if text == last_seen:
        return last_seen, None

    outcome = ingest_facebook_post_url(repository, text, batch_id)

    return text, outcome


def print_outcome(outcome: IngestOutcome) -> None:
    """Report one outcome without ever echoing raw clipboard content.

    Only a URL that has already passed strict validation (and is
    therefore the known-shape canonical TSYC permalink, not arbitrary
    clipboard text) is ever printed -- for SOURCE_INVALID, nothing about
    the actual clipboard content is printed or logged, per the privacy
    requirement above.
    """
    if outcome.status == "REGISTERED":
        print(f"REGISTERED (PENDING): {outcome.canonical_url} -> {outcome.source_url_id}")
    elif outcome.status == "ALREADY_KNOWN":
        print(f"ALREADY_KNOWN: {outcome.canonical_url} -> {outcome.source_url_id}")
    else:
        print("Clipboard content ignored (not a supported TSYC permalink).")


_CANDIDATE_CODES_PREFIX = "CANDIDATE_CODES:"


def parse_candidate_codes(stdout: str) -> list[str]:
    """Parse create_candidates_from_cleaned_posts.py's stable
    "CANDIDATE_CODES: ..." output line -- never human prose.

    Deliberately neutral name/label (not "created"): the codes on this
    line are exactly the candidate codes now available to continue
    processing downstream, whether they were freshly CREATED this run
    or already EXISTING from a prior idempotent-repeat run (see that
    script's create_candidates_from_page() / format_candidate_codes_
    line()) -- this caller treats both cases identically, so the label
    must not claim "created" for codes that were not. Keep this in sync
    with that script's own print of the same prefix."""
    for line in (stdout or "").splitlines():
        if line.startswith(_CANDIDATE_CODES_PREFIX):
            raw = line[len(_CANDIDATE_CODES_PREFIX):].strip()
            return [code.strip() for code in raw.split(",") if code.strip()]

    return []


def _print_tail(completed: subprocess.CompletedProcess, *, max_lines: int = 15) -> None:
    """Print the last few lines of a subprocess's stdout -- used only for
    the exception-visibility cases below (extraction failed, extraction
    produced no candidates, or run_batch.py exited unexpectedly), not for
    every routine successful stage."""
    lines = (completed.stdout or "").strip().splitlines()

    for line in lines[-max_lines:]:
        print(f"    {line}")


def run_process_chain(
    source_url_id: str,
    subprocess_runner: Callable[[list[str]], subprocess.CompletedProcess],
    max_candidates: int,
) -> bool:
    """Optional --process auto-continue: collect this one exact source,
    clean it, run deterministic automatic candidate extraction, then run
    the bounded orchestrator for exactly the candidate codes just
    created. Never passes --allow-woo-draft to run_batch.py -- this
    chain never crosses the Woo human-approval gate.

    Returns True once the chain has run to a normal conclusion (which
    includes "extraction found no candidates" and "run_batch.py reported
    a candidate exception" -- both are expected reporting outcomes, not
    crashes). Returns False only for an unexpected stage failure.
    """
    collect_argv = [
        str(PYTHON_EXE),
        str(SCRIPTS_DIR / "collect_one_facebook_post.py"),
        "--source-url-id", source_url_id,
        "--non-interactive",
        "--confirm-save",
    ]

    print(f"  Invoking: {' '.join(collect_argv)}")
    collected = subprocess_runner(collect_argv)

    if collected.returncode != 0:
        print(f"  Collection failed (exit {collected.returncode}); not cleaning.")
        return False

    clean_argv = [
        str(PYTHON_EXE),
        str(SCRIPTS_DIR / "clean_facebook_raw_pages.py"),
        "--source-url-id", source_url_id,
        "--action", "SAVE",
        "--non-interactive",
    ]

    print(f"  Invoking: {' '.join(clean_argv)}")
    cleaned = subprocess_runner(clean_argv)

    if cleaned.returncode != 0:
        print(f"  Cleaning failed (exit {cleaned.returncode}).")
        return False

    extract_argv = [
        str(PYTHON_EXE),
        str(SCRIPTS_DIR / "create_candidates_from_cleaned_posts.py"),
        "--source-url-id", source_url_id,
        "--max-candidates", str(max_candidates),
        "--non-interactive",
        "--confirm-create",
    ]

    print(f"  Invoking: {' '.join(extract_argv)}")
    extracted = subprocess_runner(extract_argv)

    if extracted.returncode != 0:
        print(
            "  Candidate extraction failed "
            f"(exit {extracted.returncode}) -- this may mean the raw "
            "page still needs manual cleaning review "
            "(clean_facebook_raw_pages.py --action SAVE/REVIEW)."
        )
        _print_tail(extracted)
        return False

    candidate_codes = parse_candidate_codes(extracted.stdout)

    if not candidate_codes:
        print(
            "  No candidates were created automatically for this post -- "
            "see the classification result below. Fallback: create_"
            "candidates_from_cleaned_posts.py --source-url-id "
            f"{source_url_id} --candidate-title \"...\" --confirm-create "
            "--non-interactive."
        )
        _print_tail(extracted)
        return True

    print(f"  Candidates created: {', '.join(candidate_codes)}")

    run_batch_argv = [str(PYTHON_EXE), str(SCRIPTS_DIR / "run_batch.py")]

    for code in candidate_codes:
        run_batch_argv += ["--candidate-code", code]

    run_batch_argv += [
        "--max-candidates", str(len(candidate_codes)),
        "--non-interactive",
    ]

    print(f"  Invoking: {' '.join(run_batch_argv)}")
    batch_result = subprocess_runner(run_batch_argv)
    _print_tail(batch_result)

    # run_batch.py's own exit code 1 ("hard_failure") means at least one
    # of these candidates ended in BLOCKED/STAGE_FAILED/... -- that is
    # already reported by its own grouped summary above and is expected
    # exception reporting, not a process-chain crash. Only an
    # unrecognized exit code (e.g. 2: an argument/preflight error) is
    # treated as an unexpected failure here.
    if batch_result.returncode not in (0, 1):
        print(f"  run_batch.py exited unexpectedly (code {batch_result.returncode}).")
        return False

    return True


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Ingest a Facebook permalink from the Windows clipboard, "
            "using the shared source_ingestion service. Never browses, "
            "scrapes, or discovers Facebook content -- only reacts to a "
            "URL the operator already copied."
        )
    )

    mode_group = parser.add_mutually_exclusive_group(required=True)

    mode_group.add_argument(
        "--once",
        action="store_true",
        help="Read the clipboard exactly once and ingest if valid.",
    )

    mode_group.add_argument(
        "--watch",
        action="store_true",
        help=(
            "Poll the clipboard for changes until --max-sources new "
            "URLs have been ingested or the process is stopped "
            "(Ctrl+C)."
        ),
    )

    parser.add_argument(
        "--batch-code",
        required=True,
        help="Exact batch code these sources belong to (e.g. FB-2026-001).",
    )

    parser.add_argument(
        "--max-sources",
        type=int,
        help=(
            "Required with --watch: stop after this many URLs have been "
            "REGISTERED or found ALREADY_KNOWN. Bounds --watch so it "
            "never runs unattended indefinitely."
        ),
    )

    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help=(
            f"Seconds between clipboard checks in --watch mode "
            f"(default {DEFAULT_POLL_INTERVAL_SECONDS}, minimum "
            f"{MIN_POLL_INTERVAL_SECONDS})."
        ),
    )

    parser.add_argument(
        "--process",
        action="store_true",
        help=(
            "After a new URL is REGISTERED, automatically run the "
            "existing collect_one_facebook_post.py and clean_facebook_"
            "raw_pages.py for that exact source, then deterministic "
            "automatic candidate extraction, then run_batch.py for "
            "exactly the candidate codes created. Requires --max-"
            "candidates. Never creates a WooCommerce draft (no "
            "--allow-woo-draft is ever passed)."
        ),
    )

    parser.add_argument(
        "--max-candidates",
        type=int,
        help=(
            "Required with --process: hard upper bound on candidates "
            "created (and then run through run_batch.py) per ingested "
            "post. Never unbounded."
        ),
    )

    return parser.parse_args(argv)


def validate_arguments(args: argparse.Namespace) -> None:
    """Bounding checks that must fail before any I/O."""
    if args.watch:
        if args.max_sources is None:
            raise WatcherArgumentError(
                "--watch requires --max-sources (no unbounded watching)."
            )

        if args.max_sources < 1:
            raise WatcherArgumentError("--max-sources must be at least 1.")

    if args.interval < MIN_POLL_INTERVAL_SECONDS:
        raise WatcherArgumentError(
            f"--interval must be at least {MIN_POLL_INTERVAL_SECONDS} "
            "seconds (do not poll more aggressively than necessary)."
        )

    if args.process:
        if args.max_candidates is None:
            raise WatcherArgumentError(
                "--process requires --max-candidates (no unbounded "
                "candidate creation)."
            )

        if args.max_candidates < 1:
            raise WatcherArgumentError("--max-candidates must be at least 1.")


def main(
    argv: list[str] | None = None,
    *,
    repository: SupabaseRepository | None = None,
    clipboard_reader: Callable[[], str | None] | None = None,
    subprocess_runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    """Run the clipboard watcher. Returns a process exit code."""
    args = parse_arguments(argv)

    try:
        validate_arguments(args)
    except WatcherArgumentError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2

    if repository is None:
        repository = SupabaseRepository()

    if clipboard_reader is None:
        clipboard_reader = read_clipboard_text

    if subprocess_runner is None:
        subprocess_runner = default_subprocess_runner

    if sleep is None:
        sleep = time.sleep

    batch = repository.get_batch_by_code(args.batch_code)

    if batch is None:
        print(f"Error: --batch-code {args.batch_code} does not match any batch.", file=sys.stderr)
        return 2

    batch_id = batch["batch_id"]

    print("=" * 78)
    print(f"TSYC FACEBOOK CLIPBOARD WATCHER (v{SCRIPT_VERSION})")
    print("=" * 78)
    print(f"Batch code: {args.batch_code}")
    print(f"Mode: {'watch' if args.watch else 'once'}")

    if args.watch:
        print(f"Max sources this run: {args.max_sources}")
        print(f"Poll interval: {args.interval}s")

    print(f"Auto-continue (--process): {args.process}")
    print()

    last_seen: str | None = None
    ingested_count = 0
    # Every source_url_id whose --process chain returned False this run.
    # ingested_count (above) counts sources the clipboard watcher itself
    # successfully recognized/registered -- it must never be read as a
    # claim that every one of those sources' downstream --process chain
    # also completed successfully, so a chain failure is tracked here
    # instead of folded into that counter.
    process_chain_failures: list[str] = []

    def handle_one_reading() -> None:
        nonlocal last_seen, ingested_count

        text = clipboard_reader()
        new_last_seen, outcome = process_clipboard_value(
            text,
            repository=repository,
            batch_id=batch_id,
            last_seen=last_seen,
        )
        last_seen = new_last_seen

        if outcome is None:
            return

        print_outcome(outcome)

        if outcome.status in ("REGISTERED", "ALREADY_KNOWN"):
            ingested_count += 1

            if args.process and outcome.status == "REGISTERED":
                # run_process_chain() itself never raises for an
                # ordinary stage failure (a non-zero subprocess exit is
                # reported and returned as False) -- exception isolation
                # for one source's processing is unchanged by this
                # failure-tracking; only its already-boolean result is
                # now recorded instead of discarded.
                chain_succeeded = run_process_chain(
                    outcome.source_url_id, subprocess_runner, args.max_candidates
                )

                if not chain_succeeded:
                    process_chain_failures.append(outcome.source_url_id)
                    print(
                        "  WARNING: --process auto-continue chain failed "
                        f"for source_url_id {outcome.source_url_id} -- see "
                        "the stage output above for the exact failing step."
                    )

    def report_process_chain_failures() -> int:
        """Print a clear summary of any --process chain failures and
        return the exit code they imply. Called from every normal exit
        path below (--once, --watch reaching --max-sources, --watch
        stopped by Ctrl+C) so a failed processing attempt can never be
        reported as OS-level success."""
        if not process_chain_failures:
            return 0

        print()
        print(
            f"--process auto-continue failed for "
            f"{len(process_chain_failures)} source(s): "
            + ", ".join(process_chain_failures)
        )
        return PROCESS_CHAIN_FAILURE_EXIT_CODE

    if args.once:
        handle_one_reading()
        return report_process_chain_failures()

    # --watch: each clipboard reading/source is handled independently
    # (handle_one_reading() catches nothing itself -- an unexpected
    # exception still propagates and stops the run, unchanged by this
    # fix); a --process chain failure for one source is recorded and the
    # loop continues to the next independent source, since run_process_
    # chain() already isolates that failure to a boolean result rather
    # than raising.
    try:
        while ingested_count < args.max_sources:
            handle_one_reading()
            sleep(args.interval)

    except KeyboardInterrupt:
        print()
        print("Clipboard watcher stopped by user.")
        return report_process_chain_failures()

    print()
    print(f"Reached --max-sources ({args.max_sources}). Stopping.")
    return report_process_chain_failures()


if __name__ == "__main__":
    sys.exit(main())
