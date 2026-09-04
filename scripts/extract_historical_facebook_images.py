"""
Historical Facebook image extraction (TSYC pipeline stabilization Phase 3).

Reads one candidate's already-persisted `source_evidence.local_media_paths`
(written by import_historical_facebook_candidates*.py) and writes the
referenced still images -- plus a JSON sidecar carrying the same fields the
live Facebook collector already writes -- into the local cache directory
scripts/upload_facebook_images_to_supabase.py already scans
(data/raw/facebook-images/<batch-code>/<raw_page_id>/).

This script performs NO Supabase writes. It is the upstream half of the
generalized historical image path -- the existing production ingestion
script (upload_facebook_images_to_supabase.py) remains the only place that
inserts product_images rows; this script only prepares its local input in
the exact shape it already understands.

Safety:
    - Refuses to extract when the Facebook export archive (gitignored,
      data/raw/facebook-*.zip) is not present -- prints
      CAPABILITY_UNAVAILABLE and exits 0 without writing anything. This is
      an expected, reportable condition, not a script bug.
    - Refuses to extract when the candidate's source Facebook post also
      produced other candidates (a multi-book post) -- prints
      AMBIGUOUS_GROUP_IMAGE naming the sibling candidate codes and exits 0
      without writing anything. CLAUDE.md section 11: image ownership
      across a shared post must never be auto-resolved.
    - Extracts still images only (jpg/jpeg/png/gif/webp). Video/other
      media referenced by local_media_paths is reported as skipped, never
      extracted.
    - Idempotent: re-running with the same archive produces the same
      hash-derived local filenames, so a rerun overwrites identical bytes
      rather than accumulating duplicates.

Usage:
    .venv/Scripts/python.exe scripts/extract_historical_facebook_images.py \\
        --candidate-code FB-HIST-2026-001-CAN-0001 \\
        --non-interactive --confirm-extract
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.cli_bootstrap import configure_utf8_console
from src.domain.decisions import Outcome
from src.domain.rules import image_rules
from src.repositories.supabase_repository import SupabaseRepository
from src.services.historical_image_extraction import (
    check_capability,
    filter_image_paths,
    read_archive_image_bytes,
    write_local_image_cache,
)

configure_utf8_console()


SCRIPT_VERSION = "1.0.0"
DEFAULT_BATCH_CODE = "FB-2026-001"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract one historical Facebook candidate's local media into "
            "the local image cache upload_facebook_images_to_supabase.py "
            "already scans."
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
            f"Default: {DEFAULT_BATCH_CODE} (the same directory "
            "upload_facebook_images_to_supabase.py scans by default)."
        ),
    )
    parser.add_argument(
        "--confirm-extract",
        action="store_true",
        help="Confirm extraction without an interactive prompt.",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Disable input prompts. Requires --confirm-extract.",
    )
    return parser.parse_args()


def get_candidate(
    repository: SupabaseRepository,
    candidate_code: str,
) -> dict[str, Any]:
    rows = (
        repository.client
        .table("product_candidates")
        .select(
            "candidate_id, candidate_code, raw_page_id, source_url_id, "
            "source_evidence"
        )
        .eq("candidate_code", candidate_code)
        .limit(1)
        .execute()
        .data
        or []
    )

    if not rows:
        raise RuntimeError(f"No candidate matched candidate_code={candidate_code!r}.")

    return rows[0]


def get_sibling_candidate_codes(
    repository: SupabaseRepository,
    raw_page_id: str,
    candidate_id: str,
) -> list[str]:
    rows = (
        repository.client
        .table("product_candidates")
        .select("candidate_id, candidate_code")
        .eq("raw_page_id", raw_page_id)
        .execute()
        .data
        or []
    )

    return [
        row["candidate_code"]
        for row in rows
        if row.get("candidate_id") != candidate_id and row.get("candidate_code")
    ]


def main() -> int:
    load_dotenv()
    args = parse_arguments()

    if args.non_interactive and not args.confirm_extract:
        print(
            "Error: --non-interactive requires --confirm-extract.",
            file=sys.stderr,
        )
        return 2

    print("=" * 78)
    print("TSYC HISTORICAL FACEBOOK IMAGE EXTRACTION")
    print("=" * 78)
    print(f"Version: {SCRIPT_VERSION}")
    print(f"Candidate code: {args.candidate_code}")

    repository = SupabaseRepository()
    candidate = get_candidate(repository, args.candidate_code)

    source_evidence = candidate.get("source_evidence") or {}
    local_media_paths = filter_image_paths(
        source_evidence.get("local_media_paths") or []
    )

    if not local_media_paths:
        print()
        print(
            "No historical still-image media is recorded for this "
            "candidate. Nothing to extract."
        )
        return 0

    print(f"Local media (image) entries: {len(local_media_paths)}")

    raw_page_id = candidate.get("raw_page_id")

    if not raw_page_id:
        print()
        print("Error: candidate has no raw_page_id -- cannot check post ownership.")
        return 1

    sibling_codes = get_sibling_candidate_codes(
        repository, raw_page_id, candidate["candidate_id"]
    )
    ownership = image_rules.evaluate_historical_image_ownership(sibling_codes)

    print()
    print(f"Ownership check [{ownership.rule_code}]: {ownership.reason}")

    if ownership.outcome != Outcome.AUTO_PASS:
        print()
        print("Result: AMBIGUOUS_GROUP_IMAGE")
        print(
            "No files were extracted. Resolve ownership manually before "
            "retrying (assign each image to its correct candidate)."
        )
        return 0

    capability = check_capability(PROJECT_ROOT)

    print(f"Capability check: {capability.reason}")

    if not capability.available:
        print()
        print("Result: CAPABILITY_UNAVAILABLE")
        print("No files were extracted.")
        return 0

    print()
    print("Files to extract:")
    for path in local_media_paths:
        print(f"  - {path}")

    if args.confirm_extract:
        confirmation = "EXTRACT"
    elif args.non_interactive:
        confirmation = ""
    else:
        confirmation = input(
            "Type EXTRACT to write these images to the local cache, "
            "or press Enter to cancel: "
        ).strip().upper()

    if confirmation != "EXTRACT":
        print()
        print("Extraction cancelled.")
        return 0

    source_url_id = candidate.get("source_url_id")
    facebook_post_url = source_evidence.get("source_url")

    extracted = 0
    failed = 0

    print()
    for relative_path in local_media_paths:
        try:
            data = read_archive_image_bytes(capability.archive_path, relative_path)
            image_path, metadata = write_local_image_cache(
                project_root=PROJECT_ROOT,
                batch_code=args.batch_code,
                raw_page_id=raw_page_id,
                relative_path=relative_path,
                data=data,
                source_url_id=source_url_id,
                facebook_post_url=facebook_post_url,
            )
            extracted += 1
            print(f"  OK   {relative_path} -> {image_path}")

            if metadata.get("natural_width") is None:
                print(
                    "       Warning: could not determine image dimensions "
                    "(unrecognized/truncated format)."
                )

        except Exception as error:
            failed += 1
            print(f"  FAIL {relative_path}: {type(error).__name__}: {error}")

    print()
    print("=" * 78)
    print("EXTRACTION RESULT")
    print("=" * 78)
    print(f"Extracted: {extracted}")
    print(f"Failed: {failed}")
    print(
        "Next step: run "
        "scripts/upload_facebook_images_to_supabase.py "
        f"--candidate-code {args.candidate_code} "
        f"--batch-code {args.batch_code} --images ALL --non-interactive "
        "--confirm-upload"
    )

    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        print("Historical image extraction was cancelled.")
        sys.exit(130)
    except Exception as error:
        print()
        print("Historical image extraction failed.")
        print(f"Error type: {type(error).__name__}")
        print(f"Error details: {error}")
        sys.exit(1)
