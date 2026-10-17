"""
Collect every image of a candidate's own MATCH Fahasa product page (cover,
gallery, back cover, "Đọc thử" preview pages) -- CLAUDE.md 14.8.

For each EXACT --candidate-code (bounded by --max-candidates), serially:

  1. local gates: candidate identity (IDENTITY_VERIFIED; an FB-HIST
     candidate anything but IDENTITY_CONFLICT), exactly one FAHASA MATCH
     product_reference of THIS candidate on fahasa.com, linked to an
     authorized FAHASA source_urls row.
  2. read the page (Playwright, the same public read the reference
     collector already does) and classify its images with
     image_rules.classify_fahasa_page_images (COVER / SUPPLEMENTARY /
     PREVIEW / OTHER_PRODUCT, in source order).
  3. per image, in page order: OTHER_PRODUCT (another volume's cover) is
     recorded and never downloaded; an image already registered for this
     candidate (same asset path) is reused; otherwise it is downloaded
     from the Fahasa CDN, hashed (SHA-256), measured, deduplicated by
     content hash (same candidate -> reused; another candidate -> recorded
     DUPLICATE_OTHER_CANDIDATE, never relinked), stored in the existing
     "product-images" bucket and registered as a product_images row with
     safe defaults (PENDING, RIGHTS_UNKNOWN, not main, not publish
     eligible). A failed download is recorded and the next image continues.
  4. the per-image manifest (url, asset path, role, position, sha256,
     download status, image_id, error) and the authorization provenance are
     merged into product_references.raw_metadata.page_images /
     page_images_collection -- every other raw_metadata key is kept.

Nothing is approved here: review_fahasa_gallery_images.py classifies each
image, and only an approved image ever becomes publish eligible. No
WooCommerce, price, identity or content writes. Idempotent: a re-run
reuses every registered image and rewrites the same manifest. Dry run
(read the page, print the manifest, write nothing) unless
--confirm-collect.

Usage:
  .venv/Scripts/python.exe scripts/collect_fahasa_gallery_images.py \
      --candidate-code FAHASA-2026-002-CAN-0001 --max-candidates 1
  ... --non-interactive --confirm-collect
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import download_bookstore_product_image as downloader  # noqa: E402
from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain.identity_status import MatchDecision  # noqa: E402
from src.domain.rules import image_rules  # noqa: E402

configure_utf8_console()

COLLECTOR_NAME = "fahasa_gallery_image_collector"
COLLECTOR_VERSION = "1.0.0"
MAX_CANDIDATES_HARD_CAP = 10
STORAGE_PREFIX = "fahasa"

# Per-image download statuses recorded in the manifest.
DOWNLOADED = "DOWNLOADED"
ALREADY_REGISTERED = "ALREADY_REGISTERED"
DUPLICATE = "DUPLICATE"
DUPLICATE_OTHER_CANDIDATE = "DUPLICATE_OTHER_CANDIDATE"
DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
REGISTRATION_FAILED = "REGISTRATION_FAILED"
SKIPPED_OTHER_PRODUCT = "SKIPPED_OTHER_PRODUCT"
PLANNED = "PLANNED"

# Per-candidate results.
COLLECTED = "COLLECTED"
DRY_RUN = "DRY_RUN"
LOCAL_NOT_READY = "LOCAL_NOT_READY"
PAGE_READ_FAILED = "PAGE_READ_FAILED"

_COLUMN_ROLE_BY_PAGE_ROLE = {
    image_rules.FAHASA_IMAGE_ROLE_COVER: "FRONT_COVER",
    image_rules.FAHASA_IMAGE_ROLE_PREVIEW: "INSIDE_PAGE",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CollectorPorts:
    """The I/O this collector performs, injectable for offline tests."""

    read_page_images: Callable[[str, str | None], list[dict[str, Any]]]
    fetch_image: Callable[[str, str], tuple[bytes, str]]
    storage_exists: Callable[[str], bool]
    storage_upload: Callable[[bytes, str, str], None]
    storage_remove: Callable[[str], None]


def _asset_path(url: str | None) -> str:
    return urlsplit(str(url or "")).path


def resolve_fahasa_target(repository: Any, candidate_code: str) -> dict[str, Any]:
    """Candidate + its single FAHASA MATCH reference + authorized source."""
    candidate = downloader.resolve_candidate(repository, candidate_code)
    references = [
        reference
        for reference in (
            repository.client.table("product_references").select("*")
            .eq("candidate_id", candidate["candidate_id"]).eq("source_type", "FAHASA")
            .eq("match_decision", MatchDecision.MATCH).execute().data
            or []
        )
    ]
    if len(references) != 1:
        raise RuntimeError(f"Expected exactly one FAHASA MATCH reference, found {len(references)}.")
    reference = references[0]
    if urlsplit(str(reference.get("source_url") or "")).hostname not in ("www.fahasa.com", "fahasa.com"):
        raise RuntimeError("The FAHASA MATCH reference page is not on fahasa.com.")
    if not reference.get("source_url_id"):
        raise RuntimeError("The FAHASA MATCH reference has no registered source_url_id.")
    source = downloader.resolve_authorized_reference_source(repository, reference["source_url_id"], "FAHASA")
    return {"candidate": candidate, "reference": reference, "source": source}


def _candidate_images(repository: Any, candidate_id: str) -> list[dict[str, Any]]:
    return (
        repository.client.table("product_images")
        .select("image_id, candidate_id, source_url, image_hash, image_status, storage_path")
        .eq("candidate_id", candidate_id).execute().data
        or []
    )


def _images_with_hash(repository: Any, image_hash: str) -> list[dict[str, Any]]:
    return (
        repository.client.table("product_images")
        .select("image_id, candidate_id, image_hash")
        .eq("image_hash", image_hash).execute().data
        or []
    )


def build_image_payload(
    target: dict[str, Any],
    entry: dict[str, Any],
    storage_path: str,
    content: bytes,
    content_type: str,
    image_hash: str,
) -> dict[str, Any]:
    """Safe-default product_images row: PENDING / RIGHTS_UNKNOWN (DB
    defaults), never main, never publish eligible."""
    candidate, reference, source = target["candidate"], target["reference"], target["source"]
    payload = {
        "candidate_id": candidate["candidate_id"],
        "reference_id": reference["reference_id"],
        "source_type": "FAHASA",
        "source_url": entry["url"],
        "source_photo_url": reference["source_url"],
        "source_url_id": source["source_url_id"],
        "storage_bucket": downloader.STORAGE_BUCKET,
        "storage_path": storage_path,
        "original_file_name": Path(entry["asset_path"]).name or "image",
        "mime_type": content_type,
        "file_size_bytes": len(content),
        "image_hash": image_hash,
        "image_role": _COLUMN_ROLE_BY_PAGE_ROLE.get(entry["role"]),
        "is_main_image_candidate": False,
        "is_selected_main_image": False,
        "is_publish_eligible": False,
        "collector_name": COLLECTOR_NAME,
        "collector_version": COLLECTOR_VERSION,
        "uploaded_at": utc_now(),
    }
    payload.update(downloader.measure_image_dimensions(content))
    return payload


def collect_entry(
    repository: Any,
    ports: CollectorPorts,
    target: dict[str, Any],
    entry: dict[str, Any],
    registered_by_path: dict[str, dict[str, Any]],
    registered_by_hash: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Download/register one page image; returns its manifest record.
    Never raises for a per-image problem -- the next image continues."""
    record = {key: entry.get(key) for key in ("position", "role", "url", "asset_path")}
    candidate = target["candidate"]

    if entry["role"] == image_rules.FAHASA_IMAGE_ROLE_OTHER_PRODUCT:
        return {**record, "download_status": SKIPPED_OTHER_PRODUCT}

    existing = registered_by_path.get(entry["asset_path"])
    if existing:
        return {
            **record,
            "download_status": ALREADY_REGISTERED,
            "image_id": existing.get("image_id"),
            "sha256": existing.get("image_hash"),
        }

    try:
        content, content_type = ports.fetch_image(entry["url"], target["reference"]["source_url"])
    except Exception as error:  # a dead/blocked asset fails only this image
        return {**record, "download_status": DOWNLOAD_FAILED, "error": f"{type(error).__name__}: {error}"}

    image_hash = downloader.calculate_sha256(content)
    record["sha256"] = image_hash
    same_candidate = registered_by_hash.get(image_hash)
    if same_candidate:
        return {**record, "download_status": DUPLICATE, "image_id": same_candidate.get("image_id")}
    foreign = [
        match for match in _images_with_hash(repository, image_hash)
        if str(match.get("candidate_id")) != str(candidate["candidate_id"])
    ]
    if foreign:
        return {
            **record,
            "download_status": DUPLICATE_OTHER_CANDIDATE,
            "error": f"content hash already registered for candidate {foreign[0].get('candidate_id')}",
        }

    extension = downloader.determine_extension(content_type=content_type, image_url=entry["url"])
    storage_path = f"{STORAGE_PREFIX}/{candidate['candidate_code']}/{image_hash[:16]}{extension}"
    uploaded = False
    try:
        if not ports.storage_exists(storage_path):
            ports.storage_upload(content, storage_path, content_type)
            uploaded = True
        payload = build_image_payload(target, entry, storage_path, content, content_type, image_hash)
        inserted = (repository.client.table("product_images").insert(payload).execute().data or [None])[0]
        if not inserted:
            raise RuntimeError("Supabase returned no product_images row after insert.")
    except Exception as error:
        if uploaded:
            try:
                ports.storage_remove(storage_path)
            except Exception:
                pass
        return {**record, "download_status": REGISTRATION_FAILED, "error": f"{type(error).__name__}: {error}"}

    registered_by_path[entry["asset_path"]] = inserted
    registered_by_hash[image_hash] = inserted
    return {
        **record,
        "download_status": DOWNLOADED,
        "image_id": inserted.get("image_id"),
        "width_pixels": payload.get("width_pixels"),
        "height_pixels": payload.get("height_pixels"),
        "storage_path": storage_path,
    }


