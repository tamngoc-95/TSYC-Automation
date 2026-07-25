import re
import shutil
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import (
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.repositories.supabase_repository import SupabaseRepository


BATCH_CODE = "FB-2026-001"
COLLECTOR_NAME = "facebook_permalink_image_detector"
COLLECTOR_VERSION = "0.4.1"

FACEBOOK_PROFILE_DIRECTORY = (
    PROJECT_ROOT
    / "playwright"
    / "facebook-profile"
)

MIN_IMAGE_WIDTH = 250
MIN_IMAGE_HEIGHT = 250
MAX_PREVIEW_IMAGES = 20

POST_CONTAINER_SELECTORS = (
    "div[role='dialog'] div[role='article']",
    "div[role='main'] div[role='article']",
    "div[role='article']",
    "div[role='dialog']",
    "div[role='main']",
)

PROFILE_CACHE_DIRECTORIES = (
    "GPUPersistentCache",
    "ShaderCache",
    "GrShaderCache",
    "DawnCache",
    "GraphiteDawnCache",
)

IGNORED_ALT_TEXT_PARTS = (
    "profile picture",
    "ảnh đại diện",
    "avatar",
    "emoji",
    "facebook",
    "logo",
    "icon",
    "biểu tượng",
    "sticker",
)

IGNORED_URL_PARTS = (
    "emoji.php",
    "rsrc.php",
    "static.xx.fbcdn.net",
    "/safe_image.php",
    "profile",
)


def normalize_text(value: str | None) -> str:
    """Normalize text for stable content comparison."""
    if not value:
        return ""

    return " ".join(value.split()).strip()


def tokenize_text(value: str) -> set[str]:
    """Create normalized text tokens used for container scoring."""
    normalized = normalize_text(value).lower()

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
    """Calculate token overlap between cleaned text and a DOM container."""
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

    return len(matching_tokens) / len(
        reference_tokens
    )


def remove_directory_safely(
    directory: Path,
) -> None:
    """Remove one cache directory without failing the full process."""
    if not directory.exists():
        return

    try:
        shutil.rmtree(directory)
        print(
            f"Removed browser cache directory: {directory.name}"
        )
    except PermissionError:
        print(
            "Warning: browser cache directory is still locked: "
            f"{directory}"
        )
    except OSError as error:
        print(
            "Warning: browser cache directory could not be removed: "
            f"{directory}"
        )
        print(
            f"Details: {error}"
        )


def clean_browser_profile_cache() -> None:
    """Remove disposable browser cache while preserving login data."""
    if not FACEBOOK_PROFILE_DIRECTORY.exists():
        raise RuntimeError(
            "The Facebook Playwright profile directory "
            "does not exist: "
            f"{FACEBOOK_PROFILE_DIRECTORY}"
        )

    print()
    print("Cleaning disposable browser profile cache...")

    for directory_name in PROFILE_CACHE_DIRECTORIES:
        remove_directory_safely(
            FACEBOOK_PROFILE_DIRECTORY
            / directory_name
        )

    for temporary_directory in (
        FACEBOOK_PROFILE_DIRECTORY.glob(
            "*.CHROME_DELETE"
        )
    ):
        remove_directory_safely(
            temporary_directory
        )

    print(
        "Browser profile cache cleanup finished."
    )


def get_collected_raw_pages(
    repository: SupabaseRepository,
    batch_id: str,
) -> list[dict[str, Any]]:
    """Return cleaned Facebook pages available for image detection."""
    response = (
        repository.client.table("raw_pages")
        .select(
            "raw_page_id, source_url_id, page_url, "
            "raw_title, raw_text, cleaned_text, "
            "cleaning_status, collected_at"
        )
        .eq("batch_id", batch_id)
        .eq("page_type", "FACEBOOK_POST")
        .eq("cleaning_status", "CLEANED")
        .order(
            "collected_at",
            desc=True,
        )
        .execute()
    )

    return response.data or []


def select_raw_page(
    raw_pages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Allow the user to select one collected Facebook post."""
    if not raw_pages:
        raise RuntimeError(
            "No cleaned Facebook raw pages were found."
        )

    print()
    print("Collected Facebook pages:")

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
        "Enter the page number to inspect "
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


def wait_for_facebook_render(
    page: Page,
) -> None:
    """Wait until the Facebook page has rendered usable content."""
    page.wait_for_load_state(
        "domcontentloaded"
    )

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

    page.wait_for_timeout(7_000)


def get_container_text(
    container: Locator,
) -> str:
    """Read normalized text from one candidate container."""
    try:
        text = container.evaluate(
            """
            element => {
                return (
                    element.innerText
                    || element.textContent
                    || ""
                );
            }
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
    """Find the visible DOM container matching the collected post text."""
    best_container: Locator | None = None
    best_score = 0.0
    best_selector = ""

    seen_texts: set[str] = set()

    print()
    print(
        "Searching for the target post container..."
    )

    for selector in POST_CONTAINER_SELECTORS:
        containers = page.locator(
            selector
        )

        container_count = containers.count()

        print(
            f"Selector: {selector} "
            f"-> containers found: {container_count}"
        )

        for index in range(container_count):
            container = containers.nth(index)

            try:
                if not container.is_visible():
                    continue
            except Exception:
                continue

            candidate_text = get_container_text(
                container
            )

            if len(candidate_text) < 80:
                continue

            text_key = candidate_text[:500]

            if text_key in seen_texts:
                continue

            seen_texts.add(text_key)

            overlap_score = calculate_text_overlap(
                reference_text=cleaned_text,
                candidate_text=candidate_text,
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

    if best_score < 0.30:
        raise RuntimeError(
            "A container was found, but its text overlap "
            f"was too low: {best_score:.3f}"
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
    """Extract image attributes from the selected post container."""
    images = container.locator("img")

    image_data = images.evaluate_all(
        """
        elements => elements.map((image, index) => {
            const rect = image.getBoundingClientRect();
            const computedStyle =
                window.getComputedStyle(image);

            return {
                dom_index: index,
                src:
                    image.currentSrc
                    || image.src
                    || "",
                alt:
                    image.alt
                    || "",
                width_attribute:
                    image.getAttribute("width"),
                height_attribute:
                    image.getAttribute("height"),
                natural_width:
                    image.naturalWidth
                    || 0,
                natural_height:
                    image.naturalHeight
                    || 0,
                rendered_width:
                    Math.round(rect.width || 0),
                rendered_height:
                    Math.round(rect.height || 0),
                visible:
                    rect.width > 0
                    && rect.height > 0
                    && computedStyle.visibility
                        !== "hidden"
                    && computedStyle.display
                        !== "none",
                loading:
                    image.loading
                    || "",
                parent_link:
                    image.closest("a")?.href
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
    """Return True for usable HTTP or HTTPS image URLs."""
    if not url:
        return False

    parsed = urlparse(url)

    return parsed.scheme in {
        "http",
        "https",
    }


def classify_image_candidate(
    image: dict[str, Any],
) -> tuple[str, list[str]]:
    """Classify one image candidate using conservative rules."""
    reasons: list[str] = []

    src = str(
        image.get("src") or ""
    ).strip()

    alt = normalize_text(
        str(image.get("alt") or "")
    )

    natural_width = int(
        image.get("natural_width") or 0
    )
    natural_height = int(
        image.get("natural_height") or 0
    )

    rendered_width = int(
        image.get("rendered_width") or 0
    )
    rendered_height = int(
        image.get("rendered_height") or 0
    )

    visible = bool(
        image.get("visible")
    )

    if not is_http_image_url(src):
        reasons.append(
            "The image does not have an HTTP URL."
        )
        return "REJECTED", reasons

    if not visible:
        reasons.append(
            "The image is not visible."
        )
        return "REJECTED", reasons

    src_lower = src.lower()
    alt_lower = alt.lower()

    for ignored_part in IGNORED_URL_PARTS:
        if ignored_part in src_lower:
            reasons.append(
                "The image URL resembles a Facebook "
                f"interface asset: {ignored_part}"
            )
            return "REJECTED", reasons

    for ignored_part in IGNORED_ALT_TEXT_PARTS:
        if ignored_part in alt_lower:
            reasons.append(
                "The image alternative text resembles "
                "an interface or avatar image: "
                f"{ignored_part}"
            )
            return "REJECTED", reasons

    if (
        natural_width < MIN_IMAGE_WIDTH
        or natural_height < MIN_IMAGE_HEIGHT
    ):
        reasons.append(
            "The natural image dimensions are below "
            f"{MIN_IMAGE_WIDTH} x {MIN_IMAGE_HEIGHT}."
        )
        return "REJECTED", reasons

    if (
        rendered_width < 100
        or rendered_height < 100
    ):
        reasons.append(
            "The rendered image is too small."
        )
        return "REJECTED", reasons

    aspect_ratio = (
        natural_width / natural_height
        if natural_height
        else 0
    )

    if (
        aspect_ratio > 4.0
        or aspect_ratio < 0.20
    ):
        reasons.append(
            "The image aspect ratio is unusually extreme."
        )
        return "REVIEW_REQUIRED", reasons

    reasons.append(
        "The image is visible and has sufficient dimensions."
    )

    return "CANDIDATE", reasons


def remove_duplicate_images(
    images: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove duplicate image URLs while preserving order."""
    unique_images: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for image in images:
        src = str(
            image.get("src") or ""
        ).strip()

        if (
            not src
            or src in seen_urls
        ):
            continue

        seen_urls.add(src)
        unique_images.append(
            image
        )

    return unique_images


def score_image_candidate(
    image: dict[str, Any],
) -> int:
    """Calculate a simple ranking score for preview order."""
    natural_width = int(
        image.get("natural_width") or 0
    )
    natural_height = int(
        image.get("natural_height") or 0
    )

    rendered_width = int(
        image.get("rendered_width") or 0
    )
    rendered_height = int(
        image.get("rendered_height") or 0
    )

    status = str(
        image.get("candidate_status") or ""
    )

    status_score = {
        "CANDIDATE": 1_000_000,
        "REVIEW_REQUIRED": 500_000,
        "REJECTED": 0,
    }.get(
        status,
        0,
    )

    natural_area = (
        natural_width
        * natural_height
    )

    rendered_area = (
        rendered_width
        * rendered_height
    )

    return (
        status_score
        + natural_area
        + rendered_area
    )


def prepare_image_candidates(
    raw_images: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Classify, deduplicate, and rank extracted images."""
    unique_images = remove_duplicate_images(
        raw_images
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

        prepared_image = {
            **image,
            "candidate_status": status,
            "classification_reasons": reasons,
        }

        prepared_image["score"] = (
            score_image_candidate(
                prepared_image
            )
        )

        prepared_images.append(
            prepared_image
        )

    prepared_images.sort(
        key=lambda item: int(
            item.get("score") or 0
        ),
        reverse=True,
    )

    return prepared_images


def print_image_candidates(
    images: list[dict[str, Any]],
) -> None:
    """Print detailed image candidate information."""
    if not images:
        print()
        print(
            "No images were found inside "
            "the selected post container."
        )
        return

    candidate_count = sum(
        1
        for image in images
        if image.get(
            "candidate_status"
        ) == "CANDIDATE"
    )

    review_count = sum(
        1
        for image in images
        if image.get(
            "candidate_status"
        ) == "REVIEW_REQUIRED"
    )

    rejected_count = sum(
        1
        for image in images
        if image.get(
            "candidate_status"
        ) == "REJECTED"
    )

    print()
    print("=" * 78)
    print(
        "IMAGE DETECTION SUMMARY"
    )
    print("=" * 78)
    print(
        f"Unique images found: {len(images)}"
    )
    print(
        f"Candidate images: {candidate_count}"
    )
    print(
        f"Review required: {review_count}"
    )
    print(
        f"Rejected images: {rejected_count}"
    )

    for index, image in enumerate(
        images[:MAX_PREVIEW_IMAGES],
        start=1,
    ):
        print()
        print("-" * 78)
        print(
            f"Image #{index}"
        )
        print(
            "Status: "
            f"{image.get('candidate_status')}"
        )
        print(
            "Natural size: "
            f"{image.get('natural_width')} x "
            f"{image.get('natural_height')}"
        )
        print(
            "Rendered size: "
            f"{image.get('rendered_width')} x "
            f"{image.get('rendered_height')}"
        )
        print(
            "Alt text: "
            f"{image.get('alt') or '[empty]'}"
        )
        print(
            "Parent link: "
            f"{image.get('parent_link') or '[none]'}"
        )
        print(
            "Image URL:"
        )
        print(
            image.get("src")
        )

        reasons = (
            image.get(
                "classification_reasons"
            )
            or []
        )

        print(
            "Reasons:"
        )

        for reason in reasons:
            print(
                f"  - {reason}"
            )


def add_visual_labels(
    container: Locator,
    images: list[dict[str, Any]],
) -> None:
    """Add temporary labels around image candidates in the browser."""
    status_by_url = {
        str(image.get("src")): {
            "status": image.get(
                "candidate_status"
            ),
            "index": index,
        }
        for index, image in enumerate(
            images,
            start=1,
        )
    }

    container.locator(
        "img"
    ).evaluate_all(
        """
        (elements, statusByUrl) => {
            elements.forEach(image => {
                const source =
                    image.currentSrc
                    || image.src
                    || "";

                const metadata =
                    statusByUrl[source];

                if (!metadata) {
                    return;
                }

                image.style.outline =
                    metadata.status === "CANDIDATE"
                    ? "5px solid green"
                    : metadata.status
                        === "REVIEW_REQUIRED"
                    ? "5px solid orange"
                    : "3px solid red";

                const parent =
                    image.parentElement;

                if (!parent) {
                    return;
                }

                const existingLabel =
                    parent.querySelector(
                        ":scope > .tsyc-image-label"
                    );

                if (existingLabel) {
                    existingLabel.remove();
                }

                const label =
                    document.createElement(
                        "div"
                    );

                label.className =
                    "tsyc-image-label";

                label.textContent =
                    `TSYC #${metadata.index} `
                    + metadata.status;

                label.style.background =
                    "black";
                label.style.color =
                    "white";
                label.style.fontSize =
                    "14px";
                label.style.fontWeight =
                    "bold";
                label.style.padding =
                    "4px 8px";
                label.style.margin =
                    "4px 0";
                label.style.zIndex =
                    "999999";

                parent.insertBefore(
                    label,
                    image
                );
            });
        }
        """,
        status_by_url,
    )

    print()
    print(
        "Temporary image labels were added "
        "to the browser page:"
    )
    print(
        "  Green  = CANDIDATE"
    )
    print(
        "  Orange = REVIEW_REQUIRED"
    )
    print(
        "  Red    = REJECTED"
    )


def open_browser_context(
    playwright: Any,
) -> BrowserContext:
    """Open Google Chrome with the persistent Facebook login profile."""
    if not FACEBOOK_PROFILE_DIRECTORY.exists():
        raise RuntimeError(
            "The Facebook Playwright profile "
            "directory does not exist: "
            f"{FACEBOOK_PROFILE_DIRECTORY}"
        )

    return (
        playwright.chromium
        .launch_persistent_context(
            user_data_dir=str(
                FACEBOOK_PROFILE_DIRECTORY
            ),
            channel="chrome",
            headless=False,
            no_viewport=True,
            args=[
                "--start-maximized",
                "--disable-gpu-shader-disk-cache",
                "--disable-features=OptimizationHints",
                "--disable-background-networking",
            ],
        )
    )


def inspect_facebook_images(
    page: Page,
    raw_page: dict[str, Any],
) -> None:
    """Open one permalink and inspect images in its post container."""
    page_url = str(
        raw_page.get("page_url") or ""
    ).strip()

    cleaned_text = str(
        raw_page.get("cleaned_text") or ""
    ).strip()

    if not page_url:
        raise RuntimeError(
            "The selected raw page has no page URL."
        )

    if not cleaned_text:
        raise RuntimeError(
            "The selected raw page has no cleaned text."
        )

    print()
    print("=" * 78)
    print(
        "FACEBOOK IMAGE DETECTOR"
    )
    print("=" * 78)
    print(
        f"Collector: {COLLECTOR_NAME}"
    )
    print(
        f"Version: {COLLECTOR_VERSION}"
    )
    print(
        "Raw page ID: "
        f"{raw_page.get('raw_page_id')}"
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

    container, overlap_score, selector = (
        find_best_post_container(
            page=page,
            cleaned_text=cleaned_text,
        )
    )

    raw_images = extract_image_candidates(
        container
    )

    prepared_images = prepare_image_candidates(
        raw_images
    )

    print_image_candidates(
        prepared_images
    )

    add_visual_labels(
        container=container,
        images=prepared_images,
    )

    candidate_images = [
        image
        for image in prepared_images
        if image.get(
            "candidate_status"
        )
        in {
            "CANDIDATE",
            "REVIEW_REQUIRED",
        }
    ]

    print()
    print("=" * 78)
    print(
        "DETECTION RESULT"
    )
    print("=" * 78)
    print(
        f"Container selector: {selector}"
    )
    print(
        "Text overlap score: "
        f"{overlap_score:.3f}"
    )
    print(
        "Usable image candidates: "
        f"{len(candidate_images)}"
    )
    print()
    print(
        "No images have been downloaded, "
        "uploaded, or saved to Supabase."
    )
    print(
        "Review the browser and CMD output."
    )

    input(
        "Press Enter after you finish reviewing "
        "the highlighted images..."
    )


def main() -> None:
    """Run Facebook post image detection for one cleaned page."""
    load_dotenv()

    print(
        "Facebook post image detector started."
    )

    clean_browser_profile_cache()

    repository = SupabaseRepository()

    batch = repository.get_batch_by_code(
        BATCH_CODE
    )

    if batch is None:
        raise RuntimeError(
            f"Batch was not found: {BATCH_CODE}"
        )

    raw_pages = get_collected_raw_pages(
        repository=repository,
        batch_id=batch["batch_id"],
    )

    print(
        "Cleaned Facebook pages found: "
        f"{len(raw_pages)}"
    )

    selected_raw_page = select_raw_page(
        raw_pages
    )

    with sync_playwright() as playwright:
        context = open_browser_context(
            playwright
        )

        try:
            pages = context.pages

            page = (
                pages[0]
                if pages
                else context.new_page()
            )

            inspect_facebook_images(
                page=page,
                raw_page=selected_raw_page,
            )

        finally:
            context.close()

    print()
    print(
        "Facebook post image detector finished."
    )


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print()
        print(
            "Image detection was cancelled "
            "by the user."
        )
        sys.exit(130)

    except Exception as error:
        print()
        print(
            "Facebook post image detection failed."
        )
        print(
            f"Error type: {type(error).__name__}"
        )
        print(
            f"Error details: {error}"
        )
        sys.exit(1)
