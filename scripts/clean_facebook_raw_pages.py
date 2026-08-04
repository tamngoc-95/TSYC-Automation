import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.repositories.supabase_repository import SupabaseRepository


BATCH_CODE = "FB-2026-001"
CLEANING_METHOD = "facebook_rule_based_cleaner_v0.2.0"


EXACT_LINES_TO_REMOVE = {
    "Facebook",
    "Yêu thích",
    "Quản trị viên",
    "Người đóng góp nhiều nhất",
    "Chưa có bình luận nào",
    "Hãy là người đầu tiên bình luận.",
    "Thích",
    "Bình luận",
    "Chia sẻ",
    "Gefällt mir",
    "Kommentieren",
    "Teilen",
    "Bài viết của Yêu Con",
    "Tiệm sách Yêu Con ở Đức",
    "Yêu Con",
}


PREFIXES_TO_REMOVE = (
    "Bình luận dưới tên ",
    "Bạn đang bình luận dưới tên ",
    "Được tài trợ",
    "Nhà quảng cáo",
)


FEED_INDICATORS = (
    "Menu trên Facebook",
    "Lối tắt của bạn",
    "Lời mời kết bạn",
    "Sinh nhật",
    "Người liên hệ",
    "Bài viết trên Bảng feed",
    "Tạo bài viết",
)


POST_CONTENT_MARKERS = (
    "Sách có sẵn",
    "Sách có sẵn ở Đức",
)


CONTENT_END_MARKERS = (
    "Chưa có bình luận nào",
    "Hãy là người đầu tiên bình luận.",
    "Bình luận dưới tên ",
    "Bạn đang bình luận dưới tên ",
)


def remove_invisible_unicode_characters(
    value: str,
) -> str:
    """Remove invisible Unicode formatting and control characters."""
    cleaned_characters: list[str] = []

    for character in value:
        category = unicodedata.category(character)

        if category in {
            "Cf",
            "Cc",
        }:
            if character in {
                "\n",
                "\r",
                "\t",
            }:
                cleaned_characters.append(character)

            continue

        cleaned_characters.append(character)

    return "".join(cleaned_characters)


def normalize_unicode_text(
    value: str,
) -> str:
    """Normalize Unicode and remove invisible characters."""
    normalized_value = unicodedata.normalize(
        "NFKC",
        value,
    )

    return remove_invisible_unicode_characters(
        normalized_value
    )


def normalize_line(
    line: str,
) -> str:
    """Normalize Unicode and whitespace in one line."""
    line = normalize_unicode_text(line)

    return " ".join(
        line.split()
    ).strip()


def is_single_character_noise(
    line: str,
) -> bool:
    """Return True for isolated one-character Facebook noise."""
    clean_line = normalize_line(line)

    if len(clean_line) != 1:
        return False

    return bool(
        re.fullmatch(
            r"[A-Za-zÀ-ỹ0-9]",
            clean_line,
        )
    )


def is_short_symbol_noise(
    line: str,
) -> bool:
    """Return True for isolated punctuation and symbol noise."""
    clean_line = normalize_line(line)

    if not clean_line:
        return True

    if len(clean_line) > 2:
        return False

    return bool(
        re.fullmatch(
            r"[·•|._\-–—]+",
            clean_line,
        )
    )


def is_noise_line(
    line: str,
) -> bool:
    """Return True when a line is considered Facebook interface noise."""
    clean_line = normalize_line(line)

    if not clean_line:
        return True

    if clean_line in EXACT_LINES_TO_REMOVE:
        return True

    if any(
        clean_line.startswith(prefix)
        for prefix in PREFIXES_TO_REMOVE
    ):
        return True

    if is_single_character_noise(clean_line):
        return True

    if is_short_symbol_noise(clean_line):
        return True

    return False


def remove_consecutive_duplicates(
    lines: list[str],
) -> list[str]:
    """Remove consecutive duplicate lines."""
    result: list[str] = []
    previous_line: str | None = None

    for line in lines:
        if line == previous_line:
            continue

        result.append(line)
        previous_line = line

    return result


def find_content_start_index(
    lines: list[str],
) -> int:
    """Find the start of the actual Facebook post content."""
    for index, line in enumerate(lines):
        if line in POST_CONTENT_MARKERS:
            return index

    return 0


def find_content_end_index(
    lines: list[str],
    start_index: int,
) -> int:
    """Find where comments or Facebook interface content begins."""
    for index in range(
        start_index,
        len(lines),
    ):
        line = lines[index]

        if any(
            line == marker
            or line.startswith(marker)
            for marker in CONTENT_END_MARKERS
        ):
            return index

    return len(lines)


def trim_to_post_content(
    lines: list[str],
) -> list[str]:
    """Trim Facebook header and comment areas around the post."""
    if not lines:
        return []

    start_index = find_content_start_index(
        lines
    )

    end_index = find_content_end_index(
        lines=lines,
        start_index=start_index,
    )

    return lines[
        start_index:end_index
    ]


