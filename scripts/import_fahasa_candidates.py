"""
Create live-pipeline candidates for NEW_CONFIRMED Fahasa discoveries.

Pipeline position: after scripts/discover_fahasa_new_books.py (read-only)
and before the existing stages. For each EXACT allowlisted discovery
record it:

  1. re-classifies the listing against a FRESH unified identity index
     (every Woo status incl. trash, Woo sync records incl. owner-removed
     products, internal products, candidates, MATCH references, the
     Facebook export) -- only NEW_CONFIRMED with at least MODERATE identity
     and a SINGLE_BOOK sellable unit is created; everything else is
     isolated and reported;
  2. registers the Fahasa product page as an authorized FAHASA source_url
     (crawl PENDING) -- the candidate's origin provenance;
  3. inserts the product_candidates row (FAHASA-<batch>-CAN-NNNN, live
     pipeline; IDENTITY_PENDING) with the discovery evidence, the
     persisted Fahasa-cover visual review and supply status
     PREORDER_PENDING_SUPPLY_CONFIRMATION;
  4. registers the same page as the candidate's selected reference through
     the existing writer register_reference_source.py.

The existing stages then run unchanged (collect -> AUTO identity match ->
internal product -> image -> content -> readiness). Nothing here writes
identity, content, images, prices or WooCommerce. Idempotent: a candidate
already created from the same Fahasa URL in the batch is never duplicated.
Dry run unless --execute.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from dotenv import load_dotenv  # noqa: E402

from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain.candidate_origin import (  # noqa: E402
    FAHASA_DISCOVERY_CODE_PREFIX,
    FAHASA_DISCOVERY_ORIGIN,
)
from src.domain.rules import catalog_identity_index as idx  # noqa: E402

IMPORTER_NAME = "fahasa_candidate_importer"
IMPORTER_VERSION = "1.0.0"
DEFAULT_BATCH_CODE = f"{FAHASA_DISCOVERY_CODE_PREFIX}2026-001"
MAX_CANDIDATES_HARD_CAP = 20
SUPPLY_STATUS = "PREORDER_PENDING_SUPPLY_CONFIRMATION"
PYTHON_EXE = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
_ACCEPTED_IDENTITY = frozenset({idx.IDENTITY_STRONG, idx.IDENTITY_MODERATE})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create live candidates for NEW_CONFIRMED Fahasa discoveries.")
    parser.add_argument("--discovery-report", type=Path, required=True)
    parser.add_argument(
        "--cover-reviews",
        type=Path,
        required=True,
        help="JSON list of persisted Fahasa-cover visual reviews ({discovery_index, image_url, price_label_visible, ...}).",
    )
    parser.add_argument("--discovery-index", type=int, action="append", required=True, help="Exact record index (1-based). Repeat.")
    parser.add_argument("--max-candidates", type=int, required=True)
    parser.add_argument("--batch-code", default=DEFAULT_BATCH_CODE)
    parser.add_argument("--execute", action="store_true", help="Perform writes. Default is a dry run.")
    args = parser.parse_args(argv)
    if not args.batch_code.startswith(FAHASA_DISCOVERY_CODE_PREFIX):
        parser.error(f"--batch-code must start with {FAHASA_DISCOVERY_CODE_PREFIX}.")
    if not 1 <= args.max_candidates <= MAX_CANDIDATES_HARD_CAP:
        parser.error(f"--max-candidates must be between 1 and {MAX_CANDIDATES_HARD_CAP}.")
    if len(set(args.discovery_index)) != len(args.discovery_index):
        parser.error("--discovery-index values must be unique.")
    if len(args.discovery_index) > args.max_candidates:
        parser.error("More --discovery-index values than --max-candidates.")
    return args


def eligibility_problems(classification: dict[str, Any]) -> list[str]:
    """Why a freshly re-classified listing may NOT become a candidate."""
    problems = []
    if classification["classification"] != idx.NEW_CONFIRMED:
        problems.append(f"{classification['classification']}: {classification['reason']}")
    if classification["identity_strength"] not in _ACCEPTED_IDENTITY:
        problems.append(f"identity strength {classification['identity_strength']}")
    if classification["sellable_unit"] != "SINGLE_BOOK":
        problems.append(f"sellable unit {classification['sellable_unit']} needs a business decision")
    return problems


def validate_cover_review(review: dict[str, Any] | None, record: dict[str, Any]) -> list[str]:
    """A cover review must exist for exactly this listing's cover URL and
    carry explicit booleans -- never inferred."""
    if not review:
        return ["no persisted Fahasa-cover visual review"]
    problems = []
    if review.get("image_url") != (record.get("image") or {}).get("url"):
        problems.append("cover review is for a different image URL")
    for key in ("price_label_visible", "misleading_transformation", "depicts_candidate"):
        if not isinstance(review.get(key), bool):
            problems.append(f"cover review field {key} is not an explicit boolean")
    return problems


def build_source_evidence(
    record: dict[str, Any],
    classification: dict[str, Any],
    review: dict[str, Any],
    report_path: Path,
    discovery_index: int,
    coverage_note: str | None,
    index_counts: dict[str, Any],
) -> dict[str, Any]:
    return {
        "origin": FAHASA_DISCOVERY_ORIGIN,
        "importer_name": IMPORTER_NAME,
        "importer_version": IMPORTER_VERSION,
        "source_type": "FAHASA",
        "source_url": record["source_url"],
        "first_seen_at": record.get("first_seen_at"),
        "discovery_report": report_path.name,
        "discovery_index": discovery_index,
        "listing": {
            "title": record.get("title"),
            "author": record.get("author"),
            "translator": record.get("translator"),
            "publisher": record.get("publisher"),
            "displayed_supplier": record.get("displayed_supplier"),
            "book_format": record.get("book_format"),
            "publication_year": record.get("publication_year"),
            "page_count": record.get("page_count"),
            "isbn": record.get("isbn"),
            "isbn_raw_value": record.get("isbn_raw_value"),
        },
        "duplicate_check": {
            "classification": classification["classification"],
            "reason": classification["reason"],
            "identity_strength": classification["identity_strength"],
            "sellable_unit": classification["sellable_unit"],
            "index_rules_version": idx.INDEX_RULES_VERSION,
            "index_counts": index_counts,
            "facebook_coverage_note": coverage_note,
            "checked_at": utc_now(),
        },
        "fahasa_cover_review": review,
        # Never inferred from Fahasa availability (CLAUDE.md 8.2/8.3).
        "supply_status": SUPPLY_STATUS,
        "pricing_note": "No purchase or selling price recorded; Fahasa is never a TSYC price source.",
    }


def build_candidate_payload(
    batch_id: str,
    candidate_code: str,
    source_url_id: str,
    record: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "batch_id": batch_id,
        "candidate_code": candidate_code,
        "candidate_type": "SINGLE_BOOK",
        "combo_group_code": None,
        "raw_page_id": None,
        "source_url_id": source_url_id,
        "extracted_title": record["title"],
        "extracted_author": record.get("author"),
        "possible_isbn": record.get("isbn"),
        "workflow_status": "EXTRACTED",
        "extraction_confidence": 1.0,
        "source_evidence": evidence,
        "conflict_fields": [],
        "review_required": False,
        "decision_reason": (
            "Created from a NEW_CONFIRMED Fahasa discovery; identity is verified "
            "by the existing reference collection and matching stages."
        ),
        "extraction_method": "RULE_BASED",
        "extractor_name": IMPORTER_NAME,
        "extractor_version": IMPORTER_VERSION,
    }


def build_register_command(candidate_code: str, batch_code: str, source_url: str) -> list[str]:
    return [
        str(PYTHON_EXE),
        str(PROJECT_ROOT / "scripts" / "register_reference_source.py"),
        "--candidate-code", candidate_code,
        "--batch-code", batch_code,
        "--source-type", "FAHASA",
        "--source-name", "Fahasa",
        "--source-url", source_url,
        "--discovery-method", "SITE_SEARCH",
        "--discovery-rank", "1",
        "--discovery-confidence", "1",
        "--selection-reason", "The Fahasa product page this candidate was discovered from (its origin listing).",
        "--authorized",
        "--select-for-crawl",
        "--confirm-register",
        "--non-interactive",
    ]


def next_candidate_code(existing_codes: list[str], batch_code: str) -> str:
    prefix = f"{batch_code}-CAN-"
    numbers = [int(code[len(prefix):]) for code in existing_codes if code.startswith(prefix) and code[len(prefix):].isdigit()]
    return f"{prefix}{max(numbers, default=0) + 1:04d}"


def run(
    args: argparse.Namespace,
    repository: Any,
    entries: list[idx.IndexEntry],
    posts: list[idx.IndexEntry] | None,
    coverage_note: str | None,
    index_counts: dict[str, Any],
    runner: Callable[[list[str]], subprocess.CompletedProcess],
) -> list[dict[str, Any]]:
    import discover_fahasa_new_books as discovery

    report = json.loads(args.discovery_report.read_text(encoding="utf-8"))
    reviews = {
        int(review["discovery_index"]): {k: v for k, v in review.items() if k != "discovery_index"}
        for review in json.loads(args.cover_reviews.read_text(encoding="utf-8"))
    }
    records = report["records"]
    results: list[dict[str, Any]] = []

    batch = repository.get_batch_by_code(args.batch_code)
    if batch is None and args.execute:
        batch = repository.create_batch(
            batch_code=args.batch_code,
            batch_name="Fahasa new-book discovery",
            description="Live-pipeline candidates created from NEW_CONFIRMED Fahasa discoveries (CLAUDE.md 14.8).",
        )

    for discovery_index in args.discovery_index:
        if not 1 <= discovery_index <= len(records):
            results.append({"discovery_index": discovery_index, "status": "ISOLATED", "reason": "index out of range"})
            continue
        record = records[discovery_index - 1]
        source_url = record["source_url"]

        existing = None
        if batch is not None:
            existing = next(
                iter(
                    repository.client.table("product_candidates")
                    .select("candidate_id, candidate_code, source_evidence")
                    .eq("batch_id", batch["batch_id"])
                    .contains("source_evidence", {"source_url": source_url})
                    .execute()
                    .data
                    or []
                ),
                None,
            )
        if existing is not None:
            results.append({"discovery_index": discovery_index, "title": record["title"], "status": "ALREADY_EXISTED", "candidate_code": existing["candidate_code"]})
            continue

        classification = discovery.classify_record(record, entries, posts, coverage_note)
        problems = eligibility_problems(classification) + validate_cover_review(reviews.get(discovery_index), record)
        if problems:
            results.append({"discovery_index": discovery_index, "title": record["title"], "status": "ISOLATED", "reason": "; ".join(problems)})
            continue

        if not args.execute:
            results.append({"discovery_index": discovery_index, "title": record["title"], "status": "WOULD_CREATE"})
            continue

        source_rows = (
            repository.client.table("source_urls")
            .select("source_url_id, is_authorized, crawl_status")
            .eq("batch_id", batch["batch_id"]).eq("source_type", "FAHASA").eq("source_url", source_url)
            .limit(1).execute().data
            or []
        )
        source_row = source_rows[0] if source_rows else repository.save_source_url(
            batch_id=batch["batch_id"], source_url=source_url, selection_reason="Fahasa", active=True, source_type="FAHASA",
        )

        codes = [
            row["candidate_code"]
            for row in repository.client.table("product_candidates").select("candidate_code").eq("batch_id", batch["batch_id"]).execute().data or []
        ]
        candidate_code = next_candidate_code(codes, args.batch_code)
        evidence = build_source_evidence(
            record, classification, reviews[discovery_index], args.discovery_report, discovery_index, coverage_note, index_counts
        )
        created = repository.client.table("product_candidates").insert(
            build_candidate_payload(batch["batch_id"], candidate_code, source_row["source_url_id"], record, evidence)
        ).execute().data
        if not created:
            raise RuntimeError(f"product_candidates insert returned no data for {source_url}.")
        candidate = created[0]
        repository.write_process_log(
            message=f"Fahasa candidate {candidate_code} created from NEW_CONFIRMED discovery {source_url}.",
            process_name=IMPORTER_NAME,
            batch_id=batch["batch_id"],
            candidate_id=candidate["candidate_id"],
            process_step="IMPORT_CANDIDATE",
            log_level="INFO",
            status="SUCCESS",
        )

        completed = runner(build_register_command(candidate_code, args.batch_code, source_url))
        if completed.returncode != 0:
            # Candidate exists; reference registration failed deterministically
            # locally -- reported, never retried blindly.
            results.append({
                "discovery_index": discovery_index, "title": record["title"], "status": "CREATED_REFERENCE_FAILED",
                "candidate_code": candidate_code, "reason": (completed.stdout or "")[-500:] + (completed.stderr or "")[-500:],
            })
            continue
        results.append({"discovery_index": discovery_index, "title": record["title"], "status": "CREATED", "candidate_code": candidate_code})

    return results


def _subprocess_runner(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)


def main() -> None:
    configure_utf8_console()
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_arguments()

    import discover_fahasa_new_books as discovery
    from src.repositories.supabase_repository import SupabaseRepository

    repository = SupabaseRepository()
    woo_entries = discovery.load_woo_entries()
    database_entries = discovery.load_database_entries(repository)
    posts, coverage_note = discovery.load_facebook_posts(discovery.FACEBOOK_EXPORT_CSV)
    index_counts = {
        "woo_products": len(woo_entries),
        "database_records": len(database_entries),
        "facebook_posts": None if posts is None else len(posts),
    }
    print(f"FAHASA CANDIDATE IMPORT v{IMPORTER_VERSION} -- {'EXECUTE' if args.execute else 'DRY RUN'} -- batch {args.batch_code}")
    print(f"Fresh index: {index_counts}")

    results = run(args, repository, woo_entries + database_entries, posts, coverage_note, index_counts, _subprocess_runner)
    for result in results:
        print(f"[{result['discovery_index']:>2}] {result['status']:<26} {result.get('candidate_code') or '':<28} {result.get('title') or ''}")
        if result.get("reason"):
            print(f"      {result['reason']}")
    if any(result["status"] == "CREATED_REFERENCE_FAILED" for result in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