def save_manifest(repository: Any, reference: dict[str, Any], manifest: list[dict[str, Any]]) -> None:
    """Merge the manifest into raw_metadata; every other key is kept."""
    raw_metadata = dict(reference.get("raw_metadata") or {})
    raw_metadata["page_images"] = manifest
    raw_metadata["page_images_collection"] = {
        "collector": COLLECTOR_NAME,
        "collector_version": COLLECTOR_VERSION,
        "collected_at": utc_now(),
        "page_url": reference.get("source_url"),
        "authorization": {
            key: image_rules.FAHASA_IMAGE_AUTHORIZATION[key]
            for key in ("policy", "adopted", "cover_policy_adopted", "granted_by")
        },
    }
    response = (
        repository.client.table("product_references")
        .update({"raw_metadata": raw_metadata, "updated_at": utc_now()})
        .eq("reference_id", reference["reference_id"]).execute()
    )
    if len(response.data or []) != 1:
        raise RuntimeError("Manifest update did not affect exactly one product_references row.")


def run_collect(
    repository: Any,
    ports: CollectorPorts,
    candidate_code: str,
    dry_run: bool,
) -> dict[str, Any]:
    report: dict[str, Any] = {"candidate_code": candidate_code, "result": None}
    try:
        target = resolve_fahasa_target(repository, candidate_code)
    except RuntimeError as error:
        report.update(result=LOCAL_NOT_READY, detail=str(error))
        return report

    reference = target["reference"]
    report["reference_page"] = reference["source_url"]
    try:
        entries = ports.read_page_images(reference["source_url"], reference.get("reference_image_url"))
    except Exception as error:
        report.update(result=PAGE_READ_FAILED, detail=f"{type(error).__name__}: {error}")
        return report

    if dry_run:
        manifest = [{**entry, "download_status": PLANNED} for entry in entries]
        report.update(result=DRY_RUN, images=manifest)
        return report

    existing = _candidate_images(repository, target["candidate"]["candidate_id"])
    registered_by_path = {_asset_path(image.get("source_url")): image for image in existing}
    registered_by_hash = {image["image_hash"]: image for image in existing if image.get("image_hash")}
    manifest = [
        collect_entry(repository, ports, target, entry, registered_by_path, registered_by_hash)
        for entry in entries
    ]
    for record in manifest:
        record["recorded_at"] = utc_now()
    save_manifest(repository, reference, manifest)

    counts: dict[str, int] = {}
    for record in manifest:
        counts[record["download_status"]] = counts.get(record["download_status"], 0) + 1
    report.update(result=COLLECTED, counts=counts, images=manifest)
    return report


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect every image of exact candidates' MATCH Fahasa pages.")
    parser.add_argument("--candidate-code", action="append", required=True, help="Exact candidate code. Repeat.")
    parser.add_argument("--max-candidates", type=int, required=True)
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--confirm-collect", action="store_true", help="Required to download/write; otherwise a dry run.")
    args = parser.parse_args(argv)
    if not 1 <= args.max_candidates <= MAX_CANDIDATES_HARD_CAP:
        parser.error(f"--max-candidates must be between 1 and {MAX_CANDIDATES_HARD_CAP}.")
    if len(set(args.candidate_code)) != len(args.candidate_code):
        parser.error("--candidate-code values must be unique.")
    if len(args.candidate_code) > args.max_candidates:
        parser.error("More --candidate-code values than --max-candidates.")
    return args


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_arguments()

    from playwright.sync_api import sync_playwright

    import collect_reference_metadata as reference_metadata
    from src.repositories.supabase_repository import SupabaseRepository

    repository = SupabaseRepository()
    with sync_playwright() as playwright:
        browser, context = reference_metadata.create_browser(playwright)
        try:
            def read_page_images(page_url: str, cover_url: str | None) -> list[dict[str, Any]]:
                page, _text, _html, _title = reference_metadata.collect_page(context=context, source_url=page_url)
                try:
                    return reference_metadata.extract_fahasa_page_images(page, cover_url)
                finally:
                    page.close()

            def fetch_image(image_url: str, referer: str) -> tuple[bytes, str]:
                return downloader.download_image_bytes(context=context, image_url=image_url, referer_url=referer)

            ports = CollectorPorts(
                read_page_images=read_page_images,
                fetch_image=fetch_image,
                storage_exists=lambda path: downloader.storage_file_exists(repository, path),
                storage_upload=lambda content, path, content_type: downloader.upload_storage_bytes(
                    repository, content, path, content_type
                ),
                storage_remove=lambda path: downloader.remove_storage_file(repository, path),
            )
            for candidate_code in args.candidate_code:  # serial: one candidate at a time
                report = run_collect(repository, ports, candidate_code, dry_run=not args.confirm_collect)
                print(f"FAHASA_GALLERY_COLLECT_RESULT {json.dumps(report, ensure_ascii=False, default=str)}")
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
