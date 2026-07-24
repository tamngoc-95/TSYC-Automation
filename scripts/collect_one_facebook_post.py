import hashlib
import re
import sys
from pathlib import Path

from playwright.sync_api import Locator, Page, sync_playwright


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.repositories.supabase_repository import SupabaseRepository


PROFILE_DIRECTORY = (
    PROJECT_ROOT
    / "playwright"
    / "facebook-profile"
)

BATCH_CODE = "FB-2026-001"
AUTHORIZED_GROUP_ID = "2415122391976246"

COLLECTOR_NAME = "facebook_permalink_container_collector"
COLLECTOR_VERSION = "0.3.0"


def validate_facebook_post_url(url: str) -> None:
    """Validate that the URL belongs to the authorized Facebook group."""
    expected_path = f"facebook.com/groups/{AUTHORIZED_GROUP_ID}/"

    if expected_path not in url:
        raise ValueError(
            "The Facebook URL does not belong to the authorized group."
        )

    if "/permalink/" not in url:
        raise ValueError(
            "The Facebook URL is not a supported permalink URL."
        )


def extract_post_id(url: str) -> str:
    """Extract the Facebook post ID from a permalink URL."""
    match = re.search(r"/permalink/(\d+)", url)

    if not match:
        raise ValueError(
            f"Facebook post ID could not be extracted from URL: {url}"
        )

    return match.group(1)


def normalize_text(text: str) -> str:
    """Normalize whitespace while preserving useful line breaks."""
    cleaned_lines: list[str] = []

    for line in text.splitlines():
        cleaned_line = " ".join(line.split())

        if cleaned_line:
            cleaned_lines.append(cleaned_line)

    return "\n".join(cleaned_lines)


def calculate_content_hash(content: str) -> str:
    """Calculate a SHA-256 content hash."""
    return hashlib.sha256(
        content.encode("utf-8")
    ).hexdigest()


def read_locator_text(locator: Locator) -> str:
    """Read text using browser JavaScript with multiple fallbacks."""
    try:
        text = locator.evaluate(
            """
            element => {
                const innerText = element.innerText || "";
                const textContent = element.textContent || "";
                return innerText.trim() || textContent.trim();
            }
            """
        )

        return normalize_text(text or "")

    except Exception:
        return ""


def contains_full_feed_indicators(text: str) -> bool:
    """Detect content that appears to be the complete Facebook feed."""
    indicators = (
        "Menu trên Facebook",
        "Lối tắt của bạn",
        "Lời mời kết bạn",
        "Sinh nhật",
        "Người liên hệ",
        "Bài viết trên Bảng feed",
        "Tạo bài viết",
        "Tâm ơi, bạn đang nghĩ gì thế?",
    )

    found_count = sum(
        1
        for indicator in indicators
        if indicator in text
    )

    return found_count >= 3


def score_candidate_text(text: str) -> int:
    """Score one text container as a possible target Facebook post."""
    if len(text) < 80:
        return -1

    if contains_full_feed_indicators(text):
        return -1

    score = len(text)

    post_indicators = (
        "Thích",
        "Bình luận",
        "Chia sẻ",
        "Gefällt mir",
        "Kommentieren",
        "Teilen",
    )

    if any(indicator in text for indicator in post_indicators):
        score += 500

    book_indicators = (
        "sách",
        "Sách",
        "combo",
        "Combo",
        "cuốn",
        "bộ sách",
        "giá",
        "Giá",
    )

    if any(indicator in text for indicator in book_indicators):
        score += 1000

    if len(text) > 15_000:
        score -= 3000

    return score


def expand_visible_text(page: Page) -> None:
    """Click visible See more controls when possible."""
    labels = (
        "Xem thêm",
        "See more",
        "Mehr anzeigen",
    )

    for label in labels:
        controls = page.get_by_text(
            label,
            exact=True,
        )

        for index in range(min(controls.count(), 5)):
            try:
                controls.nth(index).click(
                    timeout=3_000,
                )

                print(f"Expanded content using: {label}")

            except Exception:
                continue


def wait_for_facebook_content(page: Page) -> None:
    """Wait for Facebook to render meaningful visible content."""
    for attempt in range(1, 7):
        page.wait_for_timeout(5_000)

        body_text = read_locator_text(
            page.locator("body")
        )

        print(
            f"Render check {attempt}: "
            f"body text length = {len(body_text)}"
        )

        if len(body_text) >= 200:
            return

    raise RuntimeError(
        "Facebook page did not render meaningful text."
    )


