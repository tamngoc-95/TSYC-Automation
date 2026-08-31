"""
FIRST BOUNDED historical Facebook candidate import into Supabase.

Reads the already-validated OFFLINE final preview
(data/processed/facebook_history_candidate_final_preview.csv --
built and QA'd by build_facebook_history_candidate_final_preview.py)
and writes an EXPLICIT, hardcoded, bounded selection of AUTO_PASS
candidates into production Supabase tables:

    batches (get-or-create, batch_code=BATCH_CODE)
    source_urls (idempotent upsert per historical record)
    raw_pages (get-or-create per historical record, deduped by content_hash)
    product_candidates (get-or-create per (raw_page_id, normalized title,
        candidate_type) -- the historical idempotent import key)
    process_logs (one entry per successful/skipped/failed candidate)

This script performs NO extraction, NO classification, and NO gate
re-evaluation -- it only imports rows that already passed the offline
validate_and_gate() AUTO_PASS decision. It NEVER calls WooCommerce,
never sets a price field, never sets identity_status beyond the
schema default (IDENTITY_PENDING), and never marks review_required
false for a freshly imported row (identity/metadata verification is
a separate, later stage -- see CLAUDE.md Section 13).

Idempotency:
    Rerunning this exact script is a no-op for every candidate that
    was already imported: batches.create_batch is guarded by
    get_batch_by_code; source_urls uses the repository's own
    upsert-on-conflict; raw_pages is deduped by (batch_id,
    content_hash); product_candidates is deduped by
    (raw_page_id, normalized title, candidate_type) BEFORE any
    insert is attempted, independent of candidate_code assignment
    order.

Safety:
    Default mode is DRY RUN. Pass --execute to perform real writes.
    A dry run performs ONLY reads (get_batch_by_code, existing-row
    lookups) -- no insert/upsert call is made in dry-run mode.

Usage:
    .venv/Scripts/python.exe scripts/import_historical_facebook_candidates.py --dry-run
    .venv/Scripts/python.exe scripts/import_historical_facebook_candidates.py --execute
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.cli_bootstrap import configure_utf8_console
from src.domain.rules.historical_text_cleaner import clean_historical_facebook_text
from src.repositories.supabase_repository import SupabaseRepository
from src.services.facebook_history_parser import load_facebook_history_export
from src.services.historical_candidate_semantic_provider import (
    DEFAULT_MODEL,
    PROMPT_VERSION,
    PROVIDER_NAME,
    SCHEMA_VERSION,
)

configure_utf8_console()

SCRIPT_VERSION = "1.0.0"

# --- bounded, explicit identity -------------------------------------------

BATCH_CODE = "FB-HIST-2026-001"
BATCH_NAME = "Historical Facebook export -- first bounded import"
BATCH_DESCRIPTION = (
    "First bounded historical import of pre-validated (offline-gated) "
    "Facebook candidate-extraction results into production Supabase. "
    "Source: data/processed/facebook_history_candidate_final_preview.csv."
)
IMPORT_TYPE = "HISTORICAL_FACEBOOK_EXPORT"
IMPORTER_NAME = "historical_facebook_candidate_importer"

DEFAULT_SOURCE_EXPORT_LABEL = (
    "your_facebook_activity/posts/"
    "your_posts__check_ins__photos_and_videos_1.html"
)
DEFAULT_SOURCE_EXPORT = (
    PROJECT_ROOT
    / "data"
    / "raw"
    / "facebook_export_probe"
    / "your_facebook_activity"
    / "posts"
    / "your_posts__check_ins__photos_and_videos_1.html"
)
FINAL_PREVIEW_CSV = (
    PROJECT_ROOT / "data" / "processed" / "facebook_history_candidate_final_preview.csv"
)

CANDIDATE_CODE_PATTERN = re.compile(
    rf"^{re.escape(BATCH_CODE)}-CAN-(\d+)$"
)

DETERMINISTIC_EXTRACTOR_NAME = "historical_facebook_deterministic_extractor"
DETERMINISTIC_EXTRACTOR_VERSION = "1.0.0"

REVIEW_REASON = (
    "Candidate was imported from a historical Facebook export "
    "(offline pre-validated extraction; see source_evidence.historical_import_key). "
    "Book identity, ISBN, publisher, and metadata still require verification."
)

# Records this first batch must never import, per explicit instruction.
FORBIDDEN_RECORD_IDS = {"1483", "1038"}

# --- the exact, hardcoded, bounded selection for this first batch ---------
# (historical_record_id, candidate_index within that record's AUTO_PASS group)
SELECTED_CANDIDATES: list[tuple[str, int]] = [
    ("1064", 1),
    ("1180", 1),
    ("1231", 1),
    ("1343", 1),
    ("978", 1),
    ("1155", 1),
    ("1156", 1),
    ("1551", 1),
    ("1603", 1),
    ("1062", 1),  # deliberately only 1 of 6 AUTO_PASS candidates from this
                  # multi-candidate record -- demonstrates individual-row-only
                  # import from a multi-candidate source without using up
                  # more than one of this batch's 10 slots.
]


def _normalize_for_dedupe(text: str) -> str:
    """Casefold + collapse whitespace -- matches the existing
    find_existing_candidate() idiom in create_candidates_from_cleaned_posts.py."""
    return " ".join((text or "").split()).casefold()


def load_final_preview_rows() -> list[dict[str, str]]:
    if not FINAL_PREVIEW_CSV.is_file():
        raise RuntimeError(
            f"Final preview CSV not found: {FINAL_PREVIEW_CSV}. "
            "Run build_facebook_history_candidate_final_preview.py first."
        )
    with FINAL_PREVIEW_CSV.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def resolve_selected_rows(
    preview_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Validate and resolve SELECTED_CANDIDATES against the preview CSV.

    Hard safety assertions -- fail loudly rather than importing anything
    unintended:
      - exactly 10 selections
      - every selection resolves to a real AUTO_PASS row
      - no forbidden record_id is present
      - no duplicate (record_id, candidate_index) selection
    """
    if len(SELECTED_CANDIDATES) != 10:
        raise RuntimeError(
            f"SELECTED_CANDIDATES must contain exactly 10 entries, "
            f"found {len(SELECTED_CANDIDATES)}."
        )

    if len(set(SELECTED_CANDIDATES)) != len(SELECTED_CANDIDATES):
        raise RuntimeError("SELECTED_CANDIDATES contains a duplicate selection.")

    by_key: dict[tuple[str, int], dict[str, str]] = {}
    for row in preview_rows:
        if row["final_outcome"] != "AUTO_PASS":
            continue
        if not row["candidate_index"]:
            continue
        by_key[(row["record_id"], int(row["candidate_index"]))] = row

    resolved: list[dict[str, str]] = []
    for record_id, candidate_index in SELECTED_CANDIDATES:
        if record_id in FORBIDDEN_RECORD_IDS:
            raise RuntimeError(
                f"Record #{record_id} is on the forbidden list for this batch."
            )
        row = by_key.get((record_id, candidate_index))
        if row is None:
            raise RuntimeError(
                f"Selection ({record_id}, {candidate_index}) does not resolve "
                "to an AUTO_PASS row in the final preview CSV."
            )
        if row["candidate_type"] not in {"SINGLE_BOOK", "BOOK_COMBO"}:
            raise RuntimeError(
                f"Selection ({record_id}, {candidate_index}) has unexpected "
                f"candidate_type={row['candidate_type']!r}."
            )
        resolved.append(row)

    return resolved


