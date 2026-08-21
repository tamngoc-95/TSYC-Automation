import argparse
import html
import mimetypes
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.cli_bootstrap import configure_utf8_console
from src.repositories.supabase_repository import SupabaseRepository

configure_utf8_console()


CREATOR_NAME = "woocommerce_draft_creator"
CREATOR_VERSION = "1.3.0"

VALID_CONFIRMATIONS = {
    "UPLOAD_AND_CREATE",
    "CREATE",
    "CREATE_DRAFT",
    "DRAFT",
}

SUPPORTED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

MAX_IMAGE_SIZE_BYTES = 15 * 1024 * 1024


class WooCommerceCreateError(Exception):
    """Represent a WooCommerce product creation failure."""

    def __init__(
        self,
        error_code: str,
        error_message: str,
        response_payload: Any,
    ) -> None:
        super().__init__(error_message)

        self.error_code = error_code
        self.error_message = error_message
        self.response_payload = response_payload


class WordPressMediaError(Exception):
    """Represent a WordPress media upload failure."""

    def __init__(
        self,
        error_code: str,
        error_message: str,
        response_payload: Any,
    ) -> None:
        super().__init__(error_message)

        self.error_code = error_code
        self.error_message = error_message
        self.response_payload = response_payload


def utc_now() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def normalize_confirmation(
    value: str | None,
) -> str:
    """Normalize confirmation values across case, spaces, and hyphens."""
    if not value:
        return ""

    return (
        value.strip()
        .upper()
        .replace("-", "_")
        .replace(" ", "_")
    )


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Upload validated images and create one WooCommerce product draft."
        )
    )
    parser.add_argument(
        "--product-code",
        help="Create the draft for one exact internal product code.",
    )
    parser.add_argument(
        "--confirm-create",
        action="store_true",
        help="Confirm draft creation without an interactive prompt.",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help=(
            "Disable interactive input. Use with --product-code and "
            "--confirm-create for automation."
        ),
    )
    return parser.parse_args()


def get_required_environment_variable(
    variable_name: str,
) -> str:
    """Return one required environment variable."""
    value = os.getenv(
        variable_name,
        "",
    ).strip()

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {variable_name}"
        )

    return value


def get_timeout_seconds() -> int:
    """Return the configured HTTP timeout."""
    raw_value = os.getenv(
        "WOOCOMMERCE_TIMEOUT_SECONDS",
        "30",
    ).strip()

    try:
        timeout_seconds = int(raw_value)

    except ValueError as error:
        raise RuntimeError(
            "WOOCOMMERCE_TIMEOUT_SECONDS must be an integer."
        ) from error

    if timeout_seconds <= 0:
        raise RuntimeError(
            "WOOCOMMERCE_TIMEOUT_SECONDS must be greater than zero."
        )

    return timeout_seconds


def clean_application_password(
    application_password: str,
) -> str:
    """Normalize the WordPress application password."""
    return " ".join(
        application_password.split()
    )


def build_api_url(
    store_url: str,
    namespace: str,
    endpoint: str,
) -> str:
    """Build a WordPress or WooCommerce REST API URL."""
    return (
        f"{store_url.rstrip('/')}/wp-json/"
        f"{namespace.strip('/')}/"
        f"{endpoint.strip('/')}"
    )


def safe_json_response(
    response: requests.Response,
) -> Any:
    """Return JSON or a readable raw response."""
    try:
        return response.json()

    except ValueError:
        return {
            "raw_response": response.text[:5000],
        }


def sanitize_file_name(
    value: str,
) -> str:
    """Create a safe file name."""
    value = value.lower().strip()

    value = re.sub(
        r"[^a-z0-9_-]+",
        "-",
        value,
    )

    value = re.sub(
        r"-+",
        "-",
        value,
    )

    value = value.strip("-_")

    return value or "tsyc-product-image"


def get_ready_product(
    repository: SupabaseRepository,
    product_code: str | None,
) -> dict[str, Any] | None:
    """Return one exact or next internal product ready for draft creation."""
    query = (
        repository.client
        .table("internal_products")
        .select(
            "internal_product_id,"
            "candidate_id,"
            "product_code,"
            "product_type,"
            "title,"
            "author,"
            "isbn,"
            "publisher,"
            "page_count,"
            "weight_grams,"
            "length_cm,"
            "width_cm,"
            "height_cm,"
            "content_status,"
            "image_status,"
            "pricing_status,"
            "woocommerce_status,"
            "review_required,"
            "product_metadata,"
            "created_at"
        )
        .eq("is_active", True)
        .eq("woocommerce_status", "READY_FOR_DRAFT")
        .eq("review_required", False)
    )

    if product_code:
        query = query.eq("product_code", product_code)

    response = (
        query
        .order("created_at", desc=False)
        .limit(2)
        .execute()
    )

    rows = response.data or []

    if product_code and not rows:
        raise RuntimeError(
            "No READY_FOR_DRAFT internal product was found for "
            f"product code: {product_code}"
        )

    if product_code and len(rows) != 1:
        raise RuntimeError(
            "Product code did not resolve to exactly one READY_FOR_DRAFT "
            "internal product."
        )

    return rows[0] if rows else None

def get_approved_content(
    repository: SupabaseRepository,
    internal_product_id: str,
) -> dict[str, Any] | None:
    """Return approved Vietnamese product content."""
    response = (
        repository.client
        .table("product_contents")
        .select(
            "product_content_id,"
            "product_name,"
            "short_description,"
            "long_description,"
            "author_summary,"
            "product_details,"
            "seo_title,"
            "seo_description,"
            "content_status,"
            "review_required"
        )
        .eq(
            "internal_product_id",
            internal_product_id,
        )
        .eq(
            "content_language",
            "vi",
        )
        .eq(
            "content_status",
            "APPROVED",
        )
        .eq(
            "review_required",
            False,
        )
        .limit(1)
        .execute()
    )

    rows = response.data or []

    if not rows:
        return None

    return rows[0]