def count_single_character_lines(
    text: str,
) -> int:
    """Count isolated one-character lines remaining in text."""
    count = 0

    for raw_line in text.splitlines():
        line = normalize_line(raw_line)

        if len(line) == 1:
            count += 1

    return count


def clean_facebook_text(
    raw_text: str,
) -> str:
    """Clean Facebook interface noise while preserving post content."""
    normalized_text = normalize_unicode_text(
        raw_text
    )

    normalized_lines = [
        normalize_line(line)
        for line in normalized_text.splitlines()
    ]

    trimmed_lines = trim_to_post_content(
        normalized_lines
    )

    filtered_lines = [
        line
        for line in trimmed_lines
        if not is_noise_line(line)
    ]

    filtered_lines = remove_consecutive_duplicates(
        filtered_lines
    )

    cleaned_text = "\n".join(
        filtered_lines
    ).strip()

    return cleaned_text


def validate_cleaned_text(
    raw_text: str,
    cleaned_text: str,
) -> tuple[str, list[str]]:
    """Validate cleaned content and return status with warnings."""
    warnings: list[str] = []

    if not cleaned_text:
        return "FAILED", [
            "Cleaned text is empty."
        ]

    if len(cleaned_text) < 80:
        warnings.append(
            "Cleaned text is unexpectedly short."
        )

    if len(cleaned_text) > len(raw_text):
        warnings.append(
            "Cleaned text is longer than raw text."
        )

    remaining_feed_indicators = [
        indicator
        for indicator in FEED_INDICATORS
        if indicator in cleaned_text
    ]

    if remaining_feed_indicators:
        warnings.append(
            "Cleaned text still contains Facebook feed indicators: "
            + ", ".join(
                remaining_feed_indicators
            )
        )

    remaining_single_character_lines = (
        count_single_character_lines(
            cleaned_text
        )
    )

    if remaining_single_character_lines > 2:
        warnings.append(
            "Cleaned text still contains too many "
            "isolated one-character lines: "
            f"{remaining_single_character_lines}"
        )

    reduction_ratio = (
        1 - len(cleaned_text) / len(raw_text)
        if raw_text
        else 0
    )

    if reduction_ratio > 0.90:
        warnings.append(
            "More than 90% of the raw text was removed."
        )

    paragraph_count = len(
        [
            paragraph
            for paragraph in cleaned_text.splitlines()
            if len(paragraph.strip()) >= 30
        ]
    )

    if paragraph_count == 0:
        warnings.append(
            "No substantial text paragraph was found."
        )

    if warnings:
        return "REVIEW_REQUIRED", warnings

    return "CLEANED", warnings


def get_pending_raw_pages(
    repository: SupabaseRepository,
    batch_id: str,
) -> list[dict[str, Any]]:
    """Return Facebook raw pages waiting for cleaning."""
    response = (
        repository.client
        .table("raw_pages")
        .select(
            "raw_page_id, source_url_id, page_url, raw_title, "
            "raw_text, collector_name, collector_version, "
            "cleaning_status"
        )
        .eq(
            "batch_id",
            batch_id,
        )
        .eq(
            "page_type",
            "FACEBOOK_POST",
        )
        .in_(
            "cleaning_status",
            [
                "PENDING",
                "REVIEW_REQUIRED",
            ],
        )
        .order(
            "collected_at"
        )
        .execute()
    )

    return response.data or []


