"""Deterministic identity-matching rules.

Covers product_candidates.identity_status and product_references.
match_decision (src.domain.identity_status.IdentityStatus/MatchDecision).
Reference-source priority for tie-breaking must come from
src.domain.reference_sources.REFERENCE_SOURCE_PRIORITY, not a locally
invented order.

Rule codes implemented here:

    IDENTITY_EXACT_ISBN                   AUTO_PASS
    IDENTITY_EXACT_TITLE_AUTHOR            AUTO_PASS
    IDENTITY_EXACT_CANONICAL_TITLE          AUTO_PASS
    IDENTITY_SERIES_VOLUME_MATCH            AUTO_PASS
    IDENTITY_EDITION_METADATA_DIFFERENCE    AUTO_PASS
    IDENTITY_COMBO_COMPLETE_MATCH           AUTO_PASS
    IDENTITY_CONFIRMED_NO_MATCH             AUTO_REJECT
    IDENTITY_COMBO_SINGLE_AMBIGUITY         REVIEW_REQUIRED
    IDENTITY_CONFLICTING_CREDIBLE_SOURCES   REVIEW_REQUIRED
    IDENTITY_INSUFFICIENT_EVIDENCE          REVIEW_REQUIRED

See docs/TSYC_DECISION_MATRIX.md for the full specification.

CLAUDE.md section 2.3: identifiers beginning with 893 are normally EAN/
product barcodes, not ISBNs. A valid ISBN-13 begins with 978 or 979.
looks_like_valid_isbn() enforces this -- a barcode recorded in
possible_isbn/reference_isbn is never treated as an ISBN match here,
even if it happens to be digit-for-digit identical on both sides.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Sequence

from src.domain.decisions import DecisionResult, Outcome
from src.domain.identity_status import MatchDecision

# --- rule codes --------------------------------------------------------

IDENTITY_EXACT_ISBN = "IDENTITY_EXACT_ISBN"
IDENTITY_EXACT_TITLE_AUTHOR = "IDENTITY_EXACT_TITLE_AUTHOR"
IDENTITY_EXACT_CANONICAL_TITLE = "IDENTITY_EXACT_CANONICAL_TITLE"
IDENTITY_SERIES_VOLUME_MATCH = "IDENTITY_SERIES_VOLUME_MATCH"
IDENTITY_EDITION_METADATA_DIFFERENCE = "IDENTITY_EDITION_METADATA_DIFFERENCE"
IDENTITY_COMBO_COMPLETE_MATCH = "IDENTITY_COMBO_COMPLETE_MATCH"
IDENTITY_COMBO_SINGLE_AMBIGUITY = "IDENTITY_COMBO_SINGLE_AMBIGUITY"
IDENTITY_CONFLICTING_CREDIBLE_SOURCES = "IDENTITY_CONFLICTING_CREDIBLE_SOURCES"
IDENTITY_INSUFFICIENT_EVIDENCE = "IDENTITY_INSUFFICIENT_EVIDENCE"
# Not in the original task list, but required by the Outcome vocabulary:
# a confident, deterministic non-match (e.g. two valid but different
# ISBNs, or a very low title similarity) is a real AUTO_REJECT, distinct
# from the ambiguous REVIEW_REQUIRED codes above.
IDENTITY_CONFIRMED_NO_MATCH = "IDENTITY_CONFIRMED_NO_MATCH"


# --- shared helpers ------------------------------------------------------

_BARCODE_PREFIX = "893"
_ISBN13_PREFIXES = ("978", "979")


def normalize_isbn(value: str | None) -> str:
    """Strip separators and case-fold an identifier for comparison."""
    if not value:
        return ""
    return re.sub(r"[^0-9Xx]", "", value).upper()


def looks_like_valid_isbn(value: str | None) -> bool:
    """
    Return True only for a value that is plausibly an ISBN, not a
    barcode.

    - ISBN-13: exactly 13 digits, starting with 978 or 979.
    - ISBN-10: exactly 10 characters, 9 digits plus a trailing digit or
      'X' check digit.
    - Any 13-digit value starting with 893 (the Vietnamese EAN/product
      barcode range) is explicitly rejected even though it is
      superficially the right length -- CLAUDE.md section 2.3.
    """
    normalized = normalize_isbn(value)

    if len(normalized) == 13 and normalized.isdigit():
        if normalized.startswith(_BARCODE_PREFIX):
            return False
        return normalized.startswith(_ISBN13_PREFIXES)

    if len(normalized) == 10:
        return normalized[:9].isdigit() and (
            normalized[9].isdigit() or normalized[9] == "X"
        )

    return False


def normalize_text(value: str | None) -> str:
    """
    Diacritic-stripping, case-folding normalization for FUZZY title/
    author comparison only (SequenceMatcher-based similarity, below).

    This intentionally strips Vietnamese diacritics (NFD decomposition,
    dropping Unicode category "Mn" combining marks) so similarity
    scoring is resilient to typing/OCR/extraction variation -- this is
    the exact normalization match_candidate_identity.py's original
    implementation used and this rule module now shares.

    Do not reuse this for customer-facing content or storage: CLAUDE.md
    section 12 requires Vietnamese diacritics be preserved everywhere
    except this narrow fuzzy-comparison use.
    """
    if not value:
        return ""

    normalized = unicodedata.normalize("NFD", value)
    normalized = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Mn"
    )
    normalized = normalized.lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return " ".join(normalized.split())


def calculate_similarity(first: str | None, second: str | None) -> float:
    """SequenceMatcher-based similarity ratio on normalized text, 0..1."""
    first_normalized = normalize_text(first)
    second_normalized = normalize_text(second)
    if not first_normalized or not second_normalized:
        return 0.0
    return round(
        SequenceMatcher(None, first_normalized, second_normalized).ratio(), 4
    )


GENERIC_AUTHOR_VALUES = {
    "nhieu tac gia",
    "dang cap nhat",
    "khong ro",
    "unknown",
    "various authors",
}


def is_specific_author(value: str | None) -> bool:
    """True when an author value names a specific person/group rather
    than a generic placeholder ("Various authors", "Updating", ...)."""
    normalized = normalize_text(value)
    return bool(normalized and normalized not in GENERIC_AUTHOR_VALUES)


# --- single-reference identity match --------------------------------------


def evaluate_single_reference_identity(
    candidate: dict[str, Any],
    reference: dict[str, Any],
) -> DecisionResult:
    """
    Compare one candidate's extracted identity against one reference.

    Confidence-weighted title/author comparison (0.65/0.35 blend,
    0.90/0.85/0.80/0.75/0.60 thresholds) matching the existing
    automated-matching implementation, with one correction: ISBN
    comparison requires looks_like_valid_isbn() on both sides, so an
    893-prefixed barcode is never treated as an ISBN match.

    evidence["match_decision"] carries the exact
    src.domain.identity_status.MatchDecision value the caller should
    persist to product_references.match_decision.
    """
    candidate_title = candidate.get("extracted_title")
    reference_title = reference.get("reference_title")
    candidate_author = candidate.get("extracted_author")
    reference_author = reference.get("reference_author")

    candidate_isbn_raw = candidate.get("possible_isbn")
    reference_isbn_raw = reference.get("reference_isbn")
    candidate_isbn = normalize_isbn(candidate_isbn_raw)
    reference_isbn = normalize_isbn(reference_isbn_raw)

    candidate_isbn_valid = looks_like_valid_isbn(candidate_isbn_raw)
    reference_isbn_valid = looks_like_valid_isbn(reference_isbn_raw)

    isbn_match = bool(
        candidate_isbn_valid
        and reference_isbn_valid
        and candidate_isbn == reference_isbn
    )
    isbn_conflict = bool(
        candidate_isbn_valid
        and reference_isbn_valid
        and candidate_isbn != reference_isbn
    )

    title_similarity = calculate_similarity(candidate_title, reference_title)
    author_similarity = calculate_similarity(candidate_author, reference_author)

    base_evidence = {
        "title_similarity": title_similarity,
        "author_similarity": author_similarity,
        "isbn_match": isbn_match,
        "isbn_conflict": isbn_conflict,
        "candidate_isbn": candidate_isbn or None,
        "reference_isbn": reference_isbn or None,
    }

    def result(
        outcome: str,
        rule_code: str,
        reason: str,
        match_decision: str,
        confidence: float,
    ) -> DecisionResult:
        return DecisionResult(
            outcome=outcome,
            rule_code=rule_code,
            reason=reason,
            evidence={**base_evidence, "match_decision": match_decision},
            confidence=confidence,
        )

    if isbn_match:
        return result(
            Outcome.AUTO_PASS,
            IDENTITY_EXACT_ISBN,
            "Candidate ISBN and reference ISBN are identical and both "
            "are valid ISBNs (not barcodes).",
            MatchDecision.MATCH,
            0.99,
        )

    if isbn_conflict:
        return result(
            Outcome.AUTO_REJECT,
            IDENTITY_CONFIRMED_NO_MATCH,
            "Candidate ISBN and reference ISBN are both valid ISBNs "
            "but differ.",
            MatchDecision.NO_MATCH,
            0.99,
        )

    if title_similarity >= 0.90 and author_similarity >= 0.90:
        return result(
            Outcome.AUTO_PASS,
            IDENTITY_EXACT_TITLE_AUTHOR,
            "Title and author match strongly.",
            MatchDecision.MATCH,
            round(title_similarity * 0.65 + author_similarity * 0.35, 4),
        )

    if title_similarity >= 0.90 and (
        not normalize_text(candidate_author) or not normalize_text(reference_author)
    ):
        return result(
            Outcome.REVIEW_REQUIRED,
            IDENTITY_INSUFFICIENT_EVIDENCE,
            "Title matches strongly, but author data is missing.",
            MatchDecision.POSSIBLE_MATCH,
            round(title_similarity * 0.85, 4),
        )

    if title_similarity >= 0.80 and author_similarity >= 0.75:
        return result(
            Outcome.REVIEW_REQUIRED,
            IDENTITY_INSUFFICIENT_EVIDENCE,
            "Title and author are similar, but the evidence is not "
            "strong enough for automatic verification.",
            MatchDecision.POSSIBLE_MATCH,
            round(title_similarity * 0.65 + author_similarity * 0.35, 4),
        )

    if title_similarity < 0.60:
        return result(
            Outcome.AUTO_REJECT,
            IDENTITY_CONFIRMED_NO_MATCH,
            "Candidate title and reference title are too different.",
            MatchDecision.NO_MATCH,
            round(1 - title_similarity, 4),
        )

    return result(
        Outcome.REVIEW_REQUIRED,
        IDENTITY_INSUFFICIENT_EVIDENCE,
        "The available metadata is not conclusive.",
        MatchDecision.MANUAL_REVIEW,
        round(title_similarity * 0.65 + author_similarity * 0.35, 4),
    )


# --- multi-reference consensus identity match -----------------------------


def evaluate_consensus_identity(
    isbn_conflict: bool,
    author_conflict: bool,
    page_count_conflict: bool,
    matching_reference_count: int,
    has_specific_author: bool,
    max_individual_confidence: float = 0.0,
) -> DecisionResult:
    """
    Decide a candidate's overall identity from multiple independent
    reference sources, given the conflict/agreement signals
    match_candidate_identity.py's consensus builder already computes
    (isbn/author/page-count conflicts across the title-matching set,
    how many independent references confirmed the title, and whether at
    least one confirms a specific -- not generic-placeholder -- author).

    Deliberately more conservative than the single-reference rule for
    isbn_conflict: multiple credible sources actively disagreeing on
    ISBN is exactly the "conflicting credible sources" scenario this
    engine must stop for, not auto-decide -- CLAUDE.md section 5.2.
    """
    evidence = {
        "isbn_conflict": isbn_conflict,
        "author_conflict": author_conflict,
        "page_count_conflict": page_count_conflict,
        "matching_reference_count": matching_reference_count,
        "has_specific_author": has_specific_author,
    }

    if isbn_conflict:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=IDENTITY_CONFLICTING_CREDIBLE_SOURCES,
            reason="References contain conflicting ISBN values.",
            evidence={**evidence, "match_decision": MatchDecision.MANUAL_REVIEW},
            confidence=0.99,
        )

    if author_conflict:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=IDENTITY_CONFLICTING_CREDIBLE_SOURCES,
            reason="Strong title matches were found, but specific author "
            "values conflict across references.",
            evidence={**evidence, "match_decision": MatchDecision.MANUAL_REVIEW},
            confidence=0.80,
        )

    if page_count_conflict:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=IDENTITY_CONFLICTING_CREDIBLE_SOURCES,
            reason="Strong title matches were found, but page counts "
            "conflict across references.",
            evidence={**evidence, "match_decision": MatchDecision.MANUAL_REVIEW},
            confidence=0.82,
        )

    if matching_reference_count >= 2 and has_specific_author:
        return DecisionResult(
            outcome=Outcome.AUTO_PASS,
            rule_code=IDENTITY_EXACT_TITLE_AUTHOR,
            reason="Identity was confirmed by multiple independent sources "
            "with matching titles, consistent metadata, and at least "
            "one specific author.",
            evidence={**evidence, "match_decision": MatchDecision.MATCH},
            confidence=0.96,
        )

    if matching_reference_count >= 2:
        return DecisionResult(
            outcome=Outcome.AUTO_PASS,
            rule_code=IDENTITY_EXACT_CANONICAL_TITLE,
            reason="Identity was confirmed by multiple independent sources "
            "with matching titles and no material metadata conflicts.",
            evidence={**evidence, "match_decision": MatchDecision.MATCH},
            confidence=0.92,
        )

    return DecisionResult(
        outcome=Outcome.REVIEW_REQUIRED,
        rule_code=IDENTITY_INSUFFICIENT_EVIDENCE,
        reason="Multi-source evidence is not yet sufficient for automatic "
        "identity verification.",
        evidence={**evidence, "match_decision": MatchDecision.POSSIBLE_MATCH},
        confidence=max_individual_confidence,
    )


# --- series/volume title matching -----------------------------------------


def evaluate_series_volume_match(
    candidate_title: str | None,
    reference_title: str | None,
    series_prefix: str | None = None,
) -> DecisionResult:
    """
    AUTO_PASS when a candidate's extracted title matches a reference
    title once a known/common series prefix is stripped from either
    side -- CLAUDE.md section 9.1: "exact volume title with only a
    known/common series prefix omitted".
    """
    if not candidate_title or not reference_title:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=IDENTITY_INSUFFICIENT_EVIDENCE,
            reason="Candidate or reference title is missing.",
            evidence={
                "candidate_title": candidate_title,
                "reference_title": reference_title,
            },
        )

    direct_similarity = calculate_similarity(candidate_title, reference_title)

    stripped_candidate = normalize_text(candidate_title)
    stripped_reference = normalize_text(reference_title)

    if series_prefix:
        prefix_normalized = normalize_text(series_prefix)
        if stripped_candidate.startswith(prefix_normalized):
            stripped_candidate = stripped_candidate[len(prefix_normalized):].strip(" -:")
        if stripped_reference.startswith(prefix_normalized):
            stripped_reference = stripped_reference[len(prefix_normalized):].strip(" -:")

    stripped_similarity = calculate_similarity(stripped_candidate, stripped_reference)
    best_similarity = max(direct_similarity, stripped_similarity)

    evidence = {
        "direct_similarity": direct_similarity,
        "stripped_similarity": stripped_similarity,
        "series_prefix": series_prefix,
    }

    if best_similarity >= 0.90:
        return DecisionResult(
            outcome=Outcome.AUTO_PASS,
            rule_code=IDENTITY_SERIES_VOLUME_MATCH,
            reason=(
                "Volume title matches once the known series prefix is "
                "accounted for."
                if stripped_similarity > direct_similarity
                else "Volume title matches the reference directly."
            ),
            evidence=evidence,
            confidence=best_similarity,
        )

    return DecisionResult(
        outcome=Outcome.REVIEW_REQUIRED,
        rule_code=IDENTITY_INSUFFICIENT_EVIDENCE,
        reason="Series/volume title does not match strongly enough, "
        "with or without the series prefix.",
        evidence=evidence,
        confidence=best_similarity,
    )


# --- edition metadata differences -----------------------------------------


def evaluate_edition_metadata_difference(
    identity_confirmed: bool,
    differing_fields: Sequence[str] = (),
) -> DecisionResult:
    """
    Identity and edition-specific metadata are separate concerns
    (CLAUDE.md section 9.3): a title may be safely IDENTITY_VERIFIED
    even when ISBN/page_count/weight/dimensions/publication year differ
    by edition. This rule AUTO_PASSes identity whenever the caller has
    already confirmed identity by some other rule; it exists to make
    explicit, in one place, that edition-field differences are never
    themselves a reason to block or require review, and to list which
    fields must be left null/pending rather than guessed.
    """
    if not identity_confirmed:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=IDENTITY_INSUFFICIENT_EVIDENCE,
            reason="Product identity is not otherwise confirmed; edition "
            "differences alone cannot establish it.",
            evidence={"differing_fields": tuple(differing_fields)},
        )

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=IDENTITY_EDITION_METADATA_DIFFERENCE,
        reason=(
            "Identity is confirmed; edition-specific fields differ by "
            "edition and are left null/pending rather than guessed."
        ),
        warnings=tuple(
            f"{field} is edition-specific and left unresolved"
            for field in differing_fields
        ),
        evidence={"differing_fields": tuple(differing_fields)},
    )


# --- combo/set identity -----------------------------------------------


def evaluate_combo_identity(
    member_results: Sequence[DecisionResult],
) -> DecisionResult:
    """
    A combo/set candidate (candidate_type BOOK_COMBO/BOOK_SET) requires
    every volume/topic in the set to have its own confirmed identity
    match -- CLAUDE.md section 9.1: "verified combo/set mapping where
    all volumes/topics correspond". AUTO_PASS only when every member
    AUTO_PASSed; a single-volume match is not sufficient evidence for
    the full sellable combo/set, even if that one volume matched
    cleanly.
    """
    if not member_results:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=IDENTITY_INSUFFICIENT_EVIDENCE,
            reason="No combo member results were supplied.",
        )

    total = len(member_results)
    matched = sum(1 for member in member_results if member.is_auto_pass)

    if matched == total:
        return DecisionResult(
            outcome=Outcome.AUTO_PASS,
            rule_code=IDENTITY_COMBO_COMPLETE_MATCH,
            reason=f"All {total} combo members have a confirmed identity match.",
            evidence={"member_count": total, "matched_count": matched},
        )

    return DecisionResult(
        outcome=Outcome.REVIEW_REQUIRED,
        rule_code=IDENTITY_COMBO_SINGLE_AMBIGUITY,
        reason=(
            f"Only {matched} of {total} combo members have a confirmed "
            "identity match; a partial match does not establish identity "
            "for the full sellable combo/set."
        ),
        evidence={"member_count": total, "matched_count": matched},
    )
