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
    IDENTITY_NO_USABLE_EVIDENCE             REVIEW_REQUIRED
    IDENTITY_CONFLICT_TITLE_ONLY_RECOVERABLE AUTO_PASS
    IDENTITY_CONFLICT_NOT_RECOVERABLE        BLOCKED

See docs/TSYC_DECISION_MATRIX.md for the full specification.

evaluate_candidate_identity() is the hardened, cumulative aggregate: given
a candidate and ALL of its currently registered references, it recomputes
the whole decision from scratch every time (order-independent, idempotent)
instead of trusting whichever single reference a caller happens to process
next. See its own docstring for the full rationale -- it exists to fix two
confirmed production incidents where a reference with missing metadata
(an empty/failed crawl) was silently evaluated as a real disagreement and
overwrote or discarded an already-established, valid match.

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
# A candidate has zero references with enough metadata (a title) to
# compare against -- missing evidence, never treated as negative
# evidence. See evaluate_candidate_identity() / is_reference_evaluable().
IDENTITY_NO_USABLE_EVIDENCE = "IDENTITY_NO_USABLE_EVIDENCE"
# scripts/correct_candidate_extraction.py's IDENTITY_CONFLICT recovery
# gate -- see assess_conflict_is_title_only_recoverable().
IDENTITY_CONFLICT_TITLE_ONLY_RECOVERABLE = "IDENTITY_CONFLICT_TITLE_ONLY_RECOVERABLE"
IDENTITY_CONFLICT_NOT_RECOVERABLE = "IDENTITY_CONFLICT_NOT_RECOVERABLE"


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


# --- publisher comparison ---------------------------------------------

# Whole leading phrases only -- a bare token like "nha" or "ban" is never
# stripped mid-name, since that would corrupt a real publisher name (e.g.
# "Nhã Nam" would otherwise be mangled into "Nam"). Longest phrases first
# so "nha xuat ban" strips as one unit rather than leaving "xuat ban".
_PUBLISHER_LEADING_PHRASES = (
    "nha xuat ban ",
    "nxb ",
    "cong ty tnhh ",
    "cong ty ",
    "cty ",
)


def normalize_publisher(value: str | None) -> str:
    """
    Diacritic/case-normalize a publisher/imprint string and strip a
    common leading legal-form phrase ("NXB", "Nhà Xuất Bản", "Công Ty",
    ...) so "NXB Hội Nhà Văn" and "Hội Nhà Văn" compare equal.

    Deliberately conservative -- punctuation/case/legal-prefix
    normalization only, exactly the TSYC identity-hardening policy's
    stated boundary. It does NOT attempt semantic equivalence (e.g.
    treating "TPHCM" and "TP Hồ Chí Minh" as the same publisher would be
    semantic guessing, and is intentionally left as a real, surfaced
    disagreement rather than silently resolved).
    """
    normalized = normalize_text(value)
    if not normalized:
        return ""

    changed = True
    while changed:
        changed = False
        for phrase in _PUBLISHER_LEADING_PHRASES:
            if normalized.startswith(phrase):
                normalized = normalized[len(phrase):].strip()
                changed = True

    return normalized


def publishers_conflict(values: Sequence[str | None]) -> bool:
    """
    True when two or more distinct non-empty normalized publisher
    values are present in `values` -- i.e. sources materially disagree
    on publisher/imprint even after safe normalization. A single
    distinct value (however many times repeated), or no non-empty
    values at all, is never a conflict -- CLAUDE.md 2.2: unknown
    metadata is left pending, not treated as disagreement.
    """
    normalized_values = {
        normalize_publisher(value)
        for value in values
        if value and normalize_publisher(value)
    }
    return len(normalized_values) > 1


# --- production identity-verification invariant ---------------------------


class IdentityVerificationInvariantError(RuntimeError):
    """
    Raised when code attempts to persist identity_status=IDENTITY_VERIFIED
    from an AUTO_PASS decision that names no contributing reference.

    This is the fail-closed guard for the confirmed FB-HIST-2026 incident
    (2026-08-31): 5 historical candidates were written as IDENTITY_VERIFIED
    by a legitimate multi-source consensus AUTO_PASS, but the reference-
    sync step that ran afterward evaluated each reference independently
    (single-reference thresholds only) and never learned which references
    the consensus decision actually relied on -- so every reference row
    was left at POSSIBLE_MATCH while the candidate claimed VERIFIED. See
    assert_verifiable() and match_candidate_identity.py's
    apply_reference_match_decisions().
    """