def update_cleaned_page(
    repository: SupabaseRepository,
    raw_page_id: str,
    cleaned_text: str | None,
    cleaning_status: str,
) -> dict[str, Any]:
    """Update one raw page with cleaned content."""
    payload = {
        "cleaned_text": cleaned_text,
        "cleaning_status": cleaning_status,
        "cleaning_method": CLEANING_METHOD,
        "cleaned_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }

    response = (
        repository.client
        .table("raw_pages")
        .update(payload)
        .eq(
            "raw_page_id",
            raw_page_id,
        )
        .execute()
    )

    if not response.data:
        raise RuntimeError(
            "Cleaned page update returned no data: "
            f"{raw_page_id}"
        )

    return response.data[0]


def write_cleaning_log(
    repository: SupabaseRepository,
    batch_id: str,
    raw_page_id: str,
    cleaning_status: str,
    raw_length: int,
    cleaned_length: int,
    warnings: list[str],
) -> None:
    """Write one process log for the cleaning result."""
    log_level = (
        "INFO"
        if cleaning_status == "CLEANED"
        else "WARNING"
    )

    repository.write_process_log(
        batch_id=batch_id,
        process_name="CLEAN_FACEBOOK_RAW_PAGE",
        process_step="RULE_BASED_TEXT_CLEANING",
        log_level=log_level,
        status=cleaning_status,
        message=(
            "Facebook raw page cleaning completed."
        ),
        error_details={
            "raw_page_id": raw_page_id,
            "cleaning_method": CLEANING_METHOD,
            "raw_length": raw_length,
            "cleaned_length": cleaned_length,
            "warnings": warnings,
        },
    )


def process_raw_page(
    repository: SupabaseRepository,
    batch_id: str,
    raw_page: dict[str, Any],
) -> None:
    """Clean and update one Facebook raw page."""
    raw_page_id = raw_page[
        "raw_page_id"
    ]

    raw_text = (
        raw_page.get("raw_text")
        or ""
    )

    print()
    print("=" * 70)

    print(
        f"Raw page ID: {raw_page_id}"
    )

    print(
        f"URL: {raw_page.get('page_url')}"
    )

    print(
        f"Collector: {raw_page.get('collector_name')}"
    )

    print(
        "Current cleaning status: "
        f"{raw_page.get('cleaning_status')}"
    )

    print(
        f"Raw length: {len(raw_text)}"
    )

    try:
        cleaned_text = clean_facebook_text(
            raw_text
        )

        cleaning_status, warnings = (
            validate_cleaned_text(
                raw_text=raw_text,
                cleaned_text=cleaned_text,
            )
        )

        print(
            f"Cleaned length: {len(cleaned_text)}"
        )

        print(
            f"Cleaning status: {cleaning_status}"
        )

        if warnings:
            print("Warnings:")

            for warning in warnings:
                print(
                    f"  - {warning}"
                )

        print()
        print("Cleaned text preview:")
        print("-" * 70)

        print(
            cleaned_text[:2500]
        )

        print("-" * 70)

        confirmation = input(
            "Type SAVE to store this cleaned text, "
            "SKIP to leave the current status unchanged, "
            "or REVIEW to save as REVIEW_REQUIRED: "
        ).strip().upper()

        if confirmation == "SKIP" or not confirmation:
            print(
                "Skipped. Cleaning status was not changed."
            )

            return

        if confirmation == "REVIEW":
            cleaning_status = "REVIEW_REQUIRED"

        elif confirmation == "SAVE":
            if cleaning_status == "FAILED":
                print(
                    "The cleaned text failed validation "
                    "and cannot be saved as CLEANED."
                )

                cleaning_status = "REVIEW_REQUIRED"

        else:
            print(
                "Unsupported input. "
                "Cleaning status was not changed."
            )

            return

        updated_page = update_cleaned_page(
            repository=repository,
            raw_page_id=raw_page_id,
            cleaned_text=cleaned_text,
            cleaning_status=cleaning_status,
        )

        write_cleaning_log(
            repository=repository,
            batch_id=batch_id,
            raw_page_id=raw_page_id,
            cleaning_status=cleaning_status,
            raw_length=len(raw_text),
            cleaned_length=len(cleaned_text),
            warnings=warnings,
        )

        print(
            "Cleaned text saved successfully."
        )

        print(
            "Final cleaning status: "
            f"{updated_page.get('cleaning_status')}"
        )

    except Exception as error:
        error_message = str(error)

        update_cleaned_page(
            repository=repository,
            raw_page_id=raw_page_id,
            cleaned_text=None,
            cleaning_status="FAILED",
        )

        repository.write_process_log(
            batch_id=batch_id,
            process_name="CLEAN_FACEBOOK_RAW_PAGE",
            process_step="RULE_BASED_TEXT_CLEANING",
            log_level="ERROR",
            status="FAILED",
            message=(
                "Facebook raw page cleaning failed."
            ),
            error_code=type(error).__name__,
            error_details={
                "raw_page_id": raw_page_id,
                "error": error_message,
                "cleaning_method": CLEANING_METHOD,
            },
        )

        print(
            "Cleaning failed."
        )

        print(
            f"Error type: {type(error).__name__}"
        )

        print(
            f"Error details: {error_message}"
        )


def main() -> None:
    """Clean Facebook raw pages for the pilot batch."""
    load_dotenv(
        PROJECT_ROOT / ".env"
    )

    print(
        "Facebook raw page cleaner started."
    )

    print(
        f"Version: {CLEANING_METHOD}"
    )

    repository = SupabaseRepository()

    batch = repository.get_batch_by_code(
        BATCH_CODE
    )

    if batch is None:
        print(
            f"Batch was not found: {BATCH_CODE}"
        )

        sys.exit(1)

    raw_pages = get_pending_raw_pages(
        repository=repository,
        batch_id=batch[
            "batch_id"
        ],
    )

    print(
        "Facebook raw pages found for cleaning: "
        f"{len(raw_pages)}"
    )

    if not raw_pages:
        print(
            "No Facebook raw pages require cleaning."
        )

        return

    for raw_page in raw_pages:
        process_raw_page(
            repository=repository,
            batch_id=batch[
                "batch_id"
            ],
            raw_page=raw_page,
        )

    print()
    print(
        "Facebook raw page cleaner finished."
    )


if __name__ == "__main__":
    main()