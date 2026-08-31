import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.cli_bootstrap import configure_utf8_console
from src.domain.decisions import Outcome
from src.domain.identity_status import IdentityStatus, MatchDecision
from src.domain.rules import identity_rules
from src.domain.rules.identity_rules import (
    is_specific_author,
    looks_like_valid_isbn,
    normalize_isbn,
    normalize_text,
)
from src.repositories.supabase_repository import SupabaseRepository

configure_utf8_console()


MATCHER_NAME = "candidate_identity_matcher"
# 2.0.0: hardened cumulative decision path (evaluate_and_apply_decision /
# identity_rules.evaluate_candidate_identity) replaces per-mode ad hoc
# writes. See the "HARDENED, CUMULATIVE IDENTITY DECISION PATH" section
# below for the two confirmed production incidents this fixes.
MATCHER_VERSION = "2.0.0"

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
        choices=["AUTO", "CONSENSUS", "SINGLE", "RECOMPUTE"],
        default="AUTO",
        help=(
            "AUTO prefers multi-source consensus, CONSENSUS requires at least "
            "two references, SINGLE processes one unmatched reference. All "
            "three now route through the same hardened, cumulative decision "
            "once a candidate is resolved (see evaluate_and_apply_decision). "
            "RECOMPUTE explicitly recomputes one exact candidate (requires "
            "--candidate-code/--candidate-id) from its full current "
            "reference set, ignoring queue position -- the safe way to "
            "recover a candidate a prior run may have gotten wrong, with no "
            "raw SQL and no new discovery/crawling."
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
    """
    Compare candidate identity data with reference metadata.

    Delegates to the shared src.domain.rules.identity_rules engine
    (evaluate_single_reference_identity) rather than reimplementing the
    comparison independently -- see that function's docstring for the
    exact thresholds and the ISBN-vs-barcode validity fix. This function
    keeps its original return shape so every existing caller below is
    unaffected by the extraction.
    """
    decision_result = identity_rules.evaluate_single_reference_identity(
        candidate=candidate,
        reference=reference,
    )
    evidence = decision_result.evidence

    return {
        "match_decision": evidence["match_decision"],
        "match_confidence": decision_result.confidence,
        "match_reason": decision_result.reason,
        "title_similarity": evidence["title_similarity"],
        "author_similarity": evidence["author_similarity"],
        "isbn_match": evidence["isbn_match"],
        "isbn_conflict": evidence["isbn_conflict"],
        "candidate_isbn": evidence["candidate_isbn"],
        "reference_isbn": evidence["reference_isbn"],
        "rule_code": decision_result.rule_code,
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

    max_individual_confidence = max(
        (item["result"]["match_confidence"] for item in reference_results),
        default=0.0,
    )

    decision_result = identity_rules.evaluate_consensus_identity(
        isbn_conflict=isbn_conflict,
        author_conflict=author_conflict,
        page_count_conflict=page_count_conflict,
        matching_reference_count=len(title_matches),
        has_specific_author=bool(specific_authors),
        max_individual_confidence=max_individual_confidence,
    )

    return {
        "match_decision": decision_result.evidence["match_decision"],
        "match_confidence": decision_result.confidence,
        "match_reason": decision_result.reason,
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
        "rule_code": decision_result.rule_code,
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


# ===========================================================================
# HARDENED, CUMULATIVE IDENTITY DECISION PATH
# ===========================================================================
#
# Everything above this section (get_unmatched_reference / calculate_match /
# update_candidate_status / get_pending_consensus_candidate /
# calculate_consensus_match / update_candidate_from_consensus / ...) remains
# only as the legacy no-selector queue-discovery fallback used when main()
# is run with no --candidate-code/--candidate-id/--reference-id at all (see
# main()). Once a candidate has been identified -- by any mode, including
# via that legacy discovery -- the actual decision and every write now goes
# through evaluate_and_apply_decision() below instead of the old per-mode
# write functions.
#
# Two confirmed live incidents on the TSYC historical identity pilot drove
# this. A candidate's identity_status was being determined by whichever ONE
# reference a caller happened to process next (get_unmatched_reference), in
# whatever order references were crawled -- so a reference whose crawl came
# back completely empty (missing metadata, not a real disagreement) could
# silently overwrite an already-established, valid POSSIBLE_MATCH from a
# different reference:
#   - CAN-0015: an empty NetaBooks page was evaluated first and produced a
#     false NO_MATCH/IDENTITY_CONFLICT, before the good Fahasa reference
#     was ever considered.
#   - CAN-0039: the reverse -- a good POSSIBLE_MATCH was recorded first,
#     then an unrelated rerun picked up an empty Fahasa reference and
#     silently regressed the candidate to NO_MATCH/IDENTITY_CONFLICT.
# The multi-source consensus path (get_pending_consensus_candidate) had the
# same root issue one level up, and neither path was idempotent: rerunning
# with unchanged evidence still recomputed and rewrote candidate/reference
# rows and appended a fresh (duplicate) history entry every time, and a
# candidate with no remaining unmatched reference raised RuntimeError
# instead of a clean no-op.
#
# evaluate_and_apply_decision() instead always recomputes from ALL of a
# candidate's currently registered references via
# identity_rules.evaluate_candidate_identity() (see that function's
# docstring for the full, cumulative decision path) -- order-independent
# and idempotent via a decision fingerprint stored in
# source_evidence["decision_fingerprint"], and never overwrites an already
# IDENTITY_VERIFIED candidate's canonical identity (CLAUDE.md 2.7) even
# when new evidence would otherwise change the outcome -- it only flags
# review_required and records the conflict, additively.
# ===========================================================================


def get_candidate_by_selector(
    repository: SupabaseRepository,
    candidate_code: str | None,
    candidate_id: str | None,
) -> dict[str, Any]:
    """Load one candidate by exact code/UUID, at any identity_status --
    unlike get_unmatched_reference()/get_pending_consensus_candidate(),
    this is never filtered by identity_status, match_decision, or
    discovery/crawl status. Raises if the selector does not resolve."""
    query = repository.client.table("product_candidates").select(
        "candidate_id, candidate_code, candidate_type, extracted_title, "
        "extracted_author, possible_isbn, verified_title, verified_isbn, "
        "verified_author, verified_publisher, verified_page_count, "
        "verified_weight_grams, verified_length_cm, verified_width_cm, "
        "verified_height_cm, identity_status, workflow_status, "
        "identity_confidence, review_required, review_reason, "
        "decision_reason, source_evidence, conflict_fields"
    )

    if candidate_code:
        query = query.eq("candidate_code", candidate_code)
    elif candidate_id:
        query = query.eq("candidate_id", candidate_id)
    else:
        raise RuntimeError(
            "get_candidate_by_selector requires a candidate_code or candidate_id."
        )

    rows = query.limit(1).execute().data or []

    if not rows:
        raise RuntimeError("No candidate matched the supplied selector.")

    return rows[0]


def get_candidate_id_for_reference(
    repository: SupabaseRepository,
    reference_id: str,
) -> str:
    """Resolve one exact reference_id to its owning candidate_id --
    --reference-id now only identifies *which candidate* to recompute,
    not which single reference to evaluate in isolation."""
    rows = (
        repository.client
        .table("product_references")
        .select("candidate_id")
        .eq("reference_id", reference_id)
        .limit(1)
        .execute()
        .data
        or []
    )

    if not rows:
        raise RuntimeError(
            f"No product reference found for reference_id={reference_id}."
        )

    return rows[0]["candidate_id"]


def get_all_references_for_candidate(
    repository: SupabaseRepository,
    candidate_id: str,
) -> list[dict[str, Any]]:
    """Load every registered reference for one candidate, regardless of
    match_decision or discovery/crawl status -- the hardened decision
    path always recomputes from the full current evidence set, never an
    "unmatched" subset."""
    response = (
        repository.client
        .table("product_references")
        .select(
            "reference_id, candidate_id, source_url_id, source_type, "
            "source_name, source_url, reference_title, reference_isbn, "
            "reference_author, reference_publisher, reference_page_count, "
            "reference_weight_grams, reference_length_cm, "
            "reference_width_cm, reference_height_cm, match_decision, "
            "match_confidence, source_priority, raw_metadata, collected_at"
        )
        .eq("candidate_id", candidate_id)
        .order("source_priority", desc=False)
        .order("collected_at", desc=False)
        .execute()
    )

    return response.data or []


def build_reference_snapshot(reference: dict[str, Any]) -> dict[str, Any]:
    """The subset of a reference's fields the identity decision actually
    reads -- used to build the idempotency fingerprint. Deliberately
    excludes fields the decision never uses (image URLs, prices,
    descriptions, raw HTML, collected_at, ...) so an unrelated metadata
    refresh alone never forces a spurious rewrite."""
    return {
        "reference_id": str(reference.get("reference_id")),
        "reference_title": reference.get("reference_title"),
        "reference_author": reference.get("reference_author"),
        "reference_isbn": reference.get("reference_isbn"),
        "reference_publisher": reference.get("reference_publisher"),
        "reference_page_count": reference.get("reference_page_count"),
        "source_type": reference.get("source_type"),
        "source_priority": reference.get("source_priority"),
    }


def compute_decision_fingerprint(
    candidate: dict[str, Any],
    references: list[dict[str, Any]],
    decision: Any,
) -> str:
    """A stable fingerprint of "the exact evidence this decision was
    computed from, plus the decision itself". An unchanged fingerprint
    on rerun means true NO_OP: no candidate update, no reference
    rewrite, no duplicate history entry."""
    payload = {
        "candidate_id": str(candidate.get("candidate_id")),
        "candidate_extracted_title": candidate.get("extracted_title"),
        "candidate_extracted_author": candidate.get("extracted_author"),
        "candidate_possible_isbn": candidate.get("possible_isbn"),
        "references": sorted(
            (build_reference_snapshot(r) for r in references),
            key=lambda snapshot: snapshot["reference_id"],
        ),
        "outcome": decision.outcome,
        "rule_code": decision.rule_code,
        "confidence": decision.confidence,
        "match_decision": decision.evidence.get("match_decision"),
        "matching_reference_id": decision.evidence.get("matching_reference_id"),
        "has_genuine_conflict": decision.evidence.get("has_genuine_conflict"),
        # These four directly determine build_hardened_candidate_payload's
        # conflict_fields on the REVIEW_REQUIRED branch -- included so a
        # change to that derived field also invalidates the fingerprint.
        "isbn_conflict": decision.evidence.get("isbn_conflict"),
        "author_conflict": decision.evidence.get("author_conflict"),
        "page_count_conflict": decision.evidence.get("page_count_conflict"),
        "publisher_conflict": decision.evidence.get("publisher_conflict"),
    }
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_verified_conflict_evidence(
    candidate: dict[str, Any],
    decision: Any,
    fingerprint: str,
) -> dict[str, Any]:
    """Append-only: records that hardened re-evaluation found evidence
    conflicting with an already-VERIFIED identity, WITHOUT touching
    identity_status, workflow_status, or any verified_* field --
    CLAUDE.md 2.7: verified identity is never silently overwritten, even
    by evidence that would otherwise change the decision.

    Still stamps decision_fingerprint (exactly like the normal write
    path) so an unchanged rerun against the same conflicting evidence is
    a true NO_OP next time -- without this, every rerun would re-detect
    "fingerprint differs from the never-updated stored one" and append a
    duplicate post_verification_conflicts entry forever."""
    existing_evidence = candidate.get("source_evidence")
    source_evidence = (
        dict(existing_evidence) if isinstance(existing_evidence, dict) else {}
    )

    history = source_evidence.get("post_verification_conflicts")
    if not isinstance(history, list):
        history = []

    history.append(
        {
            "outcome": decision.outcome,
            "rule_code": decision.rule_code,
            "reason": decision.reason,
            "confidence": decision.confidence,
            "evidence": {
                key: value
                for key, value in decision.evidence.items()
                if key
                in (
                    "isbn_conflict",
                    "author_conflict",
                    "page_count_conflict",
                    "publisher_conflict",
                    "usable_reference_count",
                    "unusable_reference_count",
                    "matching_reference_id",
                    "conflicting_reference_id",
                )
            },
            "matcher_name": MATCHER_NAME,
            "matcher_version": MATCHER_VERSION,
            "detected_at": utc_now(),
        }
    )

    source_evidence["post_verification_conflicts"] = history
    source_evidence["decision_fingerprint"] = fingerprint
    return source_evidence


def build_hardened_source_evidence(
    candidate: dict[str, Any],
    decision: Any,
    fingerprint: str,
) -> dict[str, Any]:
    """Append one decision event and refresh the stored fingerprint --
    called only on an actual write, never on a NO_OP."""
    existing_evidence = candidate.get("source_evidence")
    source_evidence = (
        dict(existing_evidence) if isinstance(existing_evidence, dict) else {}
    )

    history = source_evidence.get("identity_decision_history")
    if not isinstance(history, list):
        history = []

    event = {
        "outcome": decision.outcome,
        "rule_code": decision.rule_code,
        "reason": decision.reason,
        "confidence": decision.confidence,
        "usable_reference_count": decision.evidence.get("usable_reference_count"),
        "unusable_reference_count": decision.evidence.get("unusable_reference_count"),
        "has_genuine_conflict": decision.evidence.get("has_genuine_conflict"),
        "matching_reference_id": decision.evidence.get("matching_reference_id"),
        "matcher_name": MATCHER_NAME,
        "matcher_version": MATCHER_VERSION,
        "decided_at": utc_now(),
    }
    history.append(event)

    source_evidence["identity_decision_history"] = history
    source_evidence["latest_identity_decision"] = event
    source_evidence["decision_fingerprint"] = fingerprint
    return source_evidence


def build_hardened_candidate_payload(
    candidate: dict[str, Any],
    decision: Any,
    references_by_id: dict[str, dict[str, Any]],
    fingerprint: str,
) -> dict[str, Any]:
    """Build the product_candidates update payload for a non-VERIFIED
    candidate from a hardened DecisionResult -- one write path, used
    regardless of whether the decision came from 0, 1, or 2+ usable
    references. verified_isbn is only ever populated from a value that
    passes looks_like_valid_isbn() -- an unvalidated barcode-shaped
    value is never promoted (Phase 8: never fabricate, never promote an
    unvalidated ISBN)."""
    evidence = decision.evidence

    if decision.outcome == Outcome.AUTO_PASS:
        matching_reference_id = evidence.get("matching_reference_id")
        matching_reference = references_by_id.get(matching_reference_id) or {}

        raw_isbn = matching_reference.get("reference_isbn")
        if looks_like_valid_isbn(raw_isbn):
            verified_isbn = raw_isbn
        elif looks_like_valid_isbn(candidate.get("possible_isbn")):
            verified_isbn = candidate.get("possible_isbn")
        else:
            verified_isbn = None

        payload: dict[str, Any] = {
            "identity_status": IdentityStatus.IDENTITY_VERIFIED,
            "workflow_status": IdentityStatus.IDENTITY_VERIFIED,
            "identity_confidence": decision.confidence,
            "verified_title": (
                matching_reference.get("reference_title")
                or candidate.get("extracted_title")
            ),
            "verified_isbn": verified_isbn,
            "verified_author": (
                matching_reference.get("reference_author")
                or candidate.get("extracted_author")
            ),
            "verified_publisher": matching_reference.get("reference_publisher"),
            "verified_page_count": matching_reference.get("reference_page_count"),
            "verified_weight_grams": matching_reference.get("reference_weight_grams"),
            "verified_length_cm": matching_reference.get("reference_length_cm"),
            "verified_width_cm": matching_reference.get("reference_width_cm"),
            "verified_height_cm": matching_reference.get("reference_height_cm"),
            "review_required": False,
            "review_reason": None,
            "decision_reason": decision.reason,
            "conflict_fields": [],
        }

    elif decision.outcome == Outcome.AUTO_REJECT:
        payload = {
            "identity_status": IdentityStatus.IDENTITY_CONFLICT,
            "workflow_status": IdentityStatus.IDENTITY_CONFLICT,
            "identity_confidence": decision.confidence,
            "review_required": True,
            "review_reason": decision.reason,
            "decision_reason": None,
            "conflict_fields": ["title"],
        }

    else:  # REVIEW_REQUIRED
        # Reflect exactly what conflicts, and clear stale conflict_fields
        # left by an earlier, now-superseded decision (e.g. a prior
        # AUTO_REJECT run) -- a REVIEW_REQUIRED that carries no real
        # conflict signal (just insufficient evidence) must never leave
        # behind a conflict_fields value implying otherwise.
        conflict_fields = [
            field_name
            for field_name, has_conflict in (
                ("isbn", evidence.get("isbn_conflict")),
                ("author", evidence.get("author_conflict")),
                ("page_count", evidence.get("page_count_conflict")),
                ("publisher", evidence.get("publisher_conflict")),
            )
            if has_conflict
        ]
        payload = {
            "identity_status": IdentityStatus.IDENTITY_PENDING,
            "workflow_status": IdentityStatus.IDENTITY_PENDING,
            "identity_confidence": decision.confidence,
            "review_required": True,
            "review_reason": decision.reason,
            "decision_reason": None,
            "conflict_fields": conflict_fields,
        }

    payload["source_evidence"] = build_hardened_source_evidence(
        candidate, decision, fingerprint
    )
    payload["updated_at"] = utc_now()
    return payload


def apply_reference_match_decisions(
    repository: SupabaseRepository,
    candidate: dict[str, Any],
    references: list[dict[str, Any]],
) -> int:
    """
    Write each USABLE reference's own individual match_decision/
    match_confidence (evaluate_single_reference_identity, thresholds
    unchanged) -- but only when it actually differs from what is
    already stored, so an unchanged rerun never rewrites an identical
    reference row. Unusable references are never written here -- their
    match_decision stays NULL, which already means "not yet evaluated"
    in the existing schema; no new enum value is needed for "unusable".

    Returns the number of reference rows actually updated.
    """
    updated_count = 0

    for reference in references:
        if not identity_rules.is_reference_evaluable(reference):
            continue

        result = identity_rules.evaluate_single_reference_identity(
            candidate, reference
        )
        new_decision = result.evidence["match_decision"]
        new_confidence = result.confidence

        if (
            reference.get("match_decision") == new_decision
            and reference.get("match_confidence") == new_confidence
        ):
            continue

        existing_metadata = reference.get("raw_metadata")
        raw_metadata = (
            dict(existing_metadata) if isinstance(existing_metadata, dict) else {}
        )
        raw_metadata["identity_match"] = {
            "matcher_name": MATCHER_NAME,
            "matcher_version": MATCHER_VERSION,
            "matched_at": utc_now(),
            "reason": result.reason,
            "title_similarity": result.evidence.get("title_similarity"),
            "author_similarity": result.evidence.get("author_similarity"),
            "isbn_match": result.evidence.get("isbn_match"),
            "isbn_conflict": result.evidence.get("isbn_conflict"),
        }

        response = (
            repository.client
            .table("product_references")
            .update(
                {
                    "match_decision": new_decision,
                    "match_confidence": new_confidence,
                    "raw_metadata": raw_metadata,
                    "updated_at": utc_now(),
                }
            )
            .eq("reference_id", reference["reference_id"])
            .execute()
        )

        if not response.data:
            raise RuntimeError(
                "Product reference match update returned no data for "
                f"reference_id={reference.get('reference_id')}."
            )

        updated_count += 1

    return updated_count


def evaluate_and_apply_decision(
    repository: SupabaseRepository,
    candidate: dict[str, Any],
    references: list[dict[str, Any]],
    confirm_save: bool,
) -> dict[str, Any]:
    """
    The one hardened decision-and-write path every mode uses once a
    candidate has been identified. See the module-level "HARDENED,
    CUMULATIVE IDENTITY DECISION PATH" section above for the incidents
    this fixes. Never raises for "nothing new to do" -- always returns a
    result dict with an "action" key describing what happened.
    """
    candidate_code = candidate.get("candidate_code")

    if not references:
        print(
            f"NO_OP: {candidate_code} has no registered references yet -- "
            "nothing to evaluate."
        )
        return {"candidate_code": candidate_code, "action": "NO_OP_NO_REFERENCES"}

    decision = identity_rules.evaluate_candidate_identity(candidate, references)
    fingerprint = compute_decision_fingerprint(candidate, references, decision)
    stored_evidence = candidate.get("source_evidence")
    stored_fingerprint = (
        stored_evidence.get("decision_fingerprint")
        if isinstance(stored_evidence, dict)
        else None
    )

    print()
    print("=" * 72)
    print("HARDENED IDENTITY DECISION")
    print("=" * 72)
    print(f"Candidate code: {candidate_code}")
    print(f"Candidate title: {candidate.get('extracted_title')}")
    print(f"Usable references: {decision.evidence.get('usable_reference_count')}")
    print(f"Unusable references: {decision.evidence.get('unusable_reference_count')}")
    print(f"Outcome: {decision.outcome} ({decision.rule_code})")
    print(f"Confidence: {decision.confidence}")
    print(f"Has genuine conflict: {decision.evidence.get('has_genuine_conflict')}")
    print(f"Reason: {decision.reason}")

    base_result = {
        "candidate_code": candidate_code,
        "outcome": decision.outcome,
        "rule_code": decision.rule_code,
        "confidence": decision.confidence,
        "usable_reference_count": decision.evidence.get("usable_reference_count"),
        "unusable_reference_count": decision.evidence.get("unusable_reference_count"),
        "has_genuine_conflict": decision.evidence.get("has_genuine_conflict"),
    }

    if stored_fingerprint == fingerprint:
        print()
        print(
            "NO_OP: decision unchanged since the last evaluation "
            "(fingerprint match). No write performed."
        )
        return {**base_result, "action": "NO_OP"}

    current_status = candidate.get("identity_status")

    if current_status == IdentityStatus.IDENTITY_VERIFIED:
        if decision.outcome == Outcome.AUTO_PASS:
            print()
            print(
                f"NO_OP: {candidate_code} is already IDENTITY_VERIFIED and "
                "new evidence is still consistent. Verified identity is "
                "protected -- no write performed."
            )
            return {**base_result, "action": "NO_OP_VERIFIED_CONSISTENT"}

        print()
        print(
            f"PROTECTED: {candidate_code} stays IDENTITY_VERIFIED. New "
            f"evidence conflicts (has_genuine_conflict="
            f"{decision.evidence.get('has_genuine_conflict')}) -- "
            "review_required will be set and conflict evidence recorded, "
            "but identity_status/verified_* fields are NOT modified."
        )

        if not confirm_save:
            return {**base_result, "action": "WOULD_FLAG_VERIFIED_CONFLICT"}

        payload = {
            "review_required": True,
            "source_evidence": build_verified_conflict_evidence(
                candidate, decision, fingerprint
            ),
            "updated_at": utc_now(),
        }
        response = (
            repository.client
            .table("product_candidates")
            .update(payload)
            .eq("candidate_id", candidate["candidate_id"])
            .execute()
        )
        if not response.data:
            raise RuntimeError("Candidate conflict-flag update returned no data.")

        return {**base_result, "action": "VERIFIED_PROTECTED_CONFLICT_FLAGGED"}

    references_by_id = {str(r.get("reference_id")): r for r in references}
    payload = build_hardened_candidate_payload(
        candidate, decision, references_by_id, fingerprint
    )

    if not confirm_save:
        print()
        print(f"WOULD WRITE: identity_status -> {payload['identity_status']}")
        return {**base_result, "action": f"WOULD_WRITE:{payload['identity_status']}"}

    response = (
        repository.client
        .table("product_candidates")
        .update(payload)
        .eq("candidate_id", candidate["candidate_id"])
        .execute()
    )
    if not response.data:
        raise RuntimeError("Candidate hardened-decision update returned no data.")

    updated_reference_count = apply_reference_match_decisions(
        repository, candidate, references
    )

    print()
    print(
        f"WROTE: identity_status -> {payload['identity_status']} "
        f"(reference rows updated: {updated_reference_count})"
    )

    return {
        **base_result,
        "action": f"WROTE:{payload['identity_status']}",
        "reference_rows_updated": updated_reference_count,
    }


def main() -> None:
    """
    Resolve one candidate (by selector, or -- only when none is given --
    legacy queue discovery), then run the hardened, cumulative decision
    path (evaluate_and_apply_decision) exactly once. Never raises for
    "nothing new to do" -- a candidate with no actionable change is a
    clean NO_OP, exit 0.
    """
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

    if args.mode == "RECOMPUTE" and not (
        args.candidate_code or args.candidate_id
    ):
        raise RuntimeError(
            "--mode RECOMPUTE requires --candidate-code or --candidate-id."
        )

    if args.reference_id:
        args.mode = "SINGLE"

    if args.non_interactive:
        if not (
            args.candidate_code or args.candidate_id or args.reference_id
        ):
            raise RuntimeError(
                "--non-interactive requires --candidate-code, "
                "--candidate-id, or --reference-id."
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

    candidate: dict[str, Any] | None = None

    if args.reference_id:
        candidate_id = get_candidate_id_for_reference(repository, args.reference_id)
        candidate = get_candidate_by_selector(repository, None, candidate_id)

    elif args.candidate_code or args.candidate_id:
        candidate = get_candidate_by_selector(
            repository, args.candidate_code, args.candidate_id
        )

        if args.mode == "CONSENSUS":
            reference_count = len(
                get_all_references_for_candidate(
                    repository, candidate["candidate_id"]
                )
            )
            if reference_count < 2:
                raise RuntimeError(
                    "Consensus mode requires at least two registered "
                    f"references ({reference_count} found)."
                )

    if candidate is not None:
        references = get_all_references_for_candidate(
            repository, candidate["candidate_id"]
        )
        confirm_save = resolve_save_confirmation(
            args,
            (
                "Type SAVE, APPLY, CONFIRM, or SAVE_RESULT to save this "
                "identity decision, or press Enter to cancel: "
            ),
        )
        evaluate_and_apply_decision(
            repository=repository,
            candidate=candidate,
            references=references,
            confirm_save=confirm_save,
        )
        print()
        print("Identity matching completed.")
        return

    # No explicit selector was supplied: fall back to the legacy
    # no-selector discovery queue to find ONE candidate to work on
    # (unchanged from before -- this path was never implicated in the
    # confirmed regressions, which both occurred with an explicit
    # candidate selector). Once a candidate is found, it is handed off
    # to the same hardened decision-and-write path as every other mode.
    print("No candidate selector supplied -- searching the legacy discovery queue...")

    if args.mode in {"AUTO", "CONSENSUS"}:
        consensus_item = get_pending_consensus_candidate(repository=repository)

        if consensus_item is not None:
            candidate = consensus_item["candidate"]
            references = get_all_references_for_candidate(
                repository, candidate["candidate_id"]
            )
            confirm_save = resolve_save_confirmation(
                args,
                (
                    "Type SAVE, APPLY, CONFIRM, or SAVE_RESULT to save "
                    "this identity decision, or press Enter to cancel: "
                ),
            )
            evaluate_and_apply_decision(
                repository=repository,
                candidate=candidate,
                references=references,
                confirm_save=confirm_save,
            )
            return

        if args.mode == "CONSENSUS":
            print("No pending multi-source candidate was found.")
            return

        print("No pending multi-source candidate was found.")

    print("Searching for an unmatched product reference...")

    queue_item = get_unmatched_reference(repository=repository)

    if queue_item is None:
        print("No valid unmatched product reference was found. Nothing to do.")
        return

    candidate = queue_item["candidate"]
    references = get_all_references_for_candidate(
        repository, candidate["candidate_id"]
    )
    confirm_save = resolve_save_confirmation(
        args,
        (
            "Type SAVE, APPLY, CONFIRM, or SAVE_RESULT to save this "
            "identity decision, or press Enter to cancel: "
        ),
    )
    evaluate_and_apply_decision(
        repository=repository,
        candidate=candidate,
        references=references,
        confirm_save=confirm_save,
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