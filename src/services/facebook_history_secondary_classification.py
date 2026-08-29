"""OFFLINE orchestration/output layer for the SECONDARY (semantic)
historical Facebook migration classification pass.

Combines src.services.facebook_history_report.ClassifiedRecord (one
HistoryRecord plus its deterministic ClassificationResult, from the
FIRST layer) with:

    src.domain.rules.facebook_history_semantic.route_record()
    a HistoricalPostSemanticClassifier (default:
        src.services.facebook_history_semantic_provider.
        MockHistoricalSemanticProvider -- offline, no network)
    src.domain.rules.facebook_history_semantic.synthesize_final_decision()

into one fully-provenanced SecondaryClassifiedRecord per input record,
and writes the two required artifacts:

    data/processed/facebook_history_secondary_classification.csv
    data/processed/facebook_history_secondary_summary.json

This module never touches Supabase or WooCommerce. It calls the given
HistoricalPostSemanticClassifier only for records route_record() sends
to SEND_TO_SEMANTIC -- with the default MockHistoricalSemanticProvider,
that means zero network calls anywhere in this module, keeping the
whole pipeline OFFLINE per this phase's explicit requirement. See
scripts/classify_facebook_history_secondary.py for the CLI entry point.

Determinism / idempotency: exactly like facebook_history_report.py,
build_secondary_summary() and the CSV row order depend only on the input
list (already in stable document order) and the given classifier's own
determinism -- MockHistoricalSemanticProvider is a pure function of its
input, so running the full pipeline twice against an unmodified export
file produces byte-identical output files (see tests/test_facebook_
history_secondary_classification.py's idempotency test). Neither output
file includes a wall-clock timestamp.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.domain.rules.facebook_history_semantic import (
    ALL_FINAL_DECISIONS,
    DEFAULT_HIGH_CONFIDENCE_THRESHOLD,
    FinalDecisionResult,
    HistoricalPostSemanticClassifier,
    RoutingDecision,
    SemanticClassificationInput,
    route_record,
    synthesize_final_decision,
)
from src.services.facebook_history_report import ClassifiedRecord

_LIST_JOIN = "; "

SECONDARY_CSV_COLUMNS = (
    "record_index",
    "date_text",
    "text_preview",
    "deterministic_tsyc_relevance",
    "deterministic_post_type",
    "deterministic_candidate_eligible",
    "deterministic_classification_reason",
    "routing_decision",
    "semantic_called",
    "semantic_post_type",
    "semantic_product_migration_relevant",
    "semantic_confidence",
    "semantic_reason_codes",
    "semantic_extracted_product_hints",
    "final_migration_decision",
    "decision_source",
    "final_reason_codes",
)


@dataclass(frozen=True)
class SecondaryClassifiedRecord:
    """One ClassifiedRecord (record + deterministic result, from the
    FIRST layer -- untouched) plus its routing decision, optional
    semantic result, and final synthesized decision. The deterministic
    sub-result is never mutated or discarded -- it is carried through
    via `first_layer` exactly as produced by classify_records()."""

    first_layer: ClassifiedRecord
    final: FinalDecisionResult

    def to_csv_row(self) -> dict[str, Any]:
        record = self.first_layer.record
        deterministic = self.first_layer.classification
        semantic = self.final.semantic

        return {
            "record_index": record.record_index,
            "date_text": record.date_text,
            "text_preview": record.text_preview,
            "deterministic_tsyc_relevance": deterministic.tsyc_relevance,
            "deterministic_post_type": deterministic.post_type,
            "deterministic_candidate_eligible": deterministic.candidate_eligible,
            "deterministic_classification_reason": deterministic.classification_reason,
            "routing_decision": self.final.routing_decision,
            "semantic_called": semantic is not None,
            "semantic_post_type": semantic.semantic_post_type if semantic else "",
            "semantic_product_migration_relevant": (
                semantic.product_migration_relevant if semantic else ""
            ),
            "semantic_confidence": semantic.confidence if semantic else "",
            "semantic_reason_codes": _LIST_JOIN.join(semantic.reason_codes) if semantic else "",
            "semantic_extracted_product_hints": (
                _LIST_JOIN.join(semantic.extracted_product_hints) if semantic else ""
            ),
            "final_migration_decision": self.final.final_migration_decision,
            "decision_source": self.final.decision_source,
            "final_reason_codes": _LIST_JOIN.join(self.final.reason_codes),
        }


def run_secondary_classification(
    classified_records: list[ClassifiedRecord],
    classifier: HistoricalPostSemanticClassifier,
    *,
    high_confidence_threshold: float = DEFAULT_HIGH_CONFIDENCE_THRESHOLD,
) -> list[SecondaryClassifiedRecord]:
    """Route, (conditionally) semantically classify, and synthesize a
    final decision for every record.

    The classifier is only ever invoked for a record route_record()
    sends to SEND_TO_SEMANTIC -- every other record's final decision is
    produced from the deterministic result alone, with zero calls to
    `classifier`."""
    results: list[SecondaryClassifiedRecord] = []

    for classified in classified_records:
        record = classified.record
        deterministic = classified.classification

        routing_decision = route_record(deterministic)

        semantic = None
        if routing_decision == RoutingDecision.SEND_TO_SEMANTIC:
            request = SemanticClassificationInput(
                record_id=record.record_index,
                date_text=record.date_text,
                full_text=record.full_text,
                heading=record.heading,
                strong_markers=deterministic.strong_markers,
                weak_markers=deterministic.weak_markers,
                folder_slug_evidence=deterministic.folder_slug_evidence,
                structural_mention_id=deterministic.structural_mention_id,
                local_image_count=len(record.local_image_paths),
                local_video_count=len(record.local_video_paths),
                deterministic_tsyc_relevance=deterministic.tsyc_relevance,
                deterministic_post_type=deterministic.post_type,
                deterministic_candidate_eligible=deterministic.candidate_eligible,
                deterministic_classification_reason=deterministic.classification_reason,
            )
            semantic = classifier.classify(request)

        final = synthesize_final_decision(
            record_id=record.record_index,
            deterministic=deterministic,
            routing_decision=routing_decision,
            semantic=semantic,
            high_confidence_threshold=high_confidence_threshold,
        )

        results.append(SecondaryClassifiedRecord(first_layer=classified, final=final))

    return results


def write_secondary_csv(results: list[SecondaryClassifiedRecord], path: str | Path) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=SECONDARY_CSV_COLUMNS)
        writer.writeheader()

        for result in results:
            writer.writerow(result.to_csv_row())


def build_secondary_summary(
    results: list[SecondaryClassifiedRecord],
    *,
    source_file: str | Path | None = None,
    high_confidence_threshold: float = DEFAULT_HIGH_CONFIDENCE_THRESHOLD,
) -> dict[str, Any]:
    """Build the summary dict written to
    facebook_history_secondary_summary.json. No timestamps or other
    non-reproducible values -- see this module's own docstring."""
    total_records = len(results)

    routing_counts: dict[str, int] = {}
    final_decision_counts: dict[str, int] = {decision: 0 for decision in sorted(ALL_FINAL_DECISIONS)}
    decision_source_counts: dict[str, int] = {}
    semantic_called_count = 0
    false_positive_like_removed_count = 0
    false_positive_like_removed_indices: list[int] = []
    review_required_indices: list[int] = []

    for result in results:
        routing_counts[result.final.routing_decision] = (
            routing_counts.get(result.final.routing_decision, 0) + 1
        )
        final_decision_counts[result.final.final_migration_decision] += 1
        decision_source_counts[result.final.decision_source] = (
            decision_source_counts.get(result.final.decision_source, 0) + 1
        )

        if result.final.semantic is not None:
            semantic_called_count += 1

        # A "false-positive-like case removed": the FIRST (deterministic)
        # layer had already marked this candidate_eligible=True, but the
        # secondary layer's final decision did not confirm INCLUDE --
        # i.e. the second opinion caught something the first layer
        # missed (see facebook_history_semantic_provider's
        # currency-conversion-note heuristic for the canonical example).
        deterministic = result.first_layer.classification
        if deterministic.candidate_eligible and result.final.final_migration_decision != "INCLUDE":
            false_positive_like_removed_count += 1
            false_positive_like_removed_indices.append(result.first_layer.record.record_index)

        if result.final.final_migration_decision == "REVIEW_REQUIRED":
            review_required_indices.append(result.first_layer.record.record_index)

    return {
        "source_file": str(source_file) if source_file is not None else None,
        "high_confidence_threshold": high_confidence_threshold,
        "total_records": total_records,
        "routing_decision_counts": dict(sorted(routing_counts.items())),
        "final_decision_counts": final_decision_counts,
        "decision_source_counts": dict(sorted(decision_source_counts.items())),
        "secondary_classifier_called_count": semantic_called_count,
        "false_positive_like_removed_count": false_positive_like_removed_count,
        "false_positive_like_removed_record_indices": false_positive_like_removed_indices,
        "review_required_count": len(review_required_indices),
        "review_required_record_indices": review_required_indices,
    }


def write_secondary_summary_json(summary: dict[str, Any], path: str | Path) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as json_file:
        json.dump(summary, json_file, ensure_ascii=False, indent=2, sort_keys=False)
        json_file.write("\n")
