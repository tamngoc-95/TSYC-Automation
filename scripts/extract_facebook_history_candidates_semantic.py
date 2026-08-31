"""
OFFLINE SECONDARY (semantic) historical Facebook CANDIDATE-EXTRACTION.

Runs the semantic candidate extractor (src/services/historical_
candidate_semantic_provider.py) ONLY over records the deterministic
candidate-extraction preview (scripts/build_facebook_history_candidate_
preview.py) already gave up on -- extraction_outcome=REVIEW_REQUIRED in
data/processed/facebook_history_candidate_preview.csv. A deterministic
AUTO_PASS record is never touched by this script; the deterministic
layer's own result stands.

Pipeline:
    deterministic REVIEW_REQUIRED record's own full_text
    -> historical_text_cleaner.clean_historical_facebook_text() (same,
       already-validated cleaner -- never re-implemented here)
    -> semantic candidate provider (mock or claude)
    -> historical_candidate_semantic.validate_and_gate() (hard safety
       gate -- the only place a record becomes AUTO_PASS)
    -> AUTO_PASS or REVIEW_REQUIRED

This script does NOT:
    - write to Supabase
    - create product_candidates, source_urls, or raw_pages rows
    - call WooCommerce
    - publish anything or change any price
    - modify the deterministic candidate-extraction preview or its
      cleaner
    - call a live LLM unless --provider claude is passed explicitly

It only reads local files (the two existing preview/classification
CSVs and the original Facebook export HTML) and writes two local
report files:
    data/processed/facebook_history_candidate_semantic_extraction.csv
    data/processed/facebook_history_candidate_semantic_extraction_summary.json

Usage:
    .venv/Scripts/python.exe scripts/extract_facebook_history_candidates_semantic.py --dry-run
    .venv/Scripts/python.exe scripts/extract_facebook_history_candidates_semantic.py \\
        --provider claude --record-id 1189 --record-id 1560
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

from src.cli_bootstrap import configure_utf8_console
from src.domain.rules.historical_candidate_semantic import (
    CandidateExtractionInput,
    HistoricalCandidateSemanticClassifier,
    validate_and_gate,
)
from src.domain.rules.historical_text_cleaner import clean_historical_facebook_text
from src.services.facebook_history_parser import load_facebook_history_export
from src.services.historical_candidate_semantic_provider import (
    ClaudeCandidateProviderConfigurationError,
    ClaudeHistoricalCandidateProvider,
    MockHistoricalCandidateProvider,
)

configure_utf8_console()

SCRIPT_VERSION = "1.0.0"

DEFAULT_SOURCE_EXPORT = (
    PROJECT_ROOT
    / "data"
    / "raw"
    / "facebook_export_probe"
    / "your_facebook_activity"
    / "posts"
    / "your_posts__check_ins__photos_and_videos_1.html"
)
DEFAULT_PREVIEW_CSV = PROJECT_ROOT / "data" / "processed" / "facebook_history_candidate_preview.csv"
DEFAULT_CLASSIFICATION_CSV = (
    PROJECT_ROOT / "data" / "processed" / "facebook_history_secondary_classification.csv"
)
DEFAULT_OUT_DIR = PROJECT_ROOT / "data" / "processed"
OUTPUT_CSV_FILENAME = "facebook_history_candidate_semantic_extraction.csv"
SUMMARY_JSON_FILENAME = "facebook_history_candidate_semantic_extraction_summary.json"

_LIST_JOIN = "; "


def _split_list_field(value: str) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part for part in value.split(_LIST_JOIN) if part)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "OFFLINE BY DEFAULT: run the SECONDARY semantic candidate extractor "
            "over deterministic REVIEW_REQUIRED records only. Never writes to "
            "Supabase/WooCommerce. Never calls a live LLM unless --provider "
            "claude is passed explicitly."
        )
    )
    parser.add_argument(
        "--provider",
        choices=("mock", "claude"),
        default="mock",
        help="Semantic candidate provider (default: mock -- zero network calls). "
        "'claude' makes real, billed Anthropic API calls for every record not "
        "already cached and requires ANTHROPIC_API_KEY.",
    )
    parser.add_argument(
        "--max-records",
        type=int,
        default=None,
        help="Process at most the first N eligible (REVIEW_REQUIRED) records. "
        "Ignored if --record-id is given.",
    )
    parser.add_argument(
        "--record-id",
        type=int,
        action="append",
        default=None,
        dest="record_ids",
        help="Process only this exact record_id. Repeatable. Overrides --max-records. "
        "Must be a deterministic REVIEW_REQUIRED record.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Select and print eligible records only -- no provider call, no output files.",
    )
    return parser.parse_args()


def load_eligible_records(preview_csv: Path) -> list[dict]:
    with preview_csv.open("r", encoding="utf-8", newline="") as csv_file:
        rows = list(csv.DictReader(csv_file))

    eligible = [row for row in rows if row["extraction_outcome"] == "REVIEW_REQUIRED"]
    eligible.sort(key=lambda row: int(row["record_id"]))
    return eligible


def load_semantic_post_types(classification_csv: Path) -> dict[int, str]:
    with classification_csv.open("r", encoding="utf-8", newline="") as csv_file:
        rows = list(csv.DictReader(csv_file))
    return {int(row["record_index"]): row["semantic_post_type"] or None for row in rows}


def build_extraction_input(
    row: dict,
    text_by_id: dict,
    media_by_id: dict,
    semantic_post_type_by_id: dict,
) -> CandidateExtractionInput:
    record_id = int(row["record_id"])
    full_text = text_by_id.get(record_id, "")
    cleaned_text = clean_historical_facebook_text(full_text)
    local_images, local_videos = media_by_id.get(record_id, ((), ()))

    return CandidateExtractionInput(
        record_id=record_id,
        cleaned_text=cleaned_text,
        date_text=row["date"],
        local_image_paths=local_images,
        local_video_paths=local_videos,
        deterministic_review_reasons=_split_list_field(row["review_reasons"]),
        semantic_post_type=semantic_post_type_by_id.get(record_id),
        non_book_hints=_split_list_field(row["non_book_hints"]),
    )


def _select_rows(eligible: list[dict], args: argparse.Namespace) -> list[dict]:
    if args.record_ids:
        wanted = list(dict.fromkeys(args.record_ids))
        by_id = {int(row["record_id"]): row for row in eligible}
        missing = [rid for rid in wanted if rid not in by_id]
        if missing:
            raise SystemExit(
                f"ERROR: --record-id not found among eligible (REVIEW_REQUIRED) "
                f"records: {missing}"
            )
        wanted_set = set(wanted)
        return [row for row in eligible if int(row["record_id"]) in wanted_set]

    if args.max_records is not None:
        return eligible[: args.max_records]

    return eligible


def _build_provider(args: argparse.Namespace) -> HistoricalCandidateSemanticClassifier:
    if args.provider == "claude":
        return ClaudeHistoricalCandidateProvider()
    return MockHistoricalCandidateProvider()


def write_output_csv(rows_with_results: list[tuple[dict, str, object]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "record_id",
        "date",
        "final_outcome",
        "post_product_type",
        "candidate_index",
        "title_raw",
        "candidate_type",
        "confidence",
        "evidence_text",
        "review_reason_codes",
        "rejected_hints",
    ]

    with path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()

        for row, outcome, result in rows_with_results:
            common = {
                "record_id": row["record_id"],
                "date": row["date"],
                "final_outcome": outcome,
                "post_product_type": result.post_product_type,
                "review_reason_codes": _LIST_JOIN.join(result.review_reason_codes),
                "rejected_hints": _LIST_JOIN.join(
                    f"{h.text} ({h.reason})" for h in result.rejected_hints
                ),
            }

            if not result.candidates:
                writer.writerow(
                    {
                        **common,
                        "candidate_index": "",
                        "title_raw": "",
                        "candidate_type": "",
                        "confidence": "",
                        "evidence_text": "",
                    }
                )
                continue

            for index, candidate in enumerate(result.candidates, start=1):
                writer.writerow(
                    {
                        **common,
                        "candidate_index": index,
                        "title_raw": candidate.title_raw,
                        "candidate_type": candidate.candidate_type,
                        "confidence": candidate.confidence,
                        "evidence_text": candidate.evidence_text,
                    }
                )


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")  # never printed/logged; only populates os.environ

    args = parse_arguments()

    print(f"TSYC historical Facebook SEMANTIC CANDIDATE EXTRACTION -- v{SCRIPT_VERSION}")
    print("OFFLINE PREVIEW ONLY -- creates no Supabase/WooCommerce rows.")
    print(f"Provider: {args.provider}" + ("  (no network calls)" if args.provider == "mock" else ""))
    print()

    if not DEFAULT_PREVIEW_CSV.is_file():
        print(f"ERROR: deterministic preview CSV not found: {DEFAULT_PREVIEW_CSV}")
        print("Run scripts/build_facebook_history_candidate_preview.py first.")
        return 1

    if not DEFAULT_CLASSIFICATION_CSV.is_file():
        print(f"ERROR: classification CSV not found: {DEFAULT_CLASSIFICATION_CSV}")
        return 1

    if not DEFAULT_SOURCE_EXPORT.is_file():
        print(f"ERROR: source export file not found: {DEFAULT_SOURCE_EXPORT}")
        return 1

    eligible = load_eligible_records(DEFAULT_PREVIEW_CSV)
    print(f"Deterministic REVIEW_REQUIRED records available: {len(eligible)}")

    try:
        selected_rows = _select_rows(eligible, args)
    except SystemExit as error:
        print(error)
        return 1

    print(f"Selected {len(selected_rows)} record(s) for this run.")
    print()

    records = load_facebook_history_export(DEFAULT_SOURCE_EXPORT)
    text_by_id = {r.record_index: r.full_text for r in records}
    media_by_id = {r.record_index: (r.local_image_paths, r.local_video_paths) for r in records}
    semantic_post_type_by_id = load_semantic_post_types(DEFAULT_CLASSIFICATION_CSV)

    if args.dry_run:
        print("=== DRY RUN -- no provider called, no files written ===")
        for row in selected_rows:
            print(f"  #{row['record_id']} | {row['date']} | non_book_hints={row['non_book_hints']!r}")
        print()
        print("CANDIDATE_SEMANTIC_EXTRACTION_READY: YES")
        return 0

    try:
        provider = _build_provider(args)
    except ClaudeCandidateProviderConfigurationError as error:
        print(f"ERROR: {error}")
        return 1

    rows_with_results: list[tuple[dict, str, object]] = []

    for row in selected_rows:
        extraction_input = build_extraction_input(row, text_by_id, media_by_id, semantic_post_type_by_id)
        raw_result = provider.extract(extraction_input)
        outcome, sanitized_result = validate_and_gate(raw_result, extraction_input)

        rows_with_results.append((row, outcome, sanitized_result))

        print(f"  #{row['record_id']} | {row['date']}")
        print(f"    post_product_type: {sanitized_result.post_product_type}")
        print(f"    FINAL: {outcome}")
        for candidate in sanitized_result.candidates:
            print(f"      - [{candidate.candidate_type}] {candidate.title_raw!r} (conf={candidate.confidence:.2f})")
        if sanitized_result.review_reason_codes:
            print(f"    reasons: {list(sanitized_result.review_reason_codes)}")
        print()

    out_dir = DEFAULT_OUT_DIR
    csv_path = out_dir / OUTPUT_CSV_FILENAME
    summary_path = out_dir / SUMMARY_JSON_FILENAME

    write_output_csv(rows_with_results, csv_path)

    auto_pass_count = sum(1 for _row, outcome, _r in rows_with_results if outcome == "AUTO_PASS")
    review_required_count = sum(1 for _row, outcome, _r in rows_with_results if outcome == "REVIEW_REQUIRED")
    total_candidates = sum(len(r.candidates) for _row, _o, r in rows_with_results)

    summary = {
        "records_processed": len(rows_with_results),
        "auto_pass_after_semantic": auto_pass_count,
        "still_review_required": review_required_count,
        "total_extracted_candidates": total_candidates,
        "provider": args.provider,
    }
    with summary_path.open("w", encoding="utf-8") as summary_file:
        json.dump(summary, summary_file, ensure_ascii=False, indent=2, sort_keys=True)

    print(f"Wrote {len(rows_with_results)} record(s) to:")
    print(f"  {csv_path}")
    print(f"  {summary_path}")
    print()
    print("=== SUMMARY ===")
    for key, value in summary.items():
        print(f"{key}: {value}")
    print()
    print("CANDIDATE_SEMANTIC_EXTRACTION_READY: YES")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
