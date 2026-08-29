"""
OFFLINE SECONDARY (semantic) historical Facebook migration classification
layer.

Runs the existing deterministic classifier (scripts/classify_facebook_
history_export.py's own logic, reused as a library here) and then, only
for records the deterministic layer flagged as genuinely ambiguous
(needs_secondary_review=True), asks a semantic classifier for a second
opinion before synthesizing one final, fully-provenanced migration
decision per record.

Provider selection is explicit and cost-safe by construction:

    --provider mock    (DEFAULT) MockHistoricalSemanticProvider --
                       deterministic, heuristic, zero network calls.
    --provider claude  ClaudeHistoricalSemanticProvider -- a real,
                       live Anthropic API call for every record routed
                       to the semantic layer (subject to local response
                       caching -- see src/services/facebook_history_
                       semantic_cache.py). Requires ANTHROPIC_API_KEY;
                       fails clearly and safely if it is not set, never
                       silently falling back to the mock provider.

This script never makes a live Claude call unless --provider claude is
passed explicitly. It does NOT:
    - write to Supabase
    - create source_urls rows
    - create raw_pages rows
    - create product_candidates rows
    - call WooCommerce
    - publish anything or change a selling price
    - modify any existing production candidate, product, or content row

It only reads one local HTML file and writes two local report files:
    data/processed/facebook_history_secondary_classification.csv
    data/processed/facebook_history_secondary_summary.json
(skipped entirely in --dry-run mode -- see below).

A final_migration_decision=INCLUDE row is a screening signal for a human
(or a later, explicitly-approved import stage) -- never itself a
product_candidates row, and this script never creates one.

Cost/control flags:
    --max-records N     Process at most the first N records (document
                         order) end to end. Ignored if --record-id is
                         given.
    --record-id ID       Process only this exact record_index. Repeat
                         the flag for more than one (e.g. a 1-3 record
                         live smoke test). Overrides --max-records.
    --dry-run            Parse, deterministically classify, and route
                         only -- print how many records WOULD be sent to
                         the semantic provider and which ones, then stop
                         without calling any provider (mock or claude)
                         and without writing the output files. Use this
                         to sanity-check scope/cost before spending a
                         live API budget.

Usage:
    .venv/Scripts/python.exe scripts/classify_facebook_history_secondary.py
    .venv/Scripts/python.exe scripts/classify_facebook_history_secondary.py \\
        --provider claude --record-id 1354 --record-id 719 --dry-run
    .venv/Scripts/python.exe scripts/classify_facebook_history_secondary.py \\
        --provider claude --record-id 1354 --record-id 719
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.cli_bootstrap import configure_utf8_console
from src.domain.rules.facebook_history_semantic import HistoricalPostSemanticClassifier, RoutingDecision, route_record
from src.services.facebook_history_parser import HistoryRecord, load_facebook_history_export
from src.services.facebook_history_report import ClassifiedRecord, classify_records
from src.services.facebook_history_secondary_classification import (
    SecondaryClassifiedRecord,
    build_secondary_summary,
    run_secondary_classification,
    write_secondary_csv,
    write_secondary_summary_json,
)
from src.services.facebook_history_semantic_provider import (
    ClaudeHistoricalSemanticProvider,
    ClaudeProviderConfigurationError,
    MockHistoricalSemanticProvider,
)

configure_utf8_console()

SCRIPT_VERSION = "1.1.0"

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
CSV_FILENAME = "facebook_history_secondary_classification.csv"
SUMMARY_FILENAME = "facebook_history_secondary_summary.json"

SAMPLE_COUNT = 10


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "OFFLINE BY DEFAULT: run the deterministic + secondary "
            "(semantic) historical Facebook classification layers "
            "together and synthesize a final migration decision per "
            "record. Never writes to Supabase/WooCommerce. Never calls "
            "a live LLM unless --provider claude is passed explicitly."
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
    parser.add_argument(
        "--provider",
        choices=("mock", "claude"),
        default="mock",
        help="Semantic provider to use for ambiguous records (default: "
        "mock -- zero network calls). 'claude' makes real, billed "
        "Anthropic API calls for every routed record not already cached "
        "and requires ANTHROPIC_API_KEY.",
    )
    parser.add_argument(
        "--max-records",
        type=int,
        default=None,
        help="Process at most the first N records (document order). "
        "Ignored if --record-id is given.",
    )
    parser.add_argument(
        "--record-id",
        type=int,
        action="append",
        default=None,
        dest="record_ids",
        help="Process only this exact record_index. Repeatable. "
        "Overrides --max-records.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse, deterministically classify, and route only -- "
        "print how many/which records would be sent to the semantic "
        "provider, then stop without calling any provider or writing "
        "output files.",
    )
    return parser.parse_args()


def _select_records(records: list[HistoryRecord], args: argparse.Namespace) -> list[HistoryRecord]:
    if args.record_ids:
        wanted = list(dict.fromkeys(args.record_ids))  # de-dup, preserve order given
        by_index = {record.record_index: record for record in records}
        missing = [record_id for record_id in wanted if record_id not in by_index]
        if missing:
            raise SystemExit(f"ERROR: --record-id not found in export: {missing}")
        # Keep document order, not flag order, for a stable, readable report.
        wanted_set = set(wanted)
        return [record for record in records if record.record_index in wanted_set]

    if args.max_records is not None:
        return records[: args.max_records]

    return records


def _build_provider(args: argparse.Namespace) -> HistoricalPostSemanticClassifier:
    if args.provider == "claude":
        return ClaudeHistoricalSemanticProvider()
    return MockHistoricalSemanticProvider()


def _print_row(row: SecondaryClassifiedRecord) -> None:
    record = row.first_layer.record
    deterministic = row.first_layer.classification
    final = row.final
    print(f"  #{record.record_index} | {record.date_text or '(no date)'}")
    print(f"    text: {record.text_preview!r}")
    print(
        f"    deterministic: relevance={deterministic.tsyc_relevance} "
        f"post_type={deterministic.post_type} eligible={deterministic.candidate_eligible}"
    )
    print(f"    routing: {final.routing_decision}")
    if final.semantic is not None:
        print(
            f"    semantic: relevant={final.semantic.product_migration_relevant} "
            f"confidence={final.semantic.confidence:.2f} "
            f"reasons={list(final.semantic.reason_codes)}"
        )
    print(f"    FINAL: {final.final_migration_decision} (source={final.decision_source})")
    print()


def _print_dry_run_report(classified_records: list[ClassifiedRecord]) -> None:
    routing_counts: dict[str, int] = {}
    send_to_semantic: list[ClassifiedRecord] = []

    for classified in classified_records:
        decision = route_record(classified.classification)
        routing_counts[decision] = routing_counts.get(decision, 0) + 1
        if decision == RoutingDecision.SEND_TO_SEMANTIC:
            send_to_semantic.append(classified)

    print("=== DRY RUN -- no provider was called, no files were written ===")
    print(f"TOTAL RECORDS CONSIDERED: {len(classified_records)}")
    print(f"routing_decision_counts: {routing_counts}")
    print(f"Would call the semantic provider for {len(send_to_semantic)} record(s):")
    for classified in send_to_semantic:
        record = classified.record
        print(f"  #{record.record_index} | {record.date_text or '(no date)'} | {record.text_preview!r}")
    print()


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")  # never printed/logged; only populates os.environ

    args = parse_arguments()
    source_path = Path(args.source)
    out_dir = Path(args.out_dir)

    print(f"TSYC historical Facebook SECONDARY classification -- v{SCRIPT_VERSION}")
    print(f"Source export : {source_path}")
    print(f"Output dir    : {out_dir}")
    print(f"Provider      : {args.provider}" + ("  (no network calls)" if args.provider == "mock" else ""))
    if args.dry_run:
        print("Mode          : DRY RUN")
    print()

    if not source_path.is_file():
        print(f"ERROR: source export file not found: {source_path}")
        print()
        print("HISTORICAL_SECONDARY_LAYER_READY_FOR_LLM_INTEGRATION: NO")
        return 1

    records = load_facebook_history_export(source_path)

    try:
        selected_records = _select_records(records, args)
    except SystemExit as error:
        print(error)
        print()
        print("HISTORICAL_SECONDARY_LAYER_READY_FOR_LLM_INTEGRATION: NO")
        return 1

    print(f"Selected {len(selected_records)} of {len(records)} parsed record(s).")
    print()

    classified_records = classify_records(selected_records)

    if args.dry_run:
        _print_dry_run_report(classified_records)
        print("HISTORICAL_SECONDARY_LAYER_READY_FOR_LLM_INTEGRATION: YES")
        return 0

    try:
        provider = _build_provider(args)
    except ClaudeProviderConfigurationError as error:
        print(f"ERROR: {error}")
        print()
        print("HISTORICAL_SECONDARY_LAYER_READY_FOR_LLM_INTEGRATION: NO")
        return 1

    secondary_results = run_secondary_classification(classified_records, provider)

    csv_path = out_dir / CSV_FILENAME
    summary_path = out_dir / SUMMARY_FILENAME

    write_secondary_csv(secondary_results, csv_path)
    summary = build_secondary_summary(secondary_results, source_file=source_path)
    write_secondary_summary_json(summary, summary_path)

    print(f"Wrote {len(secondary_results)} record(s) to:")
    print(f"  {csv_path}")
    print(f"  {summary_path}")
    print()

    print("=== AGGREGATE COUNTS ===")
    print(f"TOTAL RECORDS: {summary['total_records']}")
    print(f"routing_decision_counts: {summary['routing_decision_counts']}")
    print(f"final_decision_counts: {summary['final_decision_counts']}")
    print(f"decision_source_counts: {summary['decision_source_counts']}")
    print(f"SECONDARY_CLASSIFIER_CALLED: {summary['secondary_classifier_called_count']}")
    print(f"FALSE_POSITIVE_LIKE_REMOVED: {summary['false_positive_like_removed_count']}")
    print(f"REVIEW_REQUIRED: {summary['review_required_count']}")
    print()

    bypass_low = [r for r in secondary_results if r.final.routing_decision == RoutingDecision.SKIP_LOW]
    bypass_strong = [
        r for r in secondary_results if r.final.routing_decision == RoutingDecision.BYPASS_STRONG_INCLUDE
    ]
    semantic_included = [
        r
        for r in secondary_results
        if r.final.semantic is not None and r.final.final_migration_decision == "INCLUDE"
    ]
    review_required = [
        r for r in secondary_results if r.final.final_migration_decision == "REVIEW_REQUIRED"
    ]
    removed = [
        r
        for r in secondary_results
        if r.first_layer.classification.candidate_eligible
        and r.final.final_migration_decision != "INCLUDE"
    ]

    def sample(rows: list[SecondaryClassifiedRecord], count: int) -> list[SecondaryClassifiedRecord]:
        if not rows:
            return []
        stride = max(1, len(rows) // count)
        return rows[::stride][:count]

    print(f"BYPASSED_LOW: {len(bypass_low)}")
    print(f"BYPASSED_STRONG: {len(bypass_strong)}")
    print()

    if args.record_ids or (args.max_records is not None and args.max_records <= 20):
        # A small, explicitly-bounded run (a smoke test) -- show every
        # selected record's full result rather than a sparse sample.
        print("=== SELECTED RECORD RESULTS ===")
        for row in secondary_results:
            _print_row(row)
    else:
        print(f"=== {SAMPLE_COUNT} SEMANTIC-INCLUDE SAMPLE ===")
        for row in sample(semantic_included, SAMPLE_COUNT):
            _print_row(row)

        print(f"=== {SAMPLE_COUNT} REVIEW_REQUIRED SAMPLE ===")
        for row in sample(review_required, SAMPLE_COUNT):
            _print_row(row)

        print(f"=== {SAMPLE_COUNT} FALSE-POSITIVE-LIKE REMOVED SAMPLE ===")
        for row in sample(removed, SAMPLE_COUNT):
            _print_row(row)

    print("HISTORICAL_SECONDARY_LAYER_READY_FOR_LLM_INTEGRATION: YES")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