def get_publishable_images(
    repository: SupabaseRepository,
    candidate_id: str,
) -> list[dict[str, Any]]:
    """Return validated product images allowed for publishing."""
    response = (
        repository.client
        .table("product_images")
        .select(
            "image_id,"
            "candidate_id,"
            "storage_bucket,"
            "storage_path,"
            "original_file_name,"
            "mime_type,"
            "width_pixels,"
            "height_pixels,"
            "file_size_bytes,"
            "image_hash,"
            "image_role,"
            "usage_rights_status,"
            "is_selected_main_image,"
            "is_publish_eligible,"
            "image_status"
        )
        .eq(
            "candidate_id",
            candidate_id,
        )
        .eq(
            "is_publish_eligible",
            True,
        )
        .eq(
            "image_status",
            "VALIDATED",
        )
        .execute()
    )

    # Must exactly match product_images_publish_eligibility_check
    # (migrations/009_add_product_image_review_guards.sql): a row can only
    # ever have is_publish_eligible = true when usage_rights_status is one
    # of these three values, so this is a redundant defense-in-depth filter,
    # not a broadening of what the database and review_product_images.py
    # already allow. REFERENCE_ONLY and RIGHTS_UNKNOWN remain excluded.
    images = [
        image
        for image in (response.data or [])
        if image.get("usage_rights_status")
        in {
            "STORE_OWNED",
            "PUBLISHER_APPROVED",
            "SUPPLIER_APPROVED",
        }
    ]

    return sorted(
        images,
        key=lambda image: (
            not bool(
                image.get(
                    "is_selected_main_image"
                )
            ),
            image.get("image_role") or "",
            image.get("image_id") or "",
        ),
    )


def revalidate_pre_create_state(
    repository: SupabaseRepository,
    product_code: str,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    list[dict[str, Any]],
]:
    """
    Reload and validate current Supabase state immediately before Woo creation.

    This prevents a stale in-memory product/content/image snapshot from being
    used after media upload or another concurrent workflow change.
    """
    product = get_ready_product(
        repository=repository,
        product_code=product_code,
    )

    if product is None:
        raise RuntimeError(
            "Product is no longer READY_FOR_DRAFT."
        )

    if product.get("content_status") != "APPROVED":
        raise RuntimeError(
            "Internal product content is no longer APPROVED."
        )

    if product.get("image_status") != "APPROVED":
        raise RuntimeError(
            "Internal product image status is no longer APPROVED."
        )

    content = get_approved_content(
        repository=repository,
        internal_product_id=product["internal_product_id"],
    )

    if content is None:
        raise RuntimeError(
            "Approved Vietnamese product content is no longer available."
        )

    images = get_publishable_images(
        repository=repository,
        candidate_id=product["candidate_id"],
    )

    selected_main_images = [
        image
        for image in images
        if image.get("is_selected_main_image") is True
    ]

    if len(selected_main_images) != 1:
        raise RuntimeError(
            "Exactly one validated publishable selected main image must "
            "exist immediately before WooCommerce draft creation."
        )

    return product, content, images


def has_existing_remote_draft(
    existing_sync: dict[str, Any] | None,
) -> bool:
    """
    Return True when a local sync record already has a remote Woo product ID.

    A populated woocommerce_product_id -- whether from a normal successful
    run or from mark_post_create_recovery_required() leaving
    recovery_required=True after an uncertain remote result -- means a
    WooCommerce product may already exist. Draft creation must never retry
    in that case.
    """
    return bool(
        existing_sync
        and existing_sync.get("woocommerce_product_id")
    )


def assert_uploaded_image_set_matches_current(
    current_images: list[dict[str, Any]],
    uploaded_media: list[dict[str, Any]],
) -> None:
    """
    Ensure the uploaded WordPress media set still matches the freshly
    reloaded publishable image set before creating the WooCommerce product.

    This is the exact identifier contract that caused a production bug: the
    comparison must key uploaded_media items by source_image_id (the local
    product_images.id recorded when the WordPress attachment was created or
    reused), never by any WordPress-side identifier such as
    wordpress_media_id. current_images are keyed by their local image_id,
    which is the same product_images.id under a different field name.
    """
    current_image_ids = {
        str(image.get("image_id"))
        for image in current_images
    }

    uploaded_image_ids = {
        str(item.get("source_image_id"))
        for item in uploaded_media
    }

    if current_image_ids != uploaded_image_ids:
        raise RuntimeError(
            "Publishable image set changed during WooCommerce media "
            "preparation. Draft creation was stopped to avoid using "
            "a stale image set."
        )


def get_existing_sync(
    repository: SupabaseRepository,
    internal_product_id: str,
) -> dict[str, Any] | None:
    """Return the existing WooCommerce synchronization record."""
    response = (
        repository.client
        .table("woocommerce_product_syncs")
        .select("*")
        .eq(
            "internal_product_id",
            internal_product_id,
        )
        .limit(1)
        .execute()
    )

    rows = response.data or []

    if not rows:
        return None

    return rows[0]


def text_to_html_paragraphs(
    value: str | None,
) -> str:
    """Convert plain text paragraphs into safe HTML."""
    if not value:
        return ""

    paragraphs = [
        paragraph.strip()
        for paragraph in value.split("\n\n")
        if paragraph.strip()
    ]

    html_paragraphs: list[str] = []

    for paragraph in paragraphs:
        escaped = html.escape(paragraph)

        escaped = escaped.replace(
            "\n",
            "<br>",
        )

        html_paragraphs.append(
            f"<p>{escaped}</p>"
        )

    return "\n".join(html_paragraphs)


def build_description(
    content: dict[str, Any],
) -> str:
    """Build the WooCommerce long description."""
    sections: list[str] = []

    long_description = text_to_html_paragraphs(
        content.get("long_description")
    )

    if long_description:
        sections.append(long_description)

    author_summary = (
        content.get("author_summary")
        or ""
    ).strip()

    if author_summary:
        sections.append(
            "<h3>Tác giả</h3>"
            f"<p>{html.escape(author_summary)}</p>"
        )

    product_details = (
        content.get("product_details")
        or ""
    ).strip()

    if product_details:
        detail_lines = [
            line.strip()
            for line in product_details.splitlines()
            if line.strip()
        ]

        detail_items = "".join(
            f"<li>{html.escape(line)}</li>"
            for line in detail_lines
        )

        sections.append(
            "<h3>Thông tin sản phẩm</h3>"
            f"<ul>{detail_items}</ul>"
        )

    return "\n".join(sections)


def build_short_description(
    content: dict[str, Any],
) -> str:
    """Build a safe WooCommerce short description."""
    short_description = (
        content.get("short_description")
        or ""
    ).strip()

    if not short_description:
        return ""

    return (
        f"<p>{html.escape(short_description)}</p>"
    )


