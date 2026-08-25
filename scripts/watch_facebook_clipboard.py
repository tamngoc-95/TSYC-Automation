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
collector and cleaner for that one URL -- never creates a candidate,
since create_candidates_from_cleaned_posts.py requires a human-supplied
--candidate-title that cannot be safely automated):
    .venv/Scripts/python.exe scripts/watch_facebook_clipboard.py \\
        --once --batch-code FB-2026-001 --process

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


SCRIPT_VERSION = "1.0.0"

MIN_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_POLL_INTERVAL_SECONDS = 2.0

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


def run_process_chain(
    source_url_id: str,
    subprocess_runner: Callable[[list[str]], subprocess.CompletedProcess],
) -> bool:
    """Optional --process auto-continue: collect, then clean, this one
    exact source. Never creates a candidate -- create_candidates_from_
    cleaned_posts.py requires a human-supplied --candidate-title that
    cannot be safely automated (see docs/TSYC_SOURCE_INGESTION.md).
    Returns True if both stages succeeded.
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

    print(
        "  Collected and cleaned. Candidate extraction still requires a "
        "human-supplied title: run create_candidates_from_cleaned_posts.py "
        f"--source-url-id {source_url_id} --candidate-title \"...\" "
        "--confirm-create --non-interactive"
    )
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
            "raw_pages.py for that exact source. Never creates a "
            "candidate and never creates a WooCommerce draft."
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
                run_process_chain(outcome.source_url_id, subprocess_runner)

    if args.once:
        handle_one_reading()
        return 0

    # --watch
    try:
        while ingested_count < args.max_sources:
            handle_one_reading()
            sleep(args.interval)

    except KeyboardInterrupt:
        print()
        print("Clipboard watcher stopped by user.")
        return 0

    print()
    print(f"Reached --max-sources ({args.max_sources}). Stopping.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
