import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.cli_bootstrap import configure_utf8_console
from src.domain.decisions import Outcome
from src.domain.rules import extraction_rules
from src.domain.rules.extraction_rules import (
    normalize_isbn,
    normalize_text,
    validate_extracted_identity,
)
from src.repositories.supabase_repository import SupabaseRepository

configure_utf8_console()


BATCH_CODE = "FB-2026-001"

EXTRACTOR_NAME = "facebook_cleaned_post_rule_extractor"
EXTRACTOR_VERSION = "1.1.0"

DEFAULT_CANDIDATE_TYPE = "SINGLE_BOOK"
DEFAULT_WORKFLOW_STATUS = "EXTRACTED"
DEFAULT_EXTRACTION_METHOD = "RULE_BASED"

# Default bound for automatic extraction when the caller does not pass
# --max-candidates. CLAUDE.md's bounded-safety policy applies here the
# same way it applies to run_batch.py's --max-candidates: automatic
# extraction must never be allowed to create an unbounded number of
# candidates from one post.
DEFAULT_MAX_CANDIDATES = 5

DEFAULT_REVIEW_REASON = (
    "Candidate was extracted from Facebook post text. "
    "Book identity, ISBN, publisher, and metadata still require verification."
)

CANDIDATE_CODE_PATTERN = re.compile(
    rf"^{re.escape(BATCH_CODE)}-CAN-(\d+)$"
)

# Stable, greppable, machine-readable output-line prefix.
# scripts/watch_facebook_clipboard.py's --process chain parses this
# exact prefix (see its parse_candidate_codes()) to learn which
# candidate codes to hand to run_batch.py next. Deliberately neutral,
# not "CREATED_CANDIDATE_CODES" -- create_candidates_from_page() can
# return codes that were freshly created this run *or* codes that
# already existed from a prior idempotent-repeat run (see its
# existing_candidates branch); either way these are exactly the
# candidate codes now available to continue processing downstream, and
# a downstream consumer treats both cases identically. Keep this prefix
# in sync with parse_candidate_codes() in watch_facebook_clipboard.py --
# do not reformat one side without the other.
CANDIDATE_CODES_PREFIX = "CANDIDATE_CODES:"


def format_candidate_codes_line(candidate_codes: list[str]) -> str:
    """Format the stable CANDIDATE_CODES output line for a run's exact
    result -- see CANDIDATE_CODES_PREFIX's docstring above for why this
    label is neutral rather than "CREATED"."""
    return f"{CANDIDATE_CODES_PREFIX} " + ",".join(candidate_codes)


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
            "Create one product candidate from one cleaned Facebook post."
        )
    )

    parser.add_argument(
        "--raw-page-id",
        help="Process one exact cleaned Facebook raw page UUID.",
    )

    parser.add_argument(
        "--source-url-id",
        help="Process one exact Facebook source URL UUID.",
    )

    parser.add_argument(
        "--candidate-type",
        choices=[
            "SINGLE_BOOK",
            "BOOK_COMBO",
            "BOOK_SET",
            "ACTIVITY_PRODUCT",
            "OTHER",
        ],
        default=DEFAULT_CANDIDATE_TYPE,
        help="Candidate type to assign to the new candidate.",
    )

    parser.add_argument(
        "--candidate-title",
        action="append",
        default=[],
        help=(
            "Explicit candidate title. Repeat this option to create multiple "
            "candidates from one cleaned Facebook post."
        ),
    )

    parser.add_argument(
        "--candidate-author",
        action="append",
        default=[],
        help=(
            "Optional author corresponding by position to --candidate-title."
        ),
    )

    parser.add_argument(
        "--possible-isbn",
        action="append",
        default=[],
        help=(
            "Optional ISBN corresponding by position to --candidate-title."
        ),
    )

    parser.add_argument(
        "--max-candidates",
        type=int,
        default=DEFAULT_MAX_CANDIDATES,
        help=(
            "Hard upper bound on candidates created from this one post "
            f"(default {DEFAULT_MAX_CANDIDATES}). Applies to both explicit "
            "--candidate-title values and automatic extraction -- never "
            "unbounded."
        ),
    )

    parser.add_argument(
        "--confirm-create",
        action="store_true",
        help="Confirm candidate creation without prompting.",
    )

    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help=(
            "Disable input prompts. Requires --raw-page-id or "
            "--source-url-id and --confirm-create."
        ),
    )

    return parser.parse_args()