def find_product_by_sku(
    store_url: str,
    api_version: str,
    consumer_key: str,
    consumer_secret: str,
    timeout_seconds: int,
    sku: str,
) -> dict[str, Any] | None:
    """Check whether WooCommerce already contains the SKU."""
    api_url = build_api_url(
        store_url=store_url,
        namespace=api_version,
        endpoint="products",
    )

    response = requests.get(
        api_url,
        auth=HTTPBasicAuth(
            consumer_key,
            consumer_secret,
        ),
        params={
            "sku": sku,
            "status": "any",
            "per_page": 10,
        },
        headers={
            "Accept": "application/json",
            "User-Agent": (
                f"{CREATOR_NAME}/"
                f"{CREATOR_VERSION}"
            ),
        },
        timeout=timeout_seconds,
    )

    response_payload = safe_json_response(
        response
    )

    if response.status_code != 200:
        raise RuntimeError(
            "WooCommerce SKU check failed. "
            f"HTTP status: {response.status_code}. "
            f"Response: {response_payload}"
        )

    if not isinstance(
        response_payload,
        list,
    ):
        raise RuntimeError(
            "WooCommerce SKU check returned an unexpected response."
        )

    if not response_payload:
        return None

    return response_payload[0]


def create_sync_record(
    repository: SupabaseRepository,
    product: dict[str, Any],
    product_name: str,
) -> dict[str, Any]:
    """Create one local WooCommerce synchronization record."""
    response = (
        repository.client
        .table("woocommerce_product_syncs")
        .insert(
            {
                "internal_product_id": product[
                    "internal_product_id"
                ],
                "woocommerce_status": "PENDING",
                "product_sku": product[
                    "product_code"
                ],
                "product_name": product_name,
                "request_payload": {},
                "response_payload": {
                    "uploaded_media": [],
                },
                "sync_attempt_count": 0,
                "updated_at": utc_now(),
            }
        )
        .execute()
    )

    rows = response.data or []

    if not rows:
        raise RuntimeError(
            "WooCommerce sync record insert returned no data."
        )

    return rows[0]


