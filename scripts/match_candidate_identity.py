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

from src.cli_bootstrap import configure_utf8_console
from src.domain.identity_status import IdentityStatus, MatchDecision
from src.repositories.supabase_repository import SupabaseRepository

configure_utf8_console()


MATCHER_NAME = "candidate_identity_matcher"
MATCHER_VERSION = "1.4.1"

VALID_CONFIRMATIONS = {
    "SAVE",
    "APPLY",
    "CONFIRM",
    "SAVE_RESULT",
}


def utc_now() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(
        timezone.utc
    ).isoformat()


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
            "Match one product candidate identity using single-source "
            "or multi-source evidence."
        )
    )
    parser.add_argument(
        "--candidate-code",
        help="Process one exact candidate code.",
    )
    parser.add_argument(
        "--candidate-id",
        help="Process one exact candidate UUID.",
    )
    parser.add_argument(
        "--reference-id",
        help=(
            "Process one exact unmatched reference. This forces "
            "single-reference mode."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=["AUTO", "CONSENSUS", "SINGLE"],
        default="AUTO",
        help=(
            "AUTO prefers multi-source consensus, CONSENSUS requires at least "
            "two references, SINGLE processes one unmatched reference."
        ),
    )
    parser.add_argument(
        "--confirm-save",
        action="store_true",
        help="Save the calculated identity result without prompting.",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help=(
            "Disable input prompts. Requires a candidate selector and "
            "--confirm-save."
        ),
    )
    return parser.parse_args()


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
    candidate_code: str | None = None,
    candidate_id: str | None = None,
    reference_id: str | None = None,
) -> dict[str, Any] | None:
    """Return one valid unmatched reference for one exact or queued candidate."""
    query = (
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
        .is_("match_decision", "null")
        .not_.is_("source_url_id", "null")
    )

    if candidate_id:
        query = query.eq("candidate_id", candidate_id)

    if reference_id:
        query = query.eq("reference_id", reference_id)

    reference_response = (
        query
        .order("source_priority", desc=False)
        .order("collected_at", desc=False)
        .limit(50)
        .execute()
    )

    references = reference_response.data or []

    for reference in references:
        current_candidate_id = reference.get("candidate_id")
        source_url_id = reference.get("source_url_id")

        if not current_candidate_id or not source_url_id:
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
            .eq("candidate_id", current_candidate_id)
            .limit(1)
            .execute()
        )

        candidates = candidate_response.data or []
        if not candidates:
            continue

        candidate = candidates[0]

        if candidate_code and candidate.get("candidate_code") != candidate_code:
            continue

        if candidate.get("identity_status") == IdentityStatus.IDENTITY_VERIFIED:
            raise RuntimeError(
                "The selected candidate is already IDENTITY_VERIFIED. "
                "Identity matching will not overwrite a verified candidate."
            )

        discovery_response = (
            repository.client
            .table("candidate_reference_sources")
            .select(
                "discovery_id,"
                "candidate_id,"
                "source_url_id,"
                "discovery_status,"
                "is_selected_for_crawl"
            )
            .eq("candidate_id", current_candidate_id)
            .eq("source_url_id", source_url_id)
            .limit(1)
            .execute()
        )

        discoveries = discovery_response.data or []
        if not discoveries:
            continue

        discovery = discoveries[0]

        if discovery.get("discovery_status") not in {"CRAWLED", "SELECTED"}:
            continue

        return {
            "candidate": candidate,
            "reference": reference,
            "discovery": discovery,
        }

    if candidate_code or candidate_id or reference_id:
        raise RuntimeError(
            "No valid unmatched reference was found for the supplied selector."
        )

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

    decision = MatchDecision.MANUAL_REVIEW
    confidence = 0.0
    reason = (
        "The available metadata is not sufficient "
        "for an automatic identity decision."
    )

    if isbn_conflict:
        decision = MatchDecision.NO_MATCH
        confidence = 0.99
        reason = (
            "Candidate ISBN and reference ISBN are different."
        )

    elif isbn_match:
        decision = MatchDecision.MATCH
        confidence = 0.99
        reason = (
            "Candidate ISBN and reference ISBN are identical."
        )

    elif (
        title_similarity >= 0.90
        and author_similarity >= 0.90
    ):
        decision = MatchDecision.MATCH

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
        decision = MatchDecision.POSSIBLE_MATCH

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
        decision = MatchDecision.POSSIBLE_MATCH

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
        decision = MatchDecision.NO_MATCH

        confidence = round(
            1 - title_similarity,
            4,
        )

        reason = (
            "Candidate title and reference title are too different."
        )

    else:
        decision = MatchDecision.MANUAL_REVIEW

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

    if decision == MatchDecision.MATCH:
        identity_status = IdentityStatus.IDENTITY_VERIFIED
        workflow_status = IdentityStatus.IDENTITY_VERIFIED
        review_required = False
        review_reason = None
        decision_reason = result[
            "match_reason"
        ]

    elif decision in {
        MatchDecision.POSSIBLE_MATCH,
        MatchDecision.MANUAL_REVIEW,
        MatchDecision.DIFFERENT_EDITION,
    }:
        identity_status = IdentityStatus.IDENTITY_PENDING
        workflow_status = IdentityStatus.IDENTITY_PENDING
        review_required = True
        review_reason = result[
            "match_reason"
        ]
        decision_reason = None

    elif decision == MatchDecision.NO_MATCH:
        identity_status = IdentityStatus.IDENTITY_CONFLICT
        workflow_status = IdentityStatus.IDENTITY_CONFLICT
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

    if decision == MatchDecision.MATCH:
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

    elif decision == MatchDecision.NO_MATCH:
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

    if decision == MatchDecision.MATCH:
        discovery_status = "MATCHED"

    elif decision == MatchDecision.NO_MATCH:
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



