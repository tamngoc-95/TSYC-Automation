"""
GENERAL bounded, idempotent historical Facebook candidate import: import
every remaining AUTO_PASS row that has not been imported yet.

import_historical_facebook_candidates.py (FB-HIST-2026-001) and
import_historical_facebook_candidates_batch2.py (FB-HIST-2026-002) each
hardcode an explicit SELECTED_CANDIDATES allowlist that must be
hand-authored -- effectively a new bounded-selection approval cycle for
every additional chunk of already-vetted historical candidates. This
script replaces that pattern for the *remaining* AUTO_PASS backlog: its
selection is computed automatically (compute_remaining_rows(), a pure
function with no I/O) instead of hand-picked, so no new per-run
candidate list needs to be authored to keep importing already-vetted
rows.

This is authorized by the TSYC stable automation policy: "idempotent
Supabase import of AUTO_PASS candidates" is bounded, deterministic work
that does not require a human decision on every additional chunk -- the
human judgment already happened once, offline, when
build_facebook_history_candidate_final_preview.py's validate_and_gate()
pass classified each row AUTO_PASS vs REVIEW_REQUIRED. A row this script
imports is never one a human has not already effectively cleared; a row
still marked REVIEW_REQUIRED in the CSV is never imported by this
script, full stop.

Exactly the same design as import_historical_facebook_candidates.py
otherwise (see that module's docstring for the full idempotency/
provenance model, reused unchanged here):

    batches (get-or-create, batch_code=BATCH_CODE)
    source_urls (idempotent upsert per historical record)
    raw_pages (get-or-create per historical record, deduped by content_hash)
    product_candidates (get-or-create per (raw_page_id, normalized title,
        candidate_type) -- the historical idempotent import key, PLUS a
        cross-batch check by source_evidence.historical_import_key and by
        normalized title against every other batch, exactly like batch2's
        defense-in-depth)
    process_logs (one entry per successful/skipped/failed candidate)

This script performs NO extraction, NO classification, and NO gate
re-evaluation -- it only imports rows that already passed the offline
validate_and_gate() AUTO_PASS decision. It NEVER calls WooCommerce,
never sets a price field, never sets identity_status beyond the schema
default (IDENTITY_PENDING), and never marks review_required false for a
freshly imported row (identity/metadata verification is a separate,
later stage -- see CLAUDE.md Section 13).

Idempotency:
    Rerunning this script is a no-op for every candidate that was
    already imported (by this script, by FB-HIST-2026-001, or by
    FB-HIST-2026-002): compute_remaining_rows() excludes any row whose
    historical_import_key already exists anywhere in product_candidates
    before a single write is attempted, and find_existing_candidate()
    re-checks the per-batch (raw_page_id, normalized title,
    candidate_type) key immediately before each insert as a second,
    independent idempotency guard.

Safety:
    Default mode is DRY RUN. Pass --execute to perform real writes.
    A dry run performs ONLY reads -- no insert/upsert call is made.
    #1483 and #1038 are permanently excluded (FORBIDDEN_RECORD_IDS),
    exactly as in both prior batches.
    Optional --max-import caps how many rows one run imports, for
    operators who want to scale up gradually (CLAUDE.md section 27)
    instead of importing the entire remaining backlog in one run.

Usage:
    .venv/Scripts/python.exe scripts/import_historical_facebook_candidates_remaining.py
    .venv/Scripts/python.exe scripts/import_historical_facebook_candidates_remaining.py --execute
    .venv/Scripts/python.exe scripts/import_historical_facebook_candidates_remaining.py --execute --max-import 20
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

BATCH_CODE = "FB-HIST-2026-AUTOIMPORT"
BATCH_NAME = "Historical Facebook export -- remaining AUTO_PASS auto-import"
BATCH_DESCRIPTION = (
    "Idempotent import of every AUTO_PASS candidate from the offline-"
    "vetted final preview CSV that had not already been imported by "
    "FB-HIST-2026-001, FB-HIST-2026-002, or a prior run of this same "
    "batch. Selection is computed (compute_remaining_rows()), not hand-"
    "authored -- no further per-run bounded-selection approval cycle is "
    "required to keep draining the already-vetted AUTO_PASS backlog. "
    "Source: data/processed/facebook_history_candidate_final_preview.csv."
)
IMPORT_TYPE = "HISTORICAL_FACEBOOK_EXPORT"
IMPORTER_NAME = "historical_facebook_candidate_importer"
SELECTION_MODE = "AUTO_REMAINING"

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

# Records every historical batch must never import, per explicit
# standing instruction.
FORBIDDEN_RECORD_IDS = {"1483", "1038"}


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


def import_key_for_row(row: dict[str, str]) -> str:
    """The exact historical_import_key format every historical importer
    (this one and both hand-selected batches) writes into
    source_evidence -- must stay byte-identical across all of them or
    cross-batch dedupe silently breaks."""
    return (
        f"{row['record_id']}:{_normalize_for_dedupe(row['title_normalized'])}:"
        f"{row['candidate_type']}"
    )


def load_existing_candidate_evidence(
    repository: SupabaseRepository,
) -> tuple[set[str], set[str]]:
    """Read-only: every historical_import_key and every normalized title
    already present in product_candidates, across ALL batches.

    Returns (already_imported_keys, all_existing_norm_titles).
    """
    response = (
        repository.client
        .table("product_candidates")
        .select("extracted_title, source_evidence")
        .execute()
    )

    already_imported_keys: set[str] = set()
    existing_norm_titles: set[str] = set()

    for record in response.data or []:
        evidence = record.get("source_evidence") or {}
        key = evidence.get("historical_import_key")

        if key:
            already_imported_keys.add(key)

        existing_norm_titles.add(
            _normalize_for_dedupe(record.get("extracted_title") or "")
        )

    return already_imported_keys, existing_norm_titles


def compute_remaining_rows(
    preview_rows: list[dict[str, str]],
    already_imported_keys: set[str],
    forbidden_record_ids: set[str] = FORBIDDEN_RECORD_IDS,
    max_import: int | None = None,
) -> list[dict[str, str]]:
    """
    Pure selection logic -- no I/O, no side effects. Deterministic given
    its inputs, so it is directly unit-testable with plain dicts (mirrors
    pipeline_state.py's "pure derivation layer" design).

    Eligible: final_outcome == AUTO_PASS, a real candidate_index, a
    recognized candidate_type, not on the permanent forbidden list, and
    not already imported anywhere (by historical_import_key). A row
    still marked REVIEW_REQUIRED in the CSV is never eligible -- this
    function has no path that returns one.

    Sorted by (historical_record_id, candidate_index) for a stable,
    reproducible import order across reruns and across operators.
    """
    remaining: list[dict[str, str]] = []

    for row in preview_rows:
        if row.get("final_outcome") != "AUTO_PASS":
            continue

        if not row.get("candidate_index"):
            continue

        if row.get("candidate_type") not in {"SINGLE_BOOK", "BOOK_COMBO"}:
            continue

        if row.get("record_id") in forbidden_record_ids:
            continue

        if import_key_for_row(row) in already_imported_keys:
            continue

        remaining.append(row)

    remaining.sort(
        key=lambda r: (int(r["record_id"]), int(r["candidate_index"]))
    )

    if max_import is not None:
        remaining = remaining[:max_import]

    return remaining


def print_selection_preview(rows: list[dict[str, str]]) -> None:
    print(f"=== SELECTED CANDIDATES (computed, bounded): {len(rows)} ===")
    for idx, row in enumerate(rows, start=1):
        why_safe = (
            f"AUTO_PASS via {row['extraction_source']}; "
            f"confidence={row['confidence']}; "
            f"completeness={row['completeness_status']}; "
            "not on forbidden list; not already imported in any batch"
        )
        print(f"selection_index: {idx}")
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
    verifiable rather than invented."""
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
        "selection_mode": SELECTION_MODE,
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
        "historical_import_key": import_key_for_row(row),
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
    parser.add_argument(
        "--max-import",
        type=int,
        default=None,
        help=(
            "Optional cap on how many remaining AUTO_PASS candidates this "
            "run imports. Without it, every remaining AUTO_PASS candidate "
            "not yet imported anywhere is imported in this one run."
        ),
    )
    args = parser.parse_args()
    dry_run = not args.execute

    if args.max_import is not None and args.max_import < 1:
        print("Error: --max-import must be at least 1.", file=sys.stderr)
        return 2

    print(f"TSYC historical Facebook candidate import (remaining AUTO_PASS) -- v{SCRIPT_VERSION}")
    print(f"MODE: {'DRY RUN (no writes)' if dry_run else 'EXECUTE (real Supabase writes)'}")
    print(f"BATCH_CODE: {BATCH_CODE}")
    print()

    preview_rows = load_final_preview_rows()

    export_records = load_facebook_history_export(DEFAULT_SOURCE_EXPORT)
    text_by_id = {r.record_index: r.full_text for r in export_records}

    repository = SupabaseRepository()

    already_imported_keys, existing_norm_titles = load_existing_candidate_evidence(
        repository
    )

    remaining_rows = compute_remaining_rows(
        preview_rows,
        already_imported_keys,
        max_import=args.max_import,
    )

    print_selection_preview(remaining_rows)

    if not remaining_rows:
        print("Nothing to import: no remaining AUTO_PASS candidates.")
        print("HISTORICAL_IMPORT_RUN_COMPLETE: YES")
        return 0

    batch, batch_created = get_or_create_batch(repository, dry_run)
    batch_exists = not (batch_created and dry_run)
    print(f"BATCH: {batch.get('batch_code', BATCH_CODE)} "
          f"({'would create' if (batch_created and dry_run) else 'created' if batch_created else 'already existed'})")
    print()

    results: list[dict[str, Any]] = []

    for idx, row in enumerate(remaining_rows, start=1):
        record_id = row["record_id"]
        full_text = text_by_id.get(int(record_id), "")
        cleaned_text = clean_historical_facebook_text(full_text)
        content_hash_full = hashlib.sha256(cleaned_text.encode("utf-8")).hexdigest()

        try:
            norm_title = _normalize_for_dedupe(row["title_raw"])
            if norm_title in existing_norm_titles and import_key_for_row(row) not in already_imported_keys:
                # A title collision with a candidate that is NOT this same
                # historical row (defense-in-depth beyond the
                # historical_import_key check already applied in
                # compute_remaining_rows) -- exclude rather than silently
                # duplicate a possibly-live/non-historical candidate.
                raise RuntimeError(
                    f"Title {row['title_raw']!r} collides with an EXISTING "
                    "production candidate title from a different source. "
                    "Excluding rather than silently duplicating -- resolve "
                    "manually."
                )

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
                        f"Historical import (remaining AUTO_PASS): candidate "
                        f"{candidate_code} created from historical record "
                        f"#{record_id} (source={row['extraction_source']})."
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
                    "selection_index": idx,
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
                f"[{idx}/{len(remaining_rows)}] #{record_id} '{row['title_raw']}' -> "
                f"{status} ({candidate_code})"
            )

        except Exception as exc:  # noqa: BLE001 -- deliberate hard stop
            print()
            print(f"STOP: unexpected failure on selection {idx} (record #{record_id}): {exc}")
            if not dry_run:
                repository.write_process_log(
                    message=f"Historical import (remaining AUTO_PASS) STOPPED on record #{record_id}: {exc}",
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
