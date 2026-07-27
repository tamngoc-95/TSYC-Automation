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

from src.repositories.supabase_repository import SupabaseRepository


MATCHER_NAME = "candidate_identity_matcher"
MATCHER_VERSION = "1.2.0"


def utc_now() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(
        timezone.utc
    ).isoformat()


def normalize_text(
    value: str | None,
) -> str:
    """Normalize text before identity comparison."""
    if not value:
        return ""

    normalized = unicodedata.normalize(
        "NFD",
        value,
    )

    normalized = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Mn"
    )

    normalized = normalized.lower()

    normalized = re.sub(
        r"[^a-z0-9]+",
        " ",
        normalized,
    )

    return " ".join(
        normalized.split()
    )


def normalize_isbn(
    value: str | None,
) -> str:
    """Normalize ISBN by removing spaces and separators."""
    if not value:
        return ""

    return re.sub(
        r"[^0-9Xx]",
        "",
        value,
    ).upper()


def calculate_similarity(
    first_value: str | None,
    second_value: str | None,
) -> float:
    """Calculate similarity between two normalized values."""
    first_normalized = normalize_text(
        first_value
    )

    second_normalized = normalize_text(
        second_value
    )

    if not first_normalized or not second_normalized:
        return 0.0

    score = SequenceMatcher(
        None,
        first_normalized,
        second_normalized,
    ).ratio()

    return round(
        score,
        4,
    )


def get_unmatched_reference(
    repository: SupabaseRepository,
) -> dict[str, Any] | None:
    """
    Return one valid reference waiting for identity matching.

    References without source_url_id are ignored because they cannot
    be linked safely to candidate_reference_sources.
    """
    reference_response = (
        repository.client
        .table("product_references")
        .select(
            "reference_id,"
            "candidate_id,"
            "source_url_id,"
            "source_type,"
            "source_name,"
            "source_url,"
            "reference_title,"
            "reference_isbn,"
            "reference_author,"
            "reference_publisher,"
            "reference_page_count,"
            "reference_weight_grams,"
            "reference_length_cm,"
            "reference_width_cm,"
            "reference_height_cm,"
            "match_decision,"
            "match_confidence,"
            "source_priority,"
            "raw_metadata,"
            "collected_at"
        )
        .is_(
            "match_decision",
            "null",
        )
        .not_.is_(
            "source_url_id",
            "null",
        )
        .order(
            "source_priority",
            desc=False,
        )
        .order(
            "collected_at",
            desc=False,
        )
        .limit(20)
        .execute()
    )

    references = (
        reference_response.data
        or []
    )

    for reference in references:
        candidate_id = reference.get(
            "candidate_id"
        )

        source_url_id = reference.get(
            "source_url_id"
        )

        if not candidate_id:
            print(
                "Skipping reference without candidate_id: "
                f"{reference.get('reference_id')}"
            )
            continue

        if not source_url_id:
            print(
                "Skipping reference without source_url_id: "
                f"{reference.get('reference_id')}"
            )
            continue

        discovery_response = (
            repository.client
            .table(
                "candidate_reference_sources"
            )
            .select(
                "discovery_id,"
                "candidate_id,"
                "source_url_id,"
                "discovery_status,"
                "is_selected_for_crawl"
            )
            .eq(
                "candidate_id",
                candidate_id,
            )
            .eq(
                "source_url_id",
                source_url_id,
            )
            .limit(1)
            .execute()
        )

        discoveries = (
            discovery_response.data
            or []
        )

        if not discoveries:
            print(
                "Skipping reference because no matching discovery "
                "record was found: "
                f"{reference.get('reference_id')}"
            )
            continue

        discovery = discoveries[0]

        if discovery.get(
            "discovery_status"
        ) not in {
            "CRAWLED",
            "SELECTED",
        }:
            print(
                "Skipping reference with unsupported discovery status: "
                f"{discovery.get('discovery_status')}"
            )
            continue

        candidate_response = (
            repository.client
            .table("product_candidates")
            .select(
                "candidate_id,"
                "candidate_code,"
                "candidate_type,"
                "extracted_title,"
                "extracted_author,"
                "possible_isbn,"
                "verified_title,"
                "verified_isbn,"
                "verified_author,"
                "verified_publisher,"
                "verified_page_count,"
                "verified_weight_grams,"
                "verified_length_cm,"
                "verified_width_cm,"
                "verified_height_cm,"
                "identity_status,"
                "workflow_status,"
                "identity_confidence,"
                "review_required,"
                "review_reason,"
                "decision_reason,"
                "source_evidence,"
                "conflict_fields"
            )
            .eq(
                "candidate_id",
                candidate_id,
            )
            .limit(1)
            .execute()
        )

        candidates = (
            candidate_response.data
            or []
        )

        if not candidates:
            print(
                "Skipping reference because the linked candidate "
                "was not found: "
                f"{reference.get('reference_id')}"
            )
            continue

        return {
            "candidate": candidates[0],
            "reference": reference,
            "discovery": discovery,
        }

    return None


