import argparse
import hashlib
import json
import mimetypes
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import (
    APIResponse,
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(
    0,
    str(PROJECT_ROOT),
)

from src.cli_bootstrap import configure_utf8_console
from src.repositories.supabase_repository import (
    SupabaseRepository,
)

configure_utf8_console()


BATCH_CODE = "FB-2026-001"

COLLECTOR_NAME = (
    "facebook_permalink_image_downloader"
)

COLLECTOR_VERSION = "0.5.0"

FACEBOOK_PROFILE_DIRECTORY = (
    PROJECT_ROOT
    / "playwright"
    / "facebook-profile"
)

IMAGE_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "data"
    / "raw"
    / "facebook-images"
)

MIN_IMAGE_WIDTH = 250
MIN_IMAGE_HEIGHT = 250
MIN_RENDERED_WIDTH = 100
MIN_RENDERED_HEIGHT = 100
MIN_CONTAINER_OVERLAP = 0.30

MAX_DOWNLOAD_IMAGES = 30
MAX_IMAGE_BYTES = 25 * 1024 * 1024

POST_CONTAINER_SELECTORS = (
    "div[role='dialog'] div[role='article']",
    "div[role='main'] div[role='article']",
    "div[role='article']",
    "div[role='dialog']",
    "div[role='main']",
)

IGNORED_ALT_TEXT_PARTS = (
    "profile picture",
    "ảnh đại diện",
    "avatar",
    "emoji",
    "facebook logo",
    "logo facebook",
    "biểu tượng",
    "sticker",
)

IGNORED_URL_PARTS = (
    "emoji.php",
    "rsrc.php",
    "static.xx.fbcdn.net",
    "/safe_image.php",
)

ALLOWED_CONTENT_TYPES = {
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


def validate_uuid_argument(
    value: str,
) -> str:
    """Validate that a CLI value is a syntactically correct UUID."""
    try:
        return str(
            uuid.UUID(
                value
            )
        )

    except (
        ValueError,
        AttributeError,
        TypeError,
    ) as error:
        raise argparse.ArgumentTypeError(
            f"--raw-page-id must be a valid UUID: {value!r}"
        ) from error


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Download images from one already-collected, cleaned Facebook "
            "post using the persistent authenticated Facebook browser "
            "profile."
        )
    )

    parser.add_argument(
        "--raw-page-id",
        type=validate_uuid_argument,
        default=None,
        help=(
            "Exact raw_pages.raw_page_id (UUID) to target. Required with "
            "--non-interactive. When supplied, the interactive batch-wide "
            "page selection menu is skipped entirely and this exact page "
            "is used -- there is never a fallback to another page."
        ),
    )

    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help=(
            "Disable all input() prompts (page selection, download "
            "confirmation, and the closing prompt). Requires "
            "--raw-page-id and --confirm-download."
        ),
    )

    parser.add_argument(
        "--confirm-download",
        action="store_true",
        help=(
            "Explicit machine-readable confirmation that replaces the "
            "interactive 'Type DOWNLOAD' prompt. Required with "
            "--non-interactive; the browser will not be launched in "
            "non-interactive mode without it."
        ),
    )

    return parser.parse_args()


def normalize_text(
    value: str | None,
) -> str:
    """Normalize whitespace for stable text comparison."""
    if not value:
        return ""

    return " ".join(
        value.split()
    ).strip()


def tokenize_text(
    value: str,
) -> set[str]:
    """Create normalized tokens for post-container matching."""
    normalized = normalize_text(
        value
    ).lower()

    tokens = re.findall(
        r"[0-9a-zA-ZÀ-ỹ]+",
        normalized,
    )

    return {
        token
        for token in tokens
        if len(token) >= 3
    }


def calculate_text_overlap(
    reference_text: str,
    candidate_text: str,
) -> float:
    """Calculate token overlap between two text values."""
    reference_tokens = tokenize_text(
        reference_text
    )

    candidate_tokens = tokenize_text(
        candidate_text
    )

    if not reference_tokens:
        return 0.0

    matching_tokens = (
        reference_tokens
        & candidate_tokens
    )

    return (
        len(matching_tokens)
        / len(reference_tokens)
    )


