"""
Download and register one individual product image from the exact
already-verified BOOKSTORE reference page for one TSYC candidate.

This script operates on exactly one candidate per run. It never crawls,
searches, or discovers pages -- it loads only the single, already
registered and authorized source_urls row linked through the candidate's
existing MATCH / BOOKSTORE product_reference.

Safety guarantees:
- Exact candidate targeting only. No batch mode, no automatic selection,
  no fallback to another candidate or another source.
- All precondition gates (identity, reference, source, internal product,
  blocking review issues) are verified before Playwright is ever launched.
- In --non-interactive mode, --candidate-code and --confirm-download are
  both required before any database or browser work begins; no input()
  call can ever be reached.
- Reuses collect_reference_metadata.py's create_browser(), collect_page(),
  and extract_image_url() (JSON-LD -> og:image -> CSS fallback) instead of
  reimplementing extraction logic.
- Reuses the existing "product-images" Supabase Storage bucket -- no new
  storage mechanism is introduced.
- Duplicate protection mirrors upload_facebook_images_to_supabase.py:
  same candidate + same source URL, and same candidate + same image hash,
  are both treated as already-registered (no new row). The same image
  hash already linked to a DIFFERENT candidate is refused outright.
- The inserted product_images row is always PENDING, with
  is_selected_main_image, is_publish_eligible, and is_main_image_candidate
  all False. Nothing is auto-approved. A human/review_product_images.py
  pass is required before the image can become a validated main image.
- product_references.reference_image_url is never written by this script.
- No WooCommerce, pricing, candidate identity, or content writes occur.
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import (
    BrowserContext,
    Page,
    sync_playwright,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from src.cli_bootstrap import configure_utf8_console
from src.repositories.supabase_repository import SupabaseRepository
import collect_reference_metadata as reference_metadata

configure_utf8_console()


BATCH_CODE = "FB-2026-001"
STORAGE_BUCKET = "product-images"

COLLECTOR_NAME = "bookstore_product_image_downloader"
COLLECTOR_VERSION = "0.1.0"

SOURCE_TYPE = "BOOKSTORE"
BOOKSTORE_SOURCE_PRIORITY = 3

LOCAL_IMAGE_ROOT = (
    PROJECT_ROOT
    / "data"
    / "raw"
    / "bookstore-images"
    / BATCH_CODE
)

ALLOWED_MIME_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/gif",
}

CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

# Conservative safety net. JSON-LD / og:image rarely trigger this, but the
# CSS-fallback tier can pick up storefront chrome without it.
REJECTABLE_URL_KEYWORDS = (
    "logo",
    "favicon",
    "sprite",
    "placeholder",
    "spinner",
    "loading",
    "/icons/",
    "icon-",
    "banner",
    "avatar",
)

ACTIVE_REVIEW_ISSUE_STATUSES = (
    "OPEN",
    "IN_REVIEW",
)


def utc_now() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(
        timezone.utc
    ).isoformat()


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Download and register one individual product image from the "
            "exact already-verified BOOKSTORE reference page for one "
            "candidate. Exact targeting only -- there is no batch mode."
        )
    )

    parser.add_argument(
        "--candidate-code",
        default=None,
        help=(
            "Exact product candidate code to target (required in all "
            "modes -- there is no batch mode, discovery mode, or "
            "fallback candidate selection)."
        ),
    )

    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help=(
            "Disable all input() prompts. Requires --candidate-code and "
            "--confirm-download."
        ),
    )

    parser.add_argument(
        "--confirm-download",
        action="store_true",
        help=(
            "Explicit confirmation to download and register the image. "
            "Required with --non-interactive; the browser will not be "
            "launched in non-interactive mode without it."
        ),
    )

    return parser.parse_args()


# ---------------------------------------------------------------------------
# Precondition resolution / gates
# ---------------------------------------------------------------------------


def resolve_candidate(
    repository: SupabaseRepository,
    candidate_code: str,
) -> dict[str, Any]:
    """Resolve exactly one candidate and verify identity_status."""
    response = (
        repository.client
        .table("product_candidates")
        .select(
            "candidate_id, "
            "candidate_code, "
            "extracted_title, "
            "verified_title, "
            "verified_author, "
            "identity_status"
        )
        .eq(
            "candidate_code",
            candidate_code,
        )
        .limit(2)
        .execute()
    )

    rows = response.data or []

    if not rows:
        raise RuntimeError(
            "No product candidate was found for candidate_code: "
            f"{candidate_code}"
        )

    if len(rows) != 1:
        raise RuntimeError(
            "candidate_code did not resolve to exactly one candidate: "
            f"{candidate_code}"
        )

    candidate = rows[0]

    if candidate.get("identity_status") != "IDENTITY_VERIFIED":
        raise RuntimeError(
            "Candidate identity_status is not IDENTITY_VERIFIED "
            f"(current: {candidate.get('identity_status')!r}): "
            f"{candidate_code}"
        )

    return candidate


def resolve_match_bookstore_reference(
    repository: SupabaseRepository,
    candidate_id: str,
) -> dict[str, Any]:
    """Resolve exactly one MATCH / BOOKSTORE product_reference."""
    response = (
        repository.client
        .table("product_references")
        .select(
            "reference_id, "
            "candidate_id, "
            "source_url_id, "
            "source_type, "
            "source_priority, "
            "match_decision, "
            "reference_title"
        )
        .eq(
            "candidate_id",
            candidate_id,
        )
        .eq(
            "match_decision",
            "MATCH",
        )
        .execute()
    )

    rows = response.data or []

    if not rows:
        raise RuntimeError(
            "No MATCH product_reference exists for this candidate."
        )

    if len(rows) != 1:
        raise RuntimeError(
            "Candidate has more than one MATCH product_reference. Exact "
            "targeting requires exactly one relevant match; resolve the "
            "ambiguity before downloading an image."
        )

    reference = rows[0]

    if reference.get("source_type") != "BOOKSTORE":
        raise RuntimeError(
            "The MATCH product_reference is not source_type BOOKSTORE "
            f"(found: {reference.get('source_type')!r})."
        )

    if reference.get("source_priority") != BOOKSTORE_SOURCE_PRIORITY:
        raise RuntimeError(
            "The MATCH product_reference source_priority is not "
            f"compatible with BOOKSTORE (expected "
            f"{BOOKSTORE_SOURCE_PRIORITY}, found "
            f"{reference.get('source_priority')!r})."
        )

    if not reference.get("source_url_id"):
        raise RuntimeError(
            "The MATCH product_reference has no source_url_id."
        )

    return reference


def resolve_authorized_bookstore_source(
    repository: SupabaseRepository,
    source_url_id: str,
) -> dict[str, Any]:
    """Resolve exactly one authorized BOOKSTORE source_urls row."""
    response = (
        repository.client
        .table("source_urls")
        .select(
            "source_url_id, "
            "batch_id, "
            "source_type, "
            "source_url, "
            "is_authorized, "
            "crawl_status"
        )
        .eq(
            "source_url_id",
            source_url_id,
        )
        .limit(2)
        .execute()
    )

    rows = response.data or []

    if not rows:
        raise RuntimeError(
            "No source_urls row was found for source_url_id: "
            f"{source_url_id}"
        )

    if len(rows) != 1:
        raise RuntimeError(
            "source_url_id did not resolve to exactly one source_urls "
            f"row: {source_url_id}"
        )

    source = rows[0]

    if source.get("source_type") != "BOOKSTORE":
        raise RuntimeError(
            "The linked source_urls row is not source_type BOOKSTORE "
            f"(found: {source.get('source_type')!r})."
        )

    if source.get("is_authorized") is not True:
        raise RuntimeError(
            "The linked source_urls row is not authorized "
            "(is_authorized != true)."
        )

    if not str(source.get("source_url") or "").strip():
        raise RuntimeError(
            "The linked source_urls row has an empty source_url."
        )

    return source


def resolve_internal_product(
    repository: SupabaseRepository,
    candidate_id: str,
) -> dict[str, Any]:
    """Resolve exactly one internal_products row for this candidate."""
    response = (
        repository.client
        .table("internal_products")
        .select(
            "internal_product_id, "
            "product_code, "
            "candidate_id"
        )
        .eq(
            "candidate_id",
            candidate_id,
        )
        .limit(2)
        .execute()
    )

    rows = response.data or []

    if not rows:
        raise RuntimeError(
            "No internal_product exists for this candidate."
        )

    if len(rows) != 1:
        raise RuntimeError(
            "candidate_id did not resolve to exactly one internal_product."
        )

    return rows[0]


def check_no_blocking_review_issues(
    repository: SupabaseRepository,
    candidate_id: str,
) -> None:
    """Refuse to proceed while an unresolved review_issues row exists."""
    response = (
        repository.client
        .table("review_issues")
        .select(
            "review_issue_id, "
            "issue_type, "
            "issue_status, "
            "issue_severity"
        )
        .eq(
            "candidate_id",
            candidate_id,
        )
        .in_(
            "issue_status",
            list(ACTIVE_REVIEW_ISSUE_STATUSES),
        )
        .execute()
    )

    rows = response.data or []

    if rows:
        raise RuntimeError(
            "Blocking/unresolved review_issues exist for this candidate "
            f"({len(rows)} row(s)); resolve them before downloading "
            "images."
        )


# ---------------------------------------------------------------------------
# Image extraction (reuses collect_reference_metadata.py helpers)
# ---------------------------------------------------------------------------


def determine_image_extraction_method(
    product_json_ld: dict[str, Any] | None,
    page: Page,
) -> str:
    """
    Report which extraction tier supplied the image URL.

    This mirrors, without re-implementing, the tier order already used by
    reference_metadata.extract_image_url(): JSON-LD product image, then
    og:image, then a CSS fallback selector.
    """
    if product_json_ld and product_json_ld.get("image"):
        return "JSON_LD_PRODUCT_IMAGE"

    if reference_metadata.get_meta_content(
        page,
        'meta[property="og:image"]',
    ):
        return "OG_IMAGE"

    return "CSS_FALLBACK"


def reject_reason_for_image_url(
    image_url: str,
) -> str | None:
    """Return a rejection reason for an obviously non-product image URL."""
    lowered = image_url.lower()

    for keyword in REJECTABLE_URL_KEYWORDS:
        if keyword in lowered:
            return (
                "Image URL contains a rejected keyword: "
                f"{keyword!r}"
            )

    return None


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------


def normalize_content_type(
    raw_content_type: str | None,
) -> str:
    """Normalize an HTTP Content-Type header."""
    if not raw_content_type:
        return ""

    return (
        raw_content_type
        .split(";", maxsplit=1)[0]
        .strip()
        .lower()
    )


def determine_extension(
    content_type: str,
    image_url: str,
) -> str:
    """Determine a safe file extension for one downloaded image."""
    extension = CONTENT_TYPE_EXTENSIONS.get(
        content_type
    )

    if extension:
        return extension

    suffix = Path(
        urlparse(image_url).path
    ).suffix.lower()

    if suffix == ".jpeg":
        return ".jpg"

    if suffix in {".jpg", ".png", ".webp", ".gif"}:
        return suffix

    return ".bin"


def calculate_sha256(
    content: bytes,
) -> str:
    """Calculate the SHA-256 digest of downloaded content."""
    return hashlib.sha256(
        content
    ).hexdigest()


def download_image_bytes(
    context: BrowserContext,
    image_url: str,
    referer_url: str,
) -> tuple[bytes, str]:
    """Download one BOOKSTORE product image and validate its content."""
    response = context.request.get(
        image_url,
        headers={
            "Referer": referer_url,
            "Accept": (
                "image/avif,image/webp,image/apng,"
                "image/svg+xml,image/*,*/*;q=0.8"
            ),
        },
        timeout=90_000,
        fail_on_status_code=False,
    )

    if not response.ok:
        raise RuntimeError(
            "BOOKSTORE image download failed with HTTP status "
            f"{response.status}."
        )

    content = response.body()

    content_type = normalize_content_type(
        response.headers.get("content-type")
    )

    if content_type not in ALLOWED_MIME_TYPES:
        raise RuntimeError(
            "Downloaded content is not an allowed image type: "
            f"{content_type or '[missing]'}"
        )

    if not content:
        raise RuntimeError(
            "Downloaded image content is empty."
        )

    return content, content_type


# ---------------------------------------------------------------------------
# Local recovery cache (mirrors the layout style of the Facebook workflow)
# ---------------------------------------------------------------------------


def save_local_cache_copy(
    candidate_code: str,
    image_url: str,
    page_url: str,
    reference_id: str,
    source_url_id: str,
    extraction_method: str,
    content: bytes,
    content_type: str,
    image_hash: str,
    extension: str,
) -> Path:
    """
    Write a local recovery copy of the downloaded image and its metadata.

    The filename is derived from the content hash, so a path collision can
    only ever mean identical content -- an existing file is never
    overwritten with different bytes.
    """
    directory = (
        LOCAL_IMAGE_ROOT
        / candidate_code
    )

    directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    hash_prefix = image_hash[:16]

    image_path = (
        directory
        / f"image_{hash_prefix}{extension}"
    )

    metadata_path = (
        directory
        / f"image_{hash_prefix}.json"
    )

    if not image_path.exists():
        image_path.write_bytes(
            content
        )

    metadata = {
        "batch_code": BATCH_CODE,
        "candidate_code": candidate_code,
        "reference_id": reference_id,
        "source_url_id": source_url_id,
        "source_type": SOURCE_TYPE,
        "bookstore_page_url": page_url,
        "bookstore_image_url": image_url,
        "extraction_method": extraction_method,
        "content_type": content_type,
        "file_size_bytes": len(content),
        "sha256": image_hash,
        "collector_name": COLLECTOR_NAME,
        "collector_version": COLLECTOR_VERSION,
        "downloaded_at": utc_now(),
    }

    metadata_path.write_text(
        json.dumps(
            metadata,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return image_path


# ---------------------------------------------------------------------------
# Supabase Storage (reuses the existing "product-images" bucket)
# ---------------------------------------------------------------------------


def build_storage_path(
    candidate_code: str,
    image_hash: str,
    extension: str,
) -> str:
    """Build a deterministic Storage path using SHA-256."""
    hash_prefix = image_hash[:16]

    return (
        f"bookstore/{BATCH_CODE}/{candidate_code}/"
        f"{hash_prefix}{extension}"
    )


def storage_file_exists(
    repository: SupabaseRepository,
    storage_path: str,
) -> bool:
    """Check whether an image already exists in Supabase Storage."""
    storage_path_object = Path(
        storage_path
    )

    folder_path = (
        storage_path_object
        .parent
        .as_posix()
    )

    file_name = storage_path_object.name

    storage_items = (
        repository.client.storage
        .from_(STORAGE_BUCKET)
        .list(
            folder_path,
            {
                "limit": 100,
                "offset": 0,
                "search": file_name,
            },
        )
    )

    return any(
        storage_item.get("name") == file_name
        for storage_item in storage_items
    )


def upload_storage_bytes(
    repository: SupabaseRepository,
    content: bytes,
    storage_path: str,
    content_type: str,
) -> None:
    """Upload one in-memory image to Supabase Storage."""
    repository.client.storage.from_(
        STORAGE_BUCKET
    ).upload(
        path=storage_path,
        file=content,
        file_options={
            "content-type": content_type,
            "cache-control": "3600",
            "upsert": False,
        },
    )


def remove_storage_file(
    repository: SupabaseRepository,
    storage_path: str,
) -> None:
    """Remove a Storage file after a failed database insert."""
    repository.client.storage.from_(
        STORAGE_BUCKET
    ).remove(
        [storage_path]
    )


# ---------------------------------------------------------------------------
# Deduplication (mirrors upload_facebook_images_to_supabase.py behavior)
# ---------------------------------------------------------------------------


def find_existing_by_source_url(
    repository: SupabaseRepository,
    candidate_id: str,
    source_url: str,
) -> dict[str, Any] | None:
    """Find an existing image for this candidate with the same source URL."""
    response = (
        repository.client
        .table("product_images")
        .select(
            "image_id, "
            "candidate_id, "
            "source_url, "
            "image_hash, "
            "image_status"
        )
        .eq(
            "candidate_id",
            candidate_id,
        )
        .eq(
            "source_url",
            source_url,
        )
        .limit(1)
        .execute()
    )

    records = response.data or []

    return records[0] if records else None


def find_existing_by_hash(
    repository: SupabaseRepository,
    image_hash: str,
) -> list[dict[str, Any]]:
    """Find all existing images (any candidate) with the same content hash."""
    response = (
        repository.client
        .table("product_images")
        .select(
            "image_id, "
            "candidate_id, "
            "source_url, "
            "image_hash, "
            "image_status"
        )
        .eq(
            "image_hash",
            image_hash,
        )
        .execute()
    )

    return response.data or []


# ---------------------------------------------------------------------------
# product_images payload (safe defaults, mirrors upload_facebook_*.py)
# ---------------------------------------------------------------------------


def build_product_images_payload(
    candidate: dict[str, Any],
    reference: dict[str, Any],
    source: dict[str, Any],
    image_url: str,
    page_url: str,
    storage_path: str,
    original_file_name: str,
    mime_type: str,
    file_size_bytes: int,
    image_hash: str,
) -> dict[str, Any]:
    """Build a valid, safe-default product_images insert payload."""
    return {
        "candidate_id": candidate["candidate_id"],
        "reference_id": reference["reference_id"],

        # Valid value from product_images_source_type_check.
        "source_type": SOURCE_TYPE,

        # The actual product image asset URL.
        "source_url": image_url,

        # The exact verified BOOKSTORE product page URL that contains it.
        "source_photo_url": page_url,

        "source_url_id": source["source_url_id"],

        "storage_bucket": STORAGE_BUCKET,
        "storage_path": storage_path,

        "original_file_name": original_file_name,
        "mime_type": mime_type,
        "file_size_bytes": file_size_bytes,
        "image_hash": image_hash,

        # NULL is valid according to product_images_image_role_check.
        # Role assignment belongs to review_product_images.py.
        "image_role": None,

        "is_main_image_candidate": False,
        "is_selected_main_image": False,
        "is_publish_eligible": False,

        # image_status and usage_rights_status are intentionally omitted.
        # The database assigns the valid defaults: PENDING / RIGHTS_UNKNOWN.

        "collector_name": COLLECTOR_NAME,
        "collector_version": COLLECTOR_VERSION,

        "uploaded_at": utc_now(),
    }


def insert_product_image(
    repository: SupabaseRepository,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Insert one product_images record."""
    response = (
        repository.client
        .table("product_images")
        .insert(payload)
        .execute()
    )

    records = response.data or []

    if not records:
        raise RuntimeError(
            "Supabase returned no product_images record after insert."
        )

    return records[0]


