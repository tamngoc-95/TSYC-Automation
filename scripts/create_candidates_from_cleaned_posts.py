import json
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

EXTRACTOR_NAME = "facebook_cleaned_post_rule_extractor"
EXTRACTOR_VERSION = "0.8.0"

DEFAULT_CANDIDATE_TYPE = "SINGLE_BOOK"
DEFAULT_WORKFLOW_STATUS = "EXTRACTED"
DEFAULT_EXTRACTION_METHOD = "RULE_BASED"

DEFAULT_REVIEW_REASON = (
    "Candidate was extracted from Facebook post text. "
    "Book identity, ISBN, publisher, and metadata still require verification."
)

CANDIDATE_CODE_PATTERN = re.compile(
    rf"^{re.escape(BATCH_CODE)}-CAN-(\d+)$"
)

LEADING_POST_MARKERS = (
    "Sách có sẵn ở Đức",
    "Sách có sẵn",
)

TITLE_DESCRIPTION_PATTERNS = (
    re.compile(
        r"^(?P<title>.{2,160}?)\s+"
        r"(?:là|là một)\s+cuốn sách\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"^(?P<title>.{2,160}?)\s+"
        r"(?:là|là một)\s+bộ sách\b",
        flags=re.IGNORECASE,
    ),
)

TITLE_AUTHOR_PATTERNS = (
    re.compile(
        r"[“\"](?P<title>[^”\"]+)[”\"]\s+"
        r"(?:của|by)\s+"
        r"(?P<author>[A-ZÀ-Ỹ][^,\n.–—]+)",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"(?P<title>[^\n]{3,160}?)\s+"
        r"(?:của|by)\s+"
        r"(?P<author>[A-ZÀ-Ỹ][^,\n.–—]+)",
        flags=re.IGNORECASE,
    ),
)

AUTHOR_REJECTION_MARKERS = (
    "cuốn sách",
    "bộ sách",
    "cơ thể",
    "tuổi dậy thì",
    "giúp các em",
    "giúp trẻ",
    "nội dung",
    "ngôn ngữ",
    "minh họa",
    "phát triển",
    "thay đổi",
    "hướng dẫn",
    "đây là",
)

MAX_TITLE_LENGTH = 160
MAX_AUTHOR_LENGTH = 100

ISBN_PATTERN = re.compile(
    r"\b(?:ISBN(?:-1[03])?\s*:?\s*)?"
    r"(?P<isbn>97[89][\d\-\s]{10,20}\d)\b",
    flags=re.IGNORECASE,
)


def normalize_text(
    value: str | None,
) -> str:
    """Normalize whitespace while preserving Vietnamese text."""
    if not value:
        return ""

    return " ".join(
        value.split()
    ).strip()


def remove_leading_post_markers(
    value: str,
) -> str:
    """Remove known Facebook post labels before identity extraction."""
    cleaned = normalize_text(value)

    marker_removed = True

    while marker_removed:
        marker_removed = False
        lowered = cleaned.casefold()

        for marker in LEADING_POST_MARKERS:
            marker_text = normalize_text(marker)
            marker_lower = marker_text.casefold()

            if lowered == marker_lower:
                return ""

            if lowered.startswith(
                marker_lower + " "
            ):
                cleaned = cleaned[
                    len(marker_text):
                ].lstrip(
                    " \t\r\n:–—-"
                )
                marker_removed = True
                break

    return cleaned


def looks_like_description_fragment(
    value: str | None,
) -> bool:
    """Return True when a value resembles prose rather than a person name."""
    cleaned = normalize_text(value)

    if not cleaned:
        return False

    lowered = cleaned.casefold()

    if any(
        marker in lowered
        for marker in AUTHOR_REJECTION_MARKERS
    ):
        return True

    word_count = len(cleaned.split())

    if word_count > 8:
        return True

    if len(cleaned) > MAX_AUTHOR_LENGTH:
        return True

    if re.search(
        r"[.!?;:]",
        cleaned,
    ):
        return True

    return False


