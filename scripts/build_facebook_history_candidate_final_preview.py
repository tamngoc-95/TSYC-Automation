"""
OFFLINE historical Facebook candidate-extraction FINAL PREVIEW.

Combines the two already-completed preview passes into one final,
gitignored, OFFLINE-ONLY report:

    A. deterministic AUTO_PASS candidates
       (scripts/build_facebook_history_candidate_preview.py's own
       output -- data/processed/facebook_history_candidate_preview.csv)
    B. semantic AUTO_PASS candidates
       (scripts/extract_facebook_history_candidates_semantic.py's own
       output -- data/processed/facebook_history_candidate_semantic_extraction.csv)
    C. unresolved REVIEW_REQUIRED records (from either source)

This script performs NO extraction, NO classification, and NO gate
re-evaluation of its own -- it only reads the two already-validated
CSVs (both already ran the deterministic engine / the hardened
validate_and_gate() respectively) and merges them into one final,
human-reviewable table. It never creates a candidate row from an
unresolved record, and it never calls Supabase, WooCommerce, or the
Claude API.

Usage:
    .venv/Scripts/python.exe scripts/build_facebook_history_candidate_final_preview.py
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.cli_bootstrap import configure_utf8_console
from src.domain.rules.historical_text_cleaner import clean_historical_facebook_text
from src.services.facebook_history_parser import load_facebook_history_export
from src.services.historical_candidate_semantic_provider import (
    DEFAULT_MODEL,
    PROMPT_VERSION,
    PROVIDER_NAME,
    SCHEMA_VERSION,
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
DETERMINISTIC_PREVIEW_CSV = PROJECT_ROOT / "data" / "processed" / "facebook_history_candidate_preview.csv"
SEMANTIC_EXTRACTION_CSV = (
    PROJECT_ROOT / "data" / "processed" / "facebook_history_candidate_semantic_extraction.csv"
)
OUT_DIR = PROJECT_ROOT / "data" / "processed"
FINAL_CSV_FILENAME = "facebook_history_candidate_final_preview.csv"
FINAL_SUMMARY_FILENAME = "facebook_history_candidate_final_summary.json"

_LIST_JOIN = "; "
SOURCE_DETERMINISTIC = "DETERMINISTIC"
SOURCE_CLAUDE_SEMANTIC = "CLAUDE_SEMANTIC"


def _split_list_field(value: str) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part for part in value.split(_LIST_JOIN) if part)


def main() -> int:
    print(f"TSYC historical Facebook CANDIDATE-EXTRACTION FINAL PREVIEW -- v{SCRIPT_VERSION}")
    print("OFFLINE PREVIEW ONLY -- creates no Supabase/WooCommerce rows.")
    print()

    if not DETERMINISTIC_PREVIEW_CSV.is_file() or not SEMANTIC_EXTRACTION_CSV.is_file():
        print("ERROR: run both preview scripts first.")
        return 1

    with DETERMINISTIC_PREVIEW_CSV.open("r", encoding="utf-8", newline="") as f:
        det_rows = list(csv.DictReader(f))
    with SEMANTIC_EXTRACTION_CSV.open("r", encoding="utf-8", newline="") as f:
        sem_rows = list(csv.DictReader(f))

    det_by_id: dict[int, list[dict]] = {}
    for row in det_rows:
        det_by_id.setdefault(int(row["record_id"]), []).append(row)

    sem_by_id: dict[int, list[dict]] = {}
    for row in sem_rows:
        sem_by_id.setdefault(int(row["record_id"]), []).append(row)

    all_ids = sorted(set(det_by_id) | set(sem_by_id))
    print(f"FINAL INCLUDE records covered: {len(all_ids)}")

    records = load_facebook_history_export(DEFAULT_SOURCE_EXPORT)
    text_by_id = {r.record_index: r.full_text for r in records}
    media_by_id = {r.record_index: (r.local_image_paths, r.local_video_paths) for r in records}

    final_rows: list[dict] = []
    summary_counters = {
        "final_include_records": len(all_ids),
        "auto_pass_records": 0,
        "review_required_records": 0,
        "deterministic_auto_pass_records": 0,
        "semantic_auto_pass_records": 0,
        "total_final_candidates": 0,
        "single_book_candidates": 0,
        "book_combo_candidates": 0,
        "multi_candidate_records": 0,
        "hallucinated_titles": 0,  # always 0 by construction -- gate-enforced upstream
        "non_book_candidates": 0,  # always 0 by construction -- gate-enforced upstream
        "review_reason_tally": {},
    }

    for record_id in all_ids:
        det_group = det_by_id.get(record_id, [])
        sem_group = sem_by_id.get(record_id, [])

        det_outcome = det_group[0]["extraction_outcome"] if det_group else None
        sem_outcome = sem_group[0]["final_outcome"] if sem_group else None

        full_text = text_by_id.get(record_id, "")
        cleaned_text = clean_historical_facebook_text(full_text)
        cleaned_text_hash = hashlib.sha256(cleaned_text.encode("utf-8")).hexdigest()[:16]
        local_images, local_videos = media_by_id.get(record_id, ((), ()))
        local_media_paths = tuple(local_images) + tuple(local_videos)
        date_text = (det_group[0]["date"] if det_group else sem_group[0]["date"]) if (det_group or sem_group) else ""

        candidate_rows_for_record: list[dict] = []

        if det_outcome == "AUTO_PASS":
            for row in det_group:
                if not row["title_raw"]:
                    continue
                candidate_rows_for_record.append(
                    {
                        "extraction_source": SOURCE_DETERMINISTIC,
                        "title_raw": row["title_raw"],
                        "title_normalized": row["title_normalized"],
                        "candidate_type": row["candidate_type"],
                        "evidence_text": row["source_evidence"],
                        "confidence": row["confidence"],
                        "provider": "",
                        "model": "",
                        "prompt_version": "",
                        "schema_version": "",
                        "completeness_status": "COMPLETE",
                    }
                )
        elif sem_outcome == "AUTO_PASS":
            for row in sem_group:
                if not row["title_raw"]:
                    continue
                candidate_rows_for_record.append(
                    {
                        "extraction_source": SOURCE_CLAUDE_SEMANTIC,
                        "title_raw": row["title_raw"],
                        "title_normalized": row["title_raw"],
                        "candidate_type": row["candidate_type"],
                        "evidence_text": row["evidence_text"],
                        "confidence": row["confidence"],
                        "provider": PROVIDER_NAME,
                        "model": DEFAULT_MODEL,
                        "prompt_version": PROMPT_VERSION,
                        "schema_version": SCHEMA_VERSION,
                        "completeness_status": "COMPLETE",
                    }
                )

        is_auto_pass = bool(candidate_rows_for_record)
        common = {
            "record_id": record_id,
            "date": date_text,
            "local_media_count": len(local_media_paths),
            "local_media_paths": _LIST_JOIN.join(local_media_paths),
            "cleaned_text_hash": cleaned_text_hash,
        }

        if is_auto_pass:
            summary_counters["auto_pass_records"] += 1
            if det_outcome == "AUTO_PASS":
                summary_counters["deterministic_auto_pass_records"] += 1
            else:
                summary_counters["semantic_auto_pass_records"] += 1

            if len(candidate_rows_for_record) > 1:
                summary_counters["multi_candidate_records"] += 1

            for idx, candidate in enumerate(candidate_rows_for_record, start=1):
                summary_counters["total_final_candidates"] += 1
                if candidate["candidate_type"] == "SINGLE_BOOK":
                    summary_counters["single_book_candidates"] += 1
                elif candidate["candidate_type"] == "BOOK_COMBO":
                    summary_counters["book_combo_candidates"] += 1

                final_rows.append(
                    {
                        **common,
                        "final_outcome": "AUTO_PASS",
                        "candidate_index": idx,
                        "review_reason_codes": "",
                        "non_book_hints": "",
                        **candidate,
                    }
                )
        else:
            summary_counters["review_required_records"] += 1
            # Prefer the semantic layer's own reasons (it ran last and
            # is the authoritative final word for a REVIEW_REQUIRED
            # record); fall back to the deterministic layer's reasons
            # for the handful of records the semantic layer never
            # touched (there are none in this dataset, since every
            # deterministic REVIEW_REQUIRED record was processed by the
            # semantic layer -- this fallback exists for robustness).
            if sem_group:
                review_reasons = sem_group[0]["review_reason_codes"]
                non_book_hints = sem_group[0]["rejected_hints"]
            elif det_group:
                review_reasons = det_group[0]["review_reasons"]
                non_book_hints = det_group[0]["non_book_hints"]
            else:
                review_reasons = ""
                non_book_hints = ""

            for code in _split_list_field(review_reasons):
                key = code.split(":")[0].split("'")[0].strip() if code.startswith("REJECTED") else code
                summary_counters["review_reason_tally"][key] = (
                    summary_counters["review_reason_tally"].get(key, 0) + 1
                )

            final_rows.append(
                {
                    **common,
                    "final_outcome": "REVIEW_REQUIRED",
                    "extraction_source": "",
                    "candidate_index": "",
                    "title_raw": "",
                    "title_normalized": "",
                    "candidate_type": "",
                    "evidence_text": "",
                    "confidence": "",
                    "provider": "",
                    "model": "",
                    "prompt_version": "",
                    "schema_version": "",
                    "completeness_status": "",
                    "review_reason_codes": review_reasons,
                    "non_book_hints": non_book_hints,
                }
            )

    out_dir = OUT_DIR
    csv_path = out_dir / FINAL_CSV_FILENAME
    summary_path = out_dir / FINAL_SUMMARY_FILENAME

    fieldnames = [
        "record_id",
        "date",
        "final_outcome",
        "extraction_source",
        "candidate_index",
        "title_raw",
        "title_normalized",
        "candidate_type",
        "evidence_text",
        "confidence",
        "completeness_status",
        "provider",
        "model",
        "prompt_version",
        "schema_version",
        "review_reason_codes",
        "non_book_hints",
        "local_media_count",
        "local_media_paths",
        "cleaned_text_hash",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        for row in final_rows:
            writer.writerow(row)

    with summary_path.open("w", encoding="utf-8") as summary_file:
        json.dump(summary_counters, summary_file, ensure_ascii=False, indent=2, sort_keys=True)

    print(f"Wrote {len(all_ids)} record(s) / {len(final_rows)} row(s) to:")
    print(f"  {csv_path}")
    print(f"  {summary_path}")
    print()
    print("=== SUMMARY ===")
    for key, value in summary_counters.items():
        print(f"{key}: {value}")
    print()
    print("HISTORICAL_CANDIDATE_FINAL_PREVIEW_READY: YES")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