def print_selection_preview(rows: list[dict[str, str]]) -> None:
    print("=== PHASE 4: SELECTED CANDIDATES (bounded, explicit) ===")
    for idx, row in enumerate(rows, start=1):
        why_safe = (
            f"AUTO_PASS via {row['extraction_source']}; "
            f"confidence={row['confidence']}; "
            f"completeness={row['completeness_status']}; "
            "no review caveat; not on forbidden list"
        )
        print(f"candidate_selection_index: {idx}")
        print(f"  historical_record_id: {row['record_id']}")
        print(f"  title: {row['title_raw']}")
        print(f"  candidate_type: {row['candidate_type']}")
        print(f"  extraction_source: {row['extraction_source']}")
        print(f"  confidence: {row['confidence']}")
        print(f"  why_safe: {why_safe}")
    print()


def get_or_create_batch(
    repository: SupabaseRepository,
    dry_run: bool,
) -> tuple[dict[str, Any], bool]:
    existing = repository.get_batch_by_code(BATCH_CODE)
    if existing is not None:
        return existing, False

    if dry_run:
        return (
            {"batch_id": "<would-create>", "batch_code": BATCH_CODE},
            True,
        )

    created = repository.create_batch(
        batch_code=BATCH_CODE,
        batch_name=BATCH_NAME,
        description=BATCH_DESCRIPTION,
    )
    return created, True