def mark_sync_in_progress(
    repository: SupabaseRepository,
    sync: dict[str, Any],
) -> dict[str, Any]:
    """Mark the synchronization as in progress."""
    attempt_count = int(
        sync.get("sync_attempt_count")
        or 0
    )

    response = (
        repository.client
        .table("woocommerce_product_syncs")
        .update(
            {
                "woocommerce_status": "IN_PROGRESS",
                "error_code": None,
                "error_message": None,
                "sync_attempt_count": attempt_count + 1,
                "last_attempt_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        .eq(
            "sync_id",
            sync["sync_id"],
        )
        .execute()
    )

    rows = response.data or []

    if not rows:
        raise RuntimeError(
            "Could not mark WooCommerce sync as in progress."
        )

    return rows[0]


def extract_uploaded_media(
    sync: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return previously uploaded WordPress media records."""
    response_payload = sync.get(
        "response_payload"
    )

    if not isinstance(
        response_payload,
        dict,
    ):
        return []

    uploaded_media = response_payload.get(
        "uploaded_media"
    )

    if not isinstance(
        uploaded_media,
        list,
    ):
        return []

    return [
        item
        for item in uploaded_media
        if isinstance(item, dict)
    ]


def find_saved_media_for_image(
    uploaded_media: list[dict[str, Any]],
    image_id: str,
) -> dict[str, Any] | None:
    """Find an existing WordPress media record for one image."""
    for item in uploaded_media:
        if item.get("source_image_id") == image_id:
            return item

    return None


def verify_wordpress_media(
    store_url: str,
    username: str,
    application_password: str,
    timeout_seconds: int,
    media_id: int,
) -> bool:
    """Confirm that a WordPress media attachment exists."""
    api_url = build_api_url(
        store_url=store_url,
        namespace="wp/v2",
        endpoint=f"media/{media_id}",
    )

    response = requests.get(
        api_url,
        auth=HTTPBasicAuth(
            username,
            application_password,
        ),
        headers={
            "Accept": "application/json",
            "User-Agent": (
                f"{CREATOR_NAME}/"
                f"{CREATOR_VERSION}"
            ),
        },
        timeout=timeout_seconds,
    )

    return response.status_code == 200


def validate_image_signature(
    image_bytes: bytes,
    content_type: str,
) -> None:
    """Perform a basic image file signature validation."""
    if content_type == "image/jpeg":
        is_valid = image_bytes.startswith(
            b"\xff\xd8\xff"
        )

    elif content_type == "image/png":
        is_valid = image_bytes.startswith(
            b"\x89PNG\r\n\x1a\n"
        )

    elif content_type == "image/webp":
        is_valid = (
            len(image_bytes) >= 12
            and image_bytes[0:4] == b"RIFF"
            and image_bytes[8:12] == b"WEBP"
        )

    else:
        is_valid = False

    if not is_valid:
        raise WordPressMediaError(
            error_code="INVALID_IMAGE_SIGNATURE",
            error_message=(
                "The downloaded file does not match its image MIME type."
            ),
            response_payload={
                "content_type": content_type,
                "first_bytes": image_bytes[:20].hex(),
            },
        )


def download_source_image_from_supabase(
    repository: SupabaseRepository,
    storage_bucket: str,
    storage_path: str,
    configured_mime_type: str | None,
) -> tuple[bytes, str]:
    """Download and validate an image through authenticated Supabase Storage."""
    print()
    print(
        "Downloading image from authenticated Supabase Storage..."
    )

    print(
        f"Storage bucket: {storage_bucket}"
    )

    print(
        f"Storage path: {storage_path}"
    )

    try:
        image_bytes = (
            repository.client
            .storage
            .from_(storage_bucket)
            .download(storage_path)
        )

    except Exception as error:
        raise WordPressMediaError(
            error_code="SUPABASE_IMAGE_DOWNLOAD_FAILED",
            error_message=(
                "Could not download the image through "
                "authenticated Supabase Storage."
            ),
            response_payload={
                "storage_bucket": storage_bucket,
                "storage_path": storage_path,
                "exception_type": type(error).__name__,
                "exception_message": str(error),
            },
        ) from error

    if not isinstance(
        image_bytes,
        bytes,
    ):
        raise WordPressMediaError(
            error_code="INVALID_SUPABASE_DOWNLOAD_RESPONSE",
            error_message=(
                "Supabase Storage did not return image bytes."
            ),
            response_payload={
                "storage_bucket": storage_bucket,
                "storage_path": storage_path,
                "response_type": type(
                    image_bytes
                ).__name__,
            },
        )

    print(
        f"Downloaded bytes: {len(image_bytes)}"
    )

    if not image_bytes:
        raise WordPressMediaError(
            error_code="EMPTY_IMAGE_FILE",
            error_message=(
                "The downloaded image file is empty."
            ),
            response_payload={
                "storage_bucket": storage_bucket,
                "storage_path": storage_path,
            },
        )

    if len(image_bytes) > MAX_IMAGE_SIZE_BYTES:
        raise WordPressMediaError(
            error_code="IMAGE_FILE_TOO_LARGE",
            error_message=(
                "The image exceeds the configured maximum size "
                f"of {MAX_IMAGE_SIZE_BYTES} bytes."
            ),
            response_payload={
                "storage_bucket": storage_bucket,
                "storage_path": storage_path,
                "file_size_bytes": len(
                    image_bytes
                ),
            },
        )

    configured_type = (
        configured_mime_type
        or ""
    ).strip().lower()

    guessed_type, _ = mimetypes.guess_type(
        storage_path
    )

    content_type: str | None = None

    for candidate_type in [
        configured_type,
        guessed_type or "",
    ]:
        if candidate_type in SUPPORTED_IMAGE_TYPES:
            content_type = candidate_type
            break

    if not content_type:
        raise WordPressMediaError(
            error_code="UNSUPPORTED_IMAGE_TYPE",
            error_message=(
                "Could not determine a supported MIME type "
                "for the Supabase image."
            ),
            response_payload={
                "storage_bucket": storage_bucket,
                "storage_path": storage_path,
                "configured_mime_type": configured_type,
                "guessed_mime_type": guessed_type,
            },
        )

    validate_image_signature(
        image_bytes=image_bytes,
        content_type=content_type,
    )

    print(
        f"Validated MIME type: {content_type}"
    )

    return (
        image_bytes,
        content_type,
    )


def build_media_file_name(
    product_code: str,
    image: dict[str, Any],
    content_type: str,
    image_position: int,
) -> str:
    """Build a predictable WordPress media file name."""
    extension = SUPPORTED_IMAGE_TYPES[
        content_type
    ]

    role = (
        image.get("image_role")
        or f"image-{image_position}"
    )

    base_name = sanitize_file_name(
        f"{product_code}-{role}"
    )

    return (
        f"{base_name}{extension}"
    )


def upload_wordpress_media(
    store_url: str,
    username: str,
    application_password: str,
    timeout_seconds: int,
    file_name: str,
    image_bytes: bytes,
    content_type: str,
) -> dict[str, Any]:
    """Upload one image to WordPress Media Library."""
    api_url = build_api_url(
        store_url=store_url,
        namespace="wp/v2",
        endpoint="media",
    )

    print()
    print(
        f"Uploading image to WordPress: {file_name}"
    )

    response = requests.post(
        api_url,
        auth=HTTPBasicAuth(
            username,
            application_password,
        ),
        headers={
            "Content-Disposition": (
                f'attachment; filename="{file_name}"'
            ),
            "Content-Type": content_type,
            "Accept": "application/json",
            "User-Agent": (
                f"{CREATOR_NAME}/"
                f"{CREATOR_VERSION}"
            ),
        },
        data=image_bytes,
        timeout=timeout_seconds,
    )

    response_payload = safe_json_response(
        response
    )

    print(
        f"WordPress media HTTP status: {response.status_code}"
    )

    if response.status_code not in {
        200,
        201,
    }:
        error_code = "WORDPRESS_MEDIA_UPLOAD_FAILED"

        error_message = (
            "WordPress media upload failed. "
            f"HTTP status: {response.status_code}."
        )

        if isinstance(
            response_payload,
            dict,
        ):
            error_code = str(
                response_payload.get("code")
                or error_code
            )

            error_message = str(
                response_payload.get("message")
                or error_message
            )

        raise WordPressMediaError(
            error_code=error_code,
            error_message=error_message,
            response_payload=response_payload,
        )

    if not isinstance(
        response_payload,
        dict,
    ):
        raise WordPressMediaError(
            error_code="INVALID_MEDIA_RESPONSE",
            error_message=(
                "WordPress returned an unexpected media response."
            ),
            response_payload=response_payload,
        )

    media_id = response_payload.get(
        "id"
    )

    if not media_id:
        raise WordPressMediaError(
            error_code="MEDIA_ID_MISSING",
            error_message=(
                "WordPress did not return a media attachment ID."
            ),
            response_payload=response_payload,
        )

    return response_payload


def update_wordpress_media_metadata(
    store_url: str,
    username: str,
    application_password: str,
    timeout_seconds: int,
    media_id: int,
    product_name: str,
    image_role: str | None,
) -> dict[str, Any]:
    """Update attachment title and alternative text."""
    api_url = build_api_url(
        store_url=store_url,
        namespace="wp/v2",
        endpoint=f"media/{media_id}",
    )

    role_labels = {
        "FRONT_COVER": "Ảnh bìa trước",
        "BACK_COVER": "Ảnh bìa sau",
        "INSIDE_PAGE": "Ảnh trang bên trong",
        "COMBO_IMAGE": "Ảnh bộ sách",
        "LIFESTYLE": "Ảnh minh họa",
        "OTHER": "Ảnh sản phẩm",
    }

    role_label = role_labels.get(
        image_role or "",
        "Ảnh sản phẩm",
    )

    payload = {
        "title": f"{product_name} – {role_label}",
        "alt_text": product_name,
        "caption": "",
        "description": (
            f"{role_label} của sản phẩm {product_name}."
        ),
    }

    response = requests.post(
        api_url,
        auth=HTTPBasicAuth(
            username,
            application_password,
        ),
        json=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": (
                f"{CREATOR_NAME}/"
                f"{CREATOR_VERSION}"
            ),
        },
        timeout=timeout_seconds,
    )

    response_payload = safe_json_response(
        response
    )

    if response.status_code not in {
        200,
        201,
    }:
        raise WordPressMediaError(
            error_code="MEDIA_METADATA_UPDATE_FAILED",
            error_message=(
                "The image was uploaded, but WordPress could not "
                "update its metadata. "
                f"HTTP status: {response.status_code}."
            ),
            response_payload=response_payload,
        )

    if not isinstance(
        response_payload,
        dict,
    ):
        raise WordPressMediaError(
            error_code="INVALID_MEDIA_METADATA_RESPONSE",
            error_message=(
                "WordPress returned an unexpected media metadata response."
            ),
            response_payload=response_payload,
        )

    return response_payload


def save_uploaded_media_progress(
    repository: SupabaseRepository,
    sync_id: str,
    uploaded_media: list[dict[str, Any]],
) -> None:
    """Persist WordPress media IDs before creating the product."""
    response = (
        repository.client
        .table("woocommerce_product_syncs")
        .update(
            {
                "response_payload": {
                    "uploaded_media": uploaded_media,
                    "media_upload_completed": False,
                    "updated_at": utc_now(),
                },
                "updated_at": utc_now(),
            }
        )
        .eq(
            "sync_id",
            sync_id,
        )
        .execute()
    )

    if not response.data:
        raise RuntimeError(
            "Could not save WordPress media upload progress."
        )


def upload_or_reuse_product_images(
    repository: SupabaseRepository,
    sync: dict[str, Any],
    product: dict[str, Any],
    product_name: str,
    images: list[dict[str, Any]],
    store_url: str,
    username: str,
    application_password: str,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    """Upload product images or reuse existing WordPress attachments."""
    previously_uploaded_media = extract_uploaded_media(
        sync
    )

    final_media: list[dict[str, Any]] = []

    for position, image in enumerate(
        images,
        start=1,
    ):
        image_id = str(
            image["image_id"]
        )

        saved_media = find_saved_media_for_image(
            uploaded_media=previously_uploaded_media,
            image_id=image_id,
        )

        if saved_media:
            saved_media_id = saved_media.get(
                "wordpress_media_id"
            )

            if saved_media_id:
                exists = verify_wordpress_media(
                    store_url=store_url,
                    username=username,
                    application_password=application_password,
                    timeout_seconds=timeout_seconds,
                    media_id=int(
                        saved_media_id
                    ),
                )

                if exists:
                    print()
                    print(
                        "Reusing existing WordPress media ID: "
                        f"{saved_media_id}"
                    )

                    final_media.append(
                        saved_media
                    )

                    continue

        storage_bucket = image.get(
            "storage_bucket"
        )

        storage_path = image.get(
            "storage_path"
        )

        if not storage_bucket or not storage_path:
            raise WordPressMediaError(
                error_code="IMAGE_STORAGE_LOCATION_MISSING",
                error_message=(
                    "A publishable image does not have a "
                    "Supabase storage bucket and path."
                ),
                response_payload={
                    "image_id": image_id,
                },
            )

        image_bytes, content_type = (
            download_source_image_from_supabase(
                repository=repository,
                storage_bucket=storage_bucket,
                storage_path=storage_path,
                configured_mime_type=image.get(
                    "mime_type"
                ),
            )
        )

        file_name = build_media_file_name(
            product_code=product[
                "product_code"
            ],
            image=image,
            content_type=content_type,
            image_position=position,
        )

        uploaded = upload_wordpress_media(
            store_url=store_url,
            username=username,
            application_password=application_password,
            timeout_seconds=timeout_seconds,
            file_name=file_name,
            image_bytes=image_bytes,
            content_type=content_type,
        )

        media_id = int(
            uploaded["id"]
        )

        updated_media = update_wordpress_media_metadata(
            store_url=store_url,
            username=username,
            application_password=application_password,
            timeout_seconds=timeout_seconds,
            media_id=media_id,
            product_name=product_name,
            image_role=image.get(
                "image_role"
            ),
        )

        media_record = {
            "source_image_id": image_id,
            "wordpress_media_id": media_id,
            "wordpress_source_url": (
                updated_media.get(
                    "source_url"
                )
                or uploaded.get(
                    "source_url"
                )
            ),
            "storage_bucket": storage_bucket,
            "storage_path": storage_path,
            "file_name": file_name,
            "mime_type": content_type,
            "image_role": image.get(
                "image_role"
            ),
            "is_selected_main_image": bool(
                image.get(
                    "is_selected_main_image"
                )
            ),
            "uploaded_at": utc_now(),
        }

        final_media.append(
            media_record
        )

        save_uploaded_media_progress(
            repository=repository,
            sync_id=sync[
                "sync_id"
            ],
            uploaded_media=final_media,
        )

        print(
            "WordPress media uploaded successfully."
        )

        print(
            f"WordPress media ID: {media_id}"
        )

    return final_media


def build_woocommerce_payload(
    product: dict[str, Any],
    content: dict[str, Any],
    uploaded_media: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the WooCommerce product draft payload."""
    image_payload = [
        {
            "id": int(
                media[
                    "wordpress_media_id"
                ]
            ),
        }
        for media in uploaded_media
    ]

    payload = {
        "name": content[
            "product_name"
        ],
        "type": "simple",
        "status": "draft",
        "sku": product[
            "product_code"
        ],
        "description": build_description(
            content
        ),
        "short_description": build_short_description(
            content
        ),
        "manage_stock": False,
        "catalog_visibility": "visible",
        "images": image_payload,
        "meta_data": [
            {
                "key": "_tsyc_internal_product_id",
                "value": product[
                    "internal_product_id"
                ],
            },
            {
                "key": "_tsyc_candidate_id",
                "value": product[
                    "candidate_id"
                ],
            },
            {
                "key": "_tsyc_product_content_id",
                "value": content[
                    "product_content_id"
                ],
            },
            {
                "key": "_tsyc_price_review_required",
                "value": "yes",
            },
            {
                "key": "_tsyc_creator_name",
                "value": CREATOR_NAME,
            },
            {
                "key": "_tsyc_creator_version",
                "value": CREATOR_VERSION,
            },
        ],
    }

    if payload.get("status") != "draft":
        raise RuntimeError(
            "Safety guard failed: WooCommerce product status must be draft."
        )

    forbidden_price_fields = {
        "regular_price",
        "sale_price",
        "price",
    }

    included_price_fields = forbidden_price_fields.intersection(
        payload.keys()
    )

    if included_price_fields:
        raise RuntimeError(
            "Safety guard failed: draft payload must not include price fields. "
            f"Found: {sorted(included_price_fields)}"
        )

    return payload


def save_request_payload(
    repository: SupabaseRepository,
    sync_id: str,
    request_payload: dict[str, Any],
    uploaded_media: list[dict[str, Any]],
) -> None:
    """Save the final WooCommerce request before sending it."""
    response = (
        repository.client
        .table("woocommerce_product_syncs")
        .update(
            {
                "request_payload": request_payload,
                "response_payload": {
                    "uploaded_media": uploaded_media,
                    "media_upload_completed": True,
                    "updated_at": utc_now(),
                },
                "updated_at": utc_now(),
            }
        )
        .eq(
            "sync_id",
            sync_id,
        )
        .execute()
    )

    if not response.data:
        raise RuntimeError(
            "Could not save the final WooCommerce request payload."
        )


def create_woocommerce_product(
    store_url: str,
    api_version: str,
    consumer_key: str,
    consumer_secret: str,
    timeout_seconds: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Create one WooCommerce product in draft status."""
    api_url = build_api_url(
        store_url=store_url,
        namespace=api_version,
        endpoint="products",
    )

    response = requests.post(
        api_url,
        auth=HTTPBasicAuth(
            consumer_key,
            consumer_secret,
        ),
        json=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": (
                f"{CREATOR_NAME}/"
                f"{CREATOR_VERSION}"
            ),
        },
        timeout=timeout_seconds,
    )

    response_payload = safe_json_response(
        response
    )

    if response.status_code not in {
        200,
        201,
    }:
        error_code = "WOOCOMMERCE_CREATE_FAILED"

        error_message = (
            "WooCommerce product creation failed. "
            f"HTTP status: {response.status_code}."
        )

        if isinstance(
            response_payload,
            dict,
        ):
            error_code = str(
                response_payload.get("code")
                or error_code
            )

            error_message = str(
                response_payload.get("message")
                or error_message
            )

        raise WooCommerceCreateError(
            error_code=error_code,
            error_message=error_message,
            response_payload=response_payload,
        )

    if not isinstance(
        response_payload,
        dict,
    ):
        raise WooCommerceCreateError(
            error_code="INVALID_WOOCOMMERCE_RESPONSE",
            error_message=(
                "WooCommerce returned an unexpected product response."
            ),
            response_payload=response_payload,
        )

    product_id = response_payload.get(
        "id"
    )

    if not product_id:
        raise WooCommerceCreateError(
            error_code="WOOCOMMERCE_PRODUCT_ID_MISSING",
            error_message=(
                "WooCommerce did not return a product ID."
            ),
            response_payload=response_payload,
        )

    if response_payload.get("status") != "draft":
        raise WooCommerceCreateError(
            error_code="UNEXPECTED_PRODUCT_STATUS",
            error_message=(
                "WooCommerce created the product with an "
                "unexpected status."
            ),
            response_payload=response_payload,
        )

    return response_payload


def mark_sync_failed(
    repository: SupabaseRepository,
    sync_id: str,
    error_code: str,
    error_message: str,
    uploaded_media: list[dict[str, Any]],
    error_payload: Any,
) -> None:
    """Save a failed synchronization without losing media IDs."""
    if isinstance(
        error_payload,
        dict,
    ):
        normalized_error_payload = error_payload

    else:
        normalized_error_payload = {
            "raw_error": str(
                error_payload
            )[:5000],
        }

    response_payload = {
        "uploaded_media": uploaded_media,
        "media_upload_completed": bool(
            uploaded_media
        ),
        "error": normalized_error_payload,
        "failed_at": utc_now(),
    }

    (
        repository.client
        .table("woocommerce_product_syncs")
        .update(
            {
                "woocommerce_status": "FAILED",
                "response_payload": response_payload,
                "error_code": error_code,
                "error_message": error_message,
                "updated_at": utc_now(),
            }
        )
        .eq(
            "sync_id",
            sync_id,
        )
        .execute()
    )


def mark_sync_succeeded(
    repository: SupabaseRepository,
    sync_id: str,
    product_response: dict[str, Any],
    uploaded_media: list[dict[str, Any]],
) -> None:
    """Save the successful WooCommerce product result."""
    response_payload = {
        "uploaded_media": uploaded_media,
        "media_upload_completed": True,
        "woocommerce_product": product_response,
    }

    response = (
        repository.client
        .table("woocommerce_product_syncs")
        .update(
            {
                "woocommerce_product_id": (
                    product_response.get("id")
                ),
                "woocommerce_status": "DRAFT_CREATED",
                "product_permalink": (
                    product_response.get(
                        "permalink"
                    )
                ),
                "response_payload": response_payload,
                "error_code": None,
                "error_message": None,
                "synced_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        .eq(
            "sync_id",
            sync_id,
        )
        .execute()
    )

    if not response.data:
        raise RuntimeError(
            "Could not save the successful WooCommerce result."
        )


def mark_post_create_recovery_required(
    repository: SupabaseRepository,
    sync_id: str,
    product_response: dict[str, Any],
    uploaded_media: list[dict[str, Any]],
    error: Exception,
) -> None:
    """
    Preserve the remote Woo draft identity if local finalization fails.

    The sync remains DRAFT_CREATED because the remote object really exists.
    Error fields flag that Supabase reconciliation is still required.
    """
    response_payload = {
        "woocommerce_product": product_response,
        "uploaded_media": uploaded_media,
        "media_upload_completed": True,
        "recovery_required": True,
        "recovery_error": {
            "exception_type": type(error).__name__,
            "exception_message": str(error),
        },
        "failed_local_finalization_at": utc_now(),
    }

    (
        repository.client
        .table("woocommerce_product_syncs")
        .update(
            {
                "woocommerce_product_id": product_response.get("id"),
                "woocommerce_status": "DRAFT_CREATED",
                "product_permalink": product_response.get("permalink"),
                "response_payload": response_payload,
                "error_code": "LOCAL_FINALIZATION_FAILED",
                "error_message": str(error),
                "synced_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        .eq(
            "sync_id",
            sync_id,
        )
        .execute()
    )


def update_internal_product(
    repository: SupabaseRepository,
    product: dict[str, Any],
    product_response: dict[str, Any],
    uploaded_media: list[dict[str, Any]],
) -> None:
    """Mark the internal product as having a WooCommerce draft."""
    existing_metadata = product.get(
        "product_metadata"
    )

    if isinstance(
        existing_metadata,
        dict,
    ):
        product_metadata = dict(
            existing_metadata
        )

    else:
        product_metadata = {}

    product_metadata[
        "woocommerce_draft"
    ] = {
        "woocommerce_product_id": (
            product_response.get("id")
        ),
        "woocommerce_status": (
            product_response.get("status")
        ),
        "woocommerce_permalink": (
            product_response.get("permalink")
        ),
        "wordpress_media_ids": [
            item.get(
                "wordpress_media_id"
            )
            for item in uploaded_media
        ],
        "price_included": False,
        "price_review_required": True,
        "creator_name": CREATOR_NAME,
        "creator_version": CREATOR_VERSION,
        "created_at": utc_now(),
    }

    response = (
        repository.client
        .table("internal_products")
        .update(
            {
                "woocommerce_status": "DRAFT_CREATED",
                "product_metadata": product_metadata,
                "updated_at": utc_now(),
            }
        )
        .eq(
            "internal_product_id",
            product[
                "internal_product_id"
            ],
        )
        .execute()
    )

    if not response.data:
        raise RuntimeError(
            "Could not update the internal product status."
        )


def print_preview(
    product: dict[str, Any],
    content: dict[str, Any],
    images: list[dict[str, Any]],
    sync: dict[str, Any] | None,
) -> None:
    """Print the WooCommerce draft preview."""
    print()
    print("=" * 72)
    print("WORDPRESS MEDIA AND WOOCOMMERCE DRAFT PREVIEW")
    print("=" * 72)

    print(
        f"Product code: {product.get('product_code')}"
    )

    print(
        f"Product name: {content.get('product_name')}"
    )

    print(
        "WooCommerce product status: draft"
    )

    print(
        "WooCommerce product type: simple"
    )

    print(
        f"Images to process: {len(images)}"
    )

    print(
        "Image source: authenticated Supabase Storage"
    )

    print(
        "Image transfer method: direct WordPress Media API upload"
    )

    print(
        "WooCommerce image method: WordPress attachment ID"
    )

    print(
        "Price included: NO"
    )

    print(
        "Price review required before publishing: YES"
    )

    if sync:
        print()
        print(
            f"Existing sync status: {sync.get('woocommerce_status')}"
        )

        print(
            "Existing sync attempts: "
            f"{sync.get('sync_attempt_count')}"
        )

        saved_media = extract_uploaded_media(
            sync
        )

        print(
            "Reusable WordPress media records: "
            f"{len(saved_media)}"
        )


def main() -> None:
    """Upload images and create one WooCommerce product draft."""
    load_dotenv(
        PROJECT_ROOT / ".env"
    )

    args = parse_arguments()

    if args.non_interactive:
        if not args.product_code:
            raise RuntimeError(
                "--non-interactive requires --product-code."
            )

        if not args.confirm_create:
            raise RuntimeError(
                "--non-interactive requires --confirm-create."
            )

    print("=" * 72)
    print("WOOCOMMERCE DRAFT CREATOR")
    print("=" * 72)

    print(
        f"Version: {CREATOR_VERSION}"
    )

    store_url = get_required_environment_variable(
        "WOOCOMMERCE_URL"
    )

    consumer_key = get_required_environment_variable(
        "WOOCOMMERCE_CONSUMER_KEY"
    )

    consumer_secret = get_required_environment_variable(
        "WOOCOMMERCE_CONSUMER_SECRET"
    )

    wordpress_username = get_required_environment_variable(
        "WORDPRESS_USERNAME"
    )

    wordpress_application_password = clean_application_password(
        get_required_environment_variable(
            "WORDPRESS_APPLICATION_PASSWORD"
        )
    )

    api_version = os.getenv(
        "WOOCOMMERCE_API_VERSION",
        "wc/v3",
    ).strip()

    timeout_seconds = get_timeout_seconds()

    if not store_url.startswith(
        "https://"
    ):
        raise RuntimeError(
            "WOOCOMMERCE_URL must use HTTPS."
        )

    repository = SupabaseRepository()

    product = get_ready_product(
        repository=repository,
        product_code=args.product_code,
    )

    if product is None:
        print(
            "No product with READY_FOR_DRAFT status was found."
        )

        return

    content = get_approved_content(
        repository=repository,
        internal_product_id=product[
            "internal_product_id"
        ],
    )

    if content is None:
        raise RuntimeError(
            "Approved Vietnamese product content was not found."
        )

    images = get_publishable_images(
        repository=repository,
        candidate_id=product[
            "candidate_id"
        ],
    )

    if not images:
        raise RuntimeError(
            "No validated publishable product image was found."
        )

    selected_main_images = [
        image
        for image in images
        if bool(
            image.get(
                "is_selected_main_image"
            )
        )
    ]

    if len(selected_main_images) != 1:
        raise RuntimeError(
            "Exactly one selected main product image is required. "
            f"Found: {len(selected_main_images)}"
        )

    existing_sync = get_existing_sync(
        repository=repository,
        internal_product_id=product[
            "internal_product_id"
        ],
    )

    print_preview(
        product=product,
        content=content,
        images=images,
        sync=existing_sync,
    )

    if has_existing_remote_draft(existing_sync):
        print()
        print(
            "Draft creation stopped because the local sync "
            "already has a WooCommerce product ID."
        )

        print(
            "WooCommerce product ID: "
            f"{existing_sync.get('woocommerce_product_id')}"
        )

        return

    existing_product = find_product_by_sku(
        store_url=store_url,
        api_version=api_version,
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        timeout_seconds=timeout_seconds,
        sku=product[
            "product_code"
        ],
    )

    if existing_product:
        print()
        print(
            "Draft creation stopped because WooCommerce already "
            "contains this SKU."
        )

        print(
            "WooCommerce product ID: "
            f"{existing_product.get('id')}"
        )

        print(
            "WooCommerce product status: "
            f"{existing_product.get('status')}"
        )

        return

    print()

    if args.confirm_create:
        confirmation = "UPLOAD_AND_CREATE"

    elif args.non_interactive:
        confirmation = ""

    else:
        confirmation = normalize_confirmation(
            input(
                "Type UPLOAD_AND_CREATE, CREATE, CREATE_DRAFT, or DRAFT "
                "to upload images and create the WooCommerce draft, "
                "or press Enter to cancel: "
            )
        )

    if confirmation not in VALID_CONFIRMATIONS:
        print()
        print(
            "Invalid confirmation. Use UPLOAD_AND_CREATE, CREATE, "
            "CREATE_DRAFT, or DRAFT."
        )
        print(
            f"Received value: {confirmation or '[empty]'}"
        )
        print(
            "WooCommerce draft creation was cancelled."
        )
        return

    if existing_sync is None:
        existing_sync = create_sync_record(
            repository=repository,
            product=product,
            product_name=content[
                "product_name"
            ],
        )

    sync = mark_sync_in_progress(
        repository=repository,
        sync=existing_sync,
    )

    uploaded_media = extract_uploaded_media(
        sync
    )

    try:
        uploaded_media = upload_or_reuse_product_images(
            repository=repository,
            sync=sync,
            product=product,
            product_name=content[
                "product_name"
            ],
            images=images,
            store_url=store_url,
            username=wordpress_username,
            application_password=wordpress_application_password,
            timeout_seconds=timeout_seconds,
        )

        (
            product,
            content,
            current_images,
        ) = revalidate_pre_create_state(
            repository=repository,
            product_code=product["product_code"],
        )

        assert_uploaded_image_set_matches_current(
            current_images=current_images,
            uploaded_media=uploaded_media,
        )

        woo_payload = build_woocommerce_payload(
            product=product,
            content=content,
            uploaded_media=uploaded_media,
        )

        save_request_payload(
            repository=repository,
            sync_id=sync[
                "sync_id"
            ],
            request_payload=woo_payload,
            uploaded_media=uploaded_media,
        )

        product_response = create_woocommerce_product(
            store_url=store_url,
            api_version=api_version,
            consumer_key=consumer_key,
            consumer_secret=consumer_secret,
            timeout_seconds=timeout_seconds,
            payload=woo_payload,
        )

    except WordPressMediaError as error:
        mark_sync_failed(
            repository=repository,
            sync_id=sync[
                "sync_id"
            ],
            error_code=error.error_code,
            error_message=error.error_message,
            uploaded_media=uploaded_media,
            error_payload=error.response_payload,
        )

        raise

    except WooCommerceCreateError as error:
        mark_sync_failed(
            repository=repository,
            sync_id=sync[
                "sync_id"
            ],
            error_code=error.error_code,
            error_message=error.error_message,
            uploaded_media=uploaded_media,
            error_payload=error.response_payload,
        )

        raise

    except Exception as error:
        mark_sync_failed(
            repository=repository,
            sync_id=sync[
                "sync_id"
            ],
            error_code=type(error).__name__,
            error_message=str(error),
            uploaded_media=uploaded_media,
            error_payload={
                "exception_type": type(
                    error
                ).__name__,
                "exception_message": str(
                    error
                ),
            },
        )

        raise

    try:
        mark_sync_succeeded(
            repository=repository,
            sync_id=sync[
                "sync_id"
            ],
            product_response=product_response,
            uploaded_media=uploaded_media,
        )

        update_internal_product(
            repository=repository,
            product=product,
            product_response=product_response,
            uploaded_media=uploaded_media,
        )

    except Exception as error:
        try:
            mark_post_create_recovery_required(
                repository=repository,
                sync_id=sync[
                    "sync_id"
                ],
                product_response=product_response,
                uploaded_media=uploaded_media,
                error=error,
            )
        except Exception as recovery_error:
            print()
            print(
                "Warning: WooCommerce draft exists, but local recovery "
                "metadata could not be saved."
            )
            print(
                "Recovery error: "
                f"{recovery_error}"
            )

        raise RuntimeError(
            "WooCommerce draft was created remotely, but local Supabase "
            "finalization failed. Do not retry draft creation. Run the "
            "WooCommerce status synchronization/recovery workflow instead."
        ) from error

    print()
    print("=" * 72)
    print("WOOCOMMERCE DRAFT RESULT")
    print("=" * 72)

    print(
        "WooCommerce product ID: "
        f"{product_response.get('id')}"
    )

    print(
        "Product name: "
        f"{product_response.get('name')}"
    )

    print(
        "SKU: "
        f"{product_response.get('sku')}"
    )

    print(
        "Status: "
        f"{product_response.get('status')}"
    )

    print(
        "Price: "
        f"{product_response.get('regular_price') or '[not set]'}"
    )

    print(
        "WordPress media IDs: "
        + ", ".join(
            str(
                item.get(
                    "wordpress_media_id"
                )
            )
            for item in uploaded_media
        )
    )

    print(
        "Permalink: "
        f"{product_response.get('permalink') or '[not returned]'}"
    )

    print()
    print(
        "WooCommerce draft creation completed successfully."
    )

    print(
        "The shop owner must review the product and add "
        "the price before publishing."
    )


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print()
        print(
            "WooCommerce draft creation was cancelled."
        )

        sys.exit(130)

    except requests.Timeout:
        print()
        print(
            "WooCommerce draft creation failed."
        )

        print(
            "Error type: Timeout"
        )

        print(
            "The server response is uncertain. Check WordPress "
            "Media Library and the WooCommerce SKU before retrying."
        )

        sys.exit(1)

    except requests.ConnectionError as error:
        print()
        print(
            "WooCommerce draft creation failed."
        )

        print(
            "Error type: ConnectionError"
        )

        print(
            f"Error details: {error}"
        )

        print(
            "Check WordPress Media Library and WooCommerce "
            "before retrying."
        )

        sys.exit(1)

    except WordPressMediaError as error:
        print()
        print(
            "WordPress media upload failed."
        )

        print(
            f"Error code: {error.error_code}"
        )

        print(
            f"Error details: {error.error_message}"
        )

        print(
            f"Error payload: {error.response_payload}"
        )

        sys.exit(1)

    except WooCommerceCreateError as error:
        print()
        print(
            "WooCommerce draft creation failed."
        )

        print(
            f"Error code: {error.error_code}"
        )

        print(
            f"Error details: {error.error_message}"
        )

        print(
            f"Error payload: {error.response_payload}"
        )

        sys.exit(1)

    except Exception as error:
        print()
        print(
            "WooCommerce draft creation failed."
        )

        print(
            f"Error type: {type(error).__name__}"
        )

        print(
            f"Error details: {error}"
        )

        sys.exit(1)