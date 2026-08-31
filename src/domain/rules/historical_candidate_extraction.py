"""OFFLINE historical Facebook candidate-extraction preview adapter.

Pipeline (see the two modules named below for what each stage owns):

    HistoryRecord.full_text
    -> historical_text_cleaner.clean_historical_facebook_text()
    -> extraction_rules.run_automatic_extraction()          (UNCHANGED)
    -> historical_title_quality_guard.evaluate_title_quality()
    -> HistoricalExtractionResult

This module owns steps 1 and 3 of that pipeline's plumbing (calling the
cleaner, then the unchanged extraction engine, then the guard) plus its
own output-contract shaping. It deliberately contains NO new title/
author/combo/list-detection regexes or heuristics of its own -- every
extraction decision still comes from src/domain/rules/extraction_
rules.py alone; historical_text_cleaner.py only reshapes the INPUT text
extraction_rules.py's own regexes were built to expect, and historical_
title_quality_guard.py only judges an ALREADY-extracted candidate
after the fact. See both modules' own docstrings for the full
reasoning and the real historical records each was built from.

This module's own job is to:

    1. shape one historical secondary-classification FINAL INCLUDE
       record into cleaned, extraction-ready text,
    2. map extraction_rules.py's PostType/Outcome vocabulary onto this
       preview's own output contract (post_product_type /
       extraction_outcome), demoting to REVIEW_REQUIRED when the
       quality guard rejects every extracted candidate,
    3. cross-check the SECONDARY (Claude semantic) layer's own
       extracted_product_hints against what the deterministic extractor
       actually (and acceptably) found, so a hint that was never
       independently confirmed as a book title (e.g. a bundled non-book
       bonus item) is reported separately as a non_book_hint and never
       silently promoted into a candidate.

This is a PREVIEW-only tool: it never writes to Supabase, never creates
a product_candidates row, and never calls WooCommerce or the Claude API
-- it only reads a record's own text plus that record's already-cached
secondary-classification result.

Safety invariant enforced end to end: a candidate is created ONLY when
extraction_rules.run_automatic_extraction() itself deterministically
extracted a title from the record's own (cleaned) text, AND the title-
quality guard accepted it. A semantic extracted_product_hint is
evidence a human reviewer can see (in non_book_hints when unconfirmed)
-- it is never, by itself, sufficient to create a candidate. See
CLAUDE.md section 2.2 ("Never invent metadata") and this task's own
Phase 3 safety rules.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from src.domain.decisions import Outcome
from src.domain.rules import extraction_rules as rules
from src.domain.rules.historical_text_cleaner import clean_historical_facebook_text
from src.domain.rules.historical_title_quality_guard import evaluate_title_quality

# --- output-contract vocabulary -------------------------------------------
#
# extraction_outcome reuses Outcome's own three values unchanged (no
# remapping needed -- run_automatic_extraction() already returns exactly
# AUTO_PASS / AUTO_REJECT / REVIEW_REQUIRED).
#
# post_product_type maps extraction_rules.PostType's five internal
# values onto this preview's four-value contract. GENERAL_POST and
# AMBIGUOUS both become UNKNOWN here: neither is a *product type* a
# downstream human reviewer would act on -- they are "no product type
# could be determined," which is exactly what UNKNOWN means in this
# contract. candidate_type on each individual candidate is unaffected
# (extraction_rules.ExtractedCandidate.candidate_type is already exactly
# "SINGLE_BOOK" or "BOOK_COMBO", reused verbatim).

_POST_TYPE_TO_PRODUCT_TYPE = {
    rules.PostType.ONE_BOOK: "SINGLE_BOOK",
    rules.PostType.MULTIPLE_BOOKS: "MULTIPLE_BOOKS",
    rules.PostType.COMBO: "BOOK_COMBO",
    rules.PostType.GENERAL_POST: "UNKNOWN",
    rules.PostType.AMBIGUOUS: "UNKNOWN",
}


@dataclass(frozen=True)
class HistoricalExtractionInput:
    """Everything the historical extractor needs about one FINAL INCLUDE
    record -- sourced from the already-completed secondary-classification
    CSV plus the original parsed export record. No live lookups."""

    record_id: int
    date_text: str
    full_text: str
    deterministic_post_type: str
    deterministic_candidate_eligible: bool
    semantic_post_type: str | None
    decision_source: str
    semantic_extracted_product_hints: tuple[str, ...] = field(default_factory=tuple)
    local_image_paths: tuple[str, ...] = field(default_factory=tuple)
    local_video_paths: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class HistoricalCandidatePreview:
    """One deterministically-extracted book/combo identity, preview-only
    (not yet a product_candidates row)."""

    title_raw: str
    title_normalized: str
    candidate_type: str
    source_evidence: str
    confidence: float


@dataclass(frozen=True)
class HistoricalExtractionResult:
    """The full preview result for one FINAL INCLUDE record.

    cleaned_text is carried alongside the raw HistoricalExtractionInput.
    full_text the caller already holds -- Phase 4 auditability
    requirement: both the raw historical text and the exact
    extraction-ready text actually handed to run_automatic_extraction()
    must remain inspectable side by side."""

    record_id: int
    extraction_outcome: str
    post_product_type: str
    cleaned_text: str = ""
    candidates: tuple[HistoricalCandidatePreview, ...] = field(default_factory=tuple)
    non_book_hints: tuple[str, ...] = field(default_factory=tuple)
    review_reasons: tuple[str, ...] = field(default_factory=tuple)


def _normalize_title_for_display(title: str) -> str:
    """NFC-normalize and collapse whitespace -- reuses extraction_rules'
    own normalize_text() (whitespace) plus the same NFC form
    normalize_unicode_text() converges to, without re-deriving either.
    Case and wording are preserved -- CLAUDE.md/task requirement: never
    alter a title's actual wording, only its Unicode form/whitespace."""
    return unicodedata.normalize("NFC", rules.normalize_text(title))