def historical_source_url(record_id: str) -> str:
    """A truthful, non-fabricated locator -- this is NOT a live Facebook
    permalink (none exists for a personal data-export record). It points
    at the exact local export file and record position, so the claim is
    verifiable rather than invented. This is also what makes a historical
    import visibly distinguishable from a live-collected source_url."""
    return f"facebook-export://{DEFAULT_SOURCE_EXPORT_LABEL}#record={record_id}"


def get_or_create_source_url(
    repository: SupabaseRepository,
    batch_id: str,
    batch_exists: bool,
    record_id: str,
    date_text: str,
    dry_run: bool,
) -> tuple[dict[str, Any], bool]:
    source_url = historical_source_url(record_id)

    if batch_exists:
        existing_response = (
            repository.client.table("source_urls")
            .select("source_url_id, batch_id, source_type, source_url, source_name, crawl_status")
            .eq("batch_id", batch_id)
            .eq("source_type", "FACEBOOK_POST")
            .eq("source_url", source_url)
            .limit(1)
            .execute()
        )
        existing_rows = existing_response.data or []
        if existing_rows:
            return existing_rows[0], False
    # batch does not exist yet -- nothing downstream of it can exist either;
    # skip the lookup rather than querying with an invalid/synthetic batch_id.

    if dry_run:
        return (
            {"source_url_id": "<would-create>", "source_url": source_url},
            True,
        )

    created = repository.save_source_url(
        batch_id=batch_id,
        source_url=source_url,
        selection_reason=(
            f"Historical Facebook export record #{record_id} ({date_text})"
        ),
        active=True,
        source_type="FACEBOOK_POST",
    )
    # save_source_url() upserts with crawl_status=PENDING for a fresh row;
    # this record's content was already collected (it came from the export
    # file itself, not a pending live crawl) -- correct that status.
    repository.update_source_url_status(
        source_url_id=created["source_url_id"],
        crawl_status="COLLECTED",
    )
    created["crawl_status"] = "COLLECTED"
    return created, True


