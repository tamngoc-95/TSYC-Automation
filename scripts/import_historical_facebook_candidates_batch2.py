"""
SECOND BOUNDED historical Facebook candidate import into Supabase.

Same design as import_historical_facebook_candidates.py (batch
FB-HIST-2026-001) -- see that module's docstring for the full
provenance/idempotency/safety design, which is reused unchanged here.
This module only differs in:

  - BATCH_CODE = "FB-HIST-2026-002"
  - SELECTED_CANDIDATES -- a different, explicit, hardcoded set of 25
    (record_id, candidate_index) selections
  - an added CROSS-BATCH dedupe check: before any insert, the exact
    historical_import_key is looked up across ALL product_candidates
    rows (not just this batch), and a normalized-title check against
    ALL existing production candidates (any batch, historical or
    live) is run before writes -- both as defense-in-depth beyond the
    per-batch (raw_page_id, title, candidate_type) check that already
    exists in find_existing_candidate().

Usage:
    .venv/Scripts/python.exe scripts/import_historical_facebook_candidates_batch2.py
    .venv/Scripts/python.exe scripts/import_historical_facebook_candidates_batch2.py --execute
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

BATCH_CODE = "FB-HIST-2026-002"
BATCH_NAME = "Historical Facebook export -- second bounded import"
BATCH_DESCRIPTION = (
    "Second bounded historical import of pre-validated (offline-gated) "
    "Facebook candidate-extraction results into production Supabase. "
    "Source: data/processed/facebook_history_candidate_final_preview.csv. "
    "Follows FB-HIST-2026-001."
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

EXPECTED_SELECTION_COUNT = 25

# Records this batch must never import, per explicit instruction.
FORBIDDEN_RECORD_IDS = {"1483", "1038"}

# Records already imported in FB-HIST-2026-001 -- hard guard against
# re-selecting them here (defense-in-depth on top of the DB-level checks).
PRIOR_BATCH_IMPORT_KEYS = {
    "1064:rich habits – thói quen thành công:SINGLE_BOOK",
    "1180:thức tỉnh mục đích sống (a new earth):SINGLE_BOOK",
    "1231:suối nguồn:SINGLE_BOOK",
    "1343:power vs. force:SINGLE_BOOK",
    "978:chuyện xóm gà:SINGLE_BOOK",
    "1155:thiên thần và ác quỷ:SINGLE_BOOK",
    "1156:hoả ngục:SINGLE_BOOK",
    "1551:tại sao chúng tôi muốn bạn giàu:SINGLE_BOOK",
    "1603:chữa lành đứa trẻ trong bạn:SINGLE_BOOK",
    "1062:nhật ký học làm bánh:SINGLE_BOOK",
}

# --- the exact, hardcoded, bounded selection for this second batch --------
# (historical_record_id, candidate_index within that record's AUTO_PASS group)
#
# Composition (see Phase 2 of the task spec):
#   A. 15 SINGLE_BOOK -- clean titles, no QA caveat. All 6 deterministic
#      AUTO_PASS records are exhausted (4 used in batch 1, #1038
#      forbidden, #1268 is BOOK_COMBO not SINGLE_BOOK) so this group is
#      necessarily 100% CLAUDE_SEMANTIC -- "mix where available" and
#      none is available; that fact is reported, not hidden.
#   B. 5 candidates proving multi-candidate-per-record safety, drawn
#      from two distinct multi-candidate records in full (#1091: all 3
#      of its AUTO_PASS candidates; #1447: both of its 2) rather than
#      1-per-record, so the multi-row-per-raw_page path is genuinely
#      exercised.
#   C. 5 BOOK_COMBO -- every AUTO_PASS BOOK_COMBO in the entire dataset
#      (there are exactly 5) is included.
SELECTED_CANDIDATES: list[tuple[str, int]] = [
    # -- A: 15 SINGLE_BOOK (all CLAUDE_SEMANTIC; 0 unused DETERMINISTIC
    #    SINGLE_BOOK candidates remain in the pool) --
    ("1269", 1),
    ("1604", 1),
    ("1610", 1),
    ("1863", 1),
    ("1063", 1),
    ("1063", 2),
    ("1088", 1),
    ("1088", 2),
    ("1089", 1),
    ("1089", 2),
    ("1090", 1),
    ("1090", 2),
    ("1271", 1),
    ("1292", 1),
    ("1482", 1),
    # -- B: 5, multi-candidate-per-record proof (2 records, taken in full) --
    ("1091", 1),
    ("1091", 2),
    ("1091", 3),
    ("1447", 1),
    ("1447", 2),
    # -- C: 5 BOOK_COMBO (all AUTO_PASS combos in the dataset) --
    ("1189", 1),
    ("1251", 1),
    ("1268", 1),  # the one DETERMINISTIC candidate in this batch
    ("1518", 1),
    ("1560", 1),
]


def _normalize_for_dedupe(text: str) -> str:
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
    repository: SupabaseRepository,
    preview_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Validate and resolve SELECTED_CANDIDATES against the preview CSV,
    plus cross-batch/production dedupe checks (Phase 3).

    Idempotency note: a historical_import_key that already exists
    because THIS SAME BATCH (FB-HIST-2026-002) already created it on a
    prior run is NOT an error -- Phase 3 rule 1 ("same historical
    import key => ALREADY_EXISTS, do not insert") means recognize and
    skip-insert, not fail. Only a key that belongs to a DIFFERENT batch
    (accidentally re-selecting another batch's candidate, e.g. one from
    FB-HIST-2026-001) is a hard conflict here.
    """
    if len(SELECTED_CANDIDATES) != EXPECTED_SELECTION_COUNT:
        raise RuntimeError(
            f"SELECTED_CANDIDATES must contain exactly {EXPECTED_SELECTION_COUNT} "
            f"entries, found {len(SELECTED_CANDIDATES)}."
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

    this_batch = repository.get_batch_by_code(BATCH_CODE)
    this_batch_id = this_batch["batch_id"] if this_batch else None

    # Cross-batch: every existing production title + historical_import_key
    # + owning batch_id, across ALL batches (live pilot, FB-HIST-2026-001,
    # and any prior run of this very batch).
    existing_response = (
        repository.client.table("product_candidates")
        .select("candidate_code, extracted_title, candidate_type, batch_id, source_evidence")
        .execute()
    )
    existing_rows = existing_response.data or []
    existing_norm_titles = {
        _normalize_for_dedupe(r.get("extracted_title") or "") for r in existing_rows
    }
    existing_import_key_batch: dict[str, str] = {
        (r.get("source_evidence") or {}).get("historical_import_key"): r.get("batch_id")
        for r in existing_rows
        if (r.get("source_evidence") or {}).get("historical_import_key")
    }
    # Titles belonging to THIS batch's own rows are not "conflicts" --
    # exclude them from the live-source title-collision check below.
    own_batch_norm_titles = {
        _normalize_for_dedupe(r.get("extracted_title") or "")
        for r in existing_rows
        if r.get("batch_id") == this_batch_id
    }
    foreign_norm_titles = existing_norm_titles - own_batch_norm_titles

    resolved: list[dict[str, str]] = []
    seen_keys_this_batch: set[str] = set()

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

        key = (
            f"{row['record_id']}:{_normalize_for_dedupe(row['title_normalized'])}:"
            f"{row['candidate_type']}"
        )
        if key in PRIOR_BATCH_IMPORT_KEYS:
            raise RuntimeError(
                f"Selection ({record_id}, {candidate_index}) already exists "
                f"in FB-HIST-2026-001 (historical_import_key={key!r})."
            )
        owning_batch_id = existing_import_key_batch.get(key)
        if owning_batch_id is not None and owning_batch_id != this_batch_id:
            raise RuntimeError(
                f"Selection ({record_id}, {candidate_index}) already exists "
                f"in a DIFFERENT batch (historical_import_key={key!r}, "
                f"batch_id={owning_batch_id})."
            )
        if key in seen_keys_this_batch:
            raise RuntimeError(
                f"Selection ({record_id}, {candidate_index}) duplicates another "
                "selection in this same batch."
            )
        seen_keys_this_batch.add(key)

        norm_title = _normalize_for_dedupe(row["title_raw"])
        if norm_title in foreign_norm_titles:
            raise RuntimeError(
                f"Selection ({record_id}, {candidate_index}) title "
                f"{row['title_raw']!r} collides with an EXISTING production "
                "candidate title from a DIFFERENT batch (possibly a "
                "non-historical/live source). Excluding rather than silently "
                "duplicating -- resolve manually."
            )

        resolved.append(row)

    return resolved


def print_selection_preview(
    repository: SupabaseRepository, rows: list[dict[str, str]]
) -> None:
    print("=== PHASE 2: SELECTED CANDIDATES (bounded, explicit, 25) ===")
    for idx, row in enumerate(rows, start=1):
        record_id = row["record_id"]
        source_url = historical_source_url(record_id)
        existing_source = (
            repository.client.table("source_urls")
            .select("source_url_id")
            .eq("source_type", "FACEBOOK_POST")
            .eq("source_url", source_url)
            .limit(1)
            .execute()
        )
        existing_source_record = bool(existing_source.data)

        why_safe = (
            f"AUTO_PASS via {row['extraction_source']}; "
            f"confidence={row['confidence']}; "
            f"completeness={row['completeness_status']}; "
            "no review caveat; not #1038/#1483; not already imported; "
            "no title collision with any existing production candidate"
        )
        print(f"selection_index: {idx}")
        print(f"  historical_record_id: {record_id}")
        print(f"  title: {row['title_raw']}")
        print(f"  candidate_type: {row['candidate_type']}")
        print(f"  extraction_source: {row['extraction_source']}")
        print(f"  confidence: {row['confidence']}")
        print(f"  existing_source_record: {existing_source_record}")
        print(f"  existing_raw_page: (checked at import time via content_hash)")
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
    """Per-batch idempotent import key: (raw_page_id, normalized title,
    candidate_type)."""
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


def find_existing_candidate_cross_batch(
    repository: SupabaseRepository,
    historical_import_key: str,
) -> dict[str, Any] | None:
    """Cross-batch safety net (Phase 3, rule 1): the exact
    historical_import_key must not already exist ANYWHERE in production,
    regardless of which batch or raw_page created it."""
    response = (
        repository.client.table("product_candidates")
        .select("candidate_id, candidate_code, batch_id, extracted_title, source_evidence")
        .eq("source_evidence->>historical_import_key", historical_import_key)
        .execute()
    )
    records = response.data or []
    return records[0] if records else None


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
        "extracted_author": None,
        "possible_isbn": None,
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

    print(f"TSYC historical Facebook candidate import (batch 2) -- v{SCRIPT_VERSION}")
    print(f"MODE: {'DRY RUN (no writes)' if dry_run else 'EXECUTE (real Supabase writes)'}")
    print(f"BATCH_CODE: {BATCH_CODE}")
    print()

    repository = SupabaseRepository()

    preview_rows = load_final_preview_rows()
    selected_rows = resolve_selected_rows(repository, preview_rows)
    print_selection_preview(repository, selected_rows)

    export_records = load_facebook_history_export(DEFAULT_SOURCE_EXPORT)
    text_by_id = {r.record_index: r.full_text for r in export_records}

    batch, batch_created = get_or_create_batch(repository, dry_run)
    batch_exists = not (batch_created and dry_run)
    print(f"BATCH: {batch.get('batch_code', BATCH_CODE)} "
          f"({'would create' if (batch_created and dry_run) else 'created' if batch_created else 'already existed'})")
    print()

    results: list[dict[str, Any]] = []
    source_urls_created_count = 0
    source_urls_reused_count = 0
    raw_pages_created_count = 0
    raw_pages_reused_count = 0

    for idx, row in enumerate(selected_rows, start=1):
        record_id = row["record_id"]
        full_text = text_by_id.get(int(record_id), "")
        cleaned_text = clean_historical_facebook_text(full_text)
        content_hash_full = hashlib.sha256(cleaned_text.encode("utf-8")).hexdigest()

        try:
            source_url_row, source_url_created = get_or_create_source_url(
                repository, batch["batch_id"], batch_exists, record_id, row["date"], dry_run
            )
            if source_url_created:
                source_urls_created_count += 1
            else:
                source_urls_reused_count += 1

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
            if raw_page_created:
                raw_pages_created_count += 1
            else:
                raw_pages_reused_count += 1

            title_normalized = row["title_normalized"]
            candidate_type = row["candidate_type"]
            historical_import_key = (
                f"{record_id}:{_normalize_for_dedupe(title_normalized)}:{candidate_type}"
            )

            if not batch_exists or raw_page.get("raw_page_id") == "<would-create>":
                existing = None
                cross_batch_existing = None
                if not dry_run:
                    pass
                elif True:
                    # Even in dry-run, the cross-batch check can run for real
                    # (it does not depend on this batch's own not-yet-real id).
                    cross_batch_existing = find_existing_candidate_cross_batch(
                        repository, historical_import_key
                    )
            else:
                existing = find_existing_candidate(
                    repository, raw_page["raw_page_id"], title_normalized, candidate_type
                )
                cross_batch_existing = find_existing_candidate_cross_batch(
                    repository, historical_import_key
                )

            if cross_batch_existing is not None and existing is None:
                raise RuntimeError(
                    f"Cross-batch conflict: historical_import_key "
                    f"{historical_import_key!r} already exists as "
                    f"{cross_batch_existing.get('candidate_code')} in another batch."
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
                f"[{idx}/{EXPECTED_SELECTION_COUNT}] #{record_id} "
                f"'{row['title_raw']}' -> {status} ({candidate_code})"
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
    single_book = sum(1 for r in results if r["candidate_type"] == "SINGLE_BOOK")
    book_combo = sum(1 for r in results if r["candidate_type"] == "BOOK_COMBO")
    deterministic = sum(1 for r in results if r["extraction_source"] == "DETERMINISTIC")
    semantic = sum(1 for r in results if r["extraction_source"] == "CLAUDE_SEMANTIC")
    print(f"selected: {len(results)}")
    print(f"created_or_would_create: {created_count}")
    print(f"already_existed: {existed_count}")
    print(f"SINGLE_BOOK: {single_book}")
    print(f"BOOK_COMBO: {book_combo}")
    print(f"DETERMINISTIC: {deterministic}")
    print(f"CLAUDE_SEMANTIC: {semantic}")
    print(f"source_urls_created: {source_urls_created_count}")
    print(f"source_urls_reused: {source_urls_reused_count}")
    print(f"raw_pages_created: {raw_pages_created_count}")
    print(f"raw_pages_reused: {raw_pages_reused_count}")
    print()
    for r in results:
        print(
            f"{r['historical_record_id']} | {r['title']} | {r['candidate_type']} | "
            f"{r['extraction_source']} | {r['candidate_code']} | {r['status']}"
        )
    print()
    print(f"MODE: {'DRY_RUN' if dry_run else 'EXECUTE'}")
    print("HISTORICAL_IMPORT_RUN_COMPLETE: YES")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
