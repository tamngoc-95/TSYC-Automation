"""
OFFLINE historical Facebook candidate-extraction PREVIEW.

Reads the already-completed secondary-classification output (data/
processed/facebook_history_secondary_classification.csv, produced by
scripts/classify_facebook_history_secondary.py), selects the
final_migration_decision=INCLUDE rows, and runs the existing
deterministic extraction engine (src/domain/rules/extraction_rules.py,
via the thin src/domain/rules/historical_candidate_extraction.py
adapter) over each one's own record text to produce a book/product
candidate PREVIEW.

This script does NOT:
    - write to Supabase
    - create product_candidates, source_urls, or raw_pages rows
    - call WooCommerce
    - publish anything or change any price
    - call the Claude API (it only reads each record's already-cached
      secondary-classification result -- semantic_extracted_product_
      hints -- from the CSV; it never re-classifies)
    - modify any deterministic/semantic classification rule

It only reads two local files (the classification CSV and the original
Facebook export HTML, for full_text/local media paths) and writes two
local preview report files:
    data/processed/facebook_history_candidate_preview.csv
    data/processed/facebook_history_candidate_preview_summary.json

A candidate row here is a PREVIEW, never a product_candidates row --
this script creates nothing in any database. Human review (and later,
an explicitly-approved, separately-authorized bounded import stage)
comes after this preview, not automatically from it.

Usage:
    .venv/Scripts/python.exe scripts/build_facebook_history_candidate_preview.py
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import hashlib

from src.cli_bootstrap import configure_utf8_console
from src.domain.decisions import Outcome
from src.domain.rules.historical_candidate_extraction import (
    HistoricalExtractionInput,
    HistoricalExtractionResult,
    extract_historical_candidates,
)
from src.domain.rules.historical_text_cleaner import clean_historical_facebook_text_with_stats
from src.services.facebook_history_parser import load_facebook_history_export

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
DEFAULT_CLASSIFICATION_CSV = (
    PROJECT_ROOT / "data" / "processed" / "facebook_history_secondary_classification.csv"
)
DEFAULT_OUT_DIR = PROJECT_ROOT / "data" / "processed"
PREVIEW_CSV_FILENAME = "facebook_history_candidate_preview.csv"
SUMMARY_JSON_FILENAME = "facebook_history_candidate_preview_summary.json"

_LIST_JOIN = "; "


def _split_list_field(value: str) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part for part in value.split(_LIST_JOIN) if part)


def load_include_rows(classification_csv: Path) -> list[dict]:
    with classification_csv.open("r", encoding="utf-8", newline="") as csv_file:
        rows = list(csv.DictReader(csv_file))

    include_rows = [row for row in rows if row["final_migration_decision"] == "INCLUDE"]
    # Stable, readable order -- document order (record_index ascending),
    # not CSV row order (which already is document order, but be explicit).
    include_rows.sort(key=lambda row: int(row["record_index"]))
    return include_rows


def build_extraction_input(row: dict, text_by_id: dict, media_by_id: dict) -> HistoricalExtractionInput:
    record_id = int(row["record_index"])
    full_text = text_by_id.get(record_id, "")
    local_images, local_videos = media_by_id.get(record_id, ((), ()))

    return HistoricalExtractionInput(
        record_id=record_id,
        date_text=row["date_text"],
        full_text=full_text,
        deterministic_post_type=row["deterministic_post_type"],
        deterministic_candidate_eligible=row["deterministic_candidate_eligible"] == "True",
        semantic_post_type=row["semantic_post_type"] or None,
        decision_source=row["decision_source"],
        semantic_extracted_product_hints=_split_list_field(row["semantic_extracted_product_hints"]),
        local_image_paths=local_images,
        local_video_paths=local_videos,
    )


def write_preview_csv(
    rows_with_results: list[tuple[dict, HistoricalExtractionResult]],
    path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "record_id",
        "date",
        "extraction_outcome",
        "post_product_type",
        "candidate_index",
        "title_raw",
        "title_normalized",
        "candidate_type",
        "confidence",
        "source_evidence",
        "non_book_hints",
        "review_reasons",
        "local_media_count",
        "local_media_paths",
        "cleaned_text_hash",
    ]

    with path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()

        for row, result in rows_with_results:
            local_media_paths = tuple(row["_local_image_paths"]) + tuple(row["_local_video_paths"])
            cleaned_text_hash = hashlib.sha256(result.cleaned_text.encode("utf-8")).hexdigest()[:16]
            common = {
                "record_id": result.record_id,
                "date": row["date_text"],
                "extraction_outcome": result.extraction_outcome,
                "post_product_type": result.post_product_type,
                "non_book_hints": _LIST_JOIN.join(result.non_book_hints),
                "review_reasons": _LIST_JOIN.join(result.review_reasons),
                "local_media_count": len(local_media_paths),
                "local_media_paths": _LIST_JOIN.join(local_media_paths),
                "cleaned_text_hash": cleaned_text_hash,
            }

            if not result.candidates:
                writer.writerow(
                    {
                        **common,
                        "candidate_index": "",
                        "title_raw": "",
                        "title_normalized": "",
                        "candidate_type": "",
                        "confidence": "",
                        "source_evidence": "",
                    }
                )
                continue

            for index, candidate in enumerate(result.candidates, start=1):
                writer.writerow(
                    {
                        **common,
                        "candidate_index": index,
                        "title_raw": candidate.title_raw,
                        "title_normalized": candidate.title_normalized,
                        "candidate_type": candidate.candidate_type,
                        "confidence": candidate.confidence,
                        "source_evidence": candidate.source_evidence,
                    }
                )


def build_summary(
    results: list[HistoricalExtractionResult],
    ui_chrome_titles_removed: int,
    duplicated_text_blocks_collapsed: int,
) -> dict:
    total = len(results)
    outcome_counts = {
        Outcome.AUTO_PASS: 0,
        Outcome.REVIEW_REQUIRED: 0,
        Outcome.AUTO_REJECT: 0,
    }
    product_type_counts: dict[str, int] = {}
    total_candidates = 0
    single_book_count = 0
    book_combo_count = 0
    zero_candidate_records = 0
    multi_candidate_records = 0
    total_non_book_hints = 0

    for result in results:
        outcome_counts[result.extraction_outcome] = outcome_counts.get(result.extraction_outcome, 0) + 1
        product_type_counts[result.post_product_type] = (
            product_type_counts.get(result.post_product_type, 0) + 1
        )
        total_candidates += len(result.candidates)
        single_book_count += sum(
            1 for candidate in result.candidates if candidate.candidate_type == "SINGLE_BOOK"
        )
        book_combo_count += sum(
            1 for candidate in result.candidates if candidate.candidate_type == "BOOK_COMBO"
        )
        total_non_book_hints += len(result.non_book_hints)

        if not result.candidates:
            zero_candidate_records += 1
        elif len(result.candidates) > 1:
            multi_candidate_records += 1

    return {
        "final_include_records_processed": total,
        "auto_pass_count": outcome_counts[Outcome.AUTO_PASS],
        "review_required_count": outcome_counts[Outcome.REVIEW_REQUIRED],
        "auto_reject_count": outcome_counts[Outcome.AUTO_REJECT],
        "post_product_type_counts": product_type_counts,
        "total_preview_candidates": total_candidates,
        "single_book_candidates": single_book_count,
        "book_combo_candidates": book_combo_count,
        "records_with_zero_candidates": zero_candidate_records,
        "records_with_multiple_candidates": multi_candidate_records,
        "ui_chrome_titles_removed": ui_chrome_titles_removed,
        "duplicated_text_blocks_collapsed": duplicated_text_blocks_collapsed,
        "total_non_book_hints_removed": total_non_book_hints,
    }


def main() -> int:
    print(f"TSYC historical Facebook CANDIDATE-EXTRACTION PREVIEW -- v{SCRIPT_VERSION}")
    print("OFFLINE PREVIEW ONLY -- creates no Supabase/WooCommerce rows.")
    print()

    if not DEFAULT_CLASSIFICATION_CSV.is_file():
        print(f"ERROR: classification CSV not found: {DEFAULT_CLASSIFICATION_CSV}")
        print("Run scripts/classify_facebook_history_secondary.py (full run) first.")
        return 1

    if not DEFAULT_SOURCE_EXPORT.is_file():
        print(f"ERROR: source export file not found: {DEFAULT_SOURCE_EXPORT}")
        return 1

    include_rows = load_include_rows(DEFAULT_CLASSIFICATION_CSV)
    print(f"FINAL INCLUDE records found in classification CSV: {len(include_rows)}")

    records = load_facebook_history_export(DEFAULT_SOURCE_EXPORT)
    text_by_id = {record.record_index: record.full_text for record in records}
    media_by_id = {
        record.record_index: (record.local_image_paths, record.local_video_paths)
        for record in records
    }

    rows_with_results: list[tuple[dict, HistoricalExtractionResult]] = []
    results: list[HistoricalExtractionResult] = []
    ui_chrome_titles_removed = 0
    duplicated_text_blocks_collapsed = 0

    for row in include_rows:
        extraction_input = build_extraction_input(row, text_by_id, media_by_id)
        result = extract_historical_candidates(extraction_input)

        # cleaning-stage provenance metrics -- reuses the exact same
        # cleaning pipeline extract_historical_candidates() itself just
        # ran (pure function, safe to call again for reporting only).
        _cleaned_text, cleaning_stats = clean_historical_facebook_text_with_stats(
            extraction_input.full_text
        )
        if cleaning_stats.dropped_leading_boilerplate:
            ui_chrome_titles_removed += 1
        if cleaning_stats.collapsed_duplicate_sequence:
            duplicated_text_blocks_collapsed += 1

        row = dict(row)
        row["_local_image_paths"] = extraction_input.local_image_paths
        row["_local_video_paths"] = extraction_input.local_video_paths
        row["_full_text"] = extraction_input.full_text

        rows_with_results.append((row, result))
        results.append(result)

    out_dir = DEFAULT_OUT_DIR
    csv_path = out_dir / PREVIEW_CSV_FILENAME
    summary_path = out_dir / SUMMARY_JSON_FILENAME

    write_preview_csv(rows_with_results, csv_path)

    summary = build_summary(results, ui_chrome_titles_removed, duplicated_text_blocks_collapsed)
    with summary_path.open("w", encoding="utf-8") as summary_file:
        json.dump(summary, summary_file, ensure_ascii=False, indent=2, sort_keys=True)

    print(f"Wrote preview for {len(results)} record(s) to:")
    print(f"  {csv_path}")
    print(f"  {summary_path}")
    print()

    print("=== SUMMARY ===")
    for key, value in summary.items():
        print(f"{key}: {value}")
    print()

    print("HISTORICAL_CANDIDATE_PREVIEW_READY: YES")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
