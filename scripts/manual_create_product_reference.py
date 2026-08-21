import argparse
import re
import sys
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from src.repositories.supabase_repository import SupabaseRepository
import register_reference_source


BATCH_CODE = "FB-2026-001"

COLLECTOR_NAME = "manual_product_reference_collector"
COLLECTOR_VERSION = "0.9.0"

SOURCE_TYPES = (
    "PUBLISHER",
    "AUTHORIZED_SUPPLIER",
    "BOOKSTORE",
    "FAHASA",
    "FACEBOOK",
    "OTHER",
)

DEFAULT_SOURCE_TYPE_NUMBER = 4

SOURCE_NAME_DEFAULTS = {
    "PUBLISHER": "Publisher",
    "AUTHORIZED_SUPPLIER": "Authorized Supplier",
    "BOOKSTORE": "Bookstore",
    "FAHASA": "Fahasa",
    "FACEBOOK": "Facebook",
    "OTHER": "Other",
}

SOURCE_PRIORITIES = {
    "PUBLISHER": 1,
    "AUTHORIZED_SUPPLIER": 2,
    "BOOKSTORE": 3,
    "FAHASA": 4,
    "FACEBOOK": 5,
    "OTHER": 9,
}

ALLOWED_MATCH_DECISIONS = (
    "MATCH",
    "POSSIBLE_MATCH",
    "DIFFERENT_EDITION",
    "NO_MATCH",
    "MANUAL_REVIEW",
)


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Manually create one product reference and optionally update "
            "one TSYC product candidate."
        )
    )

    selector_group = parser.add_mutually_exclusive_group()

    selector_group.add_argument(
        "--candidate-code",
        help="Exact candidate code.",
    )

    selector_group.add_argument(
        "--candidate-id",
        help="Exact candidate UUID.",
    )

    parser.add_argument(
        "--confirm-save",
        action="store_true",
        help="Confirm the final SAVE action without an interactive prompt.",
    )

    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help=(
            "Disable candidate/save prompts. An exact candidate selector "
            "and --confirm-save are required. Metadata entry remains manual "
            "unless this script is extended with explicit field arguments."
        ),
    )

    return parser.parse_args()


def normalize_whitespace(
    value: str | None,
) -> str:
    """Normalize whitespace in one text value."""
    if not value:
        return ""

    return " ".join(
        value.split()
    ).strip()


def remove_accents(
    value: str,
) -> str:
    """Remove accents for identity comparison."""
    normalized = unicodedata.normalize(
        "NFD",
        value,
    )

    return "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Mn"
    )


def normalize_comparison_text(
    value: str | None,
) -> str:
    """Normalize title or author text for comparison."""
    if not value:
        return ""

    normalized = remove_accents(
        value
    ).lower()

    normalized = re.sub(
        r"[^a-z0-9]+",
        " ",
        normalized,
    )

    return normalize_whitespace(
        normalized
    )


def normalize_isbn(
    value: str | None,
) -> str | None:
    """Normalize ISBN-10 or ISBN-13."""
    if not value:
        return None

    normalized = re.sub(
        r"[^0-9Xx]",
        "",
        value,
    ).upper()

    if len(normalized) in {
        10,
        13,
    }:
        return normalized

    return None


def calculate_similarity(
    first_value: str | None,
    second_value: str | None,
) -> float | None:
    """Calculate normalized text similarity."""
    first_normalized = normalize_comparison_text(
        first_value
    )

    second_normalized = normalize_comparison_text(
        second_value
    )

    if (
        not first_normalized
        or not second_normalized
    ):
        return None

    return SequenceMatcher(
        None,
        first_normalized,
        second_normalized,
    ).ratio()


def prompt_optional_text(
    prompt: str,
    default_value: str | None = None,
) -> str | None:
    """Read optional text with an optional default."""
    if default_value:
        displayed_prompt = (
            f"{prompt} "
            f"[default: {default_value}]: "
        )
    else:
        displayed_prompt = (
            f"{prompt} "
            "[optional]: "
        )

    entered_value = input(
        displayed_prompt
    ).strip()

    if entered_value:
        return entered_value

    return default_value