GENERIC_AUTHOR_VALUES = {
    "nhieu tac gia",
    "dang cap nhat",
    "khong ro",
    "unknown",
    "various authors",
}


def is_specific_author(value: str | None) -> bool:
    """Return True when an author value names a specific person or group."""
    normalized = normalize_text(value)

    return bool(
        normalized
        and normalized not in GENERIC_AUTHOR_VALUES
    )


def get_pending_consensus_candidate(
    repository: SupabaseRepository,
    candidate_code: str | None = None,
    candidate_id: str | None = None,
) -> dict[str, Any] | None:
    """Return one pending candidate with at least two valid references."""
    query = (
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
            "conflict_fields,"
            "updated_at"
        )
        .eq("identity_status", IdentityStatus.IDENTITY_PENDING)
    )

    if candidate_code:
        query = query.eq("candidate_code", candidate_code)

    if candidate_id:
        query = query.eq("candidate_id", candidate_id)

    candidate_response = (
        query
        .order("updated_at", desc=False)
        .limit(50)
        .execute()
    )

    candidates = candidate_response.data or []

    if (candidate_code or candidate_id) and not candidates:
        raise RuntimeError(
            "No IDENTITY_PENDING candidate matched the supplied selector."
        )

    for candidate in candidates:
        current_candidate_id = candidate.get("candidate_id")
        if not current_candidate_id:
            continue

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
                "reference_cover_price_vnd,"
                "match_decision,"
                "match_confidence,"
                "source_priority,"
                "raw_metadata,"
                "collected_at"
            )
            .eq("candidate_id", current_candidate_id)
            .order("source_priority", desc=False)
            .order("collected_at", desc=False)
            .execute()
        )

        references = reference_response.data or []
        valid_references = [
            reference
            for reference in references
            if (
                reference.get("source_url_id")
                and reference.get("reference_title")
                and reference.get("match_decision") != MatchDecision.NO_MATCH
            )
        ]

        if len(valid_references) >= 2:
            return {
                "candidate": candidate,
                "references": valid_references,
            }

    return None

def choose_best_reference(
    references: list[dict[str, Any]],
) -> dict[str, Any]:
    """Choose the richest trusted reference for verified metadata."""
    def score(reference: dict[str, Any]) -> tuple[int, int]:
        metadata_score = sum(
            1
            for field_name in (
                "reference_title",
                "reference_isbn",
                "reference_author",
                "reference_publisher",
                "reference_page_count",
                "reference_weight_grams",
                "reference_length_cm",
                "reference_width_cm",
                "reference_height_cm",
            )
            if reference.get(field_name)
        )

        if is_specific_author(
            reference.get("reference_author")
        ):
            metadata_score += 3

        priority = int(
            reference.get("source_priority")
            or 99
        )

        return (
            metadata_score,
            -priority,
        )

    return max(
        references,
        key=score,
    )