def assert_verifiable(decision: DecisionResult) -> None:
    """
    Fail-closed production invariant (CLAUDE.md sections 9, 10, 13; the
    same rule scripts/audit_pipeline_state.py enforces after the fact as
    VERIFIED_IDENTITY_WITHOUT_MATCH_REFERENCE): a candidate may enter
    IDENTITY_VERIFIED only when the decision that produced it can name at
    least one reference whose own match_decision the caller can -- and
    must -- persist as MATCH.

    Every AUTO_PASS branch of evaluate_candidate_identity() populates
    evidence["contributing_reference_ids"] with exactly that evidence.
    Call this immediately before writing identity_status=IDENTITY_VERIFIED
    (scripts/match_candidate_identity.py's build_hardened_candidate_payload
    is the one canonical write path that does) so a future rule change
    that ever produces an AUTO_PASS without naming a contributing
    reference fails loudly -- IDENTITY_VERIFICATION_INVARIANT_FAILED --
    instead of silently persisting an ungrounded verified identity.

    A no-op for any non-AUTO_PASS decision: REVIEW_REQUIRED/AUTO_REJECT
    never claim IDENTITY_VERIFIED and are unaffected.
    """
    if decision.outcome != Outcome.AUTO_PASS:
        return

    if not decision.evidence.get("contributing_reference_ids"):
        raise IdentityVerificationInvariantError(
            "IDENTITY_VERIFICATION_INVARIANT_FAILED: an AUTO_PASS identity "
            f"decision (rule_code={decision.rule_code!r}) names no "
            "contributing reference -- refusing to persist "
            "IDENTITY_VERIFIED without MATCH-reference evidence."
        )


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
    publisher_conflict: bool = False,
) -> DecisionResult:
    """
    Decide a candidate's overall identity from multiple independent
    reference sources, given the conflict/agreement signals
    match_candidate_identity.py's consensus builder already computes
    (isbn/author/page-count/publisher conflicts across the title-matching
    set, how many independent references confirmed the title, and
    whether at least one confirms a specific -- not generic-placeholder
    -- author).

    Deliberately more conservative than the single-reference rule for
    isbn_conflict: multiple credible sources actively disagreeing on
    ISBN is exactly the "conflicting credible sources" scenario this
    engine must stop for, not auto-decide -- CLAUDE.md section 5.2.

    publisher_conflict defaults to False so every existing caller that
    does not (yet) compute one keeps its prior behavior unchanged --
    only match_candidate_identity.py's hardened aggregation path passes
    it. isbn_conflict here must already be computed from *validated*
    ISBNs only (looks_like_valid_isbn()) by the caller -- this function
    does not re-validate; see evaluate_candidate_identity().
    """
    evidence = {
        "isbn_conflict": isbn_conflict,
        "author_conflict": author_conflict,
        "page_count_conflict": page_count_conflict,
        "publisher_conflict": publisher_conflict,
        "matching_reference_count": matching_reference_count,
        "has_specific_author": has_specific_author,
    }

    if isbn_conflict:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=IDENTITY_CONFLICTING_CREDIBLE_SOURCES,
            reason="References contain conflicting valid ISBN values.",
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

    if publisher_conflict:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=IDENTITY_CONFLICTING_CREDIBLE_SOURCES,
            reason="Strong title matches were found, but publisher/imprint "
            "values conflict across references after normalization.",
            evidence={**evidence, "match_decision": MatchDecision.MANUAL_REVIEW},
            confidence=0.80,
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


# --- cumulative, idempotent aggregate identity decision --------------------


def is_reference_evaluable(reference: dict[str, Any]) -> bool:
    """
    True when a reference carries enough metadata (a title) to be
    positively or negatively compared against a candidate.

    A reference with no title (a failed/empty crawl -- missing
    evidence) must never be evaluated as if it were a real
    disagreement. calculate_similarity() against an empty/None title
    always returns 0.0, which evaluate_single_reference_identity()
    would then read as "confirmed no match" even though nothing about
    the actual book was ever compared -- that is missing evidence, not
    negative evidence (CLAUDE.md 2.2: unknown must stay null/pending,
    never be fabricated into a decision).

    This is the confirmed root cause of two live production incidents
    on historical candidates: a reference whose crawl came back empty
    was evaluated as a positive disagreement and silently discarded (or
    overwrote) an already-established, valid POSSIBLE_MATCH from
    another reference. See evaluate_candidate_identity(), which
    excludes unusable references from evaluation entirely while still
    preserving them, unevaluated, for audit.
    """
    return bool((reference.get("reference_title") or "").strip())