def validate_extracted_identity(
    title: str | None,
    author: str | None,
) -> tuple[str, str | None, list[str]]:
    """Validate extracted fields and return normalized values plus warnings."""
    warnings: list[str] = []

    cleaned_title = clean_extracted_title(
        title or ""
    )

    cleaned_author = (
        clean_extracted_author(author)
        if author
        else None
    )

    if not cleaned_title:
        raise RuntimeError(
            "No reliable book title could be extracted "
            "from the cleaned Facebook post."
        )

    if len(cleaned_title) > MAX_TITLE_LENGTH:
        raise RuntimeError(
            "The extracted title is too long and appears "
            "to contain post description text."
        )

    title_lower = cleaned_title.casefold()

    if title_lower in {
        "bài viết",
        "sách có sẵn",
        "sách có sẵn ở đức",
        "yêu con",
        "tiệm sách yêu con ở đức",
    }:
        raise RuntimeError(
            "The extracted title is a Facebook interface label, "
            "not a reliable book title."
        )

    if cleaned_author and looks_like_description_fragment(
        cleaned_author
    ):
        warnings.append(
            "The extracted author resembled description text "
            "and was removed."
        )
        cleaned_author = None

    return (
        cleaned_title,
        cleaned_author,
        warnings,
    )


def clean_extracted_title(
    value: str,
) -> str:
    """Clean punctuation around an extracted book title."""
    return normalize_text(
        value
    ).strip(
        " \t\r\n“”\"'.,;:–—-"
    )


def clean_extracted_author(
    value: str,
) -> str:
    """Clean punctuation and trailing description from an author name."""
    cleaned = normalize_text(
        value
    ).strip(
        " \t\r\n“”\"'.,;:–—-"
    )

    stop_markers = (
        " là cuốn sách",
        " là một cuốn sách",
        " chia sẻ",
        " giới thiệu",
        " kể về",
        " mang đến",
        " giúp",
        " được",
    )

    cleaned_lower = cleaned.lower()

    for marker in stop_markers:
        marker_position = cleaned_lower.find(
            marker
        )

        if marker_position >= 0:
            cleaned = cleaned[
                :marker_position
            ].strip()

            cleaned_lower = cleaned.lower()

    return cleaned


def normalize_isbn(
    value: str | None,
) -> str | None:
    """Normalize an ISBN candidate to digits only."""
    if not value:
        return None

    digits = re.sub(
        r"\D",
        "",
        value,
    )

    if len(digits) in {
        10,
        13,
    }:
        return digits

    return None


def extract_book_identity(
    cleaned_text: str,
) -> dict[str, Any]:
    """Extract a conservative book identity from cleaned Facebook text."""
    normalized_text = remove_leading_post_markers(
        cleaned_text
    )

    if not normalized_text:
        raise RuntimeError(
            "The cleaned Facebook post text is empty."
        )

    extracted_title: str | None = None
    extracted_author: str | None = None
    matched_pattern: str | None = None
    extraction_warnings: list[str] = []

    for pattern_number, pattern in enumerate(
        TITLE_DESCRIPTION_PATTERNS,
        start=1,
    ):
        match = pattern.search(
            normalized_text
        )

        if not match:
            continue

        extracted_title = clean_extracted_title(
            match.group("title")
        )
        extracted_author = None
        matched_pattern = (
            f"TITLE_DESCRIPTION_PATTERN_{pattern_number}"
        )
        break

    if not extracted_title:
        for pattern_number, pattern in enumerate(
            TITLE_AUTHOR_PATTERNS,
            start=1,
        ):
            match = pattern.search(
                normalized_text
            )

            if not match:
                continue

            extracted_title = clean_extracted_title(
                match.group("title")
            )

            extracted_author = clean_extracted_author(
                match.group("author")
            )

            matched_pattern = (
                f"TITLE_AUTHOR_PATTERN_{pattern_number}"
            )

            if extracted_title:
                break

    (
        extracted_title,
        extracted_author,
        validation_warnings,
    ) = validate_extracted_identity(
        title=extracted_title,
        author=extracted_author,
    )

    extraction_warnings.extend(
        validation_warnings
    )

    isbn_match = ISBN_PATTERN.search(
        normalized_text
    )

    possible_isbn = (
        normalize_isbn(
            isbn_match.group("isbn")
        )
        if isbn_match
        else None
    )

    extraction_confidence = 0.90

    if matched_pattern and matched_pattern.startswith(
        "TITLE_DESCRIPTION_PATTERN_"
    ):
        extraction_confidence = 0.85

    if not extracted_author:
        extraction_confidence = min(
            extraction_confidence,
            0.80,
        )

    if possible_isbn:
        extraction_confidence = min(
            0.95,
            extraction_confidence + 0.03,
        )

    return {
        "extracted_title": extracted_title,
        "extracted_author": extracted_author,
        "possible_isbn": possible_isbn,
        "extraction_confidence": extraction_confidence,
        "matched_pattern": matched_pattern,
        "warnings": extraction_warnings,
    }


