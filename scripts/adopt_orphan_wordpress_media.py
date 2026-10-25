"""
Adopt verified orphan WordPress media into one FAILED WooCommerce sync.

A create that failed before 7e55998 (mark_sync_failed keeping partial media
progress) can leave media it had already uploaded unrecorded on the sync
row: response_payload.uploaded_media is empty, so a retry would upload the
same images again (duplicate media). Production case 2026-10-24:
FAHASA-2026-003-CAN-0006, Supabase download dropped after media
#4999..#5003 were uploaded.

This tool records exactly the given --media-id attachments as the sync's
uploaded_media, so create_woocommerce_draft.py reuses them (its existing
find_saved_media_for_image / verify_wordpress_media path). Each media item
must be proven to be one of the product's publishable images:

  EXACT   sha256 of the served file equals product_images.image_hash
  PIXEL   WordPress downscaled the file (no original kept): the Supabase
          original, resized to the served size, differs by at most
          PIXEL_MATCH_MAX mean per channel, and every other image of the
          product differs by at least PIXEL_MARGIN_MIN (unambiguous)

and must also be unattached (post = 0/null), have a file name starting with
the product code and containing the matched image's role, and map
one-to-one onto distinct images. The sync must be the product's only one,
FAILED, with no WooCommerce product ID and no saved media, and a fresh
remote SKU lookup (all statuses) must find nothing.

Writes only that sync row (guarded by status/updated_at/no product ID) plus
a process log; the status stays FAILED -- clearing recovery remains
clear_woocommerce_sync_recovery.py's job. Dry run unless --confirm-apply.
All remote calls are GET; no media is uploaded, modified or deleted.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import clear_woocommerce_sync_recovery as recovery  # noqa: E402
import create_woocommerce_draft as cwd  # noqa: E402
from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain.woocommerce_status import WooCommerceSyncStatus  # noqa: E402

configure_utf8_console()

TOOL_NAME = "adopt_orphan_wordpress_media"
TOOL_VERSION = "1.0.0"

PIXEL_MATCH_MAX = 3.0
PIXEL_MARGIN_MIN = 15.0


@dataclass
class MediaEvidence:
    """What was read for one WordPress attachment (GET only)."""

    media_id: int
    http_status: int | None
    post: Any = None
    file: str = ""
    source_url: str = ""
    mime_type: str = ""
    date_gmt: str = ""
    sha256: str = ""
    pixel_scores: dict[str, float] = field(default_factory=dict)


@dataclass
class Plan:
    ok: bool = False
    reason: str = ""
    media_records: list[dict[str, Any]] = field(default_factory=list)
    matches: list[dict[str, Any]] = field(default_factory=list)


def match_media(item: MediaEvidence, images: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str, str]:
    """Return (image, method, problem) for one attachment; image is None on any problem."""
    by_hash = {image.get("image_hash"): image for image in images if image.get("image_hash")}
    if item.sha256 and item.sha256 in by_hash:
        return by_hash[item.sha256], "EXACT", ""
    by_id = {str(image["image_id"]): image for image in images}
    scores = sorted((score, image_id) for image_id, score in item.pixel_scores.items() if image_id in by_id)
    if not scores:
        return None, "", "no exact hash match and no pixel comparison"
    best, best_id = scores[0]
    if best > PIXEL_MATCH_MAX:
        return None, "", f"best pixel difference {best:.3f} > {PIXEL_MATCH_MAX}"
    if len(scores) > 1 and scores[1][0] < PIXEL_MARGIN_MIN:
        return None, "", f"ambiguous: second-best pixel difference {scores[1][0]:.3f} < {PIXEL_MARGIN_MIN}"
    return by_id[best_id], "PIXEL", ""


def plan_adoption(
    *,
    product: dict[str, Any],
    syncs: list[dict[str, Any]],
    images: list[dict[str, Any]],
    media: list[MediaEvidence],
    remote_sku_products: list[dict[str, Any]] | None,
) -> Plan:
    """Pure decision over freshly read state."""
    plan = Plan()
    code = str(product["product_code"])
    if len(syncs) != 1:
        plan.reason = f"Expected exactly one sync record, found {len(syncs)}."
        return plan
    sync = syncs[0]
    if sync.get("woocommerce_product_id"):
        plan.reason = f"The sync already has WooCommerce product #{sync['woocommerce_product_id']}."
        return plan
    if sync.get("woocommerce_status") != WooCommerceSyncStatus.FAILED:
        plan.reason = f"The sync status is {sync.get('woocommerce_status')!r}, not FAILED."
        return plan
    if cwd.extract_uploaded_media(sync):
        plan.reason = "The sync already records uploaded media; nothing is orphaned."
        return plan
    if remote_sku_products is None:
        plan.reason = "The remote SKU lookup did not complete; remote absence is unproven."
        return plan
    if remote_sku_products:
        plan.reason = f"A remote product already has SKU {code}: {[p.get('id') for p in remote_sku_products]}."
        return plan
    if not media:
        plan.reason = "No media IDs given."
        return plan
    if len({item.media_id for item in media}) != len(media):
        plan.reason = "Duplicate media IDs given."
        return plan

    prefix = code.lower() + "-"
    used: dict[str, int] = {}
    for item in media:
        if item.http_status != 200:
            plan.reason = f"Media #{item.media_id} could not be read (HTTP {item.http_status})."
            return plan
        if item.post:
            plan.reason = f"Media #{item.media_id} is attached to post {item.post}; only unattached media is adopted."
            return plan
        base = Path(item.file).name.lower()
        if not base.startswith(prefix):
            plan.reason = f"Media #{item.media_id} file {base!r} is not named for {code}."
            return plan
        image, method, problem = match_media(item, images)
        if image is None:
            plan.reason = f"Media #{item.media_id}: {problem}."
            return plan
        image_id = str(image["image_id"])
        role = str(image.get("image_role") or "").lower()
        if role and role not in base:
            plan.reason = f"Media #{item.media_id} file {base!r} does not carry the matched role {role}."
            return plan
        if image_id in used:
            plan.reason = f"Media #{item.media_id} and #{used[image_id]} both match image {image_id}."
            return plan
        used[image_id] = item.media_id
        plan.matches.append({
            "wordpress_media_id": item.media_id, "source_image_id": image_id, "method": method,
            "sha256": item.sha256, "pixel_scores": item.pixel_scores, "file": item.file,
        })
        plan.media_records.append({
            "source_image_id": image_id,
            "wordpress_media_id": item.media_id,
            "wordpress_source_url": item.source_url,
            "storage_bucket": image.get("storage_bucket"),
            "storage_path": image.get("storage_path"),
            "file_name": Path(item.file).name,
            "mime_type": item.mime_type or image.get("mime_type"),
            "image_role": image.get("image_role"),
            "is_selected_main_image": bool(image.get("is_selected_main_image")),
            "uploaded_at": item.date_gmt,
            "adopted_by": f"{TOOL_NAME}/{TOOL_VERSION}",
        })

    plan.ok = True
    plan.reason = (f"{len(media)} orphan media verified against {code}'s publishable images "
                   f"({', '.join(m['method'] for m in plan.matches)}).")
    return plan


def apply_plan(repository: Any, product: dict[str, Any], sync: dict[str, Any], plan: Plan, now: str) -> None:
    payload = dict(sync.get("response_payload") or {})
    payload["uploaded_media"] = plan.media_records
    payload["orphan_media_adoption"] = {
        "adopted_at": now,
        "adopted_by": f"{TOOL_NAME}/{TOOL_VERSION}",
        "reason": plan.reason,
        "matches": plan.matches,
    }
    rows = (repository.client.table("woocommerce_product_syncs")
            .update({"response_payload": payload, "updated_at": now})
            .eq("sync_id", sync["sync_id"])
            .eq("woocommerce_status", WooCommerceSyncStatus.FAILED)
            .eq("updated_at", sync["updated_at"])
            .is_("woocommerce_product_id", "null")
            .execute().data or [])
    if len(rows) != 1:
        raise RuntimeError("The sync record changed since it was read; nothing written.")
    repository.write_process_log(
        message=f"Adopted orphan WordPress media for {product['product_code']}: {plan.reason}",
        process_name=TOOL_NAME, candidate_id=product.get("candidate_id"),
        process_step="WOO_MEDIA_RECOVERY", status="ADOPTED",
        error_details={"matches": plan.matches},
    )


def read_media(store_url: str, auth: HTTPBasicAuth, timeout: int, media_id: int) -> tuple[MediaEvidence, bytes | None]:
    response = requests.get(
        cwd.build_api_url(store_url=store_url, namespace="wp/v2", endpoint=f"media/{media_id}"),
        auth=auth, params={"context": "edit"}, headers={"Accept": "application/json"}, timeout=timeout)
    if response.status_code != 200:
        return MediaEvidence(media_id=media_id, http_status=response.status_code), None
    body = response.json()
    details = body.get("media_details") or {}
    source_url = body.get("source_url") or ""
    file_response = requests.get(source_url, timeout=timeout)
    if file_response.status_code != 200:
        return MediaEvidence(media_id=media_id, http_status=file_response.status_code), None
    data = file_response.content
    return MediaEvidence(
        media_id=media_id, http_status=200, post=body.get("post"), file=details.get("file") or "",
        source_url=source_url, mime_type=body.get("mime_type") or "", date_gmt=body.get("date_gmt") or "",
        sha256=hashlib.sha256(data).hexdigest(),
    ), data


def pixel_scores(served: bytes, originals: dict[str, bytes]) -> dict[str, float]:
    """Mean per-channel difference of each original, resized to the served size."""
    from PIL import Image, ImageChops, ImageStat

    target = Image.open(io.BytesIO(served)).convert("RGB")
    scores = {}
    for image_id, data in originals.items():
        resized = Image.open(io.BytesIO(data)).convert("RGB").resize(target.size, Image.LANCZOS)
        scores[image_id] = round(sum(ImageStat.Stat(ImageChops.difference(resized, target)).mean) / 3, 3)
    return scores


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Adopt verified orphan WordPress media into one FAILED Woo sync.")
    parser.add_argument("--product-code", required=True, help="Exact internal product code (Woo SKU).")
    parser.add_argument("--media-id", type=int, action="append", required=True, help="Orphan WordPress media ID.")
    parser.add_argument("--confirm-apply", action="store_true")
    return parser.parse_args()


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_arguments()
    store_url = cwd.get_required_environment_variable("WOOCOMMERCE_URL")
    api_version = cwd.os.getenv("WOOCOMMERCE_API_VERSION", "wc/v3").strip()
    consumer_key = cwd.get_required_environment_variable("WOOCOMMERCE_CONSUMER_KEY")
    consumer_secret = cwd.get_required_environment_variable("WOOCOMMERCE_CONSUMER_SECRET")
    auth = HTTPBasicAuth(cwd.get_required_environment_variable("WORDPRESS_USERNAME"),
                         cwd.clean_application_password(cwd.get_required_environment_variable(
                             "WORDPRESS_APPLICATION_PASSWORD")))
    timeout = cwd.get_timeout_seconds()
    repository = cwd.SupabaseRepository()

    product = recovery.get_internal_product_by_code(repository, args.product_code)
    if product is None:
        raise RuntimeError(f"{args.product_code} does not resolve to an internal product.")
    syncs = (repository.client.table("woocommerce_product_syncs").select("*")
             .eq("internal_product_id", product["internal_product_id"]).execute().data or [])
    images = cwd.get_publishable_images(repository=repository, candidate_id=product["candidate_id"])
    try:
        remote = recovery.find_remote_products_by_sku(
            store_url=store_url, api_version=api_version, consumer_key=consumer_key,
            consumer_secret=consumer_secret, timeout_seconds=timeout, sku=args.product_code)
    except (requests.RequestException, RuntimeError):
        remote = None

    hashes = {image.get("image_hash") for image in images}
    originals: dict[str, bytes] | None = None
    media = []
    for media_id in args.media_id:
        item, data = read_media(store_url, auth, timeout, media_id)
        if data is not None and item.sha256 not in hashes:
            if originals is None:
                originals = {str(image["image_id"]): cwd.download_source_image_from_supabase(
                    repository=repository, storage_bucket=image["storage_bucket"],
                    storage_path=image["storage_path"], configured_mime_type=image.get("mime_type"))[0]
                    for image in images}
            item.pixel_scores = pixel_scores(data, originals)
        media.append(item)

    plan = plan_adoption(product=product, syncs=syncs, images=images, media=media, remote_sku_products=remote)
    print("=" * 72)
    print(f"ORPHAN MEDIA ADOPTION {TOOL_VERSION} ({'APPLY' if args.confirm_apply else 'DRY RUN'})")
    print("=" * 72)
    print(f"{args.product_code}\n  ok: {plan.ok}\n  reason: {plan.reason}")
    for match in plan.matches:
        best = min(match["pixel_scores"].values()) if match["pixel_scores"] else None
        print(f"  #{match['wordpress_media_id']} -> {match['source_image_id']} {match['method']}"
              + (f" (pixel {best})" if best is not None else ""))
    if not plan.ok:
        return 1
    if args.confirm_apply:
        apply_plan(repository, product, syncs[0], plan, cwd.utc_now())
        print("  written (sync stays FAILED; clear recovery next).")
    print("No WordPress media or WooCommerce product was created, modified or deleted.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"Orphan media adoption failed: {error}")
        sys.exit(1)