def calculate_consensus_match(
    candidate: dict[str, Any],
    references: list[dict[str, Any]],
) -> dict[str, Any]:
    """Calculate a multi-source identity decision for one candidate."""
    candidate_title = candidate.get("extracted_title")
    candidate_isbn = normalize_isbn(
        candidate.get("possible_isbn")
    )

    reference_results: list[dict[str, Any]] = []
    title_matches: list[dict[str, Any]] = []
    isbn_values: set[str] = set()

    for reference in references:
        result = calculate_match(
            candidate=candidate,
            reference=reference,
        )

        reference_result = {
            "reference": reference,
            "result": result,
        }

        reference_results.append(
            reference_result
        )

        if (
            result["title_similarity"] >= 0.90
            and not result["isbn_conflict"]
        ):
            title_matches.append(
                reference_result
            )

        reference_isbn = normalize_isbn(
            reference.get("reference_isbn")
        )

        if reference_isbn:
            isbn_values.add(reference_isbn)

    isbn_conflict = (
        len(isbn_values) > 1
        or bool(
            candidate_isbn
            and isbn_values
            and candidate_isbn not in isbn_values
        )
    )

    specific_authors = [
        item["reference"].get("reference_author")
        for item in title_matches
        if is_specific_author(
            item["reference"].get(
                "reference_author"
            )
        )
    ]

    normalized_specific_authors = {
        normalize_text(author)
        for author in specific_authors
        if author
    }

    author_conflict = (
        len(normalized_specific_authors) > 1
    )

    matching_page_counts = {
        item["reference"].get(
            "reference_page_count"
        )
        for item in title_matches
        if item["reference"].get(
            "reference_page_count"
        ) is not None
    }

    page_count_conflict = (
        len(matching_page_counts) > 1
    )

    best_reference = choose_best_reference(
        [
            item["reference"]
            for item in title_matches
        ]
        or references
    )

    if isbn_conflict:
        decision = MatchDecision.NO_MATCH
        confidence = 0.99
        reason = (
            "References contain conflicting ISBN values."
        )

    elif author_conflict:
        decision = MatchDecision.MANUAL_REVIEW
        confidence = 0.80
        reason = (
            "Strong title matches were found, but specific author "
            "values conflict across references."
        )

    elif page_count_conflict:
        decision = MatchDecision.MANUAL_REVIEW
        confidence = 0.82
        reason = (
            "Strong title matches were found, but page counts "
            "conflict across references."
        )

    elif (
        len(title_matches) >= 2
        and specific_authors
    ):
        decision = MatchDecision.MATCH
        confidence = 0.96
        reason = (
            "Identity was confirmed by multiple independent sources "
            "with matching titles, consistent metadata, and at least "
            "one specific author."
        )

    elif len(title_matches) >= 2:
        decision = MatchDecision.MATCH
        confidence = 0.92
        reason = (
            "Identity was confirmed by multiple independent sources "
            "with matching titles and no material metadata conflicts."
        )

    else:
        decision = MatchDecision.POSSIBLE_MATCH
        confidence = max(
            (
                item["result"]["match_confidence"]
                for item in reference_results
            ),
            default=0.0,
        )
        reason = (
            "Multi-source evidence is not yet sufficient for "
            "automatic identity verification."
        )

    return {
        "match_decision": decision,
        "match_confidence": confidence,
        "match_reason": reason,
        "isbn_conflict": isbn_conflict,
        "author_conflict": author_conflict,
        "page_count_conflict": page_count_conflict,
        "matching_reference_count": len(
            title_matches
        ),
        "reference_results": reference_results,
        "best_reference": best_reference,
        "specific_author": (
            specific_authors[0]
            if specific_authors
            else None
        ),
    }