def get_batch_by_code(
    repository: SupabaseRepository,
) -> dict[str, Any]:
    """Return the configured batch."""
    response = (
        repository.client
        .table("batches")
        .select(
            "batch_id, batch_code"
        )
        .eq(
            "batch_code",
            BATCH_CODE,
        )
        .limit(1)
        .execute()
    )

    records = response.data or []

    if not records:
        raise RuntimeError(
            f"Batch was not found: {BATCH_CODE}"
        )

    return records[0]


def get_cleaned_raw_pages(
    repository: SupabaseRepository,
    batch_id: str,
) -> list[dict[str, Any]]:
    """Return cleaned Facebook posts available for extraction."""
    response = (
        repository.client
        .table("raw_pages")
        .select(
            "raw_page_id, "
            "batch_id, "
            "source_url_id, "
            "page_url, "
            "raw_title, "
            "page_type, "
            "cleaning_status, "
            "cleaned_text, "
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
            desc=False,
        )
        .execute()
    )

    return response.data or []


def select_raw_page(
    raw_pages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Allow the user to select one cleaned Facebook post."""
    if not raw_pages:
        raise RuntimeError(
            "No cleaned Facebook posts were found."
        )

    print()
    print("Cleaned Facebook posts:")

    for index, raw_page in enumerate(
        raw_pages,
        start=1,
    ):
        print()
        print(
            f"[{index}] "
            f"{raw_page.get('raw_title') or 'Untitled post'}"
        )
        print(
            f"    URL: {raw_page.get('page_url')}"
        )
        print(
            "    Raw page ID: "
            f"{raw_page.get('raw_page_id')}"
        )

    print()

    selection = input(
        "Enter the page number to extract "
        "[default: 1]: "
    ).strip()

    if not selection:
        return raw_pages[0]

    try:
        selected_index = int(
            selection
        ) - 1

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


def find_existing_candidate(
    repository: SupabaseRepository,
    raw_page_id: str,
    extracted_title: str,
) -> dict[str, Any] | None:
    """Find an existing candidate for the same page and title."""
    response = (
        repository.client
        .table("product_candidates")
        .select(
            "candidate_id, "
            "candidate_code, "
            "raw_page_id, "
            "extracted_title, "
            "extracted_author, "
            "workflow_status, "
            "review_required"
        )
        .eq(
            "raw_page_id",
            raw_page_id,
        )
        .ilike(
            "extracted_title",
            extracted_title,
        )
        .limit(1)
        .execute()
    )

    records = response.data or []

    if not records:
        return None

    return records[0]


def get_next_candidate_code(
    repository: SupabaseRepository,
) -> str:
    """Generate the next sequential candidate code for the batch."""
    response = (
        repository.client
        .table("product_candidates")
        .select(
            "candidate_code"
        )
        .eq(
            "batch_id",
            get_batch_by_code(
                repository
            )["batch_id"],
        )
        .execute()
    )

    highest_number = 0

    for record in response.data or []:
        candidate_code = str(
            record.get("candidate_code")
            or ""
        ).strip()

        match = CANDIDATE_CODE_PATTERN.match(
            candidate_code
        )

        if not match:
            continue

        candidate_number = int(
            match.group(1)
        )

        highest_number = max(
            highest_number,
            candidate_number,
        )

    return (
        f"{BATCH_CODE}-CAN-"
        f"{highest_number + 1:04d}"
    )


def build_source_evidence(
    raw_page: dict[str, Any],
    extraction: dict[str, Any],
) -> dict[str, Any]:
    """Build traceable evidence for the extracted candidate."""
    return {
        "source_type": "FACEBOOK",
        "page_type": raw_page.get(
            "page_type"
        ),
        "raw_page_id": raw_page.get(
            "raw_page_id"
        ),
        "source_url_id": raw_page.get(
            "source_url_id"
        ),
        "source_url": raw_page.get(
            "page_url"
        ),
        "cleaning_status": raw_page.get(
            "cleaning_status"
        ),
        "extraction_method": (
            DEFAULT_EXTRACTION_METHOD
        ),
        "extractor_name": EXTRACTOR_NAME,
        "extractor_version": EXTRACTOR_VERSION,
        "matched_pattern": extraction.get(
            "matched_pattern"
        ),
        "extraction_warnings": extraction.get(
            "warnings"
        ) or [],
        "extracted_fields": {
            "title": extraction.get(
                "extracted_title"
            ),
            "author": extraction.get(
                "extracted_author"
            ),
            "possible_isbn": extraction.get(
                "possible_isbn"
            ),
        },
        "extracted_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }


def build_candidate_payload(
    batch: dict[str, Any],
    raw_page: dict[str, Any],
    extraction: dict[str, Any],
    candidate_code: str,
) -> dict[str, Any]:
    """Build a valid product_candidates insert payload."""
    return {
        "batch_id": batch["batch_id"],
        "candidate_code": candidate_code,
        "candidate_type": (
            DEFAULT_CANDIDATE_TYPE
        ),
        "combo_group_code": None,

        "raw_page_id": raw_page[
            "raw_page_id"
        ],
        "source_url_id": raw_page[
            "source_url_id"
        ],

        "extracted_title": extraction[
            "extracted_title"
        ],
        "extracted_author": extraction.get(
            "extracted_author"
        ),
        "possible_isbn": extraction.get(
            "possible_isbn"
        ),

        "workflow_status": (
            DEFAULT_WORKFLOW_STATUS
        ),

        "extraction_confidence": extraction[
            "extraction_confidence"
        ],

        "source_evidence": (
            build_source_evidence(
                raw_page=raw_page,
                extraction=extraction,
            )
        ),

        "conflict_fields": [],

        "review_required": True,
        "review_reason": (
            DEFAULT_REVIEW_REASON
        ),

        "extraction_method": (
            DEFAULT_EXTRACTION_METHOD
        ),
        "extractor_name": EXTRACTOR_NAME,
        "extractor_version": (
            EXTRACTOR_VERSION
        ),
    }


def insert_candidate(
    repository: SupabaseRepository,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Insert one product candidate."""
    response = (
        repository.client
        .table("product_candidates")
        .insert(payload)
        .execute()
    )

    records = response.data or []

    if not records:
        raise RuntimeError(
            "Supabase returned no candidate "
            "after insert."
        )

    return records[0]


def find_unlinked_images(
    repository: SupabaseRepository,
    raw_page_id: str,
) -> list[dict[str, Any]]:
    """Return unlinked images from the same Facebook post."""
    response = (
        repository.client
        .table("product_images")
        .select(
            "image_id, "
            "candidate_id, "
            "raw_page_id, "
            "image_status, "
            "image_role, "
            "storage_bucket, "
            "storage_path"
        )
        .eq(
            "raw_page_id",
            raw_page_id,
        )
        .is_(
            "candidate_id",
            "null",
        )
        .execute()
    )

    return response.data or []


def link_images_to_candidate(
    repository: SupabaseRepository,
    raw_page_id: str,
    candidate_id: str,
) -> list[dict[str, Any]]:
    """Link all currently unlinked post images to one candidate."""
    response = (
        repository.client
        .table("product_images")
        .update(
            {
                "candidate_id": candidate_id,
                "updated_at": datetime.now(
                    timezone.utc
                ).isoformat(),
            }
        )
        .eq(
            "raw_page_id",
            raw_page_id,
        )
        .is_(
            "candidate_id",
            "null",
        )
        .execute()
    )

    return response.data or []


def rollback_candidate(
    repository: SupabaseRepository,
    candidate_id: str,
) -> None:
    """Delete a newly created candidate after a critical failure."""
    (
        repository.client
        .table("product_candidates")
        .delete()
        .eq(
            "candidate_id",
            candidate_id,
        )
        .execute()
    )


def print_extraction_preview(
    raw_page: dict[str, Any],
    extraction: dict[str, Any],
    candidate_code: str,
    unlinked_images: list[dict[str, Any]],
) -> None:
    """Print the candidate before database insertion."""
    print()
    print("=" * 78)
    print(
        "CANDIDATE EXTRACTION PREVIEW"
    )
    print("=" * 78)

    print(
        "Raw page ID: "
        f"{raw_page.get('raw_page_id')}"
    )
    print(
        "Candidate code: "
        f"{candidate_code}"
    )
    print(
        "Candidate type: "
        f"{DEFAULT_CANDIDATE_TYPE}"
    )
    print(
        "Extracted title: "
        f"{extraction.get('extracted_title')}"
    )
    print(
        "Extracted author: "
        f"{extraction.get('extracted_author') or '[not found]'}"
    )
    print(
        "Possible ISBN: "
        f"{extraction.get('possible_isbn') or '[not found]'}"
    )
    print(
        "Extraction confidence: "
        f"{extraction.get('extraction_confidence'):.2f}"
    )
    print(
        "Matched rule: "
        f"{extraction.get('matched_pattern') or '[none]'}"
    )
    print(
        "Workflow status: "
        f"{DEFAULT_WORKFLOW_STATUS}"
    )
    print(
        "Review required: True"
    )
    print(
        "Unlinked images available: "
        f"{len(unlinked_images)}"
    )

    extraction_warnings = (
        extraction.get("warnings")
        or []
    )

    if extraction_warnings:
        print()
        print("Extraction warnings:")

        for warning in extraction_warnings:
            print(
                f"  - {warning}"
            )

    if unlinked_images:
        print()
        print(
            "Images that will be linked:"
        )

        for index, image in enumerate(
            unlinked_images,
            start=1,
        ):
            print(
                f"  [{index}] "
                f"{image.get('image_id')} | "
                f"{image.get('image_status')} | "
                f"{image.get('storage_path')}"
            )


def verify_candidate_result(
    repository: SupabaseRepository,
    candidate_id: str,
) -> dict[str, Any]:
    """Read back the candidate after insertion."""
    response = (
        repository.client
        .table("product_candidates")
        .select(
            "candidate_id, "
            "batch_id, "
            "candidate_code, "
            "candidate_type, "
            "raw_page_id, "
            "source_url_id, "
            "extracted_title, "
            "extracted_author, "
            "possible_isbn, "
            "identity_status, "
            "workflow_status, "
            "extraction_confidence, "
            "review_required, "
            "review_reason, "
            "extraction_method, "
            "extractor_name, "
            "extractor_version"
        )
        .eq(
            "candidate_id",
            candidate_id,
        )
        .limit(1)
        .execute()
    )

    records = response.data or []

    if not records:
        raise RuntimeError(
            "Candidate verification failed. "
            "The inserted record could not be read back."
        )

    return records[0]


def verify_linked_images(
    repository: SupabaseRepository,
    candidate_id: str,
) -> list[dict[str, Any]]:
    """Read back images linked to the candidate."""
    response = (
        repository.client
        .table("product_images")
        .select(
            "image_id, "
            "candidate_id, "
            "raw_page_id, "
            "image_status, "
            "image_role, "
            "storage_bucket, "
            "storage_path"
        )
        .eq(
            "candidate_id",
            candidate_id,
        )
        .execute()
    )

    return response.data or []


def create_candidate_from_page(
    repository: SupabaseRepository,
    batch: dict[str, Any],
    raw_page: dict[str, Any],
) -> str:
    """Create one candidate and link its source images."""
    cleaned_text = str(
        raw_page.get("cleaned_text")
        or ""
    ).strip()

    extraction = extract_book_identity(
        cleaned_text
    )

    raw_page_id = str(
        raw_page["raw_page_id"]
    )

    existing_candidate = find_existing_candidate(
        repository=repository,
        raw_page_id=raw_page_id,
        extracted_title=extraction[
            "extracted_title"
        ],
    )

    if existing_candidate is not None:
        print()
        print("=" * 78)
        print(
            "DUPLICATE CANDIDATE"
        )
        print("=" * 78)
        print(
            "A candidate already exists for this "
            "Facebook post and title."
        )
        print(
            "Candidate ID: "
            f"{existing_candidate.get('candidate_id')}"
        )
        print(
            "Candidate code: "
            f"{existing_candidate.get('candidate_code')}"
        )
        print(
            "Extracted title: "
            f"{existing_candidate.get('extracted_title')}"
        )

        return "DUPLICATE_CANDIDATE"

    candidate_code = get_next_candidate_code(
        repository
    )

    unlinked_images = find_unlinked_images(
        repository=repository,
        raw_page_id=raw_page_id,
    )

    print_extraction_preview(
        raw_page=raw_page,
        extraction=extraction,
        candidate_code=candidate_code,
        unlinked_images=unlinked_images,
    )

    print()

    confirmation = input(
        "Type CREATE to create this candidate, "
        "or press Enter to cancel: "
    ).strip().upper()

    if confirmation != "CREATE":
        print(
            "Candidate creation cancelled."
        )

        return "CANCELLED"

    payload = build_candidate_payload(
        batch=batch,
        raw_page=raw_page,
        extraction=extraction,
        candidate_code=candidate_code,
    )

    candidate = insert_candidate(
        repository=repository,
        payload=payload,
    )

    candidate_id = str(
        candidate["candidate_id"]
    )

    print()
    print(
        "Candidate created."
    )
    print(
        f"Candidate ID: {candidate_id}"
    )
    print(
        "Candidate code: "
        f"{candidate.get('candidate_code')}"
    )

    linked_images: list[
        dict[str, Any]
    ] = []

    try:
        if unlinked_images:
            linked_images = (
                link_images_to_candidate(
                    repository=repository,
                    raw_page_id=raw_page_id,
                    candidate_id=candidate_id,
                )
            )

            print(
                "Images linked to candidate: "
                f"{len(linked_images)}"
            )

        else:
            print(
                "No unlinked images were available."
            )

        verified_candidate = (
            verify_candidate_result(
                repository=repository,
                candidate_id=candidate_id,
            )
        )

        verified_images = (
            verify_linked_images(
                repository=repository,
                candidate_id=candidate_id,
            )
        )

    except Exception:
        print()
        print(
            "Candidate post-processing failed."
        )
        print(
            "Rolling back the newly created candidate..."
        )

        try:
            (
                repository.client
                .table("product_images")
                .update(
                    {
                        "candidate_id": None,
                        "updated_at": datetime.now(
                            timezone.utc
                        ).isoformat(),
                    }
                )
                .eq(
                    "candidate_id",
                    candidate_id,
                )
                .execute()
            )

            rollback_candidate(
                repository=repository,
                candidate_id=candidate_id,
            )

            print(
                "Candidate rollback completed."
            )

        except Exception as rollback_error:
            print(
                "Warning: candidate rollback failed."
            )
            print(
                "Rollback details: "
                f"{rollback_error}"
            )

        raise

    print()
    print("=" * 78)
    print(
        "CANDIDATE CREATION RESULT"
    )
    print("=" * 78)
    print(
        "Candidate ID: "
        f"{verified_candidate.get('candidate_id')}"
    )
    print(
        "Candidate code: "
        f"{verified_candidate.get('candidate_code')}"
    )
    print(
        "Extracted title: "
        f"{verified_candidate.get('extracted_title')}"
    )
    print(
        "Extracted author: "
        f"{verified_candidate.get('extracted_author')}"
    )
    print(
        "Identity status: "
        f"{verified_candidate.get('identity_status')}"
    )
    print(
        "Workflow status: "
        f"{verified_candidate.get('workflow_status')}"
    )
    print(
        "Review required: "
        f"{verified_candidate.get('review_required')}"
    )
    print(
        "Extraction method: "
        f"{verified_candidate.get('extraction_method')}"
    )
    print(
        "Linked images: "
        f"{len(verified_images)}"
    )

    return "CREATED"


def main() -> None:
    """Create one product candidate from one cleaned Facebook post."""
    load_dotenv()

    print(
        "Facebook cleaned-post candidate extractor started."
    )
    print(
        f"Version: {EXTRACTOR_VERSION}"
    )
    print(
        f"Batch: {BATCH_CODE}"
    )

    repository = SupabaseRepository()

    batch = get_batch_by_code(
        repository
    )

    raw_pages = get_cleaned_raw_pages(
        repository=repository,
        batch_id=batch["batch_id"],
    )

    print(
        "Cleaned Facebook posts found: "
        f"{len(raw_pages)}"
    )

    selected_raw_page = select_raw_page(
        raw_pages
    )

    result = create_candidate_from_page(
        repository=repository,
        batch=batch,
        raw_page=selected_raw_page,
    )

    print()
    print("=" * 78)
    print(
        "EXTRACTION RUN SUMMARY"
    )
    print("=" * 78)
    print(
        f"Result: {result}"
    )

    print()
    print(
        "Facebook cleaned-post candidate extractor finished."
    )


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print()
        print(
            "Candidate extraction was cancelled."
        )
        sys.exit(130)

    except Exception as error:
        print()
        print(
            "Candidate extraction failed."
        )
        print(
            f"Error type: {type(error).__name__}"
        )
        print(
            f"Error details: {error}"
        )
        sys.exit(1)