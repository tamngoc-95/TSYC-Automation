"""OFFLINE reporting/output layer for the historical Facebook migration
classification screening pass.

Combines src.services.facebook_history_parser.HistoryRecord (raw parsed
facts) with src.domain.rules.facebook_history_classification.classify()
(deterministic classification) into one flat row per record, and writes
the two required artifacts:

    data/processed/facebook_history_classification.csv
    data/processed/facebook_history_classification_summary.json

This module never touches Supabase or WooCommerce and never calls an
LLM/Claude API -- it only reads a list of already-parsed HistoryRecord
values (in memory) and writes local files. See
scripts/classify_facebook_history_export.py for the CLI entry point that
wires src.services.facebook_history_parser to this module.

Determinism / idempotency: build_summary() and the CSV row order both
depend only on the input records list (already in stable document
order) -- neither includes a wall-clock timestamp or any other
non-reproducible value, so running the full pipeline twice against an
unmodified export file produces byte-identical output files (see
tests/test_facebook_history_report.py's idempotency test). If a caller
wants a run timestamp for their own audit trail, it belongs in the
caller (the CLI script may print one to the console), not in these
files.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.domain.rules.facebook_history_classification import ClassificationResult, classify
from src.services.facebook_history_parser import HistoryRecord

CSV_COLUMNS = (
    "record_index",
    "date_text",
    "heading",
    "full_text",
    "text_preview",
    "external_links",
    "local_image_paths",
    "local_video_paths",
    "media_count",
    "strong_markers",
    "weak_markers",
    "structural_mention_id",
    "folder_slug_evidence",
    "tsyc_relevance",
    "post_type",
    "candidate_eligible",
    "classification_reason",
    "needs_secondary_review",
)

_LIST_JOIN = "; "
_YEAR_RE = re.compile(r"\b(20\d\d)\b")


@dataclass(frozen=True)
class ClassifiedRecord:
    """One HistoryRecord plus its ClassificationResult, flattened for
    CSV/JSON output. record and classification are kept as their own
    typed sub-objects too, so a caller working in memory (e.g. a test, or
    a future review UI) never has to re-parse the flattened strings."""

    record: HistoryRecord
    classification: ClassificationResult

    def to_csv_row(self) -> dict[str, Any]:
        record = self.record
        result = self.classification

        return {
            "record_index": record.record_index,
            "date_text": record.date_text,
            "heading": record.heading,
            "full_text": record.full_text,
            "text_preview": record.text_preview,
            "external_links": _LIST_JOIN.join(record.external_links),
            "local_image_paths": _LIST_JOIN.join(record.local_image_paths),
            "local_video_paths": _LIST_JOIN.join(record.local_video_paths),
            "media_count": record.media_count,
            "strong_markers": _LIST_JOIN.join(result.strong_markers),
            "weak_markers": _LIST_JOIN.join(result.weak_markers),
            "structural_mention_id": result.structural_mention_id or "",
            "folder_slug_evidence": _LIST_JOIN.join(result.folder_slug_evidence),
            "tsyc_relevance": result.tsyc_relevance,
            "post_type": result.post_type,
            "candidate_eligible": result.candidate_eligible,
            "classification_reason": result.classification_reason,
            "needs_secondary_review": result.needs_secondary_review,
        }


def classify_records(records: list[HistoryRecord]) -> list[ClassifiedRecord]:
    """Classify every record. Pure/deterministic: classify() itself is a
    pure function (see its own docstring), so this is too."""
    return [
        ClassifiedRecord(record=record, classification=classify(
            full_text=record.full_text,
            folder_slugs=record.folder_slugs,
            mention_ids=record.mention_ids,
        ))
        for record in records
    ]


def write_classification_csv(classified_records: list[ClassifiedRecord], path: str | Path) -> None:
    """Write the per-record CSV. newline="" per the csv module's own
    documented requirement (otherwise embedded newlines in full_text
    would be written with doubled line endings on Windows)."""
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
        writer.writeheader()

        for classified in classified_records:
            writer.writerow(classified.to_csv_row())


def _extract_year(date_text: str) -> str:
    match = _YEAR_RE.search(date_text or "")
    return match.group(1) if match else "UNKNOWN"


def build_summary(classified_records: list[ClassifiedRecord], *, source_file: str | Path | None = None) -> dict[str, Any]:
    """Build the summary dict written to
    facebook_history_classification_summary.json. See this module's own
    docstring for the determinism/idempotency contract -- no timestamps,
    no non-reproducible values."""
    total_records = len(classified_records)

    relevance_counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    post_type_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    candidate_eligible_count = 0
    candidate_eligible_by_year: dict[str, int] = {}
    secondary_review_indices: list[int] = []

    for classified in classified_records:
        result = classified.classification
        relevance_counts[result.tsyc_relevance] += 1
        post_type_counts[result.post_type] = post_type_counts.get(result.post_type, 0) + 1
        reason_counts[result.classification_reason] = (
            reason_counts.get(result.classification_reason, 0) + 1
        )

        if result.candidate_eligible:
            candidate_eligible_count += 1
            year = _extract_year(classified.record.date_text)
            candidate_eligible_by_year[year] = candidate_eligible_by_year.get(year, 0) + 1

        if result.needs_secondary_review:
            secondary_review_indices.append(classified.record.record_index)

    top_classification_reasons = [
        {"reason": reason, "count": count}
        for reason, count in sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))
    ]

    return {
        "source_file": str(source_file) if source_file is not None else None,
        "total_records": total_records,
        "tsyc_relevance_counts": relevance_counts,
        "post_type_counts": dict(sorted(post_type_counts.items())),
        "candidate_eligible_count": candidate_eligible_count,
        "candidate_eligible_by_year": dict(sorted(candidate_eligible_by_year.items())),
        "top_classification_reasons": top_classification_reasons,
        "secondary_review_count": len(secondary_review_indices),
        "secondary_review_record_indices": secondary_review_indices,
    }


def write_summary_json(summary: dict[str, Any], path: str | Path) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as json_file:
        json.dump(summary, json_file, ensure_ascii=False, indent=2, sort_keys=False)
        json_file.write("\n")
