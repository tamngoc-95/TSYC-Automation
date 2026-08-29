"""
OFFLINE historical Facebook migration classification layer.

Parses a personal Facebook data-export "posts, check-ins, photos and
videos" HTML file and deterministically classifies every historical
record's TSYC business relevance, post type, and migration-candidate
eligibility -- BEFORE any of that data ever touches the real pipeline.

This script is OFFLINE ONLY. It does NOT:
    - write to Supabase
    - create source_urls rows
    - create raw_pages rows
    - create product_candidates rows
    - call WooCommerce
    - call the Claude/Anthropic API
    - modify any existing production candidate, product, or content row

It only reads one local HTML file and writes two local report files:
    data/processed/facebook_history_classification.csv
    data/processed/facebook_history_classification_summary.json

A candidate_eligible=True row is a screening signal for a human (or a
later, explicitly-approved stage) to look at -- never itself a
product_candidates row, and this script never creates one.

Usage:
    .venv/Scripts/python.exe scripts/classify_facebook_history_export.py
    .venv/Scripts/python.exe scripts/classify_facebook_history_export.py \\
        --source path/to/export.html --out-dir data/processed
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.cli_bootstrap import configure_utf8_console
from src.services.facebook_history_parser import load_facebook_history_export
from src.services.facebook_history_report import (
    ClassifiedRecord,
    build_summary,
    classify_records,
    write_classification_csv,
    write_summary_json,
)

configure_utf8_console()

SCRIPT_VERSION = "1.0.0"

DEFAULT_SOURCE = (
    PROJECT_ROOT
    / "data"
    / "raw"
    / "facebook_export_probe"
    / "your_facebook_activity"
    / "posts"
    / "your_posts__check_ins__photos_and_videos_1.html"
)
DEFAULT_OUT_DIR = PROJECT_ROOT / "data" / "processed"
CSV_FILENAME = "facebook_history_classification.csv"
SUMMARY_FILENAME = "facebook_history_classification_summary.json"

SAMPLE_COUNT = 10


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "OFFLINE ONLY: classify a historical Facebook export's records "
            "by TSYC business relevance, post type, and migration-candidate "
            "eligibility. Never writes to Supabase/WooCommerce and never "
            "calls an LLM."
        )
    )

    parser.add_argument(
        "--source",
        default=str(DEFAULT_SOURCE),
        help="Path to the Facebook export HTML file (default: the known "
        "probe export under data/raw/).",
    )
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_OUT_DIR),
        help="Directory to write the CSV and summary JSON into "
        "(default: data/processed).",
    )

    return parser.parse_args()


def _print_row_summary(row: ClassifiedRecord) -> None:
    record = row.record
    result = row.classification
    print(f"  #{record.record_index} | {record.date_text or '(no date)'}")
    print(f"    text: {record.text_preview!r}")
    print(
        f"    relevance={result.tsyc_relevance} post_type={result.post_type} "
        f"candidate_eligible={result.candidate_eligible}"
    )
    print(f"    reason: {result.classification_reason}")
    print()


def main() -> int:
    args = parse_arguments()
    source_path = Path(args.source)
    out_dir = Path(args.out_dir)

    print(f"TSYC historical Facebook classification -- OFFLINE ONLY (v{SCRIPT_VERSION})")
    print(f"Source export : {source_path}")
    print(f"Output dir    : {out_dir}")
    print()

    if not source_path.is_file():
        print(f"ERROR: source export file not found: {source_path}")
        print()
        print("HISTORICAL_CLASSIFICATION_READY_FOR_REVIEW: NO")
        return 1

    records = load_facebook_history_export(source_path)
    classified_records = classify_records(records)

    csv_path = out_dir / CSV_FILENAME
    summary_path = out_dir / SUMMARY_FILENAME

    write_classification_csv(classified_records, csv_path)
    summary = build_summary(classified_records, source_file=source_path)
    write_summary_json(summary, summary_path)

    print(f"Wrote {len(classified_records)} classified record(s) to:")
    print(f"  {csv_path}")
    print(f"  {summary_path}")
    print()

    print("=== AGGREGATE COUNTS ===")
    print(f"TOTAL RECORDS: {summary['total_records']}")
    print(f"tsyc_relevance: {summary['tsyc_relevance_counts']}")
    print(f"post_type: {summary['post_type_counts']}")
    print(f"CANDIDATE_ELIGIBLE: {summary['candidate_eligible_count']}")
    print(f"candidate_eligible_by_year: {summary['candidate_eligible_by_year']}")
    print(f"SECONDARY_REVIEW_COUNT: {summary['secondary_review_count']}")
    print()

    print("=== TOP CLASSIFICATION REASONS ===")
    for entry in summary["top_classification_reasons"]:
        print(f"  {entry['count']:4d} | {entry['reason']}")
    print()

    eligible_rows = [row for row in classified_records if row.classification.candidate_eligible]
    uncertain_rows = [
        row
        for row in classified_records
        if row.classification.needs_secondary_review and not row.classification.candidate_eligible
    ]

    def sample(rows: list[ClassifiedRecord], count: int) -> list[ClassifiedRecord]:
        if not rows:
            return []
        stride = max(1, len(rows) // count)
        return rows[::stride][:count]

    print(f"=== {SAMPLE_COUNT} REPRESENTATIVE CANDIDATE_ELIGIBLE RECORDS ===")
    for row in sample(eligible_rows, SAMPLE_COUNT):
        _print_row_summary(row)

    print(f"=== {SAMPLE_COUNT} REPRESENTATIVE UNCERTAIN (SECONDARY REVIEW) RECORDS ===")
    for row in sample(uncertain_rows, SAMPLE_COUNT):
        _print_row_summary(row)

    print("HISTORICAL_CLASSIFICATION_READY_FOR_REVIEW: YES")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