def print_consensus_result(
    candidate: dict[str, Any],
    consensus: dict[str, Any],
) -> None:
    """Print a multi-source identity result."""
    print()
    print("=" * 72)
    print("MULTI-SOURCE IDENTITY CONSENSUS")
    print("=" * 72)
    print(
        "Candidate code: "
        f"{candidate.get('candidate_code')}"
    )
    print(
        "Candidate title: "
        f"{candidate.get('extracted_title')}"
    )
    print(
        "Matching references: "
        f"{consensus['matching_reference_count']}"
    )

    for index, item in enumerate(
        consensus["reference_results"],
        start=1,
    ):
        reference = item["reference"]
        result = item["result"]

        print()
        print(
            f"[{index}] "
            f"{reference.get('source_type')} | "
            f"{reference.get('source_name')}"
        )
        print(
            "    Title: "
            f"{reference.get('reference_title')}"
        )
        print(
            "    Author: "
            f"{reference.get('reference_author') or '[not found]'}"
        )
        print(
            "    Publisher: "
            f"{reference.get('reference_publisher') or '[not found]'}"
        )
        print(
            "    Page count: "
            f"{reference.get('reference_page_count') or '[not found]'}"
        )
        print(
            "    Title similarity: "
            f"{result['title_similarity']}"
        )

    print()
    print(
        "Selected verified author: "
        f"{consensus.get('specific_author') or '[not found]'}"
    )
    print(
        "Decision: "
        f"{consensus['match_decision']}"
    )
    print(
        "Confidence: "
        f"{consensus['match_confidence']}"
    )
    print(
        "Reason: "
        f"{consensus['match_reason']}"
    )


def build_consensus_source_evidence(
    candidate: dict[str, Any],
    consensus: dict[str, Any],
) -> dict[str, Any]:
    """Append a multi-source consensus event to candidate evidence."""
    existing_evidence = candidate.get(
        "source_evidence"
    )

    source_evidence = (
        dict(existing_evidence)
        if isinstance(existing_evidence, dict)
        else {}
    )

    history = source_evidence.get(
        "identity_consensus_history"
    )

    if not isinstance(history, list):
        history = []

    event = {
        "match_decision": consensus[
            "match_decision"
        ],
        "match_confidence": consensus[
            "match_confidence"
        ],
        "match_reason": consensus[
            "match_reason"
        ],
        "matching_reference_count": consensus[
            "matching_reference_count"
        ],
        "selected_reference_id": consensus[
            "best_reference"
        ].get("reference_id"),
        "selected_author": consensus.get(
            "specific_author"
        ),
        "metadata_warnings": [
            field_name
            for field_name, value in (
                (
                    "isbn",
                    consensus["best_reference"].get(
                        "reference_isbn"
                    )
                    or candidate.get(
                        "possible_isbn"
                    ),
                ),
                (
                    "weight_grams",
                    consensus["best_reference"].get(
                        "reference_weight_grams"
                    ),
                ),
            )
            if not value
        ],
        "reference_ids": [
            item["reference"].get(
                "reference_id"
            )
            for item in consensus[
                "reference_results"
            ]
        ],
        "matcher_name": MATCHER_NAME,
        "matcher_version": MATCHER_VERSION,
        "verified_at": utc_now(),
    }

    history.append(event)

    source_evidence[
        "identity_consensus_history"
    ] = history
    source_evidence[
        "latest_identity_consensus"
    ] = event

    return source_evidence


