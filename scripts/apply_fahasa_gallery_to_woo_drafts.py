"""
Add approved Fahasa gallery images to existing TSYC WooCommerce DRAFTS of
Fahasa pre-order products (CLAUDE.md 14.8.3/14.8.4,
src/domain/rules/woo_gallery_rules.py).

For each EXACT --product-code (bounded by --max-products), serially:

  1. local: the internal product is still DRAFT_CREATED (never PUBLISHED
     or REMOTE_REMOVED) and its candidate is a Fahasa pre-order
     candidate; exactly one woocommerce_product_syncs row, DRAFT_CREATED,
     with a remote id and the product's own SKU; exactly one VALIDATED,
     publish-eligible selected main image whose WordPress media TSYC
     recorded at creation; the gallery = the other publishable images
     (create_woocommerce_draft.get_publishable_images, same order).
  2. remote: GET the exact stored id. It must be the same id and SKU, a
     DRAFT, created by TSYC (_tsyc_internal_product_id meta), and its
     images must still be exactly the media TSYC last applied -- otherwise
     the gallery is owner-curated and the product is isolated, never
     written. Already carrying the full gallery -> NO_OP.
  3. upload each missing gallery image to the WordPress media library
     (existing media recorded for the same product_images.id is verified
     and reused, so a re-run never uploads a duplicate); every upload is
     merged into the sync's response_payload.uploaded_media immediately.
  4. PUT only {"images": [featured, gallery...]} -- never status, a price,
     inventory or text -- then re-GET and verify the images applied and
     every other field unchanged. An uncertain PUT (timeout/connection
     error) is never retried: the product is re-read once to reconcile.
  5. record applied_image_media_ids in the sync's response_payload (the
     baseline the next run compares against) and a process_logs entry.

Never publishes, never sets or changes a price, never touches a
published/trashed/pending/private product or an owner-curated gallery.
Dry run unless --confirm-update.

Usage:
  .venv/Scripts/python.exe scripts/apply_fahasa_gallery_to_woo_drafts.py \
      --product-code TSYC-FAHASA-2026-002-CAN-0001 --max-products 1
  ... --non-interactive --confirm-update
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import create_woocommerce_draft as cwd  # noqa: E402
from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain.rules import preorder_inventory_rules  # noqa: E402
from src.domain.rules import woo_gallery_rules as rules  # noqa: E402
from src.domain.woocommerce_status import WooCommerceStatus, WooCommerceSyncStatus  # noqa: E402

configure_utf8_console()

APPLIER_NAME = "woocommerce_fahasa_gallery"
APPLIER_VERSION = "1.0.0"
MAX_PRODUCTS_HARD_CAP = 20

UPDATED = "UPDATED"
NO_OP = "NO_OP"
DRY_RUN = "DRY_RUN"
LOCAL_NOT_READY = "LOCAL_NOT_READY"
NO_GALLERY_IMAGES = "NO_GALLERY_IMAGES"
MEDIA_UPLOAD_FAILED = "MEDIA_UPLOAD_FAILED"
UNCERTAIN = "UPDATE_RESULT_UNCERTAIN"
UPDATE_REJECTED = "UPDATE_REJECTED"
VERIFY_FAILED = "POST_UPDATE_VERIFICATION_FAILED"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WooGalleryClient:
    """Read one product / write exactly its images."""

    def __init__(self, store_url: str, api_version: str, key: str, secret: str, timeout: int) -> None:
        self.store_url = store_url
        self.api_version = api_version
        self.auth = HTTPBasicAuth(key, secret)
        self.timeout = timeout
        self.headers = {"Accept": "application/json", "User-Agent": f"{APPLIER_NAME}/{APPLIER_VERSION}"}

    def _url(self, product_id: int | str) -> str:
        return cwd.build_api_url(self.store_url, self.api_version, f"products/{product_id}")

    def get(self, product_id: int | str) -> dict[str, Any] | None:
        response = requests.get(
            self._url(product_id), auth=self.auth, params={"context": "edit"}, headers=self.headers, timeout=self.timeout
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def update_images(self, product_id: int | str, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) != set(rules.GALLERY_FIELDS):
            raise RuntimeError(f"Refusing to send fields other than {rules.GALLERY_FIELDS}.")
        response = requests.put(self._url(product_id), auth=self.auth, json=payload, headers=self.headers, timeout=self.timeout)
        response.raise_for_status()
        return response.json()


@dataclass
class MediaPorts:
    """WordPress media operations, injectable for offline tests."""

    verify_media: Callable[[int], bool]
    upload_media: Callable[[dict[str, Any], dict[str, Any], int], dict[str, Any]]


def load_local_target(repository: Any, product_code: str) -> dict[str, Any]:
    products = (
        repository.client.table("internal_products")
        .select("internal_product_id, product_code, candidate_id, woocommerce_status")
        .eq("product_code", product_code).execute().data
        or []
    )
    if len(products) != 1:
        raise RuntimeError(f"product_code did not resolve to exactly one product: {product_code}")
    product = products[0]
    if product.get("woocommerce_status") != WooCommerceStatus.DRAFT_CREATED:
        # An owner-published or remotely removed product is never enriched,
        # whatever its (possibly stale) sync row still says.
        raise RuntimeError(f"Internal product woocommerce_status is {product.get('woocommerce_status')!r}, not DRAFT_CREATED.")

    candidate = cwd.get_candidate_origin(repository=repository, candidate_id=product["candidate_id"])
    if not preorder_inventory_rules.is_fahasa_preorder_candidate(candidate):
        raise RuntimeError("candidate is not a Fahasa pre-order candidate.")

    syncs = (
        repository.client.table("woocommerce_product_syncs")
        .select("sync_id, woocommerce_product_id, product_sku, woocommerce_status, response_payload")
        .eq("internal_product_id", product["internal_product_id"]).execute().data
        or []
    )
    if len(syncs) != 1:
        raise RuntimeError(f"Expected exactly one woocommerce_product_syncs row, found {len(syncs)}.")
    sync = syncs[0]
    if not sync.get("woocommerce_product_id") or str(sync.get("product_sku") or "") != product_code:
        raise RuntimeError("Sync row has no remote product id or a different SKU; remote identity is not certain.")
    if sync.get("woocommerce_status") != WooCommerceSyncStatus.DRAFT_CREATED:
        raise RuntimeError(f"Sync status is {sync.get('woocommerce_status')!r}, not DRAFT_CREATED.")

    images = cwd.get_publishable_images(repository=repository, candidate_id=product["candidate_id"])
    mains = [image for image in images if image.get("is_selected_main_image") is True]
    if len(mains) != 1:
        raise RuntimeError("Exactly one validated publishable selected main image is required.")
    uploaded = cwd.extract_uploaded_media(sync)
    main_media = cwd.find_saved_media_for_image(uploaded, str(mains[0]["image_id"]))
    if not main_media or not main_media.get("wordpress_media_id"):
        raise RuntimeError("The featured image's WordPress media is not recorded on the sync row.")
    return {
        "product": product,
        "sync": sync,
        "main_media": main_media,
        "gallery_images": [image for image in images if image.get("is_selected_main_image") is not True],
        "uploaded_media": uploaded,
    }


def save_sync_payload(repository: Any, sync: dict[str, Any], updates: dict[str, Any]) -> None:
    """Merge into response_payload; the created product's record is kept."""
    payload = dict(sync.get("response_payload") or {})
    payload.update(updates)
    rows = (
        repository.client.table("woocommerce_product_syncs")
        .update({"response_payload": payload, "updated_at": utc_now()})
        .eq("sync_id", sync["sync_id"]).execute().data
        or []
    )
    if len(rows) != 1:
        raise RuntimeError("Sync payload update did not affect exactly one row.")
    sync["response_payload"] = payload


