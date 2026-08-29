"""Offline tests for src/services/facebook_history_report.py.

No live Supabase/WooCommerce/Facebook/Claude dependency -- everything
here operates on in-memory HistoryRecord values and a tmp_path output
directory.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from src.domain.rules.facebook_history_classification import (
    FEEDBACK_FOLDER_SLUG,
    PostType,
    STRONG_LISTING_FOLDER_SLUG,
    TsycRelevance,
)
from src.services.facebook_history_parser import HistoryRecord
from src.services.facebook_history_report import (
    build_summary,
    classify_records,
    write_classification_csv,
    write_summary_json,
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


def _sample_records() -> list[HistoryRecord]:
    return [
        _record(record_index=1),
        _record(
            record_index=2,
            date_text="Tháng 12 02, 2024 10:15:35 sáng",
            full_text="Em biết ơn khách hàng đã luôn tin tưởng và ủng hộ.",
            folder_slugs=(FEEDBACK_FOLDER_SLUG,),
        ),
        _record(
            record_index=3,
            date_text="Tháng 6 23, 2026 1:30:08 ch",
            full_text="Hội những người siêu tích cực va vào nhau 😆😆😆",
            folder_slugs=(),
        ),
    ]


def test_classify_records_produces_one_result_per_record():
    records = _sample_records()
    classified = classify_records(records)

    assert len(classified) == 3
    assert classified[0].classification.tsyc_relevance == TsycRelevance.HIGH
    assert classified[0].classification.post_type == PostType.PRODUCT_POST
    assert classified[1].classification.post_type == PostType.CUSTOMER_FEEDBACK
    assert classified[2].classification.tsyc_relevance == TsycRelevance.LOW


def test_write_classification_csv_round_trips_all_columns(tmp_path: Path):
    classified = classify_records(_sample_records())
    csv_path = tmp_path / "out.csv"

    write_classification_csv(classified, csv_path)

    with csv_path.open("r", encoding="utf-8", newline="") as csv_file:
        rows = list(csv.DictReader(csv_file))

    assert len(rows) == 3
    assert rows[0]["record_index"] == "1"
    assert rows[0]["tsyc_relevance"] == "HIGH"
    assert rows[0]["post_type"] == "PRODUCT_POST"
    assert rows[0]["candidate_eligible"] == "True"
    assert rows[0]["folder_slug_evidence"] == STRONG_LISTING_FOLDER_SLUG
    assert rows[1]["post_type"] == "CUSTOMER_FEEDBACK"
    assert rows[1]["candidate_eligible"] == "False"


def test_build_summary_counts_and_year_bucketing():
    classified = classify_records(_sample_records())

    summary = build_summary(classified, source_file="fake.html")

    assert summary["total_records"] == 3
    assert summary["tsyc_relevance_counts"]["HIGH"] == 2
    assert summary["tsyc_relevance_counts"]["LOW"] == 1
    assert summary["candidate_eligible_count"] == 1
    assert summary["candidate_eligible_by_year"] == {"2025": 1}
    assert summary["post_type_counts"]["CUSTOMER_FEEDBACK"] == 1
    assert isinstance(summary["top_classification_reasons"], list)
    assert summary["top_classification_reasons"][0]["count"] >= 1
    assert summary["secondary_review_count"] >= 0
    assert summary["source_file"] == "fake.html"


def test_write_summary_json_is_valid_json_and_matches_build_summary(tmp_path: Path):
    classified = classify_records(_sample_records())
    summary = build_summary(classified, source_file="fake.html")
    summary_path = tmp_path / "summary.json"

    write_summary_json(summary, summary_path)

    with summary_path.open("r", encoding="utf-8") as summary_file:
        loaded = json.load(summary_file)

    assert loaded == summary


# --- idempotency: repeated pipeline runs produce byte-identical output ----


def test_full_pipeline_is_idempotent_across_repeated_runs(tmp_path: Path):
    records = _sample_records()

    csv_path_1 = tmp_path / "run1.csv"
    csv_path_2 = tmp_path / "run2.csv"
    summary_path_1 = tmp_path / "run1_summary.json"
    summary_path_2 = tmp_path / "run2_summary.json"

    classified_1 = classify_records(records)
    classified_2 = classify_records(records)

    write_classification_csv(classified_1, csv_path_1)
    write_classification_csv(classified_2, csv_path_2)

    summary_1 = build_summary(classified_1, source_file="fake.html")
    summary_2 = build_summary(classified_2, source_file="fake.html")
    write_summary_json(summary_1, summary_path_1)
    write_summary_json(summary_2, summary_path_2)

    assert csv_path_1.read_bytes() == csv_path_2.read_bytes()
    assert summary_path_1.read_bytes() == summary_path_2.read_bytes()