def collect_candidate_containers(
    page: Page,
    post_id: str,
) -> list[tuple[str, Locator, str, int]]:
    """Collect and score possible Facebook post containers."""
    candidates: list[tuple[str, Locator, str, int]] = []
    seen_texts: set[str] = set()

    selectors = (
        ('dialog article', 'div[role="dialog"] div[role="article"]'),
        ('visible dialog', 'div[role="dialog"]:visible'),
        ('main article', 'div[role="main"] div[role="article"]'),
        ('visible article', 'div[role="article"]:visible'),
        ('main area', 'div[role="main"]:visible'),
    )

    for selector_name, selector in selectors:
        locators = page.locator(selector)
        locator_count = locators.count()

        print(
            f"{selector_name}: "
            f"{locator_count} container(s) found"
        )

        for index in range(min(locator_count, 10)):
            locator = locators.nth(index)

            text = read_locator_text(locator)

            if not text or text in seen_texts:
                continue

            seen_texts.add(text)

            score = score_candidate_text(text)

            try:
                permalink_count = locator.locator(
                    f'a[href*="{post_id}"]'
                ).count()

                if permalink_count > 0:
                    score += 10_000

            except Exception:
                pass

            candidates.append(
                (
                    f"{selector_name} #{index}",
                    locator,
                    text,
                    score,
                )
            )

    return candidates


def select_best_candidate(
    page: Page,
    post_id: str,
) -> tuple[str, str]:
    """Select the most likely Facebook post container."""
    candidates = collect_candidate_containers(
        page=page,
        post_id=post_id,
    )

    if not candidates:
        raise RuntimeError(
            "No readable Facebook content containers were found."
        )

    candidates.sort(
        key=lambda item: item[3],
        reverse=True,
    )

    print()
    print("Facebook container candidates:")
    print("=" * 70)

    for name, _, text, score in candidates[:8]:
        preview = text[:350].replace(
            "\n",
            " | ",
        )

        print()
        print(f"Container: {name}")
        print(f"Length: {len(text)}")
        print(f"Score: {score}")
        print(f"Preview: {preview}")

    print("=" * 70)

    valid_candidates = [
        candidate
        for candidate in candidates
        if candidate[3] >= 0
    ]

    if not valid_candidates:
        raise RuntimeError(
            "No suitable Facebook post container could be identified."
        )

    best_name, _, best_text, best_score = valid_candidates[0]

    print()
    print(f"Selected container: {best_name}")
    print(f"Selected score: {best_score}")

    return best_name, best_text