def get_cleaned_raw_pages(
    repository: SupabaseRepository,
    batch_id: str,
) -> list[dict[str, Any]]:
    """Return cleaned Facebook posts available for image download."""
    response = (
        repository.client
        .table("raw_pages")
        .select(
            "raw_page_id, "
            "source_url_id, "
            "page_url, "
            "raw_title, "
            "cleaned_text, "
            "cleaning_status, "
            "collected_at"
        )
        .eq(
            "batch_id",
            batch_id,
        )
        .eq(
            "page_type",
            "FACEBOOK_POST",
        )
        .eq(
            "cleaning_status",
            "CLEANED",
        )
        .order(
            "collected_at",
            desc=True,
        )
        .execute()
    )

    return response.data or []


def resolve_exact_raw_page(
    repository: SupabaseRepository,
    batch_id: str,
    raw_page_id: str,
) -> dict[str, Any]:
    """
    Resolve exactly one Facebook raw page by its exact raw_page_id.

    This never falls back to another raw page and never iterates through
    the batch-wide page list. Each way the requested page can fail to
    qualify raises a specific, clear error before any browser is opened.
    """
    response = (
        repository.client
        .table("raw_pages")
        .select(
            "raw_page_id, "
            "batch_id, "
            "source_url_id, "
            "page_type, "
            "page_url, "
            "raw_title, "
            "cleaned_text, "
            "cleaning_status, "
            "collected_at"
        )
        .eq(
            "raw_page_id",
            raw_page_id,
        )
        .limit(2)
        .execute()
    )

    rows = response.data or []

    if not rows:
        raise RuntimeError(
            "No raw_pages row was found for the exact raw_page_id: "
            f"{raw_page_id}"
        )

    if len(rows) != 1:
        raise RuntimeError(
            "raw_page_id did not resolve to exactly one row: "
            f"{raw_page_id}"
        )

    raw_page = rows[0]

    if str(raw_page.get("batch_id")) != str(batch_id):
        raise RuntimeError(
            "The requested raw_page_id does not belong to batch "
            f"{BATCH_CODE}: {raw_page_id}"
        )

    if raw_page.get("page_type") != "FACEBOOK_POST":
        raise RuntimeError(
            "The requested raw_page_id is not a Facebook source page "
            f"(page_type={raw_page.get('page_type')!r}): {raw_page_id}"
        )

    if raw_page.get("cleaning_status") != "CLEANED":
        raise RuntimeError(
            "The requested raw_page_id has not been cleaned "
            f"(cleaning_status={raw_page.get('cleaning_status')!r}): "
            f"{raw_page_id}"
        )

    if not str(raw_page.get("page_url") or "").strip():
        raise RuntimeError(
            f"The requested raw_page_id has no page URL: {raw_page_id}"
        )

    if not str(raw_page.get("cleaned_text") or "").strip():
        raise RuntimeError(
            f"The requested raw_page_id has no cleaned text: {raw_page_id}"
        )

    return raw_page


def select_or_resolve_raw_page(
    repository: SupabaseRepository,
    batch_id: str,
    raw_page_id: str | None,
    non_interactive: bool,
) -> dict[str, Any]:
    """
    Select exactly one raw page.

    When raw_page_id is supplied (interactive or non-interactive), it is
    resolved exactly with no fallback and the batch-wide menu is skipped.
    Otherwise, in interactive mode only, this falls back to the existing
    manual batch-wide menu selection.
    """
    if raw_page_id:
        return resolve_exact_raw_page(
            repository=repository,
            batch_id=batch_id,
            raw_page_id=raw_page_id,
        )

    if non_interactive:
        # Backstop only: main() already validates this before any
        # database or browser work happens.
        raise RuntimeError(
            "--non-interactive requires --raw-page-id."
        )

    raw_pages = get_cleaned_raw_pages(
        repository=repository,
        batch_id=batch_id,
    )

    return select_raw_page(raw_pages)


def print_preflight_summary(
    raw_page: dict[str, Any],
    batch_code: str,
    output_directory: Path,
    non_interactive: bool,
    confirm_download: bool,
) -> None:
    """Print an exact-target preflight summary before launching Playwright."""
    print()
    print("=" * 78)
    print("PREFLIGHT SUMMARY")
    print("=" * 78)
    print(
        "Target raw_page_id: "
        f"{raw_page.get('raw_page_id')}"
    )
    print(
        f"Batch: {batch_code}"
    )
    print(
        "Source URL: "
        f"{raw_page.get('page_url')}"
    )
    print(
        "Cleaning status: "
        f"{raw_page.get('cleaning_status')}"
    )
    print(
        f"Output directory: {output_directory}"
    )
    print(
        "Mode: "
        f"{'NON_INTERACTIVE' if non_interactive else 'INTERACTIVE'}"
    )

    if non_interactive:
        print(
            "Exact target locked: YES"
        )
        print(
            "Download confirmation supplied: "
            f"{'YES' if confirm_download else 'NO'}"
        )