def prompt_required_text(
    prompt: str,
) -> str:
    """Read a required non-empty text value."""
    while True:
        value = input(
            f"{prompt}: "
        ).strip()

        if value:
            return value

        print(
            f"{prompt} is required. "
            "Please enter a value."
        )


def prompt_optional_integer(
    prompt: str,
) -> int | None:
    """Read an optional positive integer."""
    while True:
        value = input(
            f"{prompt} [optional]: "
        ).strip()

        if not value:
            return None

        try:
            result = int(
                value
            )

        except ValueError:
            print(
                "Please enter a whole number "
                "or press Enter to skip."
            )
            continue

        if result <= 0:
            print(
                "The value must be greater than zero."
            )
            continue

        return result


def prompt_optional_decimal(
    prompt: str,
) -> float | None:
    """Read an optional non-negative decimal."""
    while True:
        value = input(
            f"{prompt} [optional]: "
        ).strip()

        if not value:
            return None

        normalized_value = value.replace(
            ",",
            ".",
        )

        try:
            result = float(
                normalized_value
            )

        except ValueError:
            print(
                "Please enter a numeric value "
                "or press Enter to skip."
            )
            continue

        if result < 0:
            print(
                "The value cannot be negative."
            )
            continue

        return result


def prompt_optional_isbn() -> str | None:
    """Read and validate an optional ISBN."""
    while True:
        raw_value = input(
            "Reference ISBN [optional]: "
        ).strip()

        if not raw_value:
            return None

        normalized_value = normalize_isbn(
            raw_value
        )

        if normalized_value:
            return normalized_value

        print(
            "ISBN must contain 10 or 13 valid "
            "ISBN characters. Try again or press Enter to skip."
        )