_TITLE_CONTAINMENT_MIN_COVERAGE = 0.8


def _is_short_title_contained_in_long(
    short_title: str | None,
    long_title: str | None,
    min_coverage: float = _TITLE_CONTAINMENT_MIN_COVERAGE,
) -> bool:
    """
    True when the shorter title's significant words are substantially
    present in the longer title's words -- i.e. the shorter title reads
    as an abbreviation of the longer one, not a different book.
    Word-set containment, so it is order- and punctuation-insensitive:
    exactly what a title shortened for a Facebook post (or a reference
    site's much longer official listing, subtitle, series name, or
    edition tag) looks like next to a full title. Requires at least one
    significant word on each side -- two empty/near-empty titles never
    "contain" each other.

    This is the general form of the CAN-0004 ("Power vs. Force")
    incident from _rejecting_references_corroborate_each_other, but
    applies per-reference rather than only when 2+ references reject in
    agreement -- it is what's needed to correctly handle a single-
    reference candidate whose extracted title is fully contained in a
    much longer official reference title (confirmed live on
    "Học Montessori - 100 Hoạt Động Montessori", contained inside "Học
    Montessori Để Dạy Trẻ Theo Phương Pháp Montessori - 100 Hoạt Động
    Montessori: Con Không Cần Ipad Để Lớn Khôn").
    """
    short_tokens = set(normalize_text(short_title).split())
    long_tokens = set(normalize_text(long_title).split())

    if not short_tokens or not long_tokens:
        return False

    covered = len(short_tokens & long_tokens) / len(short_tokens)
    return covered >= min_coverage


_REFERENCE_CROSS_CORROBORATION_THRESHOLD = 0.60


def _rejecting_references_corroborate_each_other(
    confirmed_no_matches: Sequence[tuple[dict[str, Any], DecisionResult]],
) -> bool:
    """
    True when 2+ references that each individually AUTO_REJECTed against
    the candidate nonetheless largely agree with EACH OTHER on title.

    Live incident this guards against: historical candidate CAN-0004,
    extracted_title "Power vs. Force" (short, from a Facebook post).
    Both of its registered references were confidently AUTO_REJECTed
    individually -- title_similarity between "power vs force" and each
    reference's full official title (e.g. "Power Vs Force - Trường Năng
    Lượng Và Những Nhân Tố Quyết Định Hành Vi Của Con Người") fell below
    the 0.60 threshold purely because of the huge length difference, not
    because the book is actually different. Both references agreed with
    each other on author, publisher, and page count -- and, decisively,
    scored similar to EACH OTHER on title (both share the same long
    official title pattern) even though neither scored similar to the
    short candidate title. That is exactly the "candidate's own title is
    abbreviated" signal, not "multiple credible sources confirm this is
    a different book" -- collapsing straight to AUTO_REJECT there was
    itself a false-conflict bug of the same shape this module exists to
    eliminate elsewhere.

    A single confirmed_no_matches entry (nothing to corroborate against)
    always returns False -- unchanged, confident AUTO_REJECT behavior.

    Title similarity is not the only corroboration signal checked. Live
    incident: historical candidate CAN-0007, extracted_title "Giận" (a
    real, very short one-word Vietnamese title). Its two references were
    "Giận (Tái Bản 2023)" and "Giận - Thích Nhất Hạnh" -- each
    individually AUTO_REJECTed against the short candidate title (same
    length-mismatch shape as CAN-0004), but this time the two references
    ALSO score low similarity against EACH OTHER, because one appends a
    reprint year and the other appends an author-name suffix -- entirely
    different kinds of extra text, so the title-similarity check above
    alone does not catch it. Both references nonetheless explicitly
    agree on the same specific (non-generic) reference_author, "Thích
    Nhất Hạnh" -- independent corroboration they describe the same book
    even though their title strings don't resemble each other. When
    every rejecting reference names the same one specific author, that
    counts as corroboration too.
    """
    if len(confirmed_no_matches) < 2:
        return False

    titles = [reference.get("reference_title") for reference, _res in confirmed_no_matches]

    if any(
        calculate_similarity(titles[i], titles[j])
        >= _REFERENCE_CROSS_CORROBORATION_THRESHOLD
        for i in range(len(titles))
        for j in range(i + 1, len(titles))
    ):
        return True

    authors = [reference.get("reference_author") for reference, _res in confirmed_no_matches]
    if all(is_specific_author(author) for author in authors):
        normalized_authors = {normalize_text(author) for author in authors}
        if len(normalized_authors) == 1:
            return True

    return False