def get_or_create_raw_page(
    repository: SupabaseRepository,
    batch_id: str,
    batch_exists: bool,
    source_url_row: dict[str, Any],
    record_id: str,
    date_text: str,
    cleaned_text: str,
    content_hash_full: str,
    dry_run: bool,
) -> tuple[dict[str, Any], bool]:
    if batch_exists:
        existing_response = (
            repository.client.table("raw_pages")
            .select(
                "raw_page_id, batch_id, source_url_id, page_type, page_url, "
                "content_hash, cleaning_status"
            )
            .eq("batch_id", batch_id)
            .eq("content_hash", content_hash_full)
            .limit(1)
            .execute()
        )
        existing_rows = existing_response.data or []
        if existing_rows:
            return existing_rows[0], False

    if dry_run:
        return (
            {"raw_page_id": "<would-create>", "content_hash": content_hash_full},
            True,
        )

    payload = {
        "batch_id": batch_id,
        "source_url_id": source_url_row["source_url_id"],
        "page_type": "FACEBOOK_POST",
        "page_url": source_url_row["source_url"],
        "raw_title": f"Historical Facebook export record #{record_id}",
        "raw_text": cleaned_text,
        "raw_html": None,
        "content_hash": content_hash_full,
        "collector_name": "historical_facebook_export_importer",
        "collector_version": SCRIPT_VERSION,
    }
    response = repository.client.table("raw_pages").insert(payload).execute()
    records = response.data or []
    if not records:
        raise RuntimeError(f"raw_pages insert returned no data for record #{record_id}.")

    raw_page = records[0]

    # cleaned_text / cleaning_status / cleaning_method exist in the schema
    # but are not covered by SupabaseRepository.save_raw_page(); this
    # content was already cleaned+validated offline in Task 6, so record
    # that honestly rather than leaving cleaning_status at its PENDING
    # default.
    update_response = (
        repository.client.table("raw_pages")
        .update(
            {
                "cleaned_text": cleaned_text,
                "cleaning_status": "CLEANED",
                "cleaning_method": "historical_text_cleaner.clean_historical_facebook_text",
                "cleaned_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        .eq("raw_page_id", raw_page["raw_page_id"])
        .execute()
    )
    updated_records = update_response.data or []
    if updated_records:
        raw_page = updated_records[0]

    return raw_page, True


def find_existing_candidate(
    repository: SupabaseRepository,
    raw_page_id: str,
    title_normalized: str,
    candidate_type: str,
) -> dict[str, Any] | None:
    """The idempotent import key: (raw_page_id, normalized title,
    candidate_type) -- raw_page_id already encodes historical_record_id
    1:1 (one raw_page per historical record in this importer)."""
    response = (
        repository.client.table("product_candidates")
        .select(
            "candidate_id, candidate_code, raw_page_id, extracted_title, "
            "candidate_type, workflow_status, review_required, source_evidence"
        )
        .eq("raw_page_id", raw_page_id)
        .eq("candidate_type", candidate_type)
        .execute()
    )
    target = _normalize_for_dedupe(title_normalized)
    for record in response.data or []:
        if _normalize_for_dedupe(record.get("extracted_title") or "") == target:
            return record
    return None


def get_next_candidate_code(repository: SupabaseRepository, batch_id: str) -> str:
    response = (
        repository.client.table("product_candidates")
        .select("candidate_code")
        .eq("batch_id", batch_id)
        .execute()
    )
    highest = 0
    for record in response.data or []:
        match = CANDIDATE_CODE_PATTERN.match(str(record.get("candidate_code") or ""))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{BATCH_CODE}-CAN-{highest + 1:04d}"


def build_source_evidence(
    row: dict[str, str],
    raw_page: dict[str, Any],
    source_url_row: dict[str, Any],
) -> dict[str, Any]:
    extraction_source = row["extraction_source"]
    evidence: dict[str, Any] = {
        "import_type": IMPORT_TYPE,
        "importer_name": IMPORTER_NAME,
        "importer_version": SCRIPT_VERSION,
        "batch_code": BATCH_CODE,
        "source_type": "FACEBOOK",
        "page_type": "FACEBOOK_POST",
        "raw_page_id": raw_page.get("raw_page_id"),
        "source_url_id": source_url_row.get("source_url_id"),
        "source_url": source_url_row.get("source_url"),
        "historical_record_id": row["record_id"],
        "historical_post_date": row["date"],
        "historical_export_file": DEFAULT_SOURCE_EXPORT_LABEL,
        "extraction_source": extraction_source,
        "title_raw": row["title_raw"],
        "title_normalized": row["title_normalized"],
        "evidence_text": row["evidence_text"],
        "confidence": float(row["confidence"]) if row["confidence"] else None,
        "completeness_status": row["completeness_status"],
        "cleaned_text_hash": row["cleaned_text_hash"],
        "local_media_count": int(row["local_media_count"] or 0),
        "local_media_paths": [
            p for p in row["local_media_paths"].split("; ") if p
        ],
        "historical_import_key": (
            f"{row['record_id']}:{_normalize_for_dedupe(row['title_normalized'])}:"
            f"{row['candidate_type']}"
        ),
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }

    if extraction_source == "DETERMINISTIC":
        evidence["extraction_method"] = "RULE_BASED"
        evidence["extractor_name"] = DETERMINISTIC_EXTRACTOR_NAME
        evidence["extractor_version"] = DETERMINISTIC_EXTRACTOR_VERSION
    else:
        evidence["extraction_method"] = "AI_ASSISTED"
        evidence["extractor_name"] = PROVIDER_NAME
        evidence["extractor_version"] = f"{DEFAULT_MODEL}/{PROMPT_VERSION}/{SCHEMA_VERSION}"
        evidence["provider"] = PROVIDER_NAME
        evidence["model"] = DEFAULT_MODEL
        evidence["prompt_version"] = PROMPT_VERSION
        evidence["schema_version"] = SCHEMA_VERSION

    return evidence


def build_candidate_payload(
    batch: dict[str, Any],
    raw_page: dict[str, Any],
    source_url_row: dict[str, Any],
    row: dict[str, str],
    candidate_code: str,
) -> dict[str, Any]:
    evidence = build_source_evidence(row, raw_page, source_url_row)
    return {
        "batch_id": batch["batch_id"],
        "candidate_code": candidate_code,
        "candidate_type": row["candidate_type"],
        "combo_group_code": None,
        "raw_page_id": raw_page["raw_page_id"],
        "source_url_id": source_url_row["source_url_id"],
        "extracted_title": row["title_raw"],
        "extracted_author": None,  # never fabricate -- CLAUDE.md 2.2
        "possible_isbn": None,  # never fabricate -- CLAUDE.md 2.2 / 2.3
        "workflow_status": "EXTRACTED",
        "extraction_confidence": float(row["confidence"]) if row["confidence"] else None,
        "source_evidence": evidence,
        "conflict_fields": [],
        "review_required": True,
        "review_reason": REVIEW_REASON,
        "extraction_method": evidence["extraction_method"],
        "extractor_name": evidence["extractor_name"],
        "extractor_version": evidence["extractor_version"],
    }


def insert_candidate(repository: SupabaseRepository, payload: dict[str, Any]) -> dict[str, Any]:
    response = repository.client.table("product_candidates").insert(payload).execute()
    records = response.data or []
    if not records:
        raise RuntimeError("product_candidates insert returned no data.")
    return records[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform real Supabase writes. Without this flag, runs as a dry run.",
    )
    args = parser.parse_args()
    dry_run = not args.execute

    print(f"TSYC historical Facebook candidate import -- v{SCRIPT_VERSION}")
    print(f"MODE: {'DRY RUN (no writes)' if dry_run else 'EXECUTE (real Supabase writes)'}")
    print(f"BATCH_CODE: {BATCH_CODE}")
    print()

    preview_rows = load_final_preview_rows()
    selected_rows = resolve_selected_rows(preview_rows)
    print_selection_preview(selected_rows)

    export_records = load_facebook_history_export(DEFAULT_SOURCE_EXPORT)
    text_by_id = {r.record_index: r.full_text for r in export_records}

    repository = SupabaseRepository()

    batch, batch_created = get_or_create_batch(repository, dry_run)
    batch_exists = not (batch_created and dry_run)  # a real row exists iff we didn't just simulate creating it
    print(f"BATCH: {batch.get('batch_code', BATCH_CODE)} "
          f"({'would create' if (batch_created and dry_run) else 'created' if batch_created else 'already existed'})")
    print()

    results: list[dict[str, Any]] = []

    for idx, row in enumerate(selected_rows, start=1):
        record_id = row["record_id"]
        full_text = text_by_id.get(int(record_id), "")
        cleaned_text = clean_historical_facebook_text(full_text)
        content_hash_full = hashlib.sha256(cleaned_text.encode("utf-8")).hexdigest()

        try:
            source_url_row, source_url_created = get_or_create_source_url(
                repository, batch["batch_id"], batch_exists, record_id, row["date"], dry_run
            )
            raw_page, raw_page_created = get_or_create_raw_page(
                repository,
                batch["batch_id"],
                batch_exists,
                source_url_row,
                record_id,
                row["date"],
                cleaned_text,
                content_hash_full,
                dry_run,
            )

            title_normalized = row["title_normalized"]
            candidate_type = row["candidate_type"]

            if not batch_exists or raw_page.get("raw_page_id") == "<would-create>":
                existing = None  # raw_page_id is synthetic in dry-run; cannot query real FKs
            else:
                existing = find_existing_candidate(
                    repository, raw_page["raw_page_id"], title_normalized, candidate_type
                )

            if existing is not None:
                status = "ALREADY_EXISTED"
                candidate_code = existing["candidate_code"]
                candidate_id = existing["candidate_id"]
            elif dry_run:
                status = "WOULD_CREATE"
                candidate_code = "<would-assign>"
                candidate_id = "<would-create>"
            else:
                candidate_code = get_next_candidate_code(repository, batch["batch_id"])
                payload = build_candidate_payload(
                    batch, raw_page, source_url_row, row, candidate_code
                )
                created = insert_candidate(repository, payload)
                status = "CREATED"
                candidate_code = created["candidate_code"]
                candidate_id = created["candidate_id"]

                repository.write_process_log(
                    message=(
                        f"Historical import: candidate {candidate_code} created "
                        f"from historical record #{record_id} "
                        f"(source={row['extraction_source']})."
                    ),
                    process_name="historical_facebook_candidate_import",
                    batch_id=batch["batch_id"],
                    candidate_id=candidate_id,
                    process_step="IMPORT_CANDIDATE",
                    log_level="INFO",
                    status="SUCCESS",
                )

            results.append(
                {
                    "candidate_selection_index": idx,
                    "historical_record_id": record_id,
                    "title": row["title_raw"],
                    "candidate_type": candidate_type,
                    "extraction_source": row["extraction_source"],
                    "status": status,
                    "candidate_code": candidate_code,
                    "candidate_id": candidate_id,
                }
            )
            print(
                f"[{idx}/10] #{record_id} '{row['title_raw']}' -> "
                f"{status} ({candidate_code})"
            )

        except Exception as exc:  # noqa: BLE001 -- deliberate hard stop
            print()
            print(f"STOP: unexpected failure on selection {idx} (record #{record_id}): {exc}")
            if not dry_run:
                repository.write_process_log(
                    message=f"Historical import STOPPED on record #{record_id}: {exc}",
                    process_name="historical_facebook_candidate_import",
                    batch_id=batch["batch_id"],
                    process_step="IMPORT_CANDIDATE",
                    log_level="CRITICAL",
                    status="FAILED",
                    error_details={"record_id": record_id, "error": str(exc)},
                )
            return 1

    print()
    print("=== SUMMARY ===")
    created_count = sum(1 for r in results if r["status"] in {"CREATED", "WOULD_CREATE"})
    existed_count = sum(1 for r in results if r["status"] == "ALREADY_EXISTED")
    print(f"selected: {len(results)}")
    print(f"created_or_would_create: {created_count}")
    print(f"already_existed: {existed_count}")
    print()
    for r in results:
        print(
            f"{r['historical_record_id']} | {r['title']} | "
            f"{r['candidate_code']} | {r['status']}"
        )
    print()
    print(f"MODE: {'DRY_RUN' if dry_run else 'EXECUTE'}")
    print("HISTORICAL_IMPORT_RUN_COMPLETE: YES")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