def extract_book_identity(
    cleaned_text: str,
) -> dict[str, Any]:
    """Extract a conservative book identity from cleaned Facebook text.

    Thin wrapper around src.domain.rules.extraction_rules.
    extract_single_book_identity() -- that module now holds the one
    canonical implementation of the title/author/ISBN regex patterns
    (including the U+034F defensive-normalization fix). This wrapper
    exists only to preserve this exact function's external contract
    (name, signature, return-dict shape, matched_pattern labels, raise-
    on-failure behavior) for its existing callers --
    scripts/correct_candidate_extraction.py and the regression tests in
    tests/test_facebook_text_normalization.py that pin down the U+034F
    fix -- unchanged.
    """
    result = extraction_rules.extract_single_book_identity(cleaned_text)

    if result is None:
        raise RuntimeError(
            "No reliable book title could be extracted "
            "from the cleaned Facebook post."
        )

    return {
        "extracted_title": result.extracted_title,
        "extracted_author": result.extracted_author,
        "possible_isbn": result.possible_isbn,
        "extraction_confidence": result.extraction_confidence,
        "matched_pattern": result.matched_pattern,
        "warnings": list(result.warnings),
    }


def build_explicit_extractions(
    titles: list[str],
    authors: list[str],
    isbns: list[str],
) -> list[dict[str, Any]]:
    """Build validated candidate extractions from explicit CLI values."""
    if not titles:
        return []

    if len(authors) > len(titles):
        raise RuntimeError(
            "More --candidate-author values were supplied than --candidate-title values."
        )

    if len(isbns) > len(titles):
        raise RuntimeError(
            "More --possible-isbn values were supplied than --candidate-title values."
        )

    extractions: list[dict[str, Any]] = []

    for index, raw_title in enumerate(titles):
        raw_author = (
            authors[index]
            if index < len(authors)
            else None
        )

        raw_isbn = (
            isbns[index]
            if index < len(isbns)
            else None
        )

        (
            cleaned_title,
            cleaned_author,
            warnings,
        ) = validate_extracted_identity(
            title=raw_title,
            author=raw_author,
        )

        possible_isbn = normalize_isbn(
            raw_isbn
        )

        if raw_isbn and not possible_isbn:
            warnings.append(
                "The supplied ISBN was invalid and was removed."
            )

        extractions.append(
            {
                "extracted_title": cleaned_title,
                "extracted_author": cleaned_author,
                "possible_isbn": possible_isbn,
                "extraction_confidence": 1.0,
                "matched_pattern": "EXPLICIT_CLI_IDENTITY",
                "warnings": warnings,
            }
        )

    return extractions


def ensure_unique_extractions(
    extractions: list[dict[str, Any]],
) -> None:
    """Reject duplicate titles within one extraction run."""
    seen_titles: set[str] = set()

    for extraction in extractions:
        normalized_title = normalize_text(
            extraction.get("extracted_title")
        ).casefold()

        if normalized_title in seen_titles:
            raise RuntimeError(
                "Duplicate candidate titles were supplied for the same raw page."
            )

        seen_titles.add(
            normalized_title
        )


def get_batch_by_code(
    repository: SupabaseRepository,
    batch_code: str = BATCH_CODE,
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
            batch_code,
        )
        .limit(1)
        .execute()
    )

    records = response.data or []

    if not records:
        raise RuntimeError(
            f"Batch was not found: {batch_code}"
        )

    return records[0]