def evaluate_candidate_identity(
    candidate: dict[str, Any],
    references: Sequence[dict[str, Any]],
) -> DecisionResult:
    """
    The single, cumulative, order-independent identity decision for one
    candidate given ALL of its currently registered references.

    Recomputes fully from the current reference set every call instead
    of trusting whichever one reference a caller happens to process
    next -- the result depends only on the current set of references
    and their metadata, never on processing order or which reference
    was evaluated most recently. That makes it both idempotent (the
    same reference set always yields the same decision) and monotonic
    with respect to missing evidence (a later reference that turns out
    to have no usable metadata can never erase or downgrade an earlier
    valid decision -- it is simply excluded, see is_reference_evaluable).

    Every usable reference is evaluated with the existing, unmodified
    evaluate_single_reference_identity() -- no threshold in that
    function is changed here; this only changes which references are
    fed to it and how multiple per-reference results are combined.

    Decision path:

    1. Any usable reference individually AUTO_PASSes (ISBN match, or
       title+author >= 0.90/0.90) and no other usable reference is a
       confident AUTO_REJECT: AUTO_PASS (MATCH), using that reference.
    2. An individual AUTO_PASS AND a confident AUTO_REJECT both exist
       among usable references: a genuine conflict between credible
       sources, not ambiguity from missing data -- REVIEW_REQUIRED
       (IDENTITY_CONFLICTING_CREDIBLE_SOURCES, evidence
       has_genuine_conflict=True).
    3. Otherwise, multi-source consensus is attempted across usable
       references with individual title_similarity >= 0.90 via the
       existing evaluate_consensus_identity(), now also checking a
       *validated*-ISBN conflict (looks_like_valid_isbn()-gated -- an
       unvalidated barcode never counts as disagreement and never
       becomes canonical, fixing a confirmed unvalidated-ISBN-
       promotion bug) and a publisher conflict (publishers_conflict(),
       simple legal-prefix normalization only, never semantic
       guessing, fixing a confirmed silent-publisher-conflict bug).
    4. Every usable reference (there is at least one) is a confident
       AUTO_REJECT: AUTO_REJECT (IDENTITY_CONFIRMED_NO_MATCH,
       has_genuine_conflict=True) -- a real, evaluated disagreement.
    5. No usable reference at all: REVIEW_REQUIRED
       (IDENTITY_NO_USABLE_EVIDENCE, has_genuine_conflict=False) --
       missing evidence, never negative evidence.
    6. Otherwise: REVIEW_REQUIRED (IDENTITY_INSUFFICIENT_EVIDENCE or
       IDENTITY_CONFLICTING_CREDIBLE_SOURCES), carrying the strongest
       non-rejected individual reference's reasoning, or the consensus
       conflict reasoning when a real isbn/author/page_count/publisher
       disagreement was found among title-matching references.

    evidence["usable_reference_count"] / ["unusable_reference_count"]
    and evidence["has_genuine_conflict"] let a caller distinguish "no
    change needed" / "just needs more evidence" from "a real
    disagreement was found" without re-deriving that from rule_code.
    """
    usable: list[dict[str, Any]] = []
    unusable: list[dict[str, Any]] = []
    for reference in references:
        (usable if is_reference_evaluable(reference) else unusable).append(reference)

    base_evidence: dict[str, Any] = {
        "usable_reference_count": len(usable),
        "unusable_reference_count": len(unusable),
        "unusable_reference_ids": [r.get("reference_id") for r in unusable],
    }

    if not usable:
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=IDENTITY_NO_USABLE_EVIDENCE,
            reason=(
                "No registered reference contains enough metadata (a "
                "title) to compare against this candidate."
            ),
            evidence={
                **base_evidence,
                "has_genuine_conflict": False,
                "match_decision": MatchDecision.MANUAL_REVIEW,
            },
            confidence=0.0,
        )

    per_reference: list[tuple[dict[str, Any], DecisionResult]] = [
        (reference, evaluate_single_reference_identity(candidate, reference))
        for reference in usable
    ]

    candidate_title = candidate.get("extracted_title")

    def _is_confirmed_reject(reference: dict[str, Any], result: DecisionResult) -> bool:
        """
        A result only counts as a *confirmed* reject -- real negative
        evidence -- if it isn't explained away by the candidate's own
        title being a short/abbreviated version of the reference's
        title, or vice versa (see _is_short_title_contained_in_long's
        docstring for the live incident this fixes: a single-reference
        candidate whose extracted title is fully contained in a much
        longer official title, e.g. "Học Montessori - 100 Hoạt Động
        Montessori" inside "Học Montessori Để Dạy Trẻ Theo Phương Pháp
        Montessori - 100 Hoạt Động Montessori: Con Không Cần Ipad Để
        Lớn Khôn", was landing as a confident AUTO_REJECT with only one
        reference to compare against -- title length mismatch alone,
        not evidence of a different book). An ISBN-conflict reject is
        never downgraded this way -- that is independent, decisive
        negative evidence unrelated to title wording.
        """
        if not result.is_auto_reject:
            return False
        if result.evidence.get("isbn_conflict"):
            return True
        reference_title = reference.get("reference_title")
        if _is_short_title_contained_in_long(
            candidate_title, reference_title
        ) or _is_short_title_contained_in_long(reference_title, candidate_title):
            return False
        return True

    strong_passes = [(r, res) for r, res in per_reference if res.is_auto_pass]
    confirmed_no_matches = [
        (r, res) for r, res in per_reference if _is_confirmed_reject(r, res)
    ]

    if strong_passes and confirmed_no_matches:
        best_pass_ref, _best_pass_res = max(
            strong_passes, key=lambda pair: pair[1].confidence or 0.0
        )
        worst_reject_ref, _worst_reject_res = max(
            confirmed_no_matches, key=lambda pair: pair[1].confidence or 0.0
        )
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=IDENTITY_CONFLICTING_CREDIBLE_SOURCES,
            reason=(
                "One reference strongly confirms this candidate's identity "
                "while another, independently evaluated reference "
                "confidently disagrees -- a genuine conflict between "
                "credible sources, not missing evidence."
            ),
            evidence={
                **base_evidence,
                "has_genuine_conflict": True,
                "match_decision": MatchDecision.MANUAL_REVIEW,
                "matching_reference_id": best_pass_ref.get("reference_id"),
                "conflicting_reference_id": worst_reject_ref.get("reference_id"),
            },
            confidence=0.85,
        )

    if strong_passes:
        best_ref, best_res = max(
            strong_passes, key=lambda pair: pair[1].confidence or 0.0
        )
        return DecisionResult(
            outcome=Outcome.AUTO_PASS,
            rule_code=best_res.rule_code,
            reason=best_res.reason,
            evidence={
                **base_evidence,
                **best_res.evidence,
                "has_genuine_conflict": False,
                "matching_reference_id": best_ref.get("reference_id"),
                # The one reference whose own individual evaluation
                # AUTO_PASSed -- the caller must persist match_decision=
                # MATCH on exactly this reference. See
                # assert_verifiable()/contributing_reference_ids on the
                # consensus branch below for why this must never be
                # empty on an AUTO_PASS.
                "contributing_reference_ids": [best_ref.get("reference_id")],
            },
            confidence=best_res.confidence,
        )

    # No individual strong pass -- attempt multi-source consensus among
    # usable references that individually look like a title match.
    title_matches = [
        (r, res)
        for r, res in per_reference
        if res.evidence.get("title_similarity", 0.0) >= 0.90
        and not res.evidence.get("isbn_conflict", False)
    ]

    candidate_isbn_raw = candidate.get("possible_isbn")
    candidate_isbn = normalize_isbn(candidate_isbn_raw)
    candidate_isbn_valid = looks_like_valid_isbn(candidate_isbn_raw)

    valid_isbn_values: set[str] = set()
    for reference, _res in title_matches:
        raw_isbn = reference.get("reference_isbn")
        if looks_like_valid_isbn(raw_isbn):
            valid_isbn_values.add(normalize_isbn(raw_isbn))

    isbn_conflict = len(valid_isbn_values) > 1 or bool(
        candidate_isbn_valid
        and candidate_isbn
        and valid_isbn_values
        and candidate_isbn not in valid_isbn_values
    )

    specific_authors = [
        reference.get("reference_author")
        for reference, _res in title_matches
        if is_specific_author(reference.get("reference_author"))
    ]
    normalized_specific_authors = {
        normalize_text(author) for author in specific_authors if author
    }
    author_conflict = len(normalized_specific_authors) > 1

    matching_page_counts = {
        reference.get("reference_page_count")
        for reference, _res in title_matches
        if reference.get("reference_page_count") is not None
    }
    page_count_conflict = len(matching_page_counts) > 1

    publisher_conflict = publishers_conflict(
        [reference.get("reference_publisher") for reference, _res in title_matches]
    )

    max_individual_confidence = max(
        (res.confidence or 0.0 for _r, res in per_reference), default=0.0
    )

    consensus = evaluate_consensus_identity(
        isbn_conflict=isbn_conflict,
        author_conflict=author_conflict,
        page_count_conflict=page_count_conflict,
        publisher_conflict=publisher_conflict,
        matching_reference_count=len(title_matches),
        has_specific_author=bool(specific_authors),
        max_individual_confidence=max_individual_confidence,
    )

    has_conflict_signal = (
        isbn_conflict or author_conflict or page_count_conflict or publisher_conflict
    )

    if consensus.is_auto_pass:
        best_ref = choose_best_reference([r for r, _res in title_matches])
        return DecisionResult(
            outcome=Outcome.AUTO_PASS,
            rule_code=consensus.rule_code,
            reason=consensus.reason,
            evidence={
                **base_evidence,
                **consensus.evidence,
                "has_genuine_conflict": False,
                "matching_reference_id": best_ref.get("reference_id"),
                "valid_isbn_values": sorted(valid_isbn_values),
                # EVERY reference that contributed to this multi-source
                # consensus AUTO_PASS -- not just the "best" one chosen
                # for canonical metadata above. The caller must persist
                # match_decision=MATCH on each of these. Without this,
                # a candidate can become IDENTITY_VERIFIED purely from
                # combined multi-source evidence while every individual
                # reference row is left at whatever
                # evaluate_single_reference_identity() alone would say
                # for it (often POSSIBLE_MATCH, since consensus exists
                # precisely to combine signals no single reference
                # reaches alone) -- the confirmed FB-HIST-2026 root
                # cause of 5 candidates verified with zero MATCH
                # references. See assert_verifiable().
                "contributing_reference_ids": [
                    r.get("reference_id") for r, _res in title_matches
                ],
            },
            confidence=consensus.confidence,
        )

    if (
        confirmed_no_matches
        and len(confirmed_no_matches) == len(per_reference)
        and not _rejecting_references_corroborate_each_other(confirmed_no_matches)
    ):
        # Every usable reference -- and there is at least one -- is a
        # real, evaluated disagreement, AND (when there are 2+) they
        # don't even agree with each other. This is the only path an
        # AUTO_REJECT survives aggregation: no usable reference supports
        # the candidate, and there is no sign the rejection is an
        # artifact of the candidate's own extracted title being short/
        # abbreviated rather than the book actually being different --
        # see _rejecting_references_corroborate_each_other's docstring
        # for the live incident (CAN-0004 "Power vs. Force") this guards
        # against. CLAUDE.md 5.2: do not stop on low string similarity
        # alone when stronger evidence (here: multiple sources agreeing
        # with each other) resolves identity.
        _worst_ref, worst_res = max(
            confirmed_no_matches, key=lambda pair: pair[1].confidence or 0.0
        )
        return DecisionResult(
            outcome=Outcome.AUTO_REJECT,
            rule_code=worst_res.rule_code,
            reason=worst_res.reason,
            evidence={
                **base_evidence,
                **worst_res.evidence,
                "has_genuine_conflict": True,
            },
            confidence=worst_res.confidence,
        )

    if confirmed_no_matches and len(confirmed_no_matches) == len(per_reference):
        # Every usable reference individually rejected the candidate,
        # but (per the guard above) they corroborate each other on
        # title -- this is evidence the candidate's own extracted title
        # is short/abbreviated relative to each reference's full
        # official title, not evidence the book itself is different.
        # REVIEW_REQUIRED with its own accurate reasoning -- never
        # IDENTITY_CONFIRMED_NO_MATCH's rule_code/reason, which would
        # misdescribe a REVIEW_REQUIRED outcome as a confirmed reject.
        return DecisionResult(
            outcome=Outcome.REVIEW_REQUIRED,
            rule_code=IDENTITY_INSUFFICIENT_EVIDENCE,
            reason=(
                "Every reference individually scored low title "
                "similarity against the candidate's extracted title, "
                "but the references agree closely with each other -- "
                "the candidate's own extracted title is likely "
                "abbreviated rather than the book being different."
            ),
            evidence={
                **base_evidence,
                "has_genuine_conflict": False,
                "match_decision": MatchDecision.POSSIBLE_MATCH,
            },
            confidence=max(res.confidence or 0.0 for _r, res in confirmed_no_matches),
        )

    # Insufficient (or conflicting-but-inconclusive) evidence -- review.
    # Prefer a non-rejected result for the fallback reasoning/confidence
    # so a confirmed-reject's confidence never masquerades as "the best
    # available review reasoning" for an outcome that isn't a rejection.
    review_candidates = [
        (r, res) for r, res in per_reference if not _is_confirmed_reject(r, res)
    ] or per_reference
    best_ref, best_res = max(
        review_candidates, key=lambda pair: pair[1].confidence or 0.0
    )

    # best_res may be a reclassified (title-containment) reject -- its
    # own rule_code/reason still literally say "too different", which
    # would misdescribe a REVIEW_REQUIRED outcome as a confirmed
    # rejection. Use accurate, generic reasoning for that case instead.
    if has_conflict_signal:
        fallback_rule_code = consensus.rule_code
        fallback_reason = consensus.reason
    elif best_res.is_auto_reject:
        fallback_rule_code = IDENTITY_INSUFFICIENT_EVIDENCE
        fallback_reason = (
            "The candidate's extracted title is short relative to the "
            "reference's full title, so title similarity alone is "
            "inconclusive; the available metadata is not conclusive."
        )
    else:
        fallback_rule_code = best_res.rule_code
        fallback_reason = best_res.reason

    return DecisionResult(
        outcome=Outcome.REVIEW_REQUIRED,
        rule_code=fallback_rule_code,
        reason=fallback_reason,
        evidence={
            **base_evidence,
            **(consensus.evidence if has_conflict_signal else best_res.evidence),
            "has_genuine_conflict": has_conflict_signal,
            "matching_reference_id": best_ref.get("reference_id"),
        },
        confidence=consensus.confidence if has_conflict_signal else best_res.confidence,
    )


def choose_best_reference(references: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """
    Choose the richest reference (most populated metadata fields, with
    a specific author weighted extra) among a non-empty set -- shared by
    evaluate_candidate_identity() and scripts/match_candidate_identity.py
    so both use the exact same "which reference's metadata becomes
    canonical" rule.
    """
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

        if is_specific_author(reference.get("reference_author")):
            metadata_score += 3

        priority = int(reference.get("source_priority") or 99)

        return (metadata_score, -priority)

    return max(references, key=score)


# --- IDENTITY_CONFLICT extraction-recovery gate -----------------------
#
# scripts/correct_candidate_extraction.py refuses to touch any candidate
# outside workflow_status=EXTRACTED / identity_status=IDENTITY_PENDING --
# by design, it never guesses whether a later state is still safe to
# correct (CLAUDE.md golden principle #6/#7: never silently overwrite
# already-decided identity). That guard is right for the general case,
# but it also blocks the one narrow, safe exception this project needs:
# a candidate whose IDENTITY_CONFLICT was caused *only* by a bad
# extracted_title (e.g. a rule-based extractor capturing a commercial/
# preorder announcement instead of the real product name), where the
# underlying reference evidence itself is not in dispute at all.
#
# assess_conflict_is_title_only_recoverable() is the validator that
# makes that exception safe: it re-derives, from the candidate's own
# currently-registered references, whether every rejection signal traces
# back to title similarity alone. Any independent ISBN, specific-author,
# or (for 2+ title-matching references) publisher disagreement blocks
# recovery outright -- a title fix can never paper over a real
# conflicting-evidence problem. This function never itself decides that a
# correction *should* happen, only whether one *may safely be attempted*.


def assess_conflict_is_title_only_recoverable(
    candidate: dict[str, Any],
    references: Sequence[dict[str, Any]],
) -> DecisionResult:
    """
    Decide whether a candidate currently at IDENTITY_CONFLICT may safely
    enter the extraction-correction recovery path.

    Reuses evaluate_single_reference_identity() (unchanged) against every
    currently usable reference -- the same per-reference evidence
    evaluate_candidate_identity() itself already computes -- rather than
    trusting the stored conflict_fields column, which build_hardened_
    candidate_payload() (scripts/match_candidate_identity.py) writes as
    the literal ["title"] for every AUTO_REJECT outcome including an
    ISBN-conflict one. That stored label is not a reliable signal here;
    this function re-derives the true cause directly from evidence.

    AUTO_PASS (IDENTITY_CONFLICT_TITLE_ONLY_RECOVERABLE) only when:
      - at least one usable reference exists, AND
      - no usable reference shows a validated-ISBN conflict, AND
      - the candidate names no specific author that conflicts with any
        usable reference's specific author, AND
      - when 2+ usable references exist, they do not conflict with each
        other on publisher/imprint (the only conflict a multi-reference
        set can raise that a single reference cannot).

    Everything else -- no usable evidence, an ISBN conflict, a specific-
    author conflict, or a publisher conflict -- is BLOCKED
    (IDENTITY_CONFLICT_NOT_RECOVERABLE): a real disagreement between
    credible sources, which extraction correction must never be used to
    route around.
    """
    usable = [reference for reference in references if is_reference_evaluable(reference)]

    if not usable:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=IDENTITY_CONFLICT_NOT_RECOVERABLE,
            reason=(
                "No usable reference evidence is registered for this "
                "candidate; there is nothing to re-verify a title "
                "correction against."
            ),
            evidence={"usable_reference_count": 0},
        )

    per_reference = [
        (reference, evaluate_single_reference_identity(candidate, reference))
        for reference in usable
    ]

    isbn_conflicts = [
        reference.get("reference_id")
        for reference, result in per_reference
        if result.evidence.get("isbn_conflict")
    ]

    if isbn_conflicts:
        return DecisionResult(
            outcome=Outcome.BLOCKED,
            rule_code=IDENTITY_CONFLICT_NOT_RECOVERABLE,
            reason=(
                "An ISBN conflict exists independent of title quality; "
                "extraction correction cannot resolve this."
            ),
            evidence={"conflicting_reference_ids": isbn_conflicts},
        )

    candidate_author = candidate.get("extracted_author")

    if is_specific_author(candidate_author):
        author_conflicts = [
            reference.get("reference_id")
            for reference, _result in per_reference
            if is_specific_author(reference.get("reference_author"))
            and normalize_text(reference.get("reference_author"))
            != normalize_text(candidate_author)
        ]

        if author_conflicts:
            return DecisionResult(
                outcome=Outcome.BLOCKED,
                rule_code=IDENTITY_CONFLICT_NOT_RECOVERABLE,
                reason=(
                    "A specific-author conflict exists independent of "
                    "title quality; extraction correction cannot resolve "
                    "this."
                ),
                evidence={"conflicting_reference_ids": author_conflicts},
            )

    if len(usable) >= 2:
        publishers = [
            reference.get("reference_publisher") for reference, _result in per_reference
        ]

        if publishers_conflict(publishers):
            return DecisionResult(
                outcome=Outcome.BLOCKED,
                rule_code=IDENTITY_CONFLICT_NOT_RECOVERABLE,
                reason=(
                    "A publisher/imprint conflict exists among this "
                    "candidate's references, independent of title "
                    "quality; extraction correction cannot resolve this."
                ),
                evidence={
                    "reference_publishers": publishers,
                },
            )

    return DecisionResult(
        outcome=Outcome.AUTO_PASS,
        rule_code=IDENTITY_CONFLICT_TITLE_ONLY_RECOVERABLE,
        reason=(
            "Every usable reference's rejection traces back to title "
            "similarity alone -- no ISBN, specific-author, or publisher "
            "conflict exists. Safe for extraction-correction recovery."
        ),
        evidence={
            "usable_reference_count": len(usable),
            "usable_reference_ids": [
                reference.get("reference_id") for reference, _result in per_reference
            ],
        },
    )