# ---------------------------------------------------------------------------
# Preflight summary
# ---------------------------------------------------------------------------


def print_preflight_summary(
    candidate: dict[str, Any],
    internal_product: dict[str, Any],
    reference: dict[str, Any],
    source: dict[str, Any],
    non_interactive: bool,
    confirmed: bool,
) -> None:
    """Print an exact-target preflight summary before launching Playwright."""
    print()
    print("=" * 78)
    print("PREFLIGHT SUMMARY")
    print("=" * 78)
    print(
        "Candidate: "
        f"{candidate.get('candidate_code')}"
    )
    print(
        "Internal product: "
        f"{internal_product.get('product_code')}"
    )
    print(
        "Reference ID: "
        f"{reference.get('reference_id')}"
    )
    print(
        "Source URL ID: "
        f"{source.get('source_url_id')}"
    )
    print(
        "Source type: "
        f"{source.get('source_type')}"
    )
    print(
        "Exact BOOKSTORE page URL: "
        f"{source.get('source_url')}"
    )
    print(
        "Mode: "
        f"{'NON_INTERACTIVE' if non_interactive else 'INTERACTIVE'}"
    )
    print(
        "Exact target locked: YES"
    )
    print(
        "Download confirmation supplied: "
        f"{'YES' if confirmed else 'NO'}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Download and register one BOOKSTORE product image for one candidate."""
    load_dotenv()

    args = parse_arguments()

    # Exact-targeting invariant: required in every mode, before any
    # database or browser work.
    if not args.candidate_code:
        raise RuntimeError(
            "--candidate-code is required. This script targets exactly "
            "one candidate; there is no batch mode, discovery mode, or "
            "fallback candidate selection."
        )

    # Safety rule: validated before any database call or Playwright
    # launch. Both flags are required together in non-interactive mode.
    if args.non_interactive and not args.confirm_download:
        raise RuntimeError(
            "--non-interactive requires --confirm-download."
        )

    print(
        "BOOKSTORE product image downloader started."
    )
    print(
        f"Version: {COLLECTOR_VERSION}"
    )
    print(
        "Mode: "
        f"{'NON_INTERACTIVE' if args.non_interactive else 'INTERACTIVE'}"
    )

    repository = SupabaseRepository()

    candidate = resolve_candidate(
        repository=repository,
        candidate_code=args.candidate_code,
    )

    reference = resolve_match_bookstore_reference(
        repository=repository,
        candidate_id=candidate["candidate_id"],
    )

    source = resolve_authorized_bookstore_source(
        repository=repository,
        source_url_id=reference["source_url_id"],
    )

    internal_product = resolve_internal_product(
        repository=repository,
        candidate_id=candidate["candidate_id"],
    )

    check_no_blocking_review_issues(
        repository=repository,
        candidate_id=candidate["candidate_id"],
    )

    if args.confirm_download:
        confirmed = True

    elif args.non_interactive:
        # Backstop only: already enforced above before any database or
        # browser work happened.
        raise RuntimeError(
            "--non-interactive requires --confirm-download."
        )

    else:
        answer = input(
            "Type DOWNLOAD to confirm downloading this BOOKSTORE product "
            "image, or press Enter to cancel: "
        ).strip().upper()

        confirmed = answer == "DOWNLOAD"

    print_preflight_summary(
        candidate=candidate,
        internal_product=internal_product,
        reference=reference,
        source=source,
        non_interactive=args.non_interactive,
        confirmed=confirmed,
    )

    if not confirmed:
        print()
        print(
            "Image download cancelled. No browser was launched."
        )
        return

    page_url = str(
        source["source_url"]
    )

    with sync_playwright() as playwright:
        browser, context = reference_metadata.create_browser(
            playwright
        )

        try:
            (
                page,
                _raw_text,
                _raw_html,
                _page_title,
            ) = reference_metadata.collect_page(
                context=context,
                source_url=page_url,
            )

            json_ld_objects = reference_metadata.extract_json_ld(
                page
            )

            product_json_ld = reference_metadata.find_product_json_ld(
                json_ld_objects
            )

            image_url = reference_metadata.extract_image_url(
                page=page,
                product_json_ld=product_json_ld,
            )

            if not image_url:
                raise RuntimeError(
                    "No product image could be extracted from the "
                    f"BOOKSTORE page: {page_url}"
                )

            extraction_method = determine_image_extraction_method(
                product_json_ld=product_json_ld,
                page=page,
            )

            rejection_reason = reject_reason_for_image_url(
                image_url
            )

            if rejection_reason:
                raise RuntimeError(
                    "Extracted image URL was rejected: "
                    f"{rejection_reason} ({image_url})"
                )

            print()
            print("=" * 78)
            print("IMAGE EXTRACTION RESULT")
            print("=" * 78)
            print(
                f"Extraction method: {extraction_method}"
            )
            print(
                f"Candidate image URL: {image_url}"
            )

            existing_by_url = find_existing_by_source_url(
                repository=repository,
                candidate_id=candidate["candidate_id"],
                source_url=image_url,
            )

            if existing_by_url:
                print()
                print(
                    "An image with this exact source URL is already "
                    "registered for this candidate."
                )
                print(
                    "Existing image_id: "
                    f"{existing_by_url.get('image_id')}"
                )
                print(
                    "Existing image_status: "
                    f"{existing_by_url.get('image_status')}"
                )
                print(
                    "No new product_images row was created."
                )
                return

            content, content_type = download_image_bytes(
                context=context,
                image_url=image_url,
                referer_url=page_url,
            )

            image_hash = calculate_sha256(
                content
            )

            hash_matches = find_existing_by_hash(
                repository=repository,
                image_hash=image_hash,
            )

            same_candidate_matches = [
                match
                for match in hash_matches
                if str(match.get("candidate_id"))
                == str(candidate["candidate_id"])
            ]

            other_candidate_matches = [
                match
                for match in hash_matches
                if str(match.get("candidate_id"))
                != str(candidate["candidate_id"])
            ]

            if same_candidate_matches:
                print()
                print(
                    "An image with this exact content hash is already "
                    "registered for this candidate."
                )
                print(
                    "Existing image_id: "
                    f"{same_candidate_matches[0].get('image_id')}"
                )
                print(
                    "No new product_images row was created."
                )
                return

            if other_candidate_matches:
                raise RuntimeError(
                    "This image's content hash is already linked to a "
                    "different candidate_id="
                    f"{other_candidate_matches[0].get('candidate_id')}. "
                    "Refusing to silently reuse or relink it across "
                    "candidates. Escalate for human review before "
                    "proceeding."
                )

            extension = determine_extension(
                content_type=content_type,
                image_url=image_url,
            )

            local_path = save_local_cache_copy(
                candidate_code=candidate["candidate_code"],
                image_url=image_url,
                page_url=page_url,
                reference_id=reference["reference_id"],
                source_url_id=source["source_url_id"],
                extraction_method=extraction_method,
                content=content,
                content_type=content_type,
                image_hash=image_hash,
                extension=extension,
            )

            storage_path = build_storage_path(
                candidate_code=candidate["candidate_code"],
                image_hash=image_hash,
                extension=extension,
            )

            already_in_storage = storage_file_exists(
                repository=repository,
                storage_path=storage_path,
            )

            uploaded_in_this_run = False

            if not already_in_storage:
                upload_storage_bytes(
                    repository=repository,
                    content=content,
                    storage_path=storage_path,
                    content_type=content_type,
                )

                uploaded_in_this_run = True

            original_file_name = (
                Path(
                    urlparse(image_url).path
                ).name
                or f"image{extension}"
            )

            payload = build_product_images_payload(
                candidate=candidate,
                reference=reference,
                source=source,
                image_url=image_url,
                page_url=page_url,
                storage_path=storage_path,
                original_file_name=original_file_name,
                mime_type=content_type,
                file_size_bytes=len(content),
                image_hash=image_hash,
            )

            try:
                inserted = insert_product_image(
                    repository=repository,
                    payload=payload,
                )

            except Exception:
                if uploaded_in_this_run:
                    print()
                    print(
                        "Database insert failed. Removing the newly "
                        "uploaded Storage file..."
                    )

                    try:
                        remove_storage_file(
                            repository=repository,
                            storage_path=storage_path,
                        )

                        print(
                            "Storage rollback completed."
                        )

                    except Exception as rollback_error:
                        print(
                            "Warning: Storage rollback failed."
                        )
                        print(
                            "Rollback error type: "
                            f"{type(rollback_error).__name__}"
                        )
                        print(
                            f"Rollback details: {rollback_error}"
                        )

                print()
                print(
                    "Image download SUCCEEDED but database registration "
                    "FAILED. The downloaded file remains cached locally "
                    f"for recovery: {local_path}"
                )

                raise

            print()
            print("=" * 78)
            print("PRODUCT IMAGE REGISTERED")
            print("=" * 78)
            print(
                f"Image ID: {inserted.get('image_id')}"
            )
            print(
                f"Image status: {inserted.get('image_status')}"
            )
            print(
                "Is selected main image: "
                f"{inserted.get('is_selected_main_image')}"
            )
            print(
                "Is publish eligible: "
                f"{inserted.get('is_publish_eligible')}"
            )
            print(
                f"Storage path: {inserted.get('storage_path')}"
            )
            print(
                f"Local cache copy: {local_path}"
            )
            print()
            print(
                "Next step: run review_product_images.py to review and "
                "potentially select this as the validated main image."
            )

        finally:
            context.close()
            browser.close()

    print()
    print(
        "BOOKSTORE product image downloader finished."
    )


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print()
        print(
            "BOOKSTORE image download was cancelled."
        )
        sys.exit(130)

    except Exception as error:
        print()
        print(
            "BOOKSTORE product image download failed."
        )
        print(
            f"Error type: {type(error).__name__}"
        )
        print(
            f"Error details: {error}"
        )
        sys.exit(1)