def update_candidate_from_consensus(
    repository: SupabaseRepository,
    candidate: dict[str, Any],
    consensus: dict[str, Any],
) -> None:
    """Update candidate using the multi-source consensus decision."""
    candidate_id = candidate.get(
        "candidate_id"
    )

    if not candidate_id:
        raise ValueError(
            "candidate_id is required."
        )

    decision = consensus[
        "match_decision"
    ]
    best_reference = consensus[
        "best_reference"
    ]

    if decision == MatchDecision.MATCH:
        verified_isbn = (
            best_reference.get(
                "reference_isbn"
            )
            or candidate.get(
                "possible_isbn"
            )
        )

        verified_weight = best_reference.get(
            "reference_weight_grams"
        )

        # Missing ISBN and weight are metadata warnings only.
        # They do not block a successfully verified book identity.
        review_required = False
        review_reason = None

        payload: dict[str, Any] = {
            "identity_status": IdentityStatus.IDENTITY_VERIFIED,
            "workflow_status": IdentityStatus.IDENTITY_VERIFIED,
            "identity_confidence": consensus[
                "match_confidence"
            ],
            "verified_title": (
                best_reference.get(
                    "reference_title"
                )
                or candidate.get(
                    "extracted_title"
                )
            ),
            "verified_isbn": verified_isbn,
            "verified_author": (
                consensus.get(
                    "specific_author"
                )
                or best_reference.get(
                    "reference_author"
                )
                or candidate.get(
                    "extracted_author"
                )
            ),
            "verified_publisher": best_reference.get(
                "reference_publisher"
            ),
            "verified_page_count": best_reference.get(
                "reference_page_count"
            ),
            "verified_weight_grams": verified_weight,
            "verified_length_cm": best_reference.get(
                "reference_length_cm"
            ),
            "verified_width_cm": best_reference.get(
                "reference_width_cm"
            ),
            "verified_height_cm": best_reference.get(
                "reference_height_cm"
            ),
            "review_required": review_required,
            "review_reason": review_reason,
            "decision_reason": consensus[
                "match_reason"
            ],
            "conflict_fields": [],
            "source_evidence": (
                build_consensus_source_evidence(
                    candidate=candidate,
                    consensus=consensus,
                )
            ),
            "updated_at": utc_now(),
        }

    elif decision == MatchDecision.NO_MATCH:
        payload = {
            "identity_status": IdentityStatus.IDENTITY_CONFLICT,
            "workflow_status": IdentityStatus.IDENTITY_CONFLICT,
            "identity_confidence": consensus[
                "match_confidence"
            ],
            "review_required": True,
            "review_reason": consensus[
                "match_reason"
            ],
            "decision_reason": None,
            "conflict_fields": [
                field_name
                for field_name, has_conflict in (
                    (
                        "isbn",
                        consensus.get(
                            "isbn_conflict"
                        ),
                    ),
                    (
                        "author",
                        consensus.get(
                            "author_conflict"
                        ),
                    ),
                    (
                        "page_count",
                        consensus.get(
                            "page_count_conflict"
                        ),
                    ),
                )
                if has_conflict
            ],
            "source_evidence": (
                build_consensus_source_evidence(
                    candidate=candidate,
                    consensus=consensus,
                )
            ),
            "updated_at": utc_now(),
        }

    else:
        payload = {
            "identity_status": IdentityStatus.IDENTITY_PENDING,
            "workflow_status": IdentityStatus.IDENTITY_PENDING,
            "identity_confidence": consensus[
                "match_confidence"
            ],
            "review_required": True,
            "review_reason": consensus[
                "match_reason"
            ],
            "decision_reason": None,
            "source_evidence": (
                build_consensus_source_evidence(
                    candidate=candidate,
                    consensus=consensus,
                )
            ),
            "updated_at": utc_now(),
        }

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
            "Candidate consensus update returned no data."
        )


def update_references_from_consensus(
    repository: SupabaseRepository,
    consensus: dict[str, Any],
) -> None:
    """Update all reference and discovery records from consensus."""
    final_decision = consensus[
        "match_decision"
    ]

    for item in consensus[
        "reference_results"
    ]:
        reference = item[
            "reference"
        ]
        individual_result = item[
            "result"
        ]

        if (
            final_decision == MatchDecision.MATCH
            and individual_result[
                "title_similarity"
            ] >= 0.90
            and not individual_result[
                "isbn_conflict"
            ]
        ):
            reference_decision = MatchDecision.MATCH
            reference_confidence = max(
                individual_result[
                    "match_confidence"
                ],
                0.90,
            )
        else:
            reference_decision = individual_result[
                "match_decision"
            ]
            reference_confidence = individual_result[
                "match_confidence"
            ]

        updated_result = dict(
            individual_result
        )
        updated_result[
            "match_decision"
        ] = reference_decision
        updated_result[
            "match_confidence"
        ] = reference_confidence
        updated_result[
            "match_reason"
        ] = (
            consensus[
                "match_reason"
            ]
            if reference_decision == MatchDecision.MATCH
            else individual_result[
                "match_reason"
            ]
        )

        update_product_reference(
            repository=repository,
            reference=reference,
            result=updated_result,
        )

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
                reference.get(
                    "candidate_id"
                ),
            )
            .eq(
                "source_url_id",
                reference.get(
                    "source_url_id"
                ),
            )
            .limit(1)
            .execute()
        )

        discoveries = (
            discovery_response.data
            or []
        )

        if discoveries:
            update_discovery_status(
                repository=repository,
                discovery=discoveries[0],
                decision=reference_decision,
            )

