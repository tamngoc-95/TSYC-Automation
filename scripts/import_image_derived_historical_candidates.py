"""
GENERIC bounded image-derived historical Facebook candidate importer --
distinct from import_historical_facebook_candidates.py (which imports
from the offline-validated final_preview.csv semantic-extraction
pipeline). This importer's candidates were extracted by a different,
equally honestly-labeled method: direct visual review of each source
image by Claude, image by image, against the raw Facebook export
archive -- not the DETERMINISTIC/AI_ASSISTED offline provider.

Writes to the SAME production Supabase tables, using the SAME repository
query-builder patterns and the SAME FK chain as the existing sanctioned
importer (batches -> source_urls -> raw_pages -> product_candidates ->
process_logs), so this is additive and structurally consistent with the
rest of the pipeline -- never raw SQL, never touching an existing row.

Refactored (as of batch 3) from a per-batch hardcoded script into a
single generic, config-driven tool: each bounded batch's identity and
selection now live in a small JSON config file under
data/processed/image_derived_batches/, not in a new copy of this script.
Batch 1 (record #1018, FB-HIST-2026-IMG-001) and batch 2 (record #1399,
FB-HIST-2026-IMG-002) already ran via their own now-superseded hardcoded
scripts before this refactor; this generic tool is for batch 3 onward.

Config schema:
{
  "batch_code": "FB-HIST-2026-IMG-003",
  "batch_name": "...",
  "batch_description": "...",
  "media_path_prefix": "your_facebook_activity/posts/media/.../",
  "records": [
    {
      "historical_record_id": "1136",
      "historical_post_date": "...",
      "items": [
        {"title": "...", "type": "SINGLE_BOOK", "author": null, "image": "....jpg", "evidence": "..."},
        ...
      ]
    },
    ...
  ]
}

Never fabricates ISBN, publisher, weight, dimensions, or reference
matches (CLAUDE.md section 2.2) -- "author" is populated only where a
name was actually legible on the image.

Idempotency: identical to the original per-batch scripts --
product_candidates is deduped by (raw_page_id, normalized title,
candidate_type) before any insert; raw_pages by (batch_id, content_hash);
source_urls by upsert-on-conflict. Rerunning this script with the same
config is a no-op for every candidate already imported.

Usage:
    .venv/Scripts/python.exe scripts/import_image_derived_historical_candidates.py --batch-config data/processed/image_derived_batches/img_003.json
    .venv/Scripts/python.exe scripts/import_image_derived_historical_candidates.py --batch-config data/processed/image_derived_batches/img_003.json --execute
"""
from __future__ import annotations

import argparse
import hashlib
import json
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

configure_utf8_console()

SCRIPT_VERSION = "2.0.0"

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

IMPORT_TYPE = "HISTORICAL_FACEBOOK_EXPORT_IMAGE_DERIVED"
IMPORTER_NAME = "image_derived_historical_candidate_importer"
EXTRACTION_SOURCE = "MANUAL_VISUAL_REVIEW"
EXTRACTOR_NAME = "claude_sonnet_5_vision_manual_review"
EXTRACTOR_VERSION = "2026-09-19-session"

REVIEW_REASON = (
    "Candidate was found by direct visual review of one historical "
    "Facebook export record's images (image-derived recovery batch; "
    "see source_evidence.historical_import_key and local_media_paths). "
    "Book identity, ISBN, publisher, weight, dimensions, and reference "
    "matches still require verification -- none were fabricated at "
    "import time."
)


def _normalize_for_dedupe(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def load_batch_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"Batch config not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def get_or_create_batch(
    repository: SupabaseRepository, batch_code: str, batch_name: str, batch_description: str, dry_run: bool
) -> tuple[dict[str, Any], bool]:
    existing = repository.get_batch_by_code(batch_code)
    if existing is not None:
        return existing, False
    if dry_run:
        return ({"batch_id": "<would-create>", "batch_code": batch_code}, True)
    created = repository.create_batch(
        batch_code=batch_code, batch_name=batch_name, description=batch_description
    )
    return created, True