def main() -> None:
    """Collect one pending Facebook post and save it to Supabase."""
    print("Collector script started.")

    if not PROFILE_DIRECTORY.exists():
        print("Facebook browser profile was not found.")
        print(f"Expected path: {PROFILE_DIRECTORY}")
        sys.exit(1)

    repository = SupabaseRepository()

    batch = repository.get_batch_by_code(
        BATCH_CODE
    )

    if batch is None:
        print(f"Batch was not found: {BATCH_CODE}")
        sys.exit(1)

    pending_sources = repository.get_pending_source_urls(
        batch_id=batch["batch_id"],
        source_type="FACEBOOK_POST",
    )

    print(
        f"Pending Facebook URLs found: "
        f"{len(pending_sources)}"
    )

    if not pending_sources:
        print("No pending Facebook source URLs were found.")
        return

    source = pending_sources[0]

    source_url_id = source["source_url_id"]
    source_url = source["source_url"]
    source_name = source.get("source_name")

    print()
    print("Selected Facebook source:")
    print(f"  Source URL ID: {source_url_id}")
    print(f"  URL: {source_url}")
    print(f"  Reason: {source_name}")

    try:
        validate_facebook_post_url(source_url)

        post_id = extract_post_id(source_url)

        print(f"Target Facebook post ID: {post_id}")

        repository.update_source_url_status(
            source_url_id=source_url_id,
            crawl_status="IN_PROGRESS",
            last_error=None,
        )

        print("Source status updated to IN_PROGRESS.")

        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIRECTORY),
                channel="chrome",
                headless=False,
                no_viewport=True,
                locale="de-DE",
            )

            page = (
                context.pages[0]
                if context.pages
                else context.new_page()
            )

            print("Opening the authorized Facebook post...")

            page.goto(
                source_url,
                wait_until="domcontentloaded",
                timeout=120_000,
            )

            print(f"Current URL: {page.url}")

            final_url = page.url.lower()

            blocked_indicators = (
                "login",
                "checkpoint",
                "recover",
            )

            if any(
                indicator in final_url
                for indicator in blocked_indicators
            ):
                raise RuntimeError(
                    "Facebook redirected to login or verification."
                )

            wait_for_facebook_content(page)

            expand_visible_text(page)

            page.wait_for_timeout(3_000)

            container_name, raw_text = select_best_candidate(
                page=page,
                post_id=post_id,
            )

            if len(raw_text) < 80:
                raise RuntimeError(
                    "Selected Facebook post text is too short."
                )

            if contains_full_feed_indicators(raw_text):
                raise RuntimeError(
                    "Selected text appears to contain the full Facebook feed."
                )

            print()
            print("Selected Facebook text preview:")
            print("-" * 70)
            print(raw_text[:2500])
            print("-" * 70)
            print()

            confirmation = input(
                "Type SAVE if this is the correct post, "
                "or press Enter to cancel: "
            ).strip()

            if confirmation.upper() != "SAVE":
                repository.update_source_url_status(
                    source_url_id=source_url_id,
                    crawl_status="PENDING",
                    last_error=(
                        "Collection cancelled during manual preview."
                    ),
                )

                print(
                    "Collection cancelled. "
                    "Status restored to PENDING."
                )

                context.close()
                return

            raw_title = page.title()
            content_hash = calculate_content_hash(
                raw_text
            )

            raw_page = repository.save_raw_page(
                batch_id=batch["batch_id"],
                source_url_id=source_url_id,
                page_type="FACEBOOK_POST",
                page_url=source_url,
                raw_title=raw_title,
                raw_text=raw_text,
                content_hash=content_hash,
                collector_name=COLLECTOR_NAME,
                collector_version=COLLECTOR_VERSION,
            )

            final_source = repository.update_source_url_status(
                source_url_id=source_url_id,
                crawl_status="COLLECTED",
                last_error=None,
            )

            repository.write_process_log(
                batch_id=batch["batch_id"],
                process_name="COLLECT_FACEBOOK_POST",
                process_step="SAVE_SELECTED_CONTAINER",
                log_level="INFO",
                status="SUCCESS",
                message=(
                    "Authorized Facebook post container "
                    "collected successfully."
                ),
                error_details={
                    "source_url_id": source_url_id,
                    "raw_page_id": raw_page["raw_page_id"],
                    "post_id": post_id,
                    "container_name": container_name,
                    "content_length": len(raw_text),
                    "content_hash": content_hash,
                    "collector_version": COLLECTOR_VERSION,
                },
            )

            print()
            print("Facebook post collected successfully.")
            print(
                f"Final status: "
                f"{final_source.get('crawl_status')}"
            )
            print(
                f"Raw page ID: "
                f"{raw_page['raw_page_id']}"
            )
            print(
                f"Content length: "
                f"{len(raw_text)}"
            )

            context.close()

    except Exception as error:
        error_message = str(error)

        try:
            repository.update_source_url_status(
                source_url_id=source_url_id,
                crawl_status="FAILED",
                last_error=error_message[:2000],
            )

            repository.write_process_log(
                batch_id=batch["batch_id"],
                process_name="COLLECT_FACEBOOK_POST",
                process_step="SAVE_SELECTED_CONTAINER",
                log_level="ERROR",
                status="FAILED",
                message="Facebook post collection failed.",
                error_code=type(error).__name__,
                error_details={
                    "source_url_id": source_url_id,
                    "source_url": source_url,
                    "error": error_message,
                    "collector_version": COLLECTOR_VERSION,
                },
            )

        except Exception as logging_error:
            print()
            print(
                "The collector also failed "
                "to write its error log."
            )
            print(f"Logging error: {logging_error}")

        print()
        print("Facebook post collection failed.")
        print(f"Error type: {type(error).__name__}")
        print(f"Error details: {error_message}")
        sys.exit(1)


if __name__ == "__main__":
    main()