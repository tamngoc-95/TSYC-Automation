"""
Bounded Facebook permalink source inbox (Mode B / Mode C ingestion adapter).

This is the fallback/manual adapter, not the business logic itself -- it
only parses CLI/file input into a list of candidate URL strings and hands
every one of them to the single shared ingestion service,
src.services.source_ingestion.ingest_facebook_post_urls(). All
validation, normalization, dedupe, and idempotent registration logic
lives there; this script never reimplements it.

This script never opens a browser, never crawls or scrolls a Facebook
group feed, and never discovers URLs on its own -- every URL it processes
must already be supplied explicitly, either with --url (repeatable) or
one per line in the file passed to --input. Direct single-URL use
(Mode C) and bounded multi-URL batch use (Mode B) are the exact same code
path here; only how many --url flags/file lines you pass differs.

Usage (single URL):
    .venv/Scripts/python.exe scripts/ingest_source_urls.py \\
        --url https://www.facebook.com/groups/2415122391976246/permalink/123456789/ \\
        --batch-code FB-2026-001 \\
        --max-sources 5 \\
        --non-interactive --confirm-register

Usage (bounded batch inbox):
    .venv/Scripts/python.exe scripts/ingest_source_urls.py \\
        --input new_posts.txt \\
        --batch-code FB-2026-001 \\
        --max-sources 5 \\
        --non-interactive --confirm-register

--input file format: one URL per line; blank lines and lines starting
with "#" are ignored. No implicit "process every pending source" mode
exists -- the exact URL list (from --url and/or --input) is always
resolved and bounded by --max-sources before anything is read or written.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.repositories.supabase_repository import SupabaseRepository  # noqa: E402
from src.services.source_ingestion import (  # noqa: E402
    IngestOutcome,
    ingest_facebook_post_urls,
)

configure_utf8_console()


SCRIPT_VERSION = "1.0.0"


class IngestionArgumentError(RuntimeError):
    """Raised for CLI/bounding violations that must fail before any I/O."""


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Validate and register a bounded list of exact, already-known "
            "Facebook group post permalink URLs as PENDING source_urls "
            "rows. Never browses, crawls, or discovers URLs -- every URL "
            "must already be supplied explicitly."
        )
    )

    parser.add_argument(
        "--url",
        action="append",
        dest="urls",
        default=[],
        help="One exact Facebook group post permalink URL. Repeatable.",
    )

    parser.add_argument(
        "--input",
        help=(
            "Path to a text file with one exact permalink URL per line "
            "(blank lines and lines starting with # are ignored)."
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
        required=True,
        help="Hard upper bound on the number of URLs processed in this run.",
    )

    parser.add_argument(
        "--confirm-register",
        action="store_true",
        help="Confirm registration without prompting.",
    )

    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Disable input prompts. Requires --confirm-register.",
    )

    return parser.parse_args(argv)


def read_urls_from_file(file_path: str) -> list[str]:
    """Read one URL per non-blank, non-comment line from a text file."""
    path = Path(file_path)

    if not path.exists():
        raise IngestionArgumentError(f"--input file not found: {file_path}")

    urls: list[str] = []

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#"):
            continue

        urls.append(line)

    return urls


def resolve_url_list(args: argparse.Namespace) -> list[str]:
    """Resolve the exact bounded URL list, or fail before any I/O.

    No implicit "all pending" / "every known post" mode exists -- only
    the explicit URLs supplied via --url and/or --input.
    """
    urls: list[str] = list(args.urls)

    if args.input:
        urls.extend(read_urls_from_file(args.input))

    ordered: list[str] = []
    seen: set[str] = set()

    for url in urls:
        if url not in seen:
            seen.add(url)
            ordered.append(url)

    if not ordered:
        raise IngestionArgumentError(
            "No URLs supplied. Pass --url (repeatable) and/or --input. "
            "Implicit 'all pending' processing is not supported."
        )

    if args.max_sources < 1:
        raise IngestionArgumentError("--max-sources must be at least 1.")

    if len(ordered) > args.max_sources:
        raise IngestionArgumentError(
            f"URL count ({len(ordered)}) exceeds --max-sources "
            f"({args.max_sources}). Failing before any read or write."
        )

    return ordered


def print_report(outcomes: list[IngestOutcome]) -> None:
    """Print the structured REGISTERED / ALREADY_KNOWN / SOURCE_INVALID report."""
    groups: dict[str, list[IngestOutcome]] = {
        "REGISTERED": [],
        "ALREADY_KNOWN": [],
        "SOURCE_INVALID": [],
    }

    for outcome in outcomes:
        groups[outcome.status].append(outcome)

    print()

    for status in ("REGISTERED", "ALREADY_KNOWN", "SOURCE_INVALID"):
        members = groups[status]
        print(f"{status}: {len(members)}")

        for index, outcome in enumerate(members, start=1):
            if status == "SOURCE_INVALID":
                print(f"{index}. {outcome.input_url} -- {outcome.reason}")
            else:
                print(f"{index}. {outcome.canonical_url} -> {outcome.source_url_id}")

        print()


def main(
    argv: list[str] | None = None,
    *,
    repository: SupabaseRepository | None = None,
) -> int:
    """Ingest a bounded list of exact Facebook permalinks. Returns exit code."""
    args = parse_arguments(argv)

    try:
        url_list = resolve_url_list(args)
    except IngestionArgumentError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2

    if args.non_interactive and not args.confirm_register:
        print(
            "Error: --non-interactive requires --confirm-register.",
            file=sys.stderr,
        )
        return 2

    if not args.non_interactive:
        answer = input(
            f"Register up to {len(url_list)} URL(s) for batch "
            f"{args.batch_code}? [y/N]: "
        ).strip().lower()

        if answer not in ("y", "yes"):
            print("Cancelled. No URLs were registered.")
            return 0

    print("=" * 78)
    print(f"TSYC SOURCE INBOX (v{SCRIPT_VERSION})")
    print("=" * 78)
    print(f"Batch code: {args.batch_code}")
    print(f"URLs supplied: {len(url_list)}")
    print(f"Max sources: {args.max_sources}")

    if repository is None:
        repository = SupabaseRepository()

    batch = repository.get_batch_by_code(args.batch_code)

    if batch is None:
        print(f"Error: --batch-code {args.batch_code} does not match any batch.", file=sys.stderr)
        return 2

    batch_id = batch["batch_id"]

    outcomes = ingest_facebook_post_urls(
        repository,
        url_list,
        batch_id,
        max_sources=args.max_sources,
    )

    print_report(outcomes)

    registered = sum(1 for o in outcomes if o.status == "REGISTERED")
    already_known = sum(1 for o in outcomes if o.status == "ALREADY_KNOWN")
    invalid = sum(1 for o in outcomes if o.status == "SOURCE_INVALID")

    # Every URL failing validation is not a structural/argument error --
    # it is reported per-URL above and the run still completes -- but it
    # should not be logged identically to a normal successful batch, or
    # an operator/orchestrator skimming process_logs could miss that
    # nothing was actually registered.
    all_invalid = invalid > 0 and registered == 0 and already_known == 0

    repository.write_process_log(
        batch_id=batch_id,
        process_name="INGEST_SOURCE_URLS",
        process_step="BATCH_INGEST",
        log_level="WARNING" if all_invalid else "INFO",
        status="ALL_INVALID" if all_invalid else "SUCCESS",
        message=(
            f"Processed {len(outcomes)} supplied URL(s): "
            f"{registered} registered, {already_known} already known, "
            f"{invalid} invalid."
        ),
        error_details={
            "registered": registered,
            "already_known": already_known,
            "invalid": invalid,
        },
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