def get_cleaned_raw_pages(
    repository: SupabaseRepository,
    batch_id: str,
    raw_page_id: str | None = None,
    source_url_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return cleaned Facebook posts available for extraction."""
    query = (
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
        .eq("batch_id", batch_id)
        .eq("page_type", "FACEBOOK_POST")
        .eq("cleaning_status", "CLEANED")
    )

    if raw_page_id:
        query = query.eq(
            "raw_page_id",
            raw_page_id,
        )

    if source_url_id:
        query = query.eq(
            "source_url_id",
            source_url_id,
        )

    response = (
        query
        .order(
            "collected_at",
            desc=False,
        )
        .execute()
    )

    return response.data or []


def select_raw_page(
    raw_pages: list[dict[str, Any]],
    non_interactive: bool = False,
) -> dict[str, Any]:
    """Select one cleaned Facebook post."""
    if not raw_pages:
        raise RuntimeError(
            "No cleaned Facebook posts were found."
        )

    if len(raw_pages) == 1:
        return raw_pages[0]

    if non_interactive:
        raise RuntimeError(
            "Multiple cleaned Facebook posts matched the selector. "
            "Use --raw-page-id to select one exact post."
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
        "Enter the page number to extract, "
        "or press Enter to cancel: "
    ).strip()

    if not selection:
        raise KeyboardInterrupt

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


def find_existing_candidate(
    repository: SupabaseRepository,
    raw_page_id: str,
    extracted_title: str,
) -> dict[str, Any] | None:
    """Find an existing candidate for the same raw page and normalized title."""
    response = (
        repository.client
        .table("product_candidates")
        .select(
            "candidate_id, "
            "candidate_code, "
            "raw_page_id, "
            "extracted_title, "
            "extracted_author, "
            "candidate_type, "
            "workflow_status, "
            "review_required"
        )
        .eq(
            "raw_page_id",
            raw_page_id,
        )
        .execute()
    )

    records = response.data or []
    normalized_target = normalize_text(
        extracted_title
    ).casefold()

    for record in records:
        normalized_existing = normalize_text(
            record.get("extracted_title")
        ).casefold()

        if normalized_existing == normalized_target:
            return record

    return None


def get_existing_candidates_for_raw_page(
    repository: SupabaseRepository,
    raw_page_id: str,
) -> list[dict[str, Any]]:
    """Return all candidates already linked to one raw page."""
    response = (
        repository.client
        .table("product_candidates")
        .select(
            "candidate_id,"
            "candidate_code,"
            "candidate_type,"
            "extracted_title,"
            "extracted_author,"
            "possible_isbn,"
            "workflow_status"
        )
        .eq(
            "raw_page_id",
            raw_page_id,
        )
        .order(
            "created_at",
            desc=False,
        )
        .execute()
    )

    return response.data or []


def get_next_candidate_code(
    repository: SupabaseRepository,
    batch_id: str,
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
            batch_id,
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
    candidate_type: str,
    combo_group_code: str | None = None,
) -> dict[str, Any]:
    """Build a valid product_candidates insert payload."""
    return {
        "batch_id": batch["batch_id"],
        "candidate_code": candidate_code,
        "candidate_type": candidate_type,
        "combo_group_code": combo_group_code,

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
    candidate_type: str,
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
        f"{candidate_type}"
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


def create_one_candidate(
    repository: SupabaseRepository,
    batch: dict[str, Any],
    raw_page: dict[str, Any],
    extraction: dict[str, Any],
    candidate_type: str,
    combo_group_code: str | None,
    confirm_create: bool,
    non_interactive: bool,
    link_images: bool,
) -> tuple[str, str | None]:
    """Create one candidate from one prepared extraction.

    Returns (status, candidate_code) -- candidate_code is populated for
    CREATED and DUPLICATE_CANDIDATE (the existing one), None for
    CANCELLED. Callers that need the exact candidate codes created by an
    automatic-extraction run (scripts/watch_facebook_clipboard.py's
    --process chain) rely on this.
    """
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
        print("DUPLICATE CANDIDATE")
        print("=" * 78)
        print(
            "A candidate already exists for this Facebook post and title."
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
        return "DUPLICATE_CANDIDATE", existing_candidate.get("candidate_code")

    candidate_code = get_next_candidate_code(
        repository=repository,
        batch_id=batch["batch_id"],
    )

    unlinked_images = (
        find_unlinked_images(
            repository=repository,
            raw_page_id=raw_page_id,
        )
        if link_images
        else []
    )

    print_extraction_preview(
        raw_page=raw_page,
        extraction=extraction,
        candidate_code=candidate_code,
        candidate_type=candidate_type,
        unlinked_images=unlinked_images,
    )

    print()

    if confirm_create:
        confirmation = "CREATE"

    elif non_interactive:
        confirmation = ""

    else:
        confirmation = normalize_confirmation(
            input(
                "Type CREATE, CREATE_CANDIDATE, or CONFIRM "
                "to create this candidate, or press Enter to cancel: "
            )
        )

    if confirmation not in {
        "CREATE",
        "CREATE_CANDIDATE",
        "CONFIRM",
    }:
        print()
        print(
            "Invalid confirmation. Use CREATE, CREATE_CANDIDATE, or CONFIRM."
        )
        print(
            f"Received value: {confirmation or '[empty]'}"
        )
        print(
            "Candidate creation cancelled."
        )
        return "CANCELLED", None

    payload = build_candidate_payload(
        batch=batch,
        raw_page=raw_page,
        extraction=extraction,
        candidate_code=candidate_code,
        candidate_type=candidate_type,
        combo_group_code=combo_group_code,
    )

    candidate = insert_candidate(
        repository=repository,
        payload=payload,
    )

    candidate_id = str(
        candidate["candidate_id"]
    )

    print()
    print("Candidate created.")
    print(
        f"Candidate ID: {candidate_id}"
    )
    print(
        "Candidate code: "
        f"{candidate.get('candidate_code')}"
    )

    try:
        if unlinked_images:
            linked_images = link_images_to_candidate(
                repository=repository,
                raw_page_id=raw_page_id,
                candidate_id=candidate_id,
            )

            print(
                "Images linked to candidate: "
                f"{len(linked_images)}"
            )

        elif link_images:
            print(
                "No unlinked images were available."
            )

        else:
            print(
                "Images were not auto-linked because this raw page "
                "contains multiple product candidates."
            )

        verified_candidate = verify_candidate_result(
            repository=repository,
            candidate_id=candidate_id,
        )

        verified_images = verify_linked_images(
            repository=repository,
            candidate_id=candidate_id,
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
    print("CANDIDATE CREATION RESULT")
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

    return "CREATED", verified_candidate.get("candidate_code")


def create_candidates_from_page(
    repository: SupabaseRepository,
    batch: dict[str, Any],
    raw_page: dict[str, Any],
    candidate_type: str,
    explicit_extractions: list[dict[str, Any]],
    confirm_create: bool,
    non_interactive: bool,
    max_candidates: int,
) -> tuple[dict[str, int], list[str]]:
    """Create one or more candidates from one cleaned Facebook post.

    Explicit --candidate-title values keep exactly their prior behavior.
    With no explicit titles, this now runs deterministic automatic
    extraction (src.domain.rules.extraction_rules.run_automatic_
    extraction): ONE_BOOK/MULTIPLE_BOOKS/COMBO create candidates
    automatically; GENERAL_POST/AMBIGUOUS create none and are reported
    with their rule_code and reason -- manual --candidate-title remains
    the fallback for those, never a guess.

    Returns (results_counts, candidate_codes) -- candidate_codes is every
    candidate code relevant to this raw_page after this call (newly
    CREATED ones, or the already-existing ones on the idempotent-repeat
    path below), for callers (scripts/watch_facebook_clipboard.py's
    --process chain) that need to hand exact codes to run_batch.py.
    """
    cleaned_text = str(
        raw_page.get("cleaned_text")
        or ""
    ).strip()

    if not cleaned_text:
        raise RuntimeError(
            "The selected raw page does not contain cleaned_text."
        )

    raw_page_id = str(raw_page["raw_page_id"])

    results = {
        "CREATED": 0,
        "DUPLICATE_CANDIDATE": 0,
        "CANCELLED": 0,
        "AUTO_REJECTED": 0,
        "REVIEW_REQUIRED": 0,
        "BLOCKED": 0,
    }
    created_candidate_codes: list[str] = []

    if explicit_extractions:
        extractions = explicit_extractions

        if len(extractions) > max_candidates:
            raise RuntimeError(
                f"{len(extractions)} --candidate-title value(s) exceed "
                f"--max-candidates ({max_candidates}). Failing before any write."
            )

        ensure_unique_extractions(extractions)

        multi_candidate_run = len(extractions) > 1

        if (
            multi_candidate_run
            and candidate_type in {"BOOK_COMBO", "BOOK_SET"}
        ):
            raise RuntimeError(
                "Use one BOOK_COMBO or BOOK_SET candidate for a bundled product. "
                "Use repeated --candidate-title values only when the Facebook "
                "post contains multiple distinct sellable products."
            )

        combo_group_code = (
            f"{BATCH_CODE}-GROUP-{raw_page_id[:8]}"
            if candidate_type in {"BOOK_COMBO", "BOOK_SET"}
            else None
        )

        for extraction in extractions:
            status, candidate_code = create_one_candidate(
                repository=repository,
                batch=batch,
                raw_page=raw_page,
                extraction=extraction,
                candidate_type=candidate_type,
                combo_group_code=combo_group_code,
                confirm_create=confirm_create,
                non_interactive=non_interactive,
                link_images=not multi_candidate_run,
            )

            results[status] += 1

            if status == "CREATED" and candidate_code:
                created_candidate_codes.append(candidate_code)

        return results, created_candidate_codes

    # --- automatic mode (no explicit --candidate-title) ---

    existing_candidates = get_existing_candidates_for_raw_page(
        repository=repository,
        raw_page_id=raw_page_id,
    )

    if existing_candidates:
        # Idempotent: this raw_page was already processed by a prior
        # run. Report the existing candidates instead of raising or
        # creating an unintended duplicate (Phase 8: running extraction
        # twice must not create duplicate candidates).
        print()
        print("=" * 78)
        print("EXTRACTION ALREADY COMPLETE FOR THIS POST")
        print("=" * 78)
        print(f"Rule code: {extraction_rules.EXTRACTION_DUPLICATE}")
        print(
            "This raw page already has "
            f"{len(existing_candidates)} candidate(s) from a previous "
            "extraction run."
        )

        existing_codes: list[str] = []

        for existing in existing_candidates:
            code = existing.get("candidate_code")
            print(f"  - {code} | {existing.get('extracted_title')}")

            if code:
                existing_codes.append(code)

        results["DUPLICATE_CANDIDATE"] = len(existing_candidates)
        return results, existing_codes

    extraction_run = extraction_rules.run_automatic_extraction(cleaned_text)
    decision = extraction_run.decision

    print()
    print(
        f"Post classified as: {extraction_run.post_type} "
        f"({decision.rule_code})"
    )

    if decision.outcome == Outcome.AUTO_REJECT:
        print(f"Reason: {decision.reason}")
        print("No candidate created (general/non-product post).")
        results["AUTO_REJECTED"] += 1
        return results, []

    if decision.outcome == Outcome.REVIEW_REQUIRED:
        print(f"Reason: {decision.reason}")
        print(
            "No candidate created automatically. Fallback: supply "
            "--candidate-title explicitly (and --candidate-author/"
            "--possible-isbn if known) to create it manually."
        )
        results["REVIEW_REQUIRED"] += 1
        return results, []

    if decision.outcome == Outcome.BLOCKED:
        print(f"Reason: {decision.reason}")
        print("No candidate created.")
        results["BLOCKED"] += 1
        return results, []

    # AUTO_PASS
    if len(extraction_run.candidates) > max_candidates:
        print(
            f"{len(extraction_run.candidates)} candidate(s) were "
            f"identified, exceeding --max-candidates ({max_candidates}). "
            "Refusing to create any of them in this run rather than "
            "silently truncating the list."
        )
        results["BLOCKED"] += 1
        return results, []

    extractions = [
        {
            "extracted_title": candidate.extracted_title,
            "extracted_author": candidate.extracted_author,
            "possible_isbn": candidate.possible_isbn,
            "extraction_confidence": candidate.extraction_confidence,
            "matched_pattern": candidate.matched_pattern,
            "warnings": list(candidate.warnings),
        }
        for candidate in extraction_run.candidates
    ]

    ensure_unique_extractions(extractions)

    multi_candidate_run = len(extractions) > 1

    combo_group_code = (
        f"{BATCH_CODE}-GROUP-{raw_page_id[:8]}"
        if extraction_run.post_type == extraction_rules.PostType.COMBO
        else None
    )

    for extraction, candidate in zip(extractions, extraction_run.candidates):
        status, candidate_code = create_one_candidate(
            repository=repository,
            batch=batch,
            raw_page=raw_page,
            extraction=extraction,
            candidate_type=candidate.candidate_type,
            combo_group_code=combo_group_code,
            confirm_create=confirm_create,
            non_interactive=non_interactive,
            link_images=not multi_candidate_run,
        )

        results[status] += 1

        if status == "CREATED" and candidate_code:
            created_candidate_codes.append(candidate_code)

    return results, created_candidate_codes



def main() -> None:
    """Create product candidates from one cleaned Facebook post."""
    load_dotenv()
    args = parse_arguments()

    if args.non_interactive:
        if not (
            args.raw_page_id
            or args.source_url_id
        ):
            raise RuntimeError(
                "--non-interactive requires --raw-page-id or --source-url-id."
            )

        if not args.confirm_create:
            raise RuntimeError(
                "--non-interactive requires --confirm-create."
            )

    explicit_extractions = build_explicit_extractions(
        titles=args.candidate_title,
        authors=args.candidate_author,
        isbns=args.possible_isbn,
    )

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
        raw_page_id=args.raw_page_id,
        source_url_id=args.source_url_id,
    )

    print(
        "Cleaned Facebook posts found: "
        f"{len(raw_pages)}"
    )

    selected_raw_page = select_raw_page(
        raw_pages=raw_pages,
        non_interactive=args.non_interactive,
    )

    results, candidate_codes = create_candidates_from_page(
        repository=repository,
        batch=batch,
        raw_page=selected_raw_page,
        candidate_type=args.candidate_type,
        explicit_extractions=explicit_extractions,
        confirm_create=args.confirm_create,
        non_interactive=args.non_interactive,
        max_candidates=args.max_candidates,
    )

    print()
    print("=" * 78)
    print(
        "EXTRACTION RUN SUMMARY"
    )
    print("=" * 78)

    for status, count in results.items():
        print(
            f"{status}: {count}"
        )

    print()
    print(format_candidate_codes_line(candidate_codes))

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