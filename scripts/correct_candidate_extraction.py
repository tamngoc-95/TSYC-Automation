import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.cli_bootstrap import configure_utf8_console
from src.domain.decisions import Outcome
from src.domain.identity_status import IdentityStatus
from src.domain.rules.identity_rules import assess_conflict_is_title_only_recoverable
from src.repositories.supabase_repository import SupabaseRepository

from create_candidates_from_cleaned_posts import extract_book_identity

configure_utf8_console()


CORRECTION_METHOD = "facebook_candidate_title_correction_v1.0.0"

STANDARD_CORRECTION_REASON = (
    "Facebook invisible Unicode obfuscation "
    "(U+034F COMBINING GRAPHEME JOINER) defeated the "
    "original title extractor. Re-derived after the "
    "normalization fix in clean_facebook_raw_pages.py / "
    "create_candidates_from_cleaned_posts.py."
)

# scripts/match_candidate_identity.py's AUTO_REJECT write path
# (build_hardened_candidate_payload) always records conflict_fields as
# the literal ["title"], including for an ISBN-conflict AUTO_REJECT --
# assess_conflict_is_title_only_recoverable() re-derives the true cause
# directly from reference evidence rather than trusting that label, so
# this recovery reason is only ever recorded when that re-derivation
# actually confirmed the conflict was title-only.
TITLE_ONLY_CONFLICT_CORRECTION_REASON = "EXTRACTION_CORRECTION_AFTER_TITLE_CONFLICT"

# Candidates may only be corrected while they are still in their initial,
# unreviewed extraction state (workflow_status=EXTRACTED, identity_status
# =IDENTITY_PENDING) -- OR, as one narrow, validated exception, while at
# IDENTITY_CONFLICT when assess_conflict_is_title_only_recoverable()
# confirms the conflict traces back to title quality alone, with no
# independent ISBN/author/publisher disagreement. Every other identity
# state (IDENTITY_VERIFIED, or IDENTITY_CONFLICT for a non-title reason)
# is still refused outright -- CLAUDE.md golden principle #6 forbids
# overwriting approved content or finalized identity evidence silently,
# and this script never guesses whether an unlisted later state is still
# "safe" to touch.
ALLOWED_WORKFLOW_STATUS = "EXTRACTED"
ALLOWED_IDENTITY_STATUS = IdentityStatus.IDENTITY_PENDING
CONFLICT_WORKFLOW_STATUS = IdentityStatus.IDENTITY_CONFLICT
CONFLICT_IDENTITY_STATUS = IdentityStatus.IDENTITY_CONFLICT


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
            "Re-derive and correct one candidate's extracted_title/"
            "extracted_author/possible_isbn from its raw page's current "
            "cleaned_text, using the same rule-based extractor "
            "create_candidates_from_cleaned_posts.py uses at creation "
            "time. Intended for repairing a candidate whose stored "
            "extraction predates a cleaning/extraction bug fix. Only "
            "candidates still in EXTRACTED / IDENTITY_PENDING state can "
            "be corrected."
        )
    )

    parser.add_argument(
        "--candidate-code",
        required=True,
        help="Process one exact candidate_code.",
    )

    parser.add_argument(
        "--confirm-correct",
        action="store_true",
        help="Confirm the correction without prompting.",
    )

    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Disable input prompts. Requires --confirm-correct.",
    )

    return parser.parse_args()


def get_candidate_by_code(
    repository: SupabaseRepository,
    candidate_code: str,
) -> dict[str, Any] | None:
    """Return one candidate by its exact candidate_code."""
    response = (
        repository.client
        .table("product_candidates")
        .select(
            "candidate_id,"
            "batch_id,"
            "candidate_code,"
            "candidate_type,"
            "raw_page_id,"
            "source_url_id,"
            "extracted_title,"
            "extracted_author,"
            "possible_isbn,"
            "extraction_confidence,"
            "identity_status,"
            "workflow_status,"
            "review_required,"
            "source_evidence"
        )
        .eq(
            "candidate_code",
            candidate_code,
        )
        .limit(1)
        .execute()
    )

    records = response.data or []

    return records[0] if records else None