def calculate_match(
    candidate: dict[str, Any],
    reference: dict[str, Any],
) -> dict[str, Any]:
    """Compare candidate identity data with reference metadata."""
    candidate_title = candidate.get(
        "extracted_title"
    )

    reference_title = reference.get(
        "reference_title"
    )

    candidate_author = candidate.get(
        "extracted_author"
    )

    reference_author = reference.get(
        "reference_author"
    )

    candidate_isbn = normalize_isbn(
        candidate.get(
            "possible_isbn"
        )
    )

    reference_isbn = normalize_isbn(
        reference.get(
            "reference_isbn"
        )
    )

    title_similarity = calculate_similarity(
        candidate_title,
        reference_title,
    )

    author_similarity = calculate_similarity(
        candidate_author,
        reference_author,
    )

    isbn_match = bool(
        candidate_isbn
        and reference_isbn
        and candidate_isbn == reference_isbn
    )

    isbn_conflict = bool(
        candidate_isbn
        and reference_isbn
        and candidate_isbn != reference_isbn
    )

    decision = "MANUAL_REVIEW"
    confidence = 0.0
    reason = (
        "The available metadata is not sufficient "
        "for an automatic identity decision."
    )

    if isbn_conflict:
        decision = "NO_MATCH"
        confidence = 0.99
        reason = (
            "Candidate ISBN and reference ISBN are different."
        )

    elif isbn_match:
        decision = "MATCH"
        confidence = 0.99
        reason = (
            "Candidate ISBN and reference ISBN are identical."
        )

    elif (
        title_similarity >= 0.90
        and author_similarity >= 0.90
    ):
        decision = "MATCH"

        confidence = round(
            (
                title_similarity * 0.65
                + author_similarity * 0.35
            ),
            4,
        )

        reason = (
            "Title and author match strongly."
        )

    elif (
        title_similarity >= 0.90
        and (
            not normalize_text(
                candidate_author
            )
            or not normalize_text(
                reference_author
            )
        )
    ):
        decision = "POSSIBLE_MATCH"

        confidence = round(
            title_similarity * 0.85,
            4,
        )

        reason = (
            "Title matches strongly, but author data is missing."
        )

    elif (
        title_similarity >= 0.80
        and author_similarity >= 0.75
    ):
        decision = "POSSIBLE_MATCH"

        confidence = round(
            (
                title_similarity * 0.65
                + author_similarity * 0.35
            ),
            4,
        )

        reason = (
            "Title and author are similar, but the evidence "
            "is not strong enough for automatic verification."
        )

    elif title_similarity < 0.60:
        decision = "NO_MATCH"

        confidence = round(
            1 - title_similarity,
            4,
        )

        reason = (
            "Candidate title and reference title are too different."
        )

    else:
        decision = "MANUAL_REVIEW"

        confidence = round(
            (
                title_similarity * 0.65
                + author_similarity * 0.35
            ),
            4,
        )

        reason = (
            "The available metadata is not conclusive."
        )

    return {
        "match_decision": decision,
        "match_confidence": confidence,
        "match_reason": reason,
        "title_similarity": title_similarity,
        "author_similarity": author_similarity,
        "isbn_match": isbn_match,
        "isbn_conflict": isbn_conflict,
        "candidate_isbn": (
            candidate_isbn
            or None
        ),
        "reference_isbn": (
            reference_isbn
            or None
        ),
    }