def select_raw_page(
    raw_pages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Allow the user to select one Facebook post."""
    if not raw_pages:
        raise RuntimeError(
            "No cleaned Facebook raw pages were found."
        )

    print()
    print("Cleaned Facebook pages:")

    for index, raw_page in enumerate(
        raw_pages,
        start=1,
    ):
        title = (
            raw_page.get("raw_title")
            or "Untitled Facebook post"
        )

        print()
        print(f"[{index}] {title}")
        print(
            f"    URL: {raw_page.get('page_url')}"
        )
        print(
            "    Raw page ID: "
            f"{raw_page.get('raw_page_id')}"
        )

    print()

    selection = input(
        "Enter the page number to download images from "
        "[default: 1]: "
    ).strip()

    if not selection:
        return raw_pages[0]

    try:
        selected_index = int(selection) - 1

    except ValueError as error:
        raise ValueError(
            "The selected page number must be numeric."
        ) from error

    if (
        selected_index < 0
        or selected_index >= len(raw_pages)
    ):
        raise ValueError(
            "The selected page number is outside "
            "the available range."
        )

    return raw_pages[selected_index]


def open_browser_context(
    playwright: Any,
) -> BrowserContext:
    """Open Google Chrome using the persistent Facebook profile."""
    if not FACEBOOK_PROFILE_DIRECTORY.exists():
        raise RuntimeError(
            "The persistent Facebook profile "
            "does not exist: "
            f"{FACEBOOK_PROFILE_DIRECTORY}"
        )

    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(
            FACEBOOK_PROFILE_DIRECTORY
        ),
        channel="chrome",
        headless=False,
        no_viewport=True,
        chromium_sandbox=True,
        ignore_default_args=[
            "--no-sandbox",
        ],
        args=[
            "--start-maximized",
            "--disable-gpu-shader-disk-cache",
        ],
    )


def get_active_page(
    context: BrowserContext,
) -> Page:
    """Return the first existing page or create a new page."""
    if context.pages:
        return context.pages[0]

    return context.new_page()


def wait_for_facebook_render(
    page: Page,
) -> None:
    """Wait until Facebook has rendered visible page content."""
    try:
        page.wait_for_selector(
            "body",
            state="visible",
            timeout=30_000,
        )

    except PlaywrightTimeoutError as error:
        raise RuntimeError(
            "Facebook body did not become visible."
        ) from error

    page.wait_for_timeout(
        8_000
    )


def get_container_text(
    container: Locator,
) -> str:
    """Read normalized visible text from one container."""
    try:
        text = container.evaluate(
            """
            element => (
                element.innerText
                || element.textContent
                || ""
            )
            """
        )

    except Exception:
        return ""

    return normalize_text(
        str(text or "")
    )


def find_best_post_container(
    page: Page,
    cleaned_text: str,
) -> tuple[Locator, float, str]:
    """Find the visible DOM container matching the saved post."""
    best_container: Locator | None = None
    best_score = 0.0
    best_selector = ""

    seen_text_keys: set[str] = set()

    print()
    print(
        "Searching for the target Facebook post container..."
    )

    for selector in POST_CONTAINER_SELECTORS:
        containers = page.locator(
            selector
        )

        container_count = (
            containers.count()
        )

        print(
            f"Selector: {selector}"
        )
        print(
            f"Containers found: {container_count}"
        )

        for index in range(
            container_count
        ):
            container = containers.nth(
                index
            )

            try:
                if not container.is_visible():
                    continue

            except Exception:
                continue

            candidate_text = (
                get_container_text(
                    container
                )
            )

            if len(candidate_text) < 80:
                continue

            text_key = candidate_text[:500]

            if text_key in seen_text_keys:
                continue

            seen_text_keys.add(
                text_key
            )

            overlap_score = (
                calculate_text_overlap(
                    reference_text=cleaned_text,
                    candidate_text=candidate_text,
                )
            )

            print(
                f"  Candidate {index + 1}: "
                f"length={len(candidate_text)}, "
                f"overlap={overlap_score:.3f}"
            )

            if overlap_score > best_score:
                best_container = container
                best_score = overlap_score
                best_selector = selector

    if best_container is None:
        raise RuntimeError(
            "No usable Facebook post container "
            "could be identified."
        )

    if best_score < MIN_CONTAINER_OVERLAP:
        raise RuntimeError(
            "A Facebook container was found, "
            "but its text overlap was too low: "
            f"{best_score:.3f}"
        )

    print()
    print(
        "Best post container found."
    )
    print(
        f"Selector: {best_selector}"
    )
    print(
        f"Text overlap score: {best_score:.3f}"
    )

    return (
        best_container,
        best_score,
        best_selector,
    )


def extract_image_candidates(
    container: Locator,
) -> list[dict[str, Any]]:
    """Extract image metadata from the selected post container."""
    images = container.locator(
        "img"
    )

    image_data = images.evaluate_all(
        """
        elements => elements.map((image, index) => {
            const rect =
                image.getBoundingClientRect();

            const style =
                window.getComputedStyle(image);

            const parentAnchor =
                image.closest("a");

            return {
                dom_index: index,

                src:
                    image.currentSrc
                    || image.src
                    || "",

                alt:
                    image.alt
                    || "",

                natural_width:
                    Number(
                        image.naturalWidth
                        || 0
                    ),

                natural_height:
                    Number(
                        image.naturalHeight
                        || 0
                    ),

                rendered_width:
                    Math.round(
                        rect.width
                        || 0
                    ),

                rendered_height:
                    Math.round(
                        rect.height
                        || 0
                    ),

                visible:
                    rect.width > 0
                    && rect.height > 0
                    && style.display !== "none"
                    && style.visibility !== "hidden"
                    && style.opacity !== "0",

                parent_link:
                    parentAnchor
                    ? parentAnchor.href
                    : "",

                aria_label:
                    image.getAttribute("aria-label")
                    || ""
            };
        })
        """
    )

    return [
        dict(item)
        for item in image_data
    ]


def is_http_image_url(
    url: str,
) -> bool:
    """Return True for HTTP and HTTPS image URLs."""
    if not url:
        return False

    parsed_url = urlparse(
        url
    )

    return parsed_url.scheme in {
        "http",
        "https",
    }


def classify_image_candidate(
    image: dict[str, Any],
) -> tuple[str, list[str]]:
    """Classify one Facebook image conservatively."""
    reasons: list[str] = []

    source_url = str(
        image.get("src")
        or ""
    ).strip()

    alt_text = normalize_text(
        str(
            image.get("alt")
            or ""
        )
    )

    aria_label = normalize_text(
        str(
            image.get("aria_label")
            or ""
        )
    )

    natural_width = int(
        image.get("natural_width")
        or 0
    )

    natural_height = int(
        image.get("natural_height")
        or 0
    )

    rendered_width = int(
        image.get("rendered_width")
        or 0
    )

    rendered_height = int(
        image.get("rendered_height")
        or 0
    )

    visible = bool(
        image.get("visible")
    )

    if not is_http_image_url(
        source_url
    ):
        reasons.append(
            "The image does not have a valid HTTP URL."
        )
        return "REJECTED", reasons

    if not visible:
        reasons.append(
            "The image is not visible."
        )
        return "REJECTED", reasons

    source_url_lower = (
        source_url.lower()
    )

    combined_description = (
        f"{alt_text} {aria_label}"
    ).lower()

    for ignored_url_part in (
        IGNORED_URL_PARTS
    ):
        if ignored_url_part in source_url_lower:
            reasons.append(
                "The URL resembles a Facebook "
                "interface asset: "
                f"{ignored_url_part}"
            )
            return "REJECTED", reasons

    for ignored_alt_part in (
        IGNORED_ALT_TEXT_PARTS
    ):
        if (
            ignored_alt_part
            in combined_description
        ):
            reasons.append(
                "The description resembles an avatar "
                "or interface image: "
                f"{ignored_alt_part}"
            )
            return "REJECTED", reasons

    if (
        natural_width < MIN_IMAGE_WIDTH
        or natural_height < MIN_IMAGE_HEIGHT
    ):
        reasons.append(
            "The natural dimensions are below "
            f"{MIN_IMAGE_WIDTH} x {MIN_IMAGE_HEIGHT}."
        )
        return "REJECTED", reasons

    if (
        rendered_width < MIN_RENDERED_WIDTH
        or rendered_height < MIN_RENDERED_HEIGHT
    ):
        reasons.append(
            "The rendered image is too small."
        )
        return "REJECTED", reasons

    aspect_ratio = (
        natural_width
        / natural_height
        if natural_height
        else 0
    )

    if (
        aspect_ratio > 4.0
        or aspect_ratio < 0.20
    ):
        reasons.append(
            "The image has an unusually extreme "
            "aspect ratio."
        )
        return "REVIEW_REQUIRED", reasons

    reasons.append(
        "The image is visible and has sufficient dimensions."
    )

    return "CANDIDATE", reasons


def normalize_image_identity(
    image_url: str,
) -> str:
    """Create a stable identity for duplicate URL detection."""
    if not image_url:
        return ""

    parsed_url = urlparse(
        image_url
    )

    return (
        f"{parsed_url.scheme}://"
        f"{parsed_url.netloc}"
        f"{parsed_url.path}"
    )


def remove_duplicate_images(
    images: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove duplicate image URLs while preserving order."""
    unique_images: list[
        dict[str, Any]
    ] = []

    seen_identities: set[str] = set()

    for image in images:
        source_url = str(
            image.get("src")
            or ""
        ).strip()

        identity = (
            normalize_image_identity(
                source_url
            )
        )

        if not identity:
            continue

        if identity in seen_identities:
            continue

        seen_identities.add(
            identity
        )

        unique_images.append(
            image
        )

    return unique_images


def prepare_image_candidates(
    raw_images: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Classify and deduplicate image candidates."""
    unique_images = (
        remove_duplicate_images(
            raw_images
        )
    )

    prepared_images: list[
        dict[str, Any]
    ] = []

    for image in unique_images:
        status, reasons = (
            classify_image_candidate(
                image
            )
        )

        prepared_images.append(
            {
                **image,
                "candidate_status": status,
                "classification_reasons": reasons,
            }
        )

    return prepared_images


def get_usable_images(
    images: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return images eligible for local download."""
    return [
        image
        for image in images
        if image.get(
            "candidate_status"
        )
        in {
            "CANDIDATE",
            "REVIEW_REQUIRED",
        }
    ]


def calculate_sha256(
    content: bytes,
) -> str:
    """Calculate the SHA-256 digest of downloaded content."""
    return hashlib.sha256(
        content
    ).hexdigest()


def normalize_content_type(
    raw_content_type: str | None,
) -> str:
    """Normalize an HTTP Content-Type header."""
    if not raw_content_type:
        return ""

    return (
        raw_content_type
        .split(
            ";",
            maxsplit=1,
        )[0]
        .strip()
        .lower()
    )


def determine_extension(
    content_type: str,
    image_url: str,
) -> str:
    """Determine a safe file extension for one image."""
    extension = CONTENT_TYPE_EXTENSIONS.get(
        content_type
    )

    if extension:
        return extension

    parsed_path = urlparse(
        image_url
    ).path

    suffix = Path(
        parsed_path
    ).suffix.lower()

    if suffix in {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
    }:
        return (
            ".jpg"
            if suffix == ".jpeg"
            else suffix
        )

    guessed_extension = mimetypes.guess_extension(
        content_type
    )

    return guessed_extension or ".bin"


def build_output_directory(
    raw_page_id: str,
) -> Path:
    """Create and return the output directory for one raw page."""
    output_directory = (
        IMAGE_OUTPUT_ROOT
        / BATCH_CODE
        / raw_page_id
    )

    output_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    return output_directory


def find_existing_hash_file(
    output_directory: Path,
    sha256_digest: str,
) -> Path | None:
    """Find an existing downloaded image with the same hash."""
    hash_prefix = sha256_digest[:16]

    matches = list(
        output_directory.glob(
            f"image_*_{hash_prefix}.*"
        )
    )

    for match in matches:
        if match.suffix.lower() != ".json":
            return match

    return None


def download_image_response(
    context: BrowserContext,
    image_url: str,
    referer_url: str,
) -> APIResponse:
    """Download one Facebook image using the authenticated context."""
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

    return response


def validate_download_response(
    response: APIResponse,
    content: bytes,
) -> tuple[str, list[str]]:
    """Validate HTTP response and downloaded image content."""
    warnings: list[str] = []

    if not response.ok:
        raise RuntimeError(
            "Facebook image download failed with HTTP "
            f"status {response.status}."
        )

    content_type = normalize_content_type(
        response.headers.get(
            "content-type"
        )
    )

    if content_type not in ALLOWED_CONTENT_TYPES:
        raise RuntimeError(
            "Downloaded content is not an allowed image type: "
            f"{content_type or '[missing]'}"
        )

    if not content:
        raise RuntimeError(
            "Downloaded image content is empty."
        )

    if len(content) > MAX_IMAGE_BYTES:
        raise RuntimeError(
            "Downloaded image exceeds the maximum allowed size: "
            f"{len(content)} bytes."
        )

    if len(content) < 1_000:
        warnings.append(
            "Downloaded image file is unusually small."
        )

    return content_type, warnings


def write_json_file(
    file_path: Path,
    payload: dict[str, Any],
) -> None:
    """Write UTF-8 JSON metadata to disk."""
    file_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def download_one_image(
    context: BrowserContext,
    raw_page: dict[str, Any],
    image: dict[str, Any],
    image_number: int,
    output_directory: Path,
) -> dict[str, Any]:
    """Download one image and write its metadata."""
    image_url = str(
        image.get("src")
        or ""
    ).strip()

    page_url = str(
        raw_page.get("page_url")
        or ""
    ).strip()

    print()
    print("-" * 78)
    print(
        f"Downloading image #{image_number}"
    )
    print(
        "Candidate status: "
        f"{image.get('candidate_status')}"
    )
    print(
        "Expected dimensions: "
        f"{image.get('natural_width')} x "
        f"{image.get('natural_height')}"
    )

    response = download_image_response(
        context=context,
        image_url=image_url,
        referer_url=page_url,
    )

    content = response.body()

    content_type, warnings = (
        validate_download_response(
            response=response,
            content=content,
        )
    )

    sha256_digest = calculate_sha256(
        content
    )

    existing_file = find_existing_hash_file(
        output_directory=output_directory,
        sha256_digest=sha256_digest,
    )

    downloaded_at = datetime.now(
        timezone.utc
    ).isoformat()

    if existing_file is not None:
        print(
            "Duplicate content detected."
        )
        print(
            f"Existing file: {existing_file}"
        )

        return {
            "status": "DUPLICATE",
            "image_path": str(
                existing_file
            ),
            "sha256": sha256_digest,
            "content_type": content_type,
            "file_size_bytes": len(content),
        }

    extension = determine_extension(
        content_type=content_type,
        image_url=image_url,
    )

    hash_prefix = sha256_digest[:16]

    image_file_name = (
        f"image_{image_number:03d}_"
        f"{hash_prefix}"
        f"{extension}"
    )

    metadata_file_name = (
        f"image_{image_number:03d}_"
        f"{hash_prefix}.json"
    )

    image_file_path = (
        output_directory
        / image_file_name
    )

    metadata_file_path = (
        output_directory
        / metadata_file_name
    )

    image_file_path.write_bytes(
        content
    )

    metadata = {
        "batch_code": BATCH_CODE,
        "raw_page_id": raw_page.get(
            "raw_page_id"
        ),
        "source_url_id": raw_page.get(
            "source_url_id"
        ),
        "facebook_post_url": page_url,
        "facebook_image_url": image_url,
        "parent_photo_url": image.get(
            "parent_link"
        ),
        "alt_text": image.get(
            "alt"
        ),
        "aria_label": image.get(
            "aria_label"
        ),
        "candidate_status": image.get(
            "candidate_status"
        ),
        "classification_reasons": image.get(
            "classification_reasons"
        ),
        "natural_width": image.get(
            "natural_width"
        ),
        "natural_height": image.get(
            "natural_height"
        ),
        "rendered_width": image.get(
            "rendered_width"
        ),
        "rendered_height": image.get(
            "rendered_height"
        ),
        "content_type": content_type,
        "file_extension": extension,
        "file_size_bytes": len(
            content
        ),
        "sha256": sha256_digest,
        "local_image_path": str(
            image_file_path.relative_to(
                PROJECT_ROOT
            )
        ),
        "collector_name": COLLECTOR_NAME,
        "collector_version": COLLECTOR_VERSION,
        "downloaded_at": downloaded_at,
        "download_warnings": warnings,
    }

    write_json_file(
        file_path=metadata_file_path,
        payload=metadata,
    )

    print(
        "Image downloaded successfully."
    )
    print(
        f"Content type: {content_type}"
    )
    print(
        f"File size: {len(content)} bytes"
    )
    print(
        f"SHA-256: {sha256_digest}"
    )
    print(
        f"Image file: {image_file_path}"
    )
    print(
        f"Metadata file: {metadata_file_path}"
    )

    if warnings:
        print(
            "Warnings:"
        )

        for warning in warnings:
            print(
                f"  - {warning}"
            )

    return {
        "status": "DOWNLOADED",
        "image_path": str(
            image_file_path
        ),
        "metadata_path": str(
            metadata_file_path
        ),
        "sha256": sha256_digest,
        "content_type": content_type,
        "file_size_bytes": len(content),
    }


def inspect_and_download_images(
    context: BrowserContext,
    page: Page,
    raw_page: dict[str, Any],
    output_directory: Path,
    non_interactive: bool = False,
    confirm_download: bool = False,
) -> None:
    """Detect and download usable images for one Facebook post."""
    page_url = str(
        raw_page.get("page_url")
        or ""
    ).strip()

    cleaned_text = str(
        raw_page.get("cleaned_text")
        or ""
    ).strip()

    raw_page_id = str(
        raw_page.get("raw_page_id")
        or ""
    ).strip()

    if not page_url:
        raise RuntimeError(
            "The selected raw page has no URL."
        )

    if not cleaned_text:
        raise RuntimeError(
            "The selected raw page has no cleaned text."
        )

    if not raw_page_id:
        raise RuntimeError(
            "The selected raw page has no raw_page_id."
        )

    print()
    print("=" * 78)
    print(
        "FACEBOOK IMAGE DOWNLOADER"
    )
    print("=" * 78)
    print(
        f"Collector: {COLLECTOR_NAME}"
    )
    print(
        f"Version: {COLLECTOR_VERSION}"
    )
    print(
        f"Raw page ID: {raw_page_id}"
    )
    print(
        f"URL: {page_url}"
    )

    page.goto(
        page_url,
        wait_until="domcontentloaded",
        timeout=90_000,
    )

    wait_for_facebook_render(
        page
    )

    (
        container,
        overlap_score,
        selector,
    ) = find_best_post_container(
        page=page,
        cleaned_text=cleaned_text,
    )

    raw_images = extract_image_candidates(
        container
    )

    prepared_images = prepare_image_candidates(
        raw_images
    )

    usable_images = get_usable_images(
        prepared_images
    )

    print()
    print("=" * 78)
    print(
        "IMAGE DOWNLOAD PREVIEW"
    )
    print("=" * 78)
    print(
        f"Container selector: {selector}"
    )
    print(
        f"Text overlap score: {overlap_score:.3f}"
    )
    print(
        f"All unique images: {len(prepared_images)}"
    )
    print(
        f"Usable images: {len(usable_images)}"
    )

    for index, image in enumerate(
        usable_images,
        start=1,
    ):
        print()
        print(
            f"[{index}] "
            f"{image.get('candidate_status')} | "
            f"{image.get('natural_width')} x "
            f"{image.get('natural_height')}"
        )
        print(
            "Alt text: "
            f"{image.get('alt') or '[empty]'}"
        )
        print(
            "Parent link: "
            f"{image.get('parent_link') or '[none]'}"
        )

    if not usable_images:
        print()
        print(
            "No usable post images were found."
        )
        return

    if len(usable_images) > MAX_DOWNLOAD_IMAGES:
        raise RuntimeError(
            "The number of usable images exceeds the "
            f"configured maximum of {MAX_DOWNLOAD_IMAGES}."
        )

    print()

    if confirm_download:
        print(
            "Download confirmation supplied via --confirm-download."
        )

    elif non_interactive:
        # Backstop only: main() already requires --confirm-download
        # whenever --non-interactive is used, before Playwright launches.
        print(
            "Image download cancelled: no download confirmation "
            "was supplied."
        )
        return

    else:
        confirmation = input(
            "Type DOWNLOAD to save these images locally, "
            "or press Enter to cancel: "
        ).strip().upper()

        if confirmation != "DOWNLOAD":
            print(
                "Image download cancelled."
            )
            return

    results: list[
        dict[str, Any]
    ] = []

    for image_number, image in enumerate(
        usable_images,
        start=1,
    ):
        try:
            result = download_one_image(
                context=context,
                raw_page=raw_page,
                image=image,
                image_number=image_number,
                output_directory=output_directory,
            )

            results.append(
                result
            )

        except Exception as error:
            print()
            print(
                f"Image #{image_number} download failed."
            )
            print(
                f"Error type: {type(error).__name__}"
            )
            print(
                f"Error details: {error}"
            )

            results.append(
                {
                    "status": "FAILED",
                    "image_number": image_number,
                    "error_type": type(
                        error
                    ).__name__,
                    "error_details": str(
                        error
                    ),
                }
            )

    downloaded_count = sum(
        1
        for result in results
        if result.get("status")
        == "DOWNLOADED"
    )

    duplicate_count = sum(
        1
        for result in results
        if result.get("status")
        == "DUPLICATE"
    )

    failed_count = sum(
        1
        for result in results
        if result.get("status")
        == "FAILED"
    )

    run_summary = {
        "batch_code": BATCH_CODE,
        "raw_page_id": raw_page_id,
        "facebook_post_url": page_url,
        "collector_name": COLLECTOR_NAME,
        "collector_version": COLLECTOR_VERSION,
        "container_selector": selector,
        "text_overlap_score": overlap_score,
        "usable_image_count": len(
            usable_images
        ),
        "downloaded_count": downloaded_count,
        "duplicate_count": duplicate_count,
        "failed_count": failed_count,
        "completed_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "results": results,
    }

    summary_file_path = (
        output_directory
        / "download_summary.json"
    )

    write_json_file(
        file_path=summary_file_path,
        payload=run_summary,
    )

    print()
    print("=" * 78)
    print(
        "DOWNLOAD RESULT"
    )
    print("=" * 78)
    print(
        f"Downloaded: {downloaded_count}"
    )
    print(
        f"Duplicates: {duplicate_count}"
    )
    print(
        f"Failed: {failed_count}"
    )
    print(
        f"Output directory: {output_directory}"
    )
    print(
        f"Summary file: {summary_file_path}"
    )
    print()
    print(
        "No image was uploaded to Supabase."
    )
    print(
        "No product_images record was created."
    )


def main() -> None:
    """Run local Facebook image download for one cleaned post."""
    load_dotenv()

    args = parse_arguments()

    # Safety rule: validated before any database call or Playwright
    # launch. Both flags are required together in non-interactive mode.
    if args.non_interactive:
        if not args.raw_page_id:
            raise RuntimeError(
                "--non-interactive requires --raw-page-id."
            )

        if not args.confirm_download:
            raise RuntimeError(
                "--non-interactive requires --confirm-download."
            )

    print(
        "Facebook post image downloader started."
    )
    print(
        f"Version: {COLLECTOR_VERSION}"
    )
    print(
        "Mode: "
        f"{'NON_INTERACTIVE' if args.non_interactive else 'INTERACTIVE'}"
    )

    repository = SupabaseRepository()

    batch = (
        repository.get_batch_by_code(
            BATCH_CODE
        )
    )

    if batch is None:
        raise RuntimeError(
            f"Batch was not found: {BATCH_CODE}"
        )

    selected_raw_page = select_or_resolve_raw_page(
        repository=repository,
        batch_id=batch["batch_id"],
        raw_page_id=args.raw_page_id,
        non_interactive=args.non_interactive,
    )

    output_directory = build_output_directory(
        raw_page_id=str(
            selected_raw_page["raw_page_id"]
        )
    )

    print_preflight_summary(
        raw_page=selected_raw_page,
        batch_code=BATCH_CODE,
        output_directory=output_directory,
        non_interactive=args.non_interactive,
        confirm_download=args.confirm_download,
    )

    with sync_playwright() as playwright:
        context = open_browser_context(
            playwright
        )

        try:
            page = get_active_page(
                context
            )

            inspect_and_download_images(
                context=context,
                page=page,
                raw_page=selected_raw_page,
                output_directory=output_directory,
                non_interactive=args.non_interactive,
                confirm_download=args.confirm_download,
            )

            if not args.non_interactive:
                input(
                    "Press Enter to close Chrome..."
                )

        finally:
            context.close()

    print()
    print(
        "Facebook post image downloader finished."
    )


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print()
        print(
            "Image download was cancelled."
        )
        sys.exit(130)

    except Exception as error:
        print()
        print(
            "Facebook post image download failed."
        )
        print(
            f"Error type: {type(error).__name__}"
        )
        print(
            f"Error details: {error}"
        )
        sys.exit(1)