import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.repositories.supabase_repository import SupabaseRepository


BATCH_CODE = "FB-2026-001"
CLEANING_METHOD = "facebook_rule_based_cleaner_v0.1.0"


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


def normalize_line(line: str) -> str:
    """Normalize whitespace in one line."""
    return " ".join(line.split()).strip()


def is_single_character_noise(line: str) -> bool:
    """Return True for isolated one-character Facebook noise."""
    if len(line) != 1:
        return False

    return bool(
        re.fullmatch(
            r"[A-Za-zÀ-ỹ0-9]",
            line,
        )
    )


def is_noise_line(line: str) -> bool:
    """Return True when a line is considered Facebook interface noise."""
    if not line:
        return True

    if line in EXACT_LINES_TO_REMOVE:
        return True

    if any(
        line.startswith(prefix)
        for prefix in PREFIXES_TO_REMOVE
    ):
        return True

    if is_single_character_noise(line):
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


def clean_facebook_text(raw_text: str) -> str:
    """Clean Facebook interface noise while preserving post content."""
    normalized_lines = [
        normalize_line(line)
        for line in raw_text.splitlines()
    ]

    filtered_lines = [
        line
        for line in normalized_lines
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
            + ", ".join(remaining_feed_indicators)
        )

    reduction_ratio = (
        1 - len(cleaned_text) / len(raw_text)
        if raw_text
        else 0
    )

    if reduction_ratio > 0.85:
        warnings.append(
            "More than 85% of the raw text was removed."
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
        repository.client.table("raw_pages")
        .select(
            "raw_page_id, source_url_id, page_url, raw_title, "
            "raw_text, collector_name, collector_version, "
            "cleaning_status"
        )
        .eq("batch_id", batch_id)
        .eq("page_type", "FACEBOOK_POST")
        .eq("cleaning_status", "PENDING")
        .order("collected_at")
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
        repository.client.table("raw_pages")
        .update(payload)
        .eq("raw_page_id", raw_page_id)
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
    raw_page_id = raw_page["raw_page_id"]
    raw_text = raw_page.get("raw_text") or ""

    print()
    print("=" * 70)
    print(f"Raw page ID: {raw_page_id}")
    print(f"URL: {raw_page.get('page_url')}")
    print(f"Collector: {raw_page.get('collector_name')}")
    print(f"Raw length: {len(raw_text)}")

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
                print(f"  - {warning}")

        print()
        print("Cleaned text preview:")
        print("-" * 70)
        print(cleaned_text[:2500])
        print("-" * 70)

        confirmation = input(
            "Type SAVE to store this cleaned text, "
            "SKIP to leave it pending, "
            "or REVIEW to save as REVIEW_REQUIRED: "
        ).strip().upper()

        if confirmation == "SKIP" or not confirmation:
            print("Skipped. Cleaning status remains PENDING.")
            return

        if confirmation == "REVIEW":
            cleaning_status = "REVIEW_REQUIRED"

        elif confirmation != "SAVE":
            print(
                "Unsupported input. "
                "Cleaning status remains PENDING."
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
            message="Facebook raw page cleaning failed.",
            error_code=type(error).__name__,
            error_details={
                "raw_page_id": raw_page_id,
                "error": error_message,
                "cleaning_method": CLEANING_METHOD,
            },
        )

        print("Cleaning failed.")
        print(
            f"Error type: {type(error).__name__}"
        )
        print(
            f"Error details: {error_message}"
        )


def main() -> None:
    """Clean pending Facebook raw pages for the pilot batch."""
    load_dotenv()

    print("Facebook raw page cleaner started.")

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
        batch_id=batch["batch_id"],
    )

    print(
        f"Pending raw pages found: {len(raw_pages)}"
    )

    if not raw_pages:
        print(
            "No Facebook raw pages require cleaning."
        )
        return

    for raw_page in raw_pages:
        process_raw_page(
            repository=repository,
            batch_id=batch["batch_id"],
            raw_page=raw_page,
        )

    print()
    print("Facebook raw page cleaner finished.")


if __name__ == "__main__":
    main()