def print_match_result(
    candidate: dict[str, Any],
    reference: dict[str, Any],
    discovery: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Print identity matching details."""
    print()
    print("=" * 72)
    print("IDENTITY MATCH RESULT")
    print("=" * 72)

    print(
        "Candidate code: "
        f"{candidate.get('candidate_code')}"
    )

    print(
        "Reference ID: "
        f"{reference.get('reference_id')}"
    )

    print(
        "Source URL ID: "
        f"{reference.get('source_url_id')}"
    )

    print(
        "Source type: "
        f"{reference.get('source_type')}"
    )

    print(
        "Source name: "
        f"{reference.get('source_name')}"
    )

    print(
        "Discovery status: "
        f"{discovery.get('discovery_status')}"
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
        "Title similarity: "
        f"{result['title_similarity']}"
    )

    print()
    print(
        "Candidate author: "
        f"{candidate.get('extracted_author') or '[not found]'}"
    )

    print(
        "Reference author: "
        f"{reference.get('reference_author') or '[not found]'}"
    )

    print(
        "Author similarity: "
        f"{result['author_similarity']}"
    )

    print()
    print(
        "Candidate ISBN: "
        f"{result['candidate_isbn'] or '[not found]'}"
    )

    print(
        "Reference ISBN: "
        f"{result['reference_isbn'] or '[not found]'}"
    )

    print()
    print(
        "Decision: "
        f"{result['match_decision']}"
    )

    print(
        "Confidence: "
        f"{result['match_confidence']}"
    )

    print(
        "Reason: "
        f"{result['match_reason']}"
    )


def build_source_evidence(
    candidate: dict[str, Any],
    reference: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Build candidate source evidence without deleting existing data."""
    existing_evidence = candidate.get(
        "source_evidence"
    )

    if isinstance(
        existing_evidence,
        dict,
    ):
        source_evidence = dict(
            existing_evidence
        )

    else:
        source_evidence = {}

    identity_history = source_evidence.get(
        "identity_verification_history"
    )

    if not isinstance(
        identity_history,
        list,
    ):
        identity_history = []

    identity_event = {
        "reference_id": reference.get(
            "reference_id"
        ),
        "source_url_id": reference.get(
            "source_url_id"
        ),
        "source_type": reference.get(
            "source_type"
        ),
        "source_name": reference.get(
            "source_name"
        ),
        "source_url": reference.get(
            "source_url"
        ),
        "match_decision": result[
            "match_decision"
        ],
        "match_confidence": result[
            "match_confidence"
        ],
        "match_reason": result[
            "match_reason"
        ],
        "title_similarity": result[
            "title_similarity"
        ],
        "author_similarity": result[
            "author_similarity"
        ],
        "isbn_match": result[
            "isbn_match"
        ],
        "isbn_conflict": result[
            "isbn_conflict"
        ],
        "matcher_name": MATCHER_NAME,
        "matcher_version": MATCHER_VERSION,
        "verified_at": utc_now(),
    }

    identity_history.append(
        identity_event
    )

    source_evidence[
        "identity_verification_history"
    ] = identity_history

    source_evidence[
        "latest_identity_verification"
    ] = identity_event

    return source_evidence


def build_conflict_fields(
    result: dict[str, Any],
) -> list[str]:
    """Return fields that conflict between candidate and reference."""
    conflict_fields: list[str] = []

    if result.get(
        "isbn_conflict"
    ):
        conflict_fields.append(
            "isbn"
        )

    if result.get(
        "title_similarity",
        0,
    ) < 0.60:
        conflict_fields.append(
            "title"
        )

    return conflict_fields


def update_candidate_status(
    repository: SupabaseRepository,
    candidate: dict[str, Any],
    reference: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Update candidate identity status and verified metadata."""
    candidate_id = candidate.get(
        "candidate_id"
    )

    if not candidate_id:
        raise ValueError(
            "candidate_id is required."
        )

    decision = result[
        "match_decision"
    ]

    if decision == "MATCH":
        identity_status = "IDENTITY_VERIFIED"
        workflow_status = "IDENTITY_VERIFIED"
        review_required = False
        review_reason = None
        decision_reason = result[
            "match_reason"
        ]

    elif decision in {
        "POSSIBLE_MATCH",
        "MANUAL_REVIEW",
        "DIFFERENT_EDITION",
    }:
        identity_status = "IDENTITY_PENDING"
        workflow_status = "IDENTITY_PENDING"
        review_required = True
        review_reason = result[
            "match_reason"
        ]
        decision_reason = None

    elif decision == "NO_MATCH":
        identity_status = "IDENTITY_CONFLICT"
        workflow_status = "IDENTITY_CONFLICT"
        review_required = True
        review_reason = result[
            "match_reason"
        ]
        decision_reason = None

    else:
        raise ValueError(
            "Unsupported match decision: "
            f"{decision}"
        )

    source_evidence = build_source_evidence(
        candidate=candidate,
        reference=reference,
        result=result,
    )

    payload: dict[str, Any] = {
        "identity_status": identity_status,
        "workflow_status": workflow_status,
        "identity_confidence": result[
            "match_confidence"
        ],
        "source_evidence": source_evidence,
        "review_required": review_required,
        "review_reason": review_reason,
        "decision_reason": decision_reason,
        "updated_at": utc_now(),
    }

    if decision == "MATCH":
        payload.update(
            {
                "verified_title": (
                    reference.get(
                        "reference_title"
                    )
                    or candidate.get(
                        "extracted_title"
                    )
                ),
                "verified_isbn": (
                    reference.get(
                        "reference_isbn"
                    )
                    or candidate.get(
                        "possible_isbn"
                    )
                ),
                "verified_author": (
                    reference.get(
                        "reference_author"
                    )
                    or candidate.get(
                        "extracted_author"
                    )
                ),
                "verified_publisher": (
                    reference.get(
                        "reference_publisher"
                    )
                ),
                "verified_page_count": (
                    reference.get(
                        "reference_page_count"
                    )
                ),
                "verified_weight_grams": (
                    reference.get(
                        "reference_weight_grams"
                    )
                ),
                "verified_length_cm": (
                    reference.get(
                        "reference_length_cm"
                    )
                ),
                "verified_width_cm": (
                    reference.get(
                        "reference_width_cm"
                    )
                ),
                "verified_height_cm": (
                    reference.get(
                        "reference_height_cm"
                    )
                ),
                "conflict_fields": [],
            }
        )

    elif decision == "NO_MATCH":
        payload[
            "conflict_fields"
        ] = build_conflict_fields(
            result
        )

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

    if not response.data:
        raise RuntimeError(
            "Candidate identity update returned no data."
        )


def update_product_reference(
    repository: SupabaseRepository,
    reference: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Save identity matching result to the product reference."""
    reference_id = reference.get(
        "reference_id"
    )

    if not reference_id:
        raise ValueError(
            "reference_id is required."
        )

    existing_metadata = reference.get(
        "raw_metadata"
    )

    if isinstance(
        existing_metadata,
        dict,
    ):
        raw_metadata = dict(
            existing_metadata
        )

    else:
        raw_metadata = {}

    raw_metadata[
        "identity_match"
    ] = {
        "matcher_name": MATCHER_NAME,
        "matcher_version": MATCHER_VERSION,
        "matched_at": utc_now(),
        "reason": result[
            "match_reason"
        ],
        "title_similarity": result[
            "title_similarity"
        ],
        "author_similarity": result[
            "author_similarity"
        ],
        "isbn_match": result[
            "isbn_match"
        ],
        "isbn_conflict": result[
            "isbn_conflict"
        ],
    }

    response = (
        repository.client
        .table("product_references")
        .update(
            {
                "match_decision": result[
                    "match_decision"
                ],
                "match_confidence": result[
                    "match_confidence"
                ],
                "raw_metadata": raw_metadata,
                "updated_at": utc_now(),
            }
        )
        .eq(
            "reference_id",
            reference_id,
        )
        .execute()
    )

    if not response.data:
        raise RuntimeError(
            "Product reference match update returned no data."
        )


def update_discovery_status(
    repository: SupabaseRepository,
    discovery: dict[str, Any],
    decision: str,
) -> None:
    """Update one exact candidate reference discovery record."""
    discovery_id = discovery.get(
        "discovery_id"
    )

    candidate_id = discovery.get(
        "candidate_id"
    )

    source_url_id = discovery.get(
        "source_url_id"
    )

    if not discovery_id:
        raise ValueError(
            "discovery_id is required."
        )

    if not candidate_id:
        raise ValueError(
            "candidate_id is required."
        )

    if not source_url_id:
        raise ValueError(
            "source_url_id is required."
        )

    if decision == "MATCH":
        discovery_status = "MATCHED"

    elif decision == "NO_MATCH":
        discovery_status = "REJECTED"

    else:
        discovery_status = "CRAWLED"

    response = (
        repository.client
        .table(
            "candidate_reference_sources"
        )
        .update(
            {
                "discovery_status": discovery_status,
                "reviewed_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        .eq(
            "discovery_id",
            discovery_id,
        )
        .eq(
            "candidate_id",
            candidate_id,
        )
        .eq(
            "source_url_id",
            source_url_id,
        )
        .execute()
    )

    if not response.data:
        raise RuntimeError(
            "No candidate reference discovery record was updated."
        )


def validate_queue_item(
    candidate: dict[str, Any],
    reference: dict[str, Any],
    discovery: dict[str, Any],
) -> None:
    """Validate linked IDs before any database updates."""
    candidate_id = candidate.get(
        "candidate_id"
    )

    reference_candidate_id = reference.get(
        "candidate_id"
    )

    discovery_candidate_id = discovery.get(
        "candidate_id"
    )

    reference_source_url_id = reference.get(
        "source_url_id"
    )

    discovery_source_url_id = discovery.get(
        "source_url_id"
    )

    if not candidate_id:
        raise RuntimeError(
            "The selected candidate has no candidate_id."
        )

    if not reference_source_url_id:
        raise RuntimeError(
            "The selected product reference has no source_url_id."
        )

    if not discovery_source_url_id:
        raise RuntimeError(
            "The selected discovery has no source_url_id."
        )

    if candidate_id != reference_candidate_id:
        raise RuntimeError(
            "Candidate ID and reference candidate ID do not match."
        )

    if candidate_id != discovery_candidate_id:
        raise RuntimeError(
            "Candidate ID and discovery candidate ID do not match."
        )

    if reference_source_url_id != discovery_source_url_id:
        raise RuntimeError(
            "Reference source URL ID and discovery source URL ID "
            "do not match."
        )


def main() -> None:
    """Match one candidate against one collected product reference."""
    load_dotenv()

    print("=" * 72)
    print("CANDIDATE IDENTITY MATCHER")
    print("=" * 72)
    print(
        f"Version: {MATCHER_VERSION}"
    )

    repository = SupabaseRepository()

    print(
        "Searching for an unmatched product reference..."
    )

    queue_item = get_unmatched_reference(
        repository
    )

    if queue_item is None:
        print(
            "No valid unmatched product reference was found."
        )
        return

    candidate = queue_item[
        "candidate"
    ]

    reference = queue_item[
        "reference"
    ]

    discovery = queue_item[
        "discovery"
    ]

    validate_queue_item(
        candidate=candidate,
        reference=reference,
        discovery=discovery,
    )

    result = calculate_match(
        candidate=candidate,
        reference=reference,
    )

    print_match_result(
        candidate=candidate,
        reference=reference,
        discovery=discovery,
        result=result,
    )

    print()

    confirmation = input(
        "Type SAVE to save this identity result, "
        "or press Enter to cancel: "
    ).strip().upper()

    if confirmation != "SAVE":
        print(
            "Identity result was not saved."
        )
        return

    # Update the candidate first because it has the strictest
    # workflow and identity-status constraints.
    update_candidate_status(
        repository=repository,
        candidate=candidate,
        reference=reference,
        result=result,
    )

    print(
        "Candidate identity status updated."
    )

    update_product_reference(
        repository=repository,
        reference=reference,
        result=result,
    )

    print(
        "Product reference match result updated."
    )

    update_discovery_status(
        repository=repository,
        discovery=discovery,
        decision=result[
            "match_decision"
        ],
    )

    print(
        "Discovery status updated."
    )

    print()
    print(
        "Identity matching completed successfully."
    )


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print()
        print(
            "Identity matching was cancelled by the user."
        )
        sys.exit(130)

    except Exception as error:
        print()
        print(
            "Identity matching failed."
        )
        print(
            f"Error type: {type(error).__name__}"
        )
        print(
            f"Error details: {error}"
        )
        sys.exit(1)