def historical_source_url(record_id: str) -> str:
    return f"facebook-export://{DEFAULT_SOURCE_EXPORT_LABEL}#record={record_id}"


def get_or_create_source_url(
    repository: SupabaseRepository, batch_id: str, batch_exists: bool,
    record_id: str, date_text: str, dry_run: bool,
) -> tuple[dict[str, Any], bool]:
    source_url = historical_source_url(record_id)
    if batch_exists:
        existing_response = (
            repository.client.table("source_urls")
            .select("source_url_id, batch_id, source_type, source_url, source_name, crawl_status")
            .eq("batch_id", batch_id).eq("source_type", "FACEBOOK_POST").eq("source_url", source_url)
            .limit(1).execute()
        )
        existing_rows = existing_response.data or []
        if existing_rows:
            return existing_rows[0], False
    if dry_run:
        return ({"source_url_id": "<would-create>", "source_url": source_url}, True)
    created = repository.save_source_url(
        batch_id=batch_id, source_url=source_url,
        selection_reason=f"Historical Facebook export record #{record_id} ({date_text})",
        active=True, source_type="FACEBOOK_POST",
    )
    repository.update_source_url_status(source_url_id=created["source_url_id"], crawl_status="COLLECTED")
    created["crawl_status"] = "COLLECTED"
    return created, True


def get_or_create_raw_page(
    repository: SupabaseRepository, batch_id: str, batch_exists: bool,
    source_url_row: dict[str, Any], record_id: str, cleaned_text: str,
    content_hash_full: str, dry_run: bool,
) -> tuple[dict[str, Any], bool]:
    if batch_exists:
        existing_response = (
            repository.client.table("raw_pages")
            .select("raw_page_id, batch_id, source_url_id, page_type, page_url, content_hash, cleaning_status")
            .eq("batch_id", batch_id).eq("content_hash", content_hash_full).limit(1).execute()
        )
        existing_rows = existing_response.data or []
        if existing_rows:
            return existing_rows[0], False
    if dry_run:
        return ({"raw_page_id": "<would-create>", "content_hash": content_hash_full}, True)
    payload = {
        "batch_id": batch_id,
        "source_url_id": source_url_row["source_url_id"],
        "page_type": "FACEBOOK_POST",
        "page_url": source_url_row["source_url"],
        "raw_title": f"Historical Facebook export record #{record_id}",
        "raw_text": cleaned_text,
        "raw_html": None,
        "content_hash": content_hash_full,
        "collector_name": "image_derived_historical_facebook_export_importer",
        "collector_version": SCRIPT_VERSION,
    }
    response = repository.client.table("raw_pages").insert(payload).execute()
    records = response.data or []
    if not records:
        raise RuntimeError(f"raw_pages insert returned no data for record #{record_id}.")
    raw_page = records[0]
    update_response = (
        repository.client.table("raw_pages")
        .update({
            "cleaned_text": cleaned_text,
            "cleaning_status": "CLEANED",
            "cleaning_method": "historical_text_cleaner.clean_historical_facebook_text",
            "cleaned_at": datetime.now(timezone.utc).isoformat(),
        })
        .eq("raw_page_id", raw_page["raw_page_id"]).execute()
    )
    updated_records = update_response.data or []
    if updated_records:
        raw_page = updated_records[0]
    return raw_page, True


def find_existing_candidate(
    repository: SupabaseRepository, raw_page_id: str, title_normalized: str, candidate_type: str,
) -> dict[str, Any] | None:
    response = (
        repository.client.table("product_candidates")
        .select("candidate_id, candidate_code, raw_page_id, extracted_title, candidate_type, workflow_status, review_required, source_evidence")
        .eq("raw_page_id", raw_page_id).eq("candidate_type", candidate_type).execute()
    )
    target = _normalize_for_dedupe(title_normalized)
    for record in response.data or []:
        if _normalize_for_dedupe(record.get("extracted_title") or "") == target:
            return record
    return None