def run_apply(
    repository: Any,
    client: Any,
    media: MediaPorts,
    product_code: str,
    dry_run: bool,
    log: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {"product_code": product_code, "result": None, "detail": None}
    try:
        local = load_local_target(repository, product_code)
    except RuntimeError as error:
        report.update(result=LOCAL_NOT_READY, detail=str(error))
        return report

    sync = local["sync"]
    product_id = sync["woocommerce_product_id"]
    report["woocommerce_product_id"] = product_id
    if not local["gallery_images"]:
        report.update(result=NO_GALLERY_IMAGES)
        return report

    before = client.get(product_id)
    applied = rules.tsyc_applied_media_ids(sync.get("response_payload"))
    classification = rules.classify_gallery_target(
        before, product_id, product_code, local["product"]["internal_product_id"], applied
    )
    report["remote_status"] = (before or {}).get("status")
    if classification != rules.ELIGIBLE:
        report.update(result=classification)
        return report

    uploaded = list(local["uploaded_media"])
    known = {
        str(image["image_id"]): cwd.find_saved_media_for_image(uploaded, str(image["image_id"]))
        for image in local["gallery_images"]
    }
    current = rules.remote_image_ids(before)
    planned_known = [int(local["main_media"]["wordpress_media_id"])] + [
        int(record["wordpress_media_id"]) for record in known.values() if record
    ]
    if all(known.values()) and current == planned_known:
        report.update(result=NO_OP)
        return report
    report["gallery_image_ids"] = list(known)
    if dry_run:
        report.update(result=DRY_RUN, uploads_needed=sum(1 for record in known.values() if not record))
        return report

    if not (sync.get("response_payload") or {}).get("applied_image_media_ids"):
        # Pin today's baseline before uploading: media recorded below but
        # never applied must not later look like an owner change.
        save_sync_payload(repository, sync, {"applied_image_media_ids": applied})

    gallery_media_ids: list[int] = []
    for position, image in enumerate(local["gallery_images"], start=2):
        record = known[str(image["image_id"])]
        if record and not media.verify_media(int(record["wordpress_media_id"])):
            record = None
        if record is None:
            try:
                record = media.upload_media(local["product"], image, position)
            except Exception as error:
                report.update(result=MEDIA_UPLOAD_FAILED, detail=f"{type(error).__name__}: {error}")
                return report
            uploaded = [item for item in uploaded if item.get("source_image_id") != str(image["image_id"])]
            uploaded.append(record)
            save_sync_payload(repository, sync, {"uploaded_media": uploaded})
        gallery_media_ids.append(int(record["wordpress_media_id"]))

    expected = [int(local["main_media"]["wordpress_media_id"]), *gallery_media_ids]
    payload = rules.build_gallery_payload(expected)
    try:
        client.update_images(product_id, payload)
    except (requests.Timeout, requests.ConnectionError) as error:
        # Never retried: re-read once to reconcile.
        problems = rules.verify_gallery_after(before, client.get(product_id), expected)
        if problems:
            report.update(result=UNCERTAIN, detail=f"{type(error).__name__}; {problems}")
            return report
        report["detail"] = f"{type(error).__name__} during PUT; gallery verified present on re-read"
    except requests.HTTPError as error:
        # A definite rejection; re-read to prove nothing changed.
        problems = rules.verify_gallery_after(before, client.get(product_id), applied)
        report.update(
            result=UPDATE_REJECTED,
            detail=f"{error}; " + ("unchanged on re-read" if not problems else f"re-read: {problems}"),
        )
        return report

    after = client.get(product_id)
    problems = rules.verify_gallery_after(before, after, expected)
    if problems:
        report.update(result=VERIFY_FAILED, detail="; ".join(problems))
        return report

    save_sync_payload(
        repository,
        sync,
        {"applied_image_media_ids": expected, "gallery_enriched_at": utc_now(), "gallery_enricher": APPLIER_NAME},
    )
    report.update(result=UPDATED, media_ids=expected, previous_media_ids=current)
    if log is not None:
        try:
            log(
                message=(
                    f"WooCommerce draft {product_id} ({product_code}) gallery set to {expected} "
                    "(status, prices, inventory and text unchanged)."
                ),
                process_name=APPLIER_NAME,
                candidate_id=local["product"].get("candidate_id"),
                process_step="APPLY_FAHASA_GALLERY",
                log_level="INFO",
                status=UPDATED,
                error_details={"woocommerce_product_id": product_id, "previous_media_ids": current},
            )
        except Exception as error:  # audit failure must not mask the update
            print(f"Warning: process_logs entry failed: {type(error).__name__}: {error}")
    return report


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Add approved Fahasa gallery images to exact TSYC Woo drafts.")
    parser.add_argument("--product-code", action="append", required=True, help="Exact internal product code. Repeat.")
    parser.add_argument("--max-products", type=int, required=True)
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--confirm-update", action="store_true", help="Required to write; otherwise a dry run.")
    args = parser.parse_args(argv)
    if not 1 <= args.max_products <= MAX_PRODUCTS_HARD_CAP:
        parser.error(f"--max-products must be between 1 and {MAX_PRODUCTS_HARD_CAP}.")
    if len(set(args.product_code)) != len(args.product_code):
        parser.error("--product-code values must be unique.")
    if len(args.product_code) > args.max_products:
        parser.error("More --product-code values than --max-products.")
    return args


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_arguments()
    store_url = cwd.get_required_environment_variable("WOOCOMMERCE_URL")
    if not store_url.startswith("https://"):
        raise RuntimeError("WOOCOMMERCE_URL must use HTTPS.")
    username = cwd.get_required_environment_variable("WORDPRESS_USERNAME")
    application_password = cwd.clean_application_password(
        cwd.get_required_environment_variable("WORDPRESS_APPLICATION_PASSWORD")
    )
    timeout = cwd.get_timeout_seconds()

    from src.repositories.supabase_repository import SupabaseRepository

    repository = SupabaseRepository()
    client = WooGalleryClient(
        store_url=store_url,
        api_version=cwd.os.getenv("WOOCOMMERCE_API_VERSION", "wc/v3").strip(),
        key=cwd.get_required_environment_variable("WOOCOMMERCE_CONSUMER_KEY"),
        secret=cwd.get_required_environment_variable("WOOCOMMERCE_CONSUMER_SECRET"),
        timeout=timeout,
    )

    def upload_media(product: dict[str, Any], image: dict[str, Any], position: int) -> dict[str, Any]:
        image_bytes, content_type = cwd.download_source_image_from_supabase(
            repository=repository,
            storage_bucket=image["storage_bucket"],
            storage_path=image["storage_path"],
            configured_mime_type=image.get("mime_type"),
        )
        file_name = cwd.build_media_file_name(
            product_code=product["product_code"], image=image, content_type=content_type, image_position=position
        )
        uploaded = cwd.upload_wordpress_media(
            store_url=store_url, username=username, application_password=application_password,
            timeout_seconds=timeout, file_name=file_name, image_bytes=image_bytes, content_type=content_type,
        )
        media_id = int(uploaded["id"])
        cwd.update_wordpress_media_metadata(
            store_url=store_url, username=username, application_password=application_password,
            timeout_seconds=timeout, media_id=media_id, product_name=product["product_code"],
            image_role=image.get("image_role"),
        )
        return {
            "source_image_id": str(image["image_id"]),
            "wordpress_media_id": media_id,
            "wordpress_source_url": uploaded.get("source_url"),
            "storage_bucket": image["storage_bucket"],
            "storage_path": image["storage_path"],
            "file_name": file_name,
            "mime_type": content_type,
            "image_role": image.get("image_role"),
            "is_selected_main_image": False,
            "uploaded_at": utc_now(),
        }

    media = MediaPorts(
        verify_media=lambda media_id: cwd.verify_wordpress_media(
            store_url=store_url, username=username, application_password=application_password,
            timeout_seconds=timeout, media_id=media_id,
        ),
        upload_media=upload_media,
    )
    for product_code in args.product_code:  # serial: one product at a time
        report = run_apply(
            repository, client, media, product_code, dry_run=not args.confirm_update, log=repository.write_process_log
        )
        print(f"FAHASA_GALLERY_WOO_RESULT {json.dumps(report, ensure_ascii=False, default=str)}")


if __name__ == "__main__":
    main()
