"""Offline tests for
src/services/facebook_history_secondary_classification.py.

No live Supabase/WooCommerce/Facebook/Claude dependency -- everything
here operates on in-memory HistoryRecord values, a spy classifier that
counts its own calls (to prove bypassed records never reach a provider),
and a tmp_path output directory.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from src.domain.rules.facebook_history_classification import (
    FEEDBACK_FOLDER_SLUG,
    STRONG_LISTING_FOLDER_SLUG,
)
from src.domain.rules.facebook_history_semantic import (
    HistoricalPostSemanticClassifier,
    RoutingDecision,
    SemanticClassificationInput,
    SemanticClassificationResult,
)
from src.services.facebook_history_parser import HistoryRecord
from src.services.facebook_history_report import classify_records
from src.services.facebook_history_secondary_classification import (
    build_secondary_summary,
    run_secondary_classification,
    write_secondary_csv,
    write_secondary_summary_json,
)
from src.services.facebook_history_semantic_provider import MockHistoricalSemanticProvider


class _CountingClassifier(HistoricalPostSemanticClassifier):
    """A spy that counts and records every call it receives, delegating
    to MockHistoricalSemanticProvider for the actual answer -- used to
    prove that bypassed (LOW / strong-eligible / confidently-excluded)
    records never reach the classifier at all."""

    def __init__(self) -> None:
        self.call_count = 0
        self.seen_record_ids: list[int] = []
        self._delegate = MockHistoricalSemanticProvider()

    def classify(self, request: SemanticClassificationInput) -> SemanticClassificationResult:
        self.call_count += 1
        self.seen_record_ids.append(request.record_id)
        return self._delegate.classify(request)


class _NetworkAttemptClassifier(HistoricalPostSemanticClassifier):
    """A classifier that would fail loudly if ever invoked -- used to
    prove routing genuinely never calls the classifier for bypassed
    records (a stronger guarantee than merely counting calls)."""

    def classify(self, request: SemanticClassificationInput) -> SemanticClassificationResult:
        raise AssertionError(
            f"Classifier was called for record {request.record_id}, which "
            "should have been bypassed by routing."
        )


def _record(**overrides) -> HistoryRecord:
    defaults = dict(
        record_index=1,
        date_text="Tháng 5 03, 2025 10:16:09 ch",
        heading="Tâm Võ đã thêm 3 ảnh mới.",
        full_text="Sách Có Sẵn tại Đức",
        text_preview="Sách Có Sẵn tại Đức",
        external_links=(),
        local_image_paths=(),
        local_video_paths=(),
        folder_slugs=(STRONG_LISTING_FOLDER_SLUG,),
        mention_ids=(),
        mention_names=(),
    )
    defaults.update(overrides)
    return HistoryRecord(**defaults)


def _mixed_records() -> list[HistoryRecord]:
    return [
        # LOW -> must bypass.
        _record(
            record_index=1,
            full_text="Hội những người siêu tích cực va vào nhau 😆😆😆",
            folder_slugs=(),
        ),
        # HIGH + eligible PRODUCT_POST -> must bypass (strong include).
        _record(record_index=2, full_text="Sách Có Sẵn tại Đức", folder_slugs=(STRONG_LISTING_FOLDER_SLUG,)),
        # Plain feedback, no extra evidence -> must bypass (confident exclude).
        _record(
            record_index=3,
            full_text="Cảm ơn chị nhiều lắm ạ",
            folder_slugs=(FEEDBACK_FOLDER_SLUG,),
        ),
        # Genuinely ambiguous MEDIUM -> must be sent to the classifier.
        _record(
            record_index=4,
            full_text="Thanh lý sách cũ giá 8€ một cuốn, ai cần inbox em nhé",
            folder_slugs=(),
        ),
        # Feedback WITH solicitation language -> must be sent too.
        _record(
            record_index=5,
            full_text="mn cần đặt sách mới cứ nhắn cho Tiệm sách Yêu Con nhé",
            folder_slugs=(FEEDBACK_FOLDER_SLUG,),
        ),
    ]


# --- routing bypass guarantees ----------------------------------------------


def test_low_and_strong_records_never_reach_the_classifier():
    classified_records = classify_records(_mixed_records())
    classifier = _CountingClassifier()

    results = run_secondary_classification(classified_records, classifier)

    assert classifier.call_count == 2  # only record #4 and #5
    assert sorted(classifier.seen_record_ids) == [4, 5]

    by_id = {r.first_layer.record.record_index: r for r in results}
    assert by_id[1].final.routing_decision == RoutingDecision.SKIP_LOW
    assert by_id[2].final.routing_decision == RoutingDecision.BYPASS_STRONG_INCLUDE
    assert by_id[3].final.routing_decision == RoutingDecision.BYPASS_CONFIDENT_EXCLUDE
    assert by_id[4].final.routing_decision == RoutingDecision.SEND_TO_SEMANTIC
    assert by_id[5].final.routing_decision == RoutingDecision.SEND_TO_SEMANTIC


def test_bypassed_records_use_a_classifier_that_would_raise_if_called():
    bypassed_only = [
        _record(record_index=1, full_text="Hội những người siêu tích cực", folder_slugs=()),
        _record(record_index=2, full_text="Sách Có Sẵn tại Đức", folder_slugs=(STRONG_LISTING_FOLDER_SLUG,)),
        _record(record_index=3, full_text="Cảm ơn chị nhiều lắm ạ", folder_slugs=(FEEDBACK_FOLDER_SLUG,)),
    ]
    classified_records = classify_records(bypassed_only)

    # Must not raise -- proves the classifier is never invoked.
    results = run_secondary_classification(classified_records, _NetworkAttemptClassifier())

    assert len(results) == 3
    assert all(r.final.semantic is None for r in results)


# --- final decisions land where expected -----------------------------------


def test_end_to_end_final_decisions_with_mock_provider():
    classified_records = classify_records(_mixed_records())
    results = run_secondary_classification(classified_records, MockHistoricalSemanticProvider())

    by_id = {r.first_layer.record.record_index: r for r in results}
    assert by_id[1].final.final_migration_decision == "EXCLUDE"
    assert by_id[2].final.final_migration_decision == "INCLUDE"
    assert by_id[3].final.final_migration_decision == "EXCLUDE"
    assert by_id[4].final.final_migration_decision == "INCLUDE"
    assert by_id[5].final.final_migration_decision == "EXCLUDE"


# --- provenance: original deterministic fields are preserved ---------------


def test_original_deterministic_result_is_preserved_verbatim():
    classified_records = classify_records(_mixed_records())
    results = run_secondary_classification(classified_records, MockHistoricalSemanticProvider())

    for classified, result in zip(classified_records, results):
        assert result.first_layer is classified
        assert result.final.deterministic is classified.classification
        assert result.first_layer.classification.classification_reason == (
            classified.classification.classification_reason
        )


def test_csv_row_includes_both_deterministic_and_semantic_columns(tmp_path: Path):
    classified_records = classify_records(_mixed_records())
    results = run_secondary_classification(classified_records, MockHistoricalSemanticProvider())
    csv_path = tmp_path / "secondary.csv"

    write_secondary_csv(results, csv_path)

    with csv_path.open("r", encoding="utf-8", newline="") as csv_file:
        rows = {int(row["record_index"]): row for row in csv.DictReader(csv_file)}

    assert len(rows) == 5

    # Record 2: bypassed strong-include -- deterministic fields present,
    # semantic columns empty/false.
    row2 = rows[2]
    assert row2["deterministic_tsyc_relevance"] == "HIGH"
    assert row2["deterministic_candidate_eligible"] == "True"
    assert row2["routing_decision"] == RoutingDecision.BYPASS_STRONG_INCLUDE
    assert row2["semantic_called"] == "False"
    assert row2["final_migration_decision"] == "INCLUDE"

    # Record 4: sent to semantic -- both deterministic AND semantic
    # columns populated, deterministic untouched.
    row4 = rows[4]
    assert row4["deterministic_tsyc_relevance"] == "MEDIUM"
    assert row4["deterministic_candidate_eligible"] == "True"
    assert row4["semantic_called"] == "True"
    assert row4["semantic_product_migration_relevant"] == "True"
    assert row4["final_migration_decision"] == "INCLUDE"


# --- summary -----------------------------------------------------------------


def test_build_secondary_summary_counts():
    classified_records = classify_records(_mixed_records())
    results = run_secondary_classification(classified_records, MockHistoricalSemanticProvider())

    summary = build_secondary_summary(results, source_file="fake.html")

    assert summary["total_records"] == 5
    assert summary["secondary_classifier_called_count"] == 2
    assert summary["final_decision_counts"]["INCLUDE"] == 2
    assert summary["final_decision_counts"]["EXCLUDE"] == 3
    assert summary["routing_decision_counts"][RoutingDecision.SKIP_LOW] == 1
    assert summary["routing_decision_counts"][RoutingDecision.BYPASS_STRONG_INCLUDE] == 1
    assert summary["routing_decision_counts"][RoutingDecision.BYPASS_CONFIDENT_EXCLUDE] == 1
    assert summary["routing_decision_counts"][RoutingDecision.SEND_TO_SEMANTIC] == 2
    assert summary["source_file"] == "fake.html"


def test_false_positive_like_removal_is_tracked():
    # Record 4 was deterministic-eligible (candidate_eligible=True) but
    # this specific text is a genuine book sale, so it stays INCLUDEd --
    # not a removal. Use a currency-conversion-note record instead, which
    # the deterministic layer marks eligible=True but the semantic layer
    # correctly disqualifies.
    records = [
        _record(
            record_index=1,
            full_text=(
                "Bác nào gửi tiền về Việt Nam thì dùng Tap Tap Send được nè. "
                "Tỉ giá giờ quá đẹp 1€=30000vnd. Tiền thu được em dồn vào "
                "mua sách thư viện nha."
            ),
            folder_slugs=(),
        ),
    ]
    classified_records = classify_records(records)
    assert classified_records[0].classification.candidate_eligible is True

    results = run_secondary_classification(classified_records, MockHistoricalSemanticProvider())

    assert results[0].final.final_migration_decision == "EXCLUDE"

    summary = build_secondary_summary(results)
    assert summary["false_positive_like_removed_count"] == 1
    assert summary["false_positive_like_removed_record_indices"] == [1]


# --- idempotency -------------------------------------------------------------


def test_full_secondary_pipeline_is_idempotent_across_repeated_runs(tmp_path: Path):
    classified_records = classify_records(_mixed_records())

    results_1 = run_secondary_classification(classified_records, MockHistoricalSemanticProvider())
    results_2 = run_secondary_classification(classified_records, MockHistoricalSemanticProvider())

    csv_path_1 = tmp_path / "run1.csv"
    csv_path_2 = tmp_path / "run2.csv"
    summary_path_1 = tmp_path / "run1_summary.json"
    summary_path_2 = tmp_path / "run2_summary.json"

    write_secondary_csv(results_1, csv_path_1)
    write_secondary_csv(results_2, csv_path_2)

    summary_1 = build_secondary_summary(results_1, source_file="fake.html")
    summary_2 = build_secondary_summary(results_2, source_file="fake.html")
    write_secondary_summary_json(summary_1, summary_path_1)
    write_secondary_summary_json(summary_2, summary_path_2)

    assert csv_path_1.read_bytes() == csv_path_2.read_bytes()
    assert summary_path_1.read_bytes() == summary_path_2.read_bytes()


def test_write_secondary_summary_json_round_trips(tmp_path: Path):
    classified_records = classify_records(_mixed_records())
    results = run_secondary_classification(classified_records, MockHistoricalSemanticProvider())
    summary = build_secondary_summary(results)
    summary_path = tmp_path / "summary.json"

    write_secondary_summary_json(summary, summary_path)

    with summary_path.open("r", encoding="utf-8") as summary_file:
        loaded = json.load(summary_file)

    assert loaded == summary