def get_raw_page(
    repository: SupabaseRepository,
    raw_page_id: str,
) -> dict[str, Any] | None:
    """Return one raw page's cleaning state and cleaned_text."""
    response = (
        repository.client
        .table("raw_pages")
        .select(
            "raw_page_id, page_url, cleaning_status, cleaning_method, "
            "cleaned_at, cleaned_text"
        )
        .eq(
            "raw_page_id",
            raw_page_id,
        )
        .limit(1)
        .execute()
    )

    records = response.data or []

    return records[0] if records else None


def get_references_for_candidate(
    repository: SupabaseRepository,
    candidate_id: str,
) -> list[dict[str, Any]]:
    """Return every product_references row for one candidate."""
    response = (
        repository.client
        .table("product_references")
        .select(
            "reference_id,"
            "candidate_id,"
            "reference_title,"
            "reference_author,"
            "reference_isbn,"
            "reference_publisher,"
            "match_decision"
        )
        .eq(
            "candidate_id",
            candidate_id,
        )
        .execute()
    )

    return response.data or []


def build_correction_payload(
    candidate: dict[str, Any],
    extraction: dict[str, Any],
    reason: str = STANDARD_CORRECTION_REASON,
) -> dict[str, Any]:
    """Build the update payload for a deterministic title correction.

    Only extraction-derived fields are touched. candidate_code,
    raw_page_id, source_url_id, candidate_type, identity_status, and
    workflow_status are never included here -- identity_status recovery
    (if any) is left entirely to a subsequent
    match_candidate_identity.py --mode RECOMPUTE, which re-derives it
    from the corrected evidence rather than this script guessing it.
    """
    existing_evidence = candidate.get("source_evidence") or {}

    corrections = list(
        existing_evidence.get("corrections") or []
    )

    corrections.append(
        {
            "corrected_at": datetime.now(
                timezone.utc
            ).isoformat(),
            "correction_method": CORRECTION_METHOD,
            "reason": reason,
            "old_extracted_title": candidate.get("extracted_title"),
            "new_extracted_title": extraction["extracted_title"],
            "old_extracted_author": candidate.get("extracted_author"),
            "new_extracted_author": extraction.get("extracted_author"),
            "old_possible_isbn": candidate.get("possible_isbn"),
            "new_possible_isbn": extraction.get("possible_isbn"),
            "matched_pattern": extraction.get("matched_pattern"),
        }
    )

    updated_evidence = dict(existing_evidence)
    updated_evidence["corrections"] = corrections

    return {
        "extracted_title": extraction["extracted_title"],
        "extracted_author": extraction.get("extracted_author"),
        "possible_isbn": extraction.get("possible_isbn"),
        "extraction_confidence": extraction["extraction_confidence"],
        "source_evidence": updated_evidence,
        # Explicit, not merely "left alone": a corrected extraction must
        # still be reviewed like any other freshly extracted candidate.
        "review_required": True,
        "updated_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }


def print_planned_change(
    candidate: dict[str, Any],
    extraction: dict[str, Any],
) -> None:
    """Print the exact planned field changes before writing anything."""
    print()
    print("=" * 78)
    print("PLANNED CORRECTION")
    print("=" * 78)

    print(
        "Candidate code: "
        f"{candidate.get('candidate_code')} (unchanged)"
    )
    print(
        "Candidate ID: "
        f"{candidate.get('candidate_id')} (unchanged)"
    )
    print(
        "Raw page ID: "
        f"{candidate.get('raw_page_id')} (unchanged)"
    )
    print(
        "Source URL ID: "
        f"{candidate.get('source_url_id')} (unchanged)"
    )
    print(
        "identity_status: "
        f"{candidate.get('identity_status')} (unchanged)"
    )
    print(
        "workflow_status: "
        f"{candidate.get('workflow_status')} (unchanged)"
    )
    print(
        "review_required: True (unchanged -- still required)"
    )

    print()
    print(
        "extracted_title:"
    )
    print(
        f"  OLD: {candidate.get('extracted_title')!r}"
    )
    print(
        f"  NEW: {extraction['extracted_title']!r}"
    )

    print()
    print(
        "extracted_author:"
    )
    print(
        f"  OLD: {candidate.get('extracted_author')!r}"
    )
    print(
        f"  NEW: {extraction.get('extracted_author')!r}"
    )

    print()
    print(
        "possible_isbn:"
    )
    print(
        f"  OLD: {candidate.get('possible_isbn')!r}"
    )
    print(
        f"  NEW: {extraction.get('possible_isbn')!r}"
    )

    print()
    print(
        "extraction_confidence:"
    )
    print(
        f"  OLD: {candidate.get('extraction_confidence')!r}"
    )
    print(
        f"  NEW: {extraction['extraction_confidence']!r}"
    )

    print()
    print(
        "Matched rule: "
        f"{extraction.get('matched_pattern')}"
    )

    extraction_warnings = extraction.get("warnings") or []

    if extraction_warnings:
        print()
        print("Extraction warnings:")

        for warning in extraction_warnings:
            print(f"  - {warning}")


def run_correction(
    repository: SupabaseRepository,
    candidate_code: str,
    confirm_correct: bool,
    non_interactive: bool,
    prompt: Any = input,
) -> dict[str, Any] | None:
    """
    Run the full correction flow for one exact candidate_code: resolve
    and gate the target, re-derive its title from source evidence,
    diff against the stored value, confirm, write, and log. Returns the
    verified post-write row, or None when the run is a no-op (title
    already matches) or is cancelled.

    Extracted from main() so it is directly testable against a fake
    repository -- mirrors prepare_product_content.py's
    run_revise_action().
    """
    candidate = get_candidate_by_code(
        repository=repository,
        candidate_code=candidate_code,
    )

    if candidate is None:
        raise RuntimeError(
            "No candidate matched the exact candidate_code: "
            f"{candidate_code}"
        )

    is_standard_state = (
        candidate.get("workflow_status") == ALLOWED_WORKFLOW_STATUS
        and candidate.get("identity_status") == ALLOWED_IDENTITY_STATUS
    )
    is_conflict_state = (
        candidate.get("workflow_status") == CONFLICT_WORKFLOW_STATUS
        and candidate.get("identity_status") == CONFLICT_IDENTITY_STATUS
    )

    correction_reason = STANDARD_CORRECTION_REASON

    if not is_standard_state and not is_conflict_state:
        raise RuntimeError(
            "Refusing to correct this candidate: workflow_status/"
            f"identity_status is {candidate.get('workflow_status')!r}/"
            f"{candidate.get('identity_status')!r}. Only a candidate "
            f"still in its initial extraction state ({ALLOWED_WORKFLOW_STATUS!r}/"
            f"{ALLOWED_IDENTITY_STATUS!r}) or at IDENTITY_CONFLICT with a "
            "validated title-only cause may be corrected by this script."
        )

    if is_conflict_state:
        references = get_references_for_candidate(
            repository=repository,
            candidate_id=candidate["candidate_id"],
        )

        recovery_decision = assess_conflict_is_title_only_recoverable(
            candidate=candidate,
            references=references,
        )

        print()
        print("=" * 78)
        print("IDENTITY_CONFLICT RECOVERY ASSESSMENT")
        print("=" * 78)
        print(f"Outcome: {recovery_decision.outcome} ({recovery_decision.rule_code})")
        print(f"Reason: {recovery_decision.reason}")

        if recovery_decision.outcome != Outcome.AUTO_PASS:
            raise RuntimeError(
                "Refusing to correct this candidate: it is at "
                "IDENTITY_CONFLICT, and assess_conflict_is_title_only_"
                f"recoverable() found the conflict is NOT title-only "
                f"({recovery_decision.rule_code}): {recovery_decision.reason}"
            )

        correction_reason = TITLE_ONLY_CONFLICT_CORRECTION_REASON

    raw_page_id = candidate.get("raw_page_id")

    if not raw_page_id:
        raise RuntimeError(
            "This candidate has no linked raw_page_id and cannot be "
            "re-derived."
        )

    raw_page = get_raw_page(
        repository=repository,
        raw_page_id=raw_page_id,
    )

    if raw_page is None:
        raise RuntimeError(
            f"Raw page not found: {raw_page_id}"
        )

    if raw_page.get("cleaning_status") != "CLEANED":
        raise RuntimeError(
            "Refusing to correct this candidate: the linked raw page's "
            "cleaning_status is "
            f"{raw_page.get('cleaning_status')!r}, not 'CLEANED'."
        )

    cleaned_text = raw_page.get("cleaned_text") or ""

    if not cleaned_text.strip():
        raise RuntimeError(
            "The linked raw page has no cleaned_text to re-derive from."
        )

    extraction = extract_book_identity(
        cleaned_text
    )

    old_title_normalized = " ".join(
        (candidate.get("extracted_title") or "").split()
    ).casefold()

    new_title_normalized = " ".join(
        extraction["extracted_title"].split()
    ).casefold()

    if old_title_normalized == new_title_normalized:
        print()
        print(
            "No correction needed: the re-derived title matches the "
            "stored title exactly."
        )
        return

    print_planned_change(
        candidate=candidate,
        extraction=extraction,
    )

    print()

    if confirm_correct:
        confirmation = "CORRECT"

    elif non_interactive:
        confirmation = ""

    else:
        confirmation = normalize_confirmation(
            prompt(
                "Type CORRECT to apply this exact change, "
                "or press Enter to cancel: "
            )
        )

    if confirmation != "CORRECT":
        print()
        print(
            "Correction cancelled. No fields were changed."
        )
        return

    payload = build_correction_payload(
        candidate=candidate,
        extraction=extraction,
        reason=correction_reason,
    )

    response = (
        repository.client
        .table("product_candidates")
        .update(payload)
        .eq(
            "candidate_id",
            candidate["candidate_id"],
        )
        .execute()
    )

    if not response.data:
        raise RuntimeError(
            "Candidate correction update returned no data."
        )

    repository.write_process_log(
        batch_id=candidate.get("batch_id"),
        candidate_id=candidate["candidate_id"],
        process_name="CORRECT_CANDIDATE_EXTRACTION",
        process_step="TITLE_CORRECTION",
        log_level="INFO",
        status="CORRECTED",
        message=(
            "Candidate extraction fields corrected: "
            f"{correction_reason}"
        ),
        error_details={
            "candidate_code": candidate.get("candidate_code"),
            "raw_page_id": raw_page_id,
            "old_extracted_title": candidate.get("extracted_title"),
            "new_extracted_title": extraction["extracted_title"],
            "correction_method": CORRECTION_METHOD,
            "correction_reason": correction_reason,
        },
    )

    verify_response = (
        repository.client
        .table("product_candidates")
        .select(
            "candidate_code, extracted_title, extracted_author, "
            "possible_isbn, extraction_confidence, identity_status, "
            "workflow_status, review_required"
        )
        .eq(
            "candidate_id",
            candidate["candidate_id"],
        )
        .limit(1)
        .execute()
    )

    verified = (verify_response.data or [{}])[0]

    print()
    print("=" * 78)
    print("CORRECTION RESULT")
    print("=" * 78)
    print(
        "Candidate code: "
        f"{verified.get('candidate_code')}"
    )
    print(
        "Extracted title: "
        f"{verified.get('extracted_title')}"
    )
    print(
        "Extracted author: "
        f"{verified.get('extracted_author')}"
    )
    print(
        "Possible ISBN: "
        f"{verified.get('possible_isbn')}"
    )
    print(
        "Extraction confidence: "
        f"{verified.get('extraction_confidence')}"
    )
    print(
        "Identity status: "
        f"{verified.get('identity_status')}"
    )
    print(
        "Workflow status: "
        f"{verified.get('workflow_status')}"
    )
    print(
        "Review required: "
        f"{verified.get('review_required')}"
    )

    print()
    print(
        "Candidate extraction correction finished."
    )

    return verified


def main() -> None:
    """Parse arguments and run the correction flow for one candidate."""
    load_dotenv(
        PROJECT_ROOT / ".env"
    )

    args = parse_arguments()

    if args.non_interactive and not args.confirm_correct:
        raise RuntimeError(
            "--non-interactive requires --confirm-correct."
        )

    print(
        "Candidate extraction correction started."
    )
    print(
        f"Method: {CORRECTION_METHOD}"
    )
    print(
        f"Target candidate code: {args.candidate_code}"
    )

    repository = SupabaseRepository()

    run_correction(
        repository=repository,
        candidate_code=args.candidate_code,
        confirm_correct=args.confirm_correct,
        non_interactive=args.non_interactive,
    )


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print()
        print(
            "Candidate extraction correction was cancelled."
        )
        sys.exit(130)

    except Exception as error:
        print()
        print(
            "Candidate extraction correction failed."
        )
        print(
            f"Error type: {type(error).__name__}"
        )
        print(
            f"Error details: {error}"
        )
        sys.exit(1)