def resolve_save_confirmation(
    args: argparse.Namespace,
    prompt: str,
) -> bool:
    """Resolve whether a calculated identity result may be saved."""
    if args.confirm_save:
        confirmation = "SAVE"
    elif args.non_interactive:
        confirmation = ""
    else:
        confirmation = normalize_confirmation(
            input(prompt)
        )

    if confirmation not in VALID_CONFIRMATIONS:
        print()
        print(
            "Invalid confirmation. Use SAVE, APPLY, CONFIRM, or SAVE_RESULT."
        )
        print(
            f"Received value: {confirmation or '[empty]'}"
        )
        return False

    return True


def main() -> None:
    """Match one candidate using multi-source consensus when available."""
    load_dotenv()
    args = parse_arguments()

    if args.candidate_code and args.candidate_id:
        raise RuntimeError(
            "Use either --candidate-code or --candidate-id, not both."
        )

    if args.reference_id and args.mode == "CONSENSUS":
        raise RuntimeError(
            "--reference-id cannot be used with --mode CONSENSUS."
        )

    if args.reference_id:
        args.mode = "SINGLE"

    if args.non_interactive:
        if not (args.candidate_code or args.candidate_id):
            raise RuntimeError(
                "--non-interactive requires --candidate-code or --candidate-id."
            )
        if not args.confirm_save:
            raise RuntimeError(
                "--non-interactive requires --confirm-save."
            )

    print("=" * 72)
    print("CANDIDATE IDENTITY MATCHER")
    print("=" * 72)
    print(f"Version: {MATCHER_VERSION}")
    print(f"Mode: {args.mode}")

    repository = SupabaseRepository()

    if args.mode in {"AUTO", "CONSENSUS"}:
        print(
            "Searching for a pending multi-source identity consensus..."
        )

        consensus_item = get_pending_consensus_candidate(
            repository=repository,
            candidate_code=args.candidate_code,
            candidate_id=args.candidate_id,
        )

        if consensus_item is not None:
            candidate = consensus_item["candidate"]
            references = consensus_item["references"]
            consensus = calculate_consensus_match(
                candidate=candidate,
                references=references,
            )
            print_consensus_result(
                candidate=candidate,
                consensus=consensus,
            )
            print()

            if not resolve_save_confirmation(
                args,
                (
                    "Type SAVE, APPLY, CONFIRM, or SAVE_RESULT to save "
                    "this consensus result, or press Enter to cancel: "
                ),
            ):
                print(
                    "Identity consensus result was not saved."
                )
                return

            update_candidate_from_consensus(
                repository=repository,
                candidate=candidate,
                consensus=consensus,
            )
            print(
                "Candidate identity consensus updated."
            )

            update_references_from_consensus(
                repository=repository,
                consensus=consensus,
            )
            print(
                "Reference and discovery statuses updated."
            )
            print()
            print(
                "Multi-source identity matching completed successfully."
            )
            return

        if args.mode == "CONSENSUS":
            raise RuntimeError(
                "Consensus mode requires at least two valid references."
            )

        print(
            "No pending multi-source candidate was found."
        )

    print(
        "Searching for an unmatched product reference..."
    )

    queue_item = get_unmatched_reference(
        repository=repository,
        candidate_code=args.candidate_code,
        candidate_id=args.candidate_id,
        reference_id=args.reference_id,
    )

    if queue_item is None:
        print(
            "No valid unmatched product reference was found."
        )
        return

    candidate = queue_item["candidate"]
    reference = queue_item["reference"]
    discovery = queue_item["discovery"]

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

    if not resolve_save_confirmation(
        args,
        (
            "Type SAVE, APPLY, CONFIRM, or SAVE_RESULT to save "
            "this identity result, or press Enter to cancel: "
        ),
    ):
        print(
            "Identity result was not saved."
        )
        return

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
        decision=result["match_decision"],
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