def get_next_candidate_code(repository: SupabaseRepository, batch_id: str, batch_code: str) -> str:
    pattern = re.compile(rf"^{re.escape(batch_code)}-CAN-(\d+)$")
    response = repository.client.table("product_candidates").select("candidate_code").eq("batch_id", batch_id).execute()
    highest = 0
    for record in response.data or []:
        match = pattern.match(str(record.get("candidate_code") or ""))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{batch_code}-CAN-{highest + 1:04d}"


def build_source_evidence(
    item: dict[str, Any], raw_page: dict[str, Any], source_url_row: dict[str, Any],
    batch_code: str, record_id: str, post_date: str, media_path_prefix: str,
) -> dict[str, Any]:
    title_normalized = _normalize_for_dedupe(item["title"])
    return {
        "import_type": IMPORT_TYPE,
        "importer_name": IMPORTER_NAME,
        "importer_version": SCRIPT_VERSION,
        "batch_code": batch_code,
        "source_type": "FACEBOOK",
        "page_type": "FACEBOOK_POST",
        "raw_page_id": raw_page.get("raw_page_id"),
        "source_url_id": source_url_row.get("source_url_id"),
        "source_url": source_url_row.get("source_url"),
        "historical_record_id": record_id,
        "historical_post_date": post_date,
        "historical_export_file": DEFAULT_SOURCE_EXPORT_LABEL,
        "extraction_source": EXTRACTION_SOURCE,
        "title_raw": item["title"],
        "title_normalized": title_normalized,
        "evidence_text": item.get("evidence"),
        "confidence": 0.9,
        "completeness_status": "VISUALLY_CONFIRMED",
        "local_media_count": 1,
        "local_media_paths": [media_path_prefix + item["image"]],
        "historical_import_key": f"{record_id}:{title_normalized}:{item['type']}",
        "extraction_method": "AI_ASSISTED",
        "extractor_name": EXTRACTOR_NAME,
        "extractor_version": EXTRACTOR_VERSION,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }


def build_candidate_payload(
    batch: dict[str, Any], raw_page: dict[str, Any], source_url_row: dict[str, Any], item: dict[str, Any],
    candidate_code: str, batch_code: str, record_id: str, post_date: str, media_path_prefix: str,
) -> dict[str, Any]:
    evidence = build_source_evidence(item, raw_page, source_url_row, batch_code, record_id, post_date, media_path_prefix)
    return {
        "batch_id": batch["batch_id"],
        "candidate_code": candidate_code,
        "candidate_type": item["type"],
        "combo_group_code": None,
        "raw_page_id": raw_page["raw_page_id"],
        "source_url_id": source_url_row["source_url_id"],
        "extracted_title": item["title"],
        "extracted_author": item.get("author"),
        "possible_isbn": None,
        "workflow_status": "EXTRACTED",
        "extraction_confidence": 0.9,
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
    parser.add_argument("--batch-config", required=True, help="Path to this batch's JSON config file.")
    parser.add_argument("--execute", action="store_true", help="Perform real Supabase writes. Without this flag, runs as a dry run.")
    args = parser.parse_args()
    dry_run = not args.execute

    config = load_batch_config(Path(args.batch_config))
    batch_code = config["batch_code"]
    batch_name = config["batch_name"]
    batch_description = config["batch_description"]
    media_path_prefix = config["media_path_prefix"]
    records = config["records"]
    total_items = sum(len(r["items"]) for r in records)

    print(f"TSYC generic image-derived historical candidate import -- v{SCRIPT_VERSION}")
    print(f"MODE: {'DRY RUN (no writes)' if dry_run else 'EXECUTE (real Supabase writes)'}")
    print(f"BATCH_CODE: {batch_code}")
    print(f"records: {[r['historical_record_id'] for r in records]}")
    print(f"total selected items: {total_items}")
    print()

    export_records = load_facebook_history_export(DEFAULT_SOURCE_EXPORT)
    text_by_id = {r.record_index: r.full_text for r in export_records}

    repository = SupabaseRepository()

    batch, batch_created = get_or_create_batch(repository, batch_code, batch_name, batch_description, dry_run)
    batch_exists = not (batch_created and dry_run)
    print(f"BATCH: {batch.get('batch_code', batch_code)} "
          f"({'would create' if (batch_created and dry_run) else 'created' if batch_created else 'already existed'})")
    print()

    results: list[dict[str, Any]] = []
    overall_idx = 0
    stop = False

    for record in records:
        if stop:
            break
        record_id = record["historical_record_id"]
        post_date = record["historical_post_date"]
        items = record["items"]

        full_text = text_by_id.get(int(record_id), "")
        if not full_text:
            raise RuntimeError(f"Historical record #{record_id} not found in export.")
        cleaned_text = clean_historical_facebook_text(full_text)
        content_hash_full = hashlib.sha256(cleaned_text.encode("utf-8")).hexdigest()

        source_url_row, _ = get_or_create_source_url(repository, batch["batch_id"], batch_exists, record_id, post_date, dry_run)
        raw_page, _ = get_or_create_raw_page(repository, batch["batch_id"], batch_exists, source_url_row, record_id, cleaned_text, content_hash_full, dry_run)
        print(f"RECORD #{record_id} -> RAW_PAGE: {raw_page.get('raw_page_id')} ({len(items)} items)")

        for item in items:
            overall_idx += 1
            try:
                title_normalized = _normalize_for_dedupe(item["title"])
                if not batch_exists or raw_page.get("raw_page_id") == "<would-create>":
                    existing = None
                else:
                    existing = find_existing_candidate(repository, raw_page["raw_page_id"], title_normalized, item["type"])

                if existing is not None:
                    status = "ALREADY_EXISTED"
                    candidate_code = existing["candidate_code"]
                    candidate_id = existing["candidate_id"]
                elif dry_run:
                    status = "WOULD_CREATE"
                    candidate_code = "<would-assign>"
                    candidate_id = "<would-create>"
                else:
                    candidate_code = get_next_candidate_code(repository, batch["batch_id"], batch_code)
                    payload = build_candidate_payload(
                        batch, raw_page, source_url_row, item, candidate_code,
                        batch_code, record_id, post_date, media_path_prefix,
                    )
                    created = insert_candidate(repository, payload)
                    status = "CREATED"
                    candidate_code = created["candidate_code"]
                    candidate_id = created["candidate_id"]
                    repository.write_process_log(
                        message=f"Image-derived historical import: candidate {candidate_code} created from record #{record_id}, image {item['image']}.",
                        process_name="image_derived_historical_candidate_import",
                        batch_id=batch["batch_id"], candidate_id=candidate_id,
                        process_step="IMPORT_CANDIDATE", log_level="INFO", status="SUCCESS",
                    )

                results.append({"index": overall_idx, "record_id": record_id, "title": item["title"], "type": item["type"], "status": status, "candidate_code": candidate_code, "candidate_id": candidate_id})
                print(f"[{overall_idx}/{total_items}] #{record_id} '{item['title']}' -> {status} ({candidate_code})")

            except Exception as exc:  # noqa: BLE001
                print()
                print(f"STOP: unexpected failure on item {overall_idx} (record #{record_id}, '{item['title']}'): {exc}")
                if not dry_run:
                    repository.write_process_log(
                        message=f"Image-derived historical import STOPPED on item {overall_idx}: {exc}",
                        process_name="image_derived_historical_candidate_import",
                        batch_id=batch["batch_id"], process_step="IMPORT_CANDIDATE",
                        log_level="CRITICAL", status="FAILED",
                        error_details={"index": overall_idx, "record_id": record_id, "title": item["title"], "error": str(exc)},
                    )
                stop = True
                break

    print()
    print("=== SUMMARY ===")
    created_count = sum(1 for r in results if r["status"] in {"CREATED", "WOULD_CREATE"})
    existed_count = sum(1 for r in results if r["status"] == "ALREADY_EXISTED")
    print(f"selected: {len(results)}")
    print(f"created_or_would_create: {created_count}")
    print(f"already_existed: {existed_count}")
    print()
    print(f"MODE: {'DRY_RUN' if dry_run else 'EXECUTE'}")
    print("IMAGE_DERIVED_HISTORICAL_IMPORT_RUN_COMPLETE: YES")
    return 1 if stop else 0


if __name__ == "__main__":
    raise SystemExit(main())