def get_batch(
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


def get_candidates(
    repository: SupabaseRepository,
    batch_id: str,
) -> list[dict[str, Any]]:
    """Return candidates available for identity verification."""
    response = (
        repository.client
        .table("product_candidates")
        .select(
            "candidate_id, "
            "candidate_code, "
            "candidate_type, "
            "extracted_title, "
            "extracted_author, "
            "possible_isbn, "
            "verified_title, "
            "verified_isbn, "
            "verified_author, "
            "verified_publisher, "
            "identity_status, "
            "workflow_status, "
            "review_required"
        )
        .eq(
            "batch_id",
            batch_id,
        )
        .neq(
            "workflow_status",
            "REJECTED",
        )
        .order(
            "candidate_code",
            desc=False,
        )
        .execute()
    )

    return response.data or []


def resolve_candidate(
    candidates: list[dict[str, Any]],
    candidate_code: str | None,
    candidate_id: str | None,
    non_interactive: bool,
) -> dict[str, Any]:
    """Resolve exactly one candidate or fall back to manual selection."""
    if candidate_code or candidate_id:
        matches = [
            candidate
            for candidate in candidates
            if (
                candidate_code
                and candidate.get("candidate_code") == candidate_code
            )
            or (
                candidate_id
                and str(candidate.get("candidate_id")) == str(candidate_id)
            )
        ]

        if len(matches) != 1:
            raise RuntimeError(
                "Candidate selector did not resolve to exactly one record."
            )

        return matches[0]

    if non_interactive:
        raise RuntimeError(
            "--non-interactive requires --candidate-code or --candidate-id."
        )

    return select_candidate(
        candidates
    )


def select_candidate(
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Allow the user to select one candidate."""
    if not candidates:
        raise RuntimeError(
            "No product candidates were found."
        )

    print()
    print("Available product candidates:")

    for index, candidate in enumerate(
        candidates,
        start=1,
    ):
        print()
        print(
            f"[{index}] "
            f"{candidate.get('candidate_code')}"
        )
        print(
            "    Title: "
            f"{candidate.get('extracted_title')}"
        )
        print(
            "    Author: "
            f"{candidate.get('extracted_author') or '[not found]'}"
        )
        print(
            "    Identity status: "
            f"{candidate.get('identity_status')}"
        )

    while True:
        print()

        selection = input(
            "Enter the candidate number "
            "[default: 1]: "
        ).strip()

        if not selection:
            return candidates[0]

        try:
            selected_index = int(
                selection
            ) - 1

        except ValueError:
            print(
                "Please enter a numeric candidate number."
            )
            continue

        if (
            selected_index < 0
            or selected_index >= len(candidates)
        ):
            print(
                "The selected candidate number is "
                "outside the available range."
            )
            continue

        return candidates[selected_index]


def select_source_type() -> str:
    """Allow the user to select a valid reference source type."""
    print()
    print("Reference source types:")

    for index, source_type in enumerate(
        SOURCE_TYPES,
        start=1,
    ):
        default_marker = (
            " [default]"
            if index == DEFAULT_SOURCE_TYPE_NUMBER
            else ""
        )

        print(
            f"[{index}] "
            f"{source_type}"
            f"{default_marker}"
        )

    while True:
        selection = input(
            "Enter the source type number "
            "[default: 4 - FAHASA]: "
        ).strip()

        if not selection:
            return SOURCE_TYPES[
                DEFAULT_SOURCE_TYPE_NUMBER - 1
            ]

        try:
            selected_index = int(
                selection
            ) - 1

        except ValueError:
            print(
                "Please enter a number from "
                f"1 to {len(SOURCE_TYPES)}."
            )
            continue

        if (
            selected_index < 0
            or selected_index >= len(SOURCE_TYPES)
        ):
            print(
                "The selected source type is "
                "outside the available range."
            )
            continue

        return SOURCE_TYPES[selected_index]


def collect_reference_input(
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Collect reference metadata from the reviewer."""
    print()
    print("=" * 78)
    print("REFERENCE DATA ENTRY")
    print("=" * 78)
    print(
        "Candidate: "
        f"{candidate.get('candidate_code')}"
    )
    print(
        "Extracted title: "
        f"{candidate.get('extracted_title')}"
    )
    print(
        "Extracted author: "
        f"{candidate.get('extracted_author') or '[not found]'}"
    )

    source_type = select_source_type()

    default_source_name = (
        SOURCE_NAME_DEFAULTS[source_type]
    )

    print()
    print(
        "Enter metadata exactly as shown "
        "on the selected reference page."
    )
    print(
        "Fields marked optional may be skipped "
        "by pressing Enter."
    )
    print()

    source_name = prompt_optional_text(
        "Source name",
        default_source_name,
    )

    source_url = prompt_required_text(
        "Source URL"
    )

    reference_title = prompt_required_text(
        "Reference title"
    )

    reference_author = prompt_optional_text(
        "Reference author"
    )

    reference_isbn = prompt_optional_isbn()

    reference_publisher = prompt_optional_text(
        "Reference publisher"
    )

    reference_page_count = (
        prompt_optional_integer(
            "Reference page count"
        )
    )

    reference_weight_grams = (
        prompt_optional_decimal(
            "Reference weight in grams"
        )
    )

    reference_length_cm = (
        prompt_optional_decimal(
            "Reference length in cm"
        )
    )

    reference_width_cm = (
        prompt_optional_decimal(
            "Reference width in cm"
        )
    )

    reference_height_cm = (
        prompt_optional_decimal(
            "Reference height in cm"
        )
    )

    reference_cover_price_vnd = (
        prompt_optional_decimal(
            "Reference cover price in VND"
        )
    )

    reference_description = (
        prompt_optional_text(
            "Short reference description"
        )
    )

    reference_image_url = (
        prompt_optional_text(
            "Reference image URL"
        )
    )

    return {
        "source_type": source_type,
        "source_name": source_name,
        "source_url": source_url,
        "reference_title": reference_title,
        "reference_isbn": reference_isbn,
        "reference_author": reference_author,
        "reference_publisher": reference_publisher,
        "reference_page_count": reference_page_count,
        "reference_weight_grams": reference_weight_grams,
        "reference_length_cm": reference_length_cm,
        "reference_width_cm": reference_width_cm,
        "reference_height_cm": reference_height_cm,
        "reference_cover_price_vnd": reference_cover_price_vnd,
        "reference_description": reference_description,
        "reference_image_url": reference_image_url,
    }


def evaluate_reference_match(
    candidate: dict[str, Any],
    reference: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate candidate-reference similarity conservatively."""
    candidate_title = (
        candidate.get("verified_title")
        or candidate.get("extracted_title")
    )

    candidate_author = (
        candidate.get("verified_author")
        or candidate.get("extracted_author")
    )

    candidate_isbn = normalize_isbn(
        candidate.get("verified_isbn")
        or candidate.get("possible_isbn")
    )

    reference_isbn = normalize_isbn(
        reference.get("reference_isbn")
    )

    title_similarity = calculate_similarity(
        candidate_title,
        reference.get("reference_title"),
    )

    author_similarity = calculate_similarity(
        candidate_author,
        reference.get("reference_author"),
    )

    isbn_exact_match: bool | None = None

    if candidate_isbn and reference_isbn:
        isbn_exact_match = (
            candidate_isbn == reference_isbn
        )

    evidence_scores: list[float] = []

    if title_similarity is not None:
        evidence_scores.append(
            title_similarity
        )

    if author_similarity is not None:
        evidence_scores.append(
            author_similarity
        )

    if isbn_exact_match is True:
        evidence_scores.append(
            1.0
        )

    if isbn_exact_match is False:
        evidence_scores.append(
            0.0
        )

    if evidence_scores:
        match_confidence = sum(
            evidence_scores
        ) / len(evidence_scores)
    else:
        match_confidence = 0.0

    source_type = reference[
        "source_type"
    ]

    if isbn_exact_match is False:
        match_decision = "DIFFERENT_EDITION"

    elif (
        isbn_exact_match is True
        and title_similarity is not None
        and title_similarity >= 0.85
    ):
        match_decision = "MATCH"

    elif (
        title_similarity is not None
        and title_similarity >= 0.90
        and author_similarity is not None
        and author_similarity >= 0.80
        and source_type in {
            "PUBLISHER",
            "AUTHORIZED_SUPPLIER",
            "BOOKSTORE",
            "FAHASA",
        }
    ):
        match_decision = "MATCH"

    elif (
        title_similarity is not None
        and title_similarity >= 0.70
    ):
        match_decision = "POSSIBLE_MATCH"

    elif title_similarity is None:
        match_decision = "MANUAL_REVIEW"

    else:
        match_decision = "NO_MATCH"

    return {
        "match_decision": match_decision,
        "match_confidence": round(
            match_confidence,
            4,
        ),
        "title_similarity": title_similarity,
        "author_similarity": author_similarity,
        "isbn_exact_match": isbn_exact_match,
    }


def print_match_preview(
    candidate: dict[str, Any],
    reference: dict[str, Any],
    evaluation: dict[str, Any],
) -> None:
    """Print reference comparison before saving."""
    print()
    print("=" * 78)
    print("REFERENCE MATCH PREVIEW")
    print("=" * 78)

    print(
        "Source type: "
        f"{reference.get('source_type')}"
    )
    print(
        "Source name: "
        f"{reference.get('source_name')}"
    )
    print(
        "Source URL: "
        f"{reference.get('source_url')}"
    )
    print()

    print(
        "Candidate title: "
        f"{candidate.get('extracted_title')}"
    )
    print(
        "Reference title: "
        f"{reference.get('reference_title')}"
    )
    print(
        "Candidate author: "
        f"{candidate.get('extracted_author') or '[not provided]'}"
    )
    print(
        "Reference author: "
        f"{reference.get('reference_author') or '[not provided]'}"
    )
    print(
        "Candidate ISBN: "
        f"{candidate.get('possible_isbn') or '[not provided]'}"
    )
    print(
        "Reference ISBN: "
        f"{reference.get('reference_isbn') or '[not provided]'}"
    )

    title_similarity = evaluation.get(
        "title_similarity"
    )

    author_similarity = evaluation.get(
        "author_similarity"
    )

    print()
    print(
        "Title similarity: "
        + (
            f"{title_similarity:.4f}"
            if title_similarity is not None
            else "[not comparable]"
        )
    )

    print(
        "Author similarity: "
        + (
            f"{author_similarity:.4f}"
            if author_similarity is not None
            else "[not comparable]"
        )
    )

    print(
        "ISBN exact match: "
        f"{evaluation.get('isbn_exact_match')}"
    )
    print(
        "Proposed decision: "
        f"{evaluation.get('match_decision')}"
    )
    print(
        "Proposed confidence: "
        f"{evaluation.get('match_confidence'):.4f}"
    )


def confirm_match_decision(
    proposed_decision: str,
) -> str:
    """Allow the reviewer to accept or override a decision."""
    print()
    print(
        "Available match decisions:"
    )

    for decision in ALLOWED_MATCH_DECISIONS:
        print(
            f"  - {decision}"
        )

    while True:
        selected_value = input(
            "Match decision "
            f"[default: {proposed_decision}]: "
        ).strip().upper()

        if not selected_value:
            return proposed_decision

        if selected_value in ALLOWED_MATCH_DECISIONS:
            return selected_value

        print(
            "Invalid match decision. "
            "Please enter one of the listed values."
        )


def find_duplicate_reference(
    repository: SupabaseRepository,
    candidate_id: str,
    source_type: str,
    source_url: str,
) -> dict[str, Any] | None:
    """Find an existing reference for the same source URL."""
    response = (
        repository.client
        .table("product_references")
        .select(
            "reference_id, "
            "candidate_id, "
            "source_type, "
            "source_name, "
            "source_url, "
            "reference_isbn, "
            "match_decision"
        )
        .eq(
            "candidate_id",
            candidate_id,
        )
        .eq(
            "source_type",
            source_type,
        )
        .eq(
            "source_url",
            source_url,
        )
        .limit(1)
        .execute()
    )

    records = response.data or []

    if not records:
        return None

    return records[0]


def build_reference_payload(
    candidate: dict[str, Any],
    reference: dict[str, Any],
    evaluation: dict[str, Any],
    match_decision: str,
) -> dict[str, Any]:
    """Build a valid product_references payload."""
    collected_at = datetime.now(
        timezone.utc
    ).isoformat()

    raw_metadata = {
        "collector_name": COLLECTOR_NAME,
        "collector_version": COLLECTOR_VERSION,
        "candidate_code": candidate.get(
            "candidate_code"
        ),
        "comparison": {
            "title_similarity": evaluation.get(
                "title_similarity"
            ),
            "author_similarity": evaluation.get(
                "author_similarity"
            ),
            "isbn_exact_match": evaluation.get(
                "isbn_exact_match"
            ),
            "proposed_match_decision": evaluation.get(
                "match_decision"
            ),
            "reviewed_match_decision": match_decision,
        },
        "entered_reference": reference,
        "purchase_price_eligible": False,
        "purchase_price_policy_note": (
            "Manual public/reference metadata is not an official purchase "
            "price source. Purchase price must come from invoice, confirmed "
            "purchase order, supplier quotation, or current supplier price list."
        ),
        "collected_at": collected_at,
    }

    return {
        "candidate_id": candidate[
            "candidate_id"
        ],
        "source_url_id": reference[
            "source_url_id"
        ],
        "source_type": reference[
            "source_type"
        ],
        "source_name": reference.get(
            "source_name"
        ),
        "source_url": reference.get(
            "source_url"
        ),
        "reference_title": reference.get(
            "reference_title"
        ),
        "reference_isbn": reference.get(
            "reference_isbn"
        ),
        "reference_author": reference.get(
            "reference_author"
        ),
        "reference_publisher": reference.get(
            "reference_publisher"
        ),
        "reference_page_count": reference.get(
            "reference_page_count"
        ),
        "reference_weight_grams": reference.get(
            "reference_weight_grams"
        ),
        "reference_length_cm": reference.get(
            "reference_length_cm"
        ),
        "reference_width_cm": reference.get(
            "reference_width_cm"
        ),
        "reference_height_cm": reference.get(
            "reference_height_cm"
        ),
        # Cover price is reference metadata only. It is never purchase price.
        "reference_cover_price_vnd": reference.get(
            "reference_cover_price_vnd"
        ),
        "reference_description": reference.get(
            "reference_description"
        ),
        "reference_image_url": reference.get(
            "reference_image_url"
        ),
        "match_decision": match_decision,
        "match_confidence": evaluation.get(
            "match_confidence"
        ),
        "source_priority": SOURCE_PRIORITIES[
            reference["source_type"]
        ],
        "raw_metadata": raw_metadata,
        "collected_at": collected_at,
    }


def insert_reference(
    repository: SupabaseRepository,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Insert one product reference."""
    response = (
        repository.client
        .table("product_references")
        .insert(payload)
        .execute()
    )

    records = response.data or []

    if not records:
        raise RuntimeError(
            "Supabase returned no product reference "
            "after insert."
        )

    return records[0]


def validate_candidate_update_allowed(
    candidate: dict[str, Any],
    match_decision: str,
) -> None:
    """
    Protect already verified candidates from accidental manual overwrite.

    Additional references may be saved for evidence, but a verified candidate
    must not be rewritten by this legacy/manual collector. Re-verification
    belongs in the standardized identity-matching workflow.
    """
    if candidate.get("identity_status") != "IDENTITY_VERIFIED":
        return

    if match_decision == "MATCH":
        raise RuntimeError(
            "Candidate is already IDENTITY_VERIFIED. Saving another MATCH "
            "reference is allowed only through the standardized identity "
            "matching/re-verification workflow, not this manual collector."
        )

    raise RuntimeError(
        "Candidate is already IDENTITY_VERIFIED. This manual reference "
        "workflow cannot downgrade or conflict an already verified identity."
    )


def build_candidate_update(
    reference: dict[str, Any],
    match_decision: str,
    match_confidence: float,
) -> dict[str, Any]:
    """Build candidate changes after reference review."""
    updated_at = datetime.now(
        timezone.utc
    ).isoformat()

    if match_decision == "MATCH":
        return {
            "verified_title": reference.get(
                "reference_title"
            ),
            "verified_isbn": reference.get(
                "reference_isbn"
            ),
            "verified_author": reference.get(
                "reference_author"
            ),
            "verified_publisher": reference.get(
                "reference_publisher"
            ),
            "verified_page_count": reference.get(
                "reference_page_count"
            ),
            "verified_weight_grams": reference.get(
                "reference_weight_grams"
            ),
            "verified_length_cm": reference.get(
                "reference_length_cm"
            ),
            "verified_width_cm": reference.get(
                "reference_width_cm"
            ),
            "verified_height_cm": reference.get(
                "reference_height_cm"
            ),
            "identity_status": "IDENTITY_VERIFIED",
            "workflow_status": "IDENTITY_VERIFIED",
            "identity_confidence": match_confidence,
            "review_required": False,
            "review_reason": None,
            "decision_reason": (
                "Identity verified using a matching "
                "product reference."
            ),
            "updated_at": updated_at,
        }

    if match_decision in {
        "POSSIBLE_MATCH",
        "MANUAL_REVIEW",
    }:
        return {
            "identity_status": "IDENTITY_PENDING",
            "workflow_status": "IDENTITY_PENDING",
            "identity_confidence": match_confidence,
            "review_required": True,
            "review_reason": (
                "The saved reference requires "
                "additional identity review."
            ),
            "decision_reason": (
                f"Reference decision: {match_decision}."
            ),
            "updated_at": updated_at,
        }

    return {
        "identity_status": "IDENTITY_CONFLICT",
        "workflow_status": "IDENTITY_CONFLICT",
        "identity_confidence": match_confidence,
        "review_required": True,
        "review_reason": (
            "The reference conflicts with the candidate identity."
        ),
        "decision_reason": (
            f"Reference decision: {match_decision}."
        ),
        "updated_at": updated_at,
    }


def update_candidate(
    repository: SupabaseRepository,
    candidate_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Update one product candidate."""
    response = (
        repository.client
        .table("product_candidates")
        .update(payload)
        .eq(
            "candidate_id",
            candidate_id,
        )
        .execute()
    )

    records = response.data or []

    if not records:
        raise RuntimeError(
            "Candidate update returned no record."
        )

    return records[0]


def verify_reference(
    repository: SupabaseRepository,
    reference_id: str,
) -> dict[str, Any]:
    """Read the inserted reference back."""
    response = (
        repository.client
        .table("product_references")
        .select(
            "reference_id, "
            "candidate_id, "
            "source_type, "
            "source_name, "
            "source_url, "
            "reference_title, "
            "reference_isbn, "
            "reference_author, "
            "reference_publisher, "
            "match_decision, "
            "match_confidence, "
            "source_priority"
        )
        .eq(
            "reference_id",
            reference_id,
        )
        .limit(1)
        .execute()
    )

    records = response.data or []

    if not records:
        raise RuntimeError(
            "Reference verification failed."
        )

    return records[0]


def verify_candidate(
    repository: SupabaseRepository,
    candidate_id: str,
) -> dict[str, Any]:
    """Read the updated candidate back."""
    response = (
        repository.client
        .table("product_candidates")
        .select(
            "candidate_id, "
            "candidate_code, "
            "extracted_title, "
            "verified_title, "
            "verified_isbn, "
            "verified_author, "
            "verified_publisher, "
            "identity_status, "
            "workflow_status, "
            "identity_confidence, "
            "review_required, "
            "review_reason"
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
            "Candidate verification failed."
        )

    return records[0]


def delete_reference(
    repository: SupabaseRepository,
    reference_id: str,
) -> None:
    """Delete a reference during rollback."""
    (
        repository.client
        .table("product_references")
        .delete()
        .eq(
            "reference_id",
            reference_id,
        )
        .execute()
    )


def resolve_registered_source(
    repository: SupabaseRepository,
    batch_id: str,
    source_type: str,
    raw_source_url: str,
) -> dict[str, Any]:
    """
    Resolve a manually entered source URL to an existing registered
    source_urls row.

    This enforces the provenance invariant required later by
    create_internal_product.py: a product_references row must trace back
    to a source_urls row that was already registered (and authorized)
    through register_reference_source.py. This function never creates a
    source_urls row itself and never allows a caller to fall back to
    source_url_id = None.
    """
    normalized_url = register_reference_source.normalize_url(
        raw_source_url
    )

    existing_source = (
        register_reference_source.find_existing_source_url(
            repository=repository,
            batch_id=batch_id,
            source_type=source_type,
            source_url=normalized_url,
        )
    )

    if existing_source is None:
        raise RuntimeError(
            "No registered source_urls row was found for "
            f"source_type={source_type!r} and URL={normalized_url!r}. "
            "Run register_reference_source.py first to register and "
            "authorize this source, then re-run this script."
        )

    if existing_source.get("source_type") != source_type:
        raise RuntimeError(
            "The registered source_urls row has a different source_type "
            f"({existing_source.get('source_type')!r}) than the requested "
            f"source_type ({source_type!r}). Re-register the source with "
            "the correct source_type before creating a manual reference."
        )

    if existing_source.get("is_authorized") is not True:
        raise RuntimeError(
            "The registered source_urls row is not authorized "
            f"(source_url_id={existing_source.get('source_url_id')}). "
            "Run register_reference_source.py with --authorized to "
            "approve this source before creating a manual product "
            "reference for it."
        )

    return existing_source


def main() -> None:
    """Create one reference and update one candidate."""
    load_dotenv()
    args = parse_arguments()

    if args.non_interactive:
        if not (
            args.candidate_code
            or args.candidate_id
        ):
            raise RuntimeError(
                "--non-interactive requires an exact candidate selector."
            )

        if not args.confirm_save:
            raise RuntimeError(
                "--non-interactive requires --confirm-save."
            )

    print(
        "Product reference workflow started."
    )
    print(
        f"Version: {COLLECTOR_VERSION}"
    )
    print(
        f"Batch: {BATCH_CODE}"
    )

    repository = SupabaseRepository()

    batch = get_batch(
        repository
    )

    candidates = get_candidates(
        repository=repository,
        batch_id=batch["batch_id"],
    )

    print(
        "Candidates found: "
        f"{len(candidates)}"
    )

    candidate = resolve_candidate(
        candidates=candidates,
        candidate_code=args.candidate_code,
        candidate_id=args.candidate_id,
        non_interactive=args.non_interactive,
    )

    reference = collect_reference_input(
        candidate
    )

    resolved_source = resolve_registered_source(
        repository=repository,
        batch_id=batch["batch_id"],
        source_type=reference["source_type"],
        raw_source_url=reference["source_url"],
    )

    reference["source_url"] = resolved_source["source_url"]
    reference["source_url_id"] = resolved_source["source_url_id"]

    evaluation = evaluate_reference_match(
        candidate=candidate,
        reference=reference,
    )

    print_match_preview(
        candidate=candidate,
        reference=reference,
        evaluation=evaluation,
    )

    match_decision = confirm_match_decision(
        proposed_decision=evaluation[
            "match_decision"
        ],
    )

    validate_candidate_update_allowed(
        candidate=candidate,
        match_decision=match_decision,
    )

    duplicate_reference = (
        find_duplicate_reference(
            repository=repository,
            candidate_id=candidate[
                "candidate_id"
            ],
            source_type=reference[
                "source_type"
            ],
            source_url=reference[
                "source_url"
            ],
        )
    )

    if duplicate_reference is not None:
        print()
        print("=" * 78)
        print("DUPLICATE REFERENCE")
        print("=" * 78)
        print(
            "Reference ID: "
            f"{duplicate_reference.get('reference_id')}"
        )
        print(
            "Source type: "
            f"{duplicate_reference.get('source_type')}"
        )
        print(
            "Source URL: "
            f"{duplicate_reference.get('source_url')}"
        )
        print(
            "Reference ISBN: "
            f"{duplicate_reference.get('reference_isbn')}"
        )
        print(
            "Match decision: "
            f"{duplicate_reference.get('match_decision')}"
        )
        return

    print()

    if args.confirm_save:
        confirmation = "SAVE"

    elif args.non_interactive:
        confirmation = ""

    else:
        confirmation = input(
            "Type SAVE to create this reference and "
            "update the candidate, or press Enter to cancel: "
        ).strip().upper()

    if confirmation != "SAVE":
        print(
            "Reference creation cancelled."
        )
        return

    reference_payload = (
        build_reference_payload(
            candidate=candidate,
            reference=reference,
            evaluation=evaluation,
            match_decision=match_decision,
        )
    )

    inserted_reference = insert_reference(
        repository=repository,
        payload=reference_payload,
    )

    reference_id = str(
        inserted_reference["reference_id"]
    )

    print()
    print(
        "Product reference created."
    )
    print(
        f"Reference ID: {reference_id}"
    )

    try:
        candidate_update = (
            build_candidate_update(
                reference=reference,
                match_decision=match_decision,
                match_confidence=evaluation[
                    "match_confidence"
                ],
            )
        )

        update_candidate(
            repository=repository,
            candidate_id=candidate[
                "candidate_id"
            ],
            payload=candidate_update,
        )

        verified_reference = (
            verify_reference(
                repository=repository,
                reference_id=reference_id,
            )
        )

        verified_candidate = (
            verify_candidate(
                repository=repository,
                candidate_id=candidate[
                    "candidate_id"
                ],
            )
        )

    except Exception:
        print()
        print(
            "Candidate update failed. "
            "Removing the newly inserted reference..."
        )

        try:
            delete_reference(
                repository=repository,
                reference_id=reference_id,
            )

            print(
                "Reference rollback completed."
            )

        except Exception as rollback_error:
            print(
                "Warning: reference rollback failed."
            )
            print(
                "Rollback details: "
                f"{rollback_error}"
            )

        raise

    print()
    print("=" * 78)
    print("REFERENCE WORKFLOW RESULT")
    print("=" * 78)
    print(
        "Reference ID: "
        f"{verified_reference.get('reference_id')}"
    )
    print(
        "Source type: "
        f"{verified_reference.get('source_type')}"
    )
    print(
        "Source name: "
        f"{verified_reference.get('source_name')}"
    )
    print(
        "Reference title: "
        f"{verified_reference.get('reference_title')}"
    )
    print(
        "Reference ISBN: "
        f"{verified_reference.get('reference_isbn')}"
    )
    print(
        "Match decision: "
        f"{verified_reference.get('match_decision')}"
    )
    print(
        "Match confidence: "
        f"{verified_reference.get('match_confidence')}"
    )

    print()
    print(
        "Candidate code: "
        f"{verified_candidate.get('candidate_code')}"
    )
    print(
        "Verified title: "
        f"{verified_candidate.get('verified_title')}"
    )
    print(
        "Verified ISBN: "
        f"{verified_candidate.get('verified_isbn')}"
    )
    print(
        "Verified author: "
        f"{verified_candidate.get('verified_author')}"
    )
    print(
        "Verified publisher: "
        f"{verified_candidate.get('verified_publisher')}"
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

    print()
    print(
        "Product reference workflow finished."
    )


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print()
        print(
            "Reference workflow was cancelled."
        )
        sys.exit(130)

    except Exception as error:
        print()
        print(
            "Product reference workflow failed."
        )
        print(
            f"Error type: {type(error).__name__}"
        )
        print(
            f"Error details: {error}"
        )
        sys.exit(1)