def _fold(value: str) -> str:
    return rules.normalize_text(value).casefold()


def _hint_is_confirmed(hint: str, candidate_titles: tuple[str, ...]) -> bool:
    """A semantic hint counts as independently confirmed only when it
    overlaps (case-insensitively, either direction) with a title the
    deterministic extractor itself found -- never the other way around.
    This is a read-only cross-check, not a second extraction path: it
    can never cause a candidate to be created, only decide whether a
    hint is reported as an (unconfirmed) non_book_hint."""
    folded_hint = _fold(hint)

    if not folded_hint:
        return False

    for title in candidate_titles:
        folded_title = _fold(title)

        if not folded_title:
            continue

        if folded_hint in folded_title or folded_title in folded_hint:
            return True

    return False


def _build_source_evidence(candidate: "rules.ExtractedCandidate") -> str:
    """A short, honest description of what pattern actually matched --
    never a fabricated summary. Safe to show a human reviewer as-is."""
    parts = [f"pattern={candidate.matched_pattern}"]

    if candidate.extracted_author:
        parts.append(f"author={candidate.extracted_author}")

    if candidate.possible_isbn:
        parts.append(f"isbn={candidate.possible_isbn}")

    return "; ".join(parts)


def extract_historical_candidates(
    extraction_input: HistoricalExtractionInput,
) -> HistoricalExtractionResult:
    """Run the existing deterministic extraction engine over one FINAL
    INCLUDE historical record's CLEANED text and shape the result into
    this preview's output contract. Pure function -- no I/O, no
    network, no database, no Claude API call."""
    cleaned_text = clean_historical_facebook_text(extraction_input.full_text)

    run_result = rules.run_automatic_extraction(cleaned_text)

    post_product_type = _POST_TYPE_TO_PRODUCT_TYPE[run_result.post_type]

    accepted_candidates: list[HistoricalCandidatePreview] = []
    quality_rejection_reasons: list[str] = []

    for candidate in run_result.candidates:
        verdict = evaluate_title_quality(
            candidate.extracted_title,
            matched_pattern=candidate.matched_pattern,
            extracted_author=candidate.extracted_author,
        )

        if not verdict.is_acceptable:
            quality_rejection_reasons.append(
                f"Rejected candidate {candidate.extracted_title!r}: "
                f"{verdict.rejection_reason}"
            )
            continue

        accepted_candidates.append(
            HistoricalCandidatePreview(
                title_raw=candidate.extracted_title,
                title_normalized=_normalize_title_for_display(candidate.extracted_title),
                candidate_type=candidate.candidate_type,
                source_evidence=_build_source_evidence(candidate),
                confidence=candidate.extraction_confidence,
            )
        )

    candidates = tuple(accepted_candidates)

    # The quality guard rejected everything the engine found (or the
    # engine found nothing) -- this record can never be a confident
    # AUTO_PASS preview regardless of what the engine's own outcome
    # was. Preferring REVIEW_REQUIRED over a wrong AUTO_PASS is this
    # task's own explicit Phase 6 requirement.
    if not candidates and run_result.decision.outcome == Outcome.AUTO_PASS:
        extraction_outcome = Outcome.REVIEW_REQUIRED
        post_product_type = "UNKNOWN"
    else:
        extraction_outcome = run_result.decision.outcome

    has_local_media = bool(
        extraction_input.local_image_paths or extraction_input.local_video_paths
    )
    upgraded_from_auto_reject = False

    # Record #1287-style: after removing whole-line boilerplate (e.g. a
    # bare "Sách Có Sẵn tại Đức" post-image-caption with nothing else),
    # cleaned_text can end up empty -- run_automatic_extraction()
    # correctly calls that AUTO_REJECT on the text alone. But this
    # record already passed FINAL INCLUDE and, per its own local media
    # paths, may carry attached photos this text-only preview can never
    # see. A confident "not relevant" (AUTO_REJECT) would be wrong here
    # -- REVIEW_REQUIRED (defer to a human's image review) is the safe
    # call, never AUTO_PASS.
    if extraction_outcome == Outcome.AUTO_REJECT and has_local_media:
        extraction_outcome = Outcome.REVIEW_REQUIRED
        post_product_type = "UNKNOWN"
        upgraded_from_auto_reject = True

    candidate_titles = tuple(candidate.title_raw for candidate in candidates)

    non_book_hints = tuple(
        hint
        for hint in extraction_input.semantic_extracted_product_hints
        if hint.strip() and not _hint_is_confirmed(hint, candidate_titles)
    )

    review_reasons: list[str] = []

    if extraction_outcome != Outcome.AUTO_PASS:
        review_reasons.append(run_result.decision.reason)

    if upgraded_from_auto_reject:
        review_reasons.append(
            "No usable text remained after removing known boilerplate, but "
            "this record has attached local media -- deferring to human/"
            "image review instead of an automatic rejection."
        )

    review_reasons.extend(quality_rejection_reasons)

    if not candidates and extraction_input.semantic_extracted_product_hints:
        review_reasons.append(
            "This record's FINAL INCLUDE decision came from the semantic "
            "layer's extracted_product_hints, but none of those hints "
            "were independently confirmed by deterministic title "
            "extraction -- see non_book_hints and local media paths for "
            "manual/image review."
        )

    return HistoricalExtractionResult(
        record_id=extraction_input.record_id,
        extraction_outcome=extraction_outcome,
        post_product_type=post_product_type,
        cleaned_text=cleaned_text,
        candidates=candidates,
        non_book_hints=non_book_hints,
        review_reasons=tuple(review_reasons),
    )
