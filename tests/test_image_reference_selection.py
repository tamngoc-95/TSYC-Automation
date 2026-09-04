"""Regression tests for
src.domain.rules.image_rules.select_preferred_image_reference().

Covers scripts/download_bookstore_product_image.py's hardened multi-MATCH
resolution: a candidate with more than one MATCH product_reference (e.g.
BOOKSTORE + FAHASA, the normal shape for a TSYC historical candidate) must
be resolved deterministically by canonical source priority
(src.domain.reference_sources.REFERENCE_SOURCE_PRIORITY / CLAUDE.md
section 8.1), never by picking whichever row the database happened to
return first, never by silently ignoring an edition conflict, and never
by guessing when two same-priority references disagree.

Pure/offline: no live Supabase, no Playwright, no network. Every case
below is plain dicts in, a DecisionResult out.
"""

from __future__ import annotations

from src.domain.decisions import Outcome
from src.domain.identity_status import MatchDecision
from src.domain.rules import image_rules


def make_candidate(
    *,
    verified_title: str = "Đạo - Con Đường Không Lối",
    verified_isbn: str | None = None,
    verified_publisher: str | None = None,
    extracted_title: str | None = None,
    possible_isbn: str | None = None,
) -> dict:
    return {
        "candidate_id": "candidate-1",
        "extracted_title": extracted_title or verified_title,
        "verified_title": verified_title,
        "verified_isbn": verified_isbn,
        "verified_publisher": verified_publisher,
        "possible_isbn": possible_isbn,
    }


def make_reference(
    *,
    reference_id: str,
    source_type: str,
    source_url_id: str | None = "source-url-1",
    match_decision: str = MatchDecision.MATCH,
    reference_title: str = "Đạo - Con Đường Không Lối",
    reference_isbn: str | None = None,
    reference_publisher: str | None = None,
) -> dict:
    return {
        "reference_id": reference_id,
        "candidate_id": "candidate-1",
        "source_url_id": source_url_id,
        "source_type": source_type,
        "match_decision": match_decision,
        "reference_title": reference_title,
        "reference_isbn": reference_isbn,
        "reference_publisher": reference_publisher,
    }


# ---------------------------------------------------------------------------
# 1. One BOOKSTORE MATCH -> selected
# ---------------------------------------------------------------------------


def test_single_bookstore_match_is_selected():
    candidate = make_candidate()
    references = [make_reference(reference_id="ref-bookstore", source_type="BOOKSTORE")]

    decision = image_rules.select_preferred_image_reference(candidate, references)

    assert decision.outcome == Outcome.AUTO_PASS
    assert decision.rule_code == image_rules.IMAGE_REFERENCE_SELECTED
    assert decision.evidence["reference_id"] == "ref-bookstore"
    assert decision.evidence["source_type"] == "BOOKSTORE"


# ---------------------------------------------------------------------------
# 2. One FAHASA MATCH, no higher-priority source -> selected
# ---------------------------------------------------------------------------


def test_single_fahasa_match_selected_when_no_higher_priority_source():
    candidate = make_candidate()
    references = [make_reference(reference_id="ref-fahasa", source_type="FAHASA")]

    decision = image_rules.select_preferred_image_reference(candidate, references)

    assert decision.outcome == Outcome.AUTO_PASS
    assert decision.evidence["reference_id"] == "ref-fahasa"
    assert decision.evidence["source_type"] == "FAHASA"


# ---------------------------------------------------------------------------
# 3. BOOKSTORE + FAHASA MATCH -> BOOKSTORE wins
# ---------------------------------------------------------------------------


def test_bookstore_beats_fahasa_when_both_match():
    candidate = make_candidate()
    references = [
        make_reference(reference_id="ref-fahasa", source_type="FAHASA"),
        make_reference(reference_id="ref-bookstore", source_type="BOOKSTORE"),
    ]

    decision = image_rules.select_preferred_image_reference(candidate, references)

    assert decision.outcome == Outcome.AUTO_PASS
    assert decision.rule_code == image_rules.IMAGE_REFERENCE_SELECTED
    assert decision.evidence["reference_id"] == "ref-bookstore"
    assert decision.evidence["source_type"] == "BOOKSTORE"


# ---------------------------------------------------------------------------
# 4. PUBLISHER + BOOKSTORE -> PUBLISHER wins
# ---------------------------------------------------------------------------


def test_publisher_beats_bookstore_when_both_match():
    candidate = make_candidate()
    references = [
        make_reference(reference_id="ref-bookstore", source_type="BOOKSTORE"),
        make_reference(reference_id="ref-publisher", source_type="PUBLISHER"),
    ]

    decision = image_rules.select_preferred_image_reference(candidate, references)

    assert decision.outcome == Outcome.AUTO_PASS
    assert decision.evidence["reference_id"] == "ref-publisher"
    assert decision.evidence["source_type"] == "PUBLISHER"


# ---------------------------------------------------------------------------
# 5. Two same-priority BOOKSTORE matches, identical identity -> deterministic
#    selection via the same-ISBN tie-break
# ---------------------------------------------------------------------------


def test_same_priority_tie_resolved_when_identity_agrees():
    candidate = make_candidate(verified_isbn="9786045123456")
    references = [
        make_reference(
            reference_id="ref-bookstore-a",
            source_type="BOOKSTORE",
            reference_isbn="9786045123456",
        ),
        make_reference(
            reference_id="ref-bookstore-b",
            source_type="BOOKSTORE",
            reference_isbn="9786045123456",
        ),
    ]

    decision = image_rules.select_preferred_image_reference(candidate, references)

    assert decision.outcome == Outcome.AUTO_PASS
    assert decision.rule_code == image_rules.IMAGE_REFERENCE_TIE_BREAK_SELECTED
    assert decision.evidence["reference_id"] in {"ref-bookstore-a", "ref-bookstore-b"}

    # Determinism: the same tied input always resolves to the same row.
    decision_again = image_rules.select_preferred_image_reference(candidate, references)
    assert decision_again.evidence["reference_id"] == decision.evidence["reference_id"]


# ---------------------------------------------------------------------------
# 6. Two same-priority BOOKSTORE matches, conflicting ISBN -> REVIEW_REQUIRED
# ---------------------------------------------------------------------------


def test_same_priority_tie_with_conflicting_isbn_requires_review():
    candidate = make_candidate()
    references = [
        make_reference(
            reference_id="ref-bookstore-a",
            source_type="BOOKSTORE",
            reference_isbn="9786045123456",
        ),
        make_reference(
            reference_id="ref-bookstore-b",
            source_type="BOOKSTORE",
            reference_isbn="9786049876543",
        ),
    ]

    decision = image_rules.select_preferred_image_reference(candidate, references)

    assert decision.outcome == Outcome.REVIEW_REQUIRED
    assert decision.rule_code == image_rules.IMAGE_REFERENCE_CONFLICT
    assert set(decision.evidence["tied_reference_ids"]) == {
        "ref-bookstore-a",
        "ref-bookstore-b",
    }


# ---------------------------------------------------------------------------
# 7. Higher-priority reference has no usable image (no source_url_id) ->
#    falls through to the next valid source
# ---------------------------------------------------------------------------


def test_falls_through_when_higher_priority_reference_has_no_source_url():
    candidate = make_candidate()
    references = [
        make_reference(
            reference_id="ref-bookstore-no-url",
            source_type="BOOKSTORE",
            source_url_id=None,
        ),
        make_reference(reference_id="ref-fahasa", source_type="FAHASA"),
    ]

    decision = image_rules.select_preferred_image_reference(candidate, references)

    assert decision.outcome == Outcome.AUTO_PASS
    assert decision.evidence["reference_id"] == "ref-fahasa"
    assert decision.evidence["source_type"] == "FAHASA"


# ---------------------------------------------------------------------------
# 8. Non-MATCH reference never selected
# ---------------------------------------------------------------------------


def test_non_match_reference_never_selected():
    candidate = make_candidate()
    references = [
        make_reference(
            reference_id="ref-publisher-possible",
            source_type="PUBLISHER",
            match_decision=MatchDecision.POSSIBLE_MATCH,
        ),
        make_reference(reference_id="ref-bookstore", source_type="BOOKSTORE"),
    ]

    decision = image_rules.select_preferred_image_reference(candidate, references)

    assert decision.outcome == Outcome.AUTO_PASS
    assert decision.evidence["reference_id"] == "ref-bookstore"


# ---------------------------------------------------------------------------
# 9. Unsupported/unrecognized source_type never selected
# ---------------------------------------------------------------------------


def test_unrecognized_source_type_never_selected():
    candidate = make_candidate()
    references = [
        make_reference(reference_id="ref-unknown", source_type="SCRAPED_BLOG"),
        make_reference(reference_id="ref-fahasa", source_type="FAHASA"),
    ]

    decision = image_rules.select_preferred_image_reference(candidate, references)

    assert decision.outcome == Outcome.AUTO_PASS
    assert decision.evidence["reference_id"] == "ref-fahasa"


def test_no_usable_reference_is_blocked():
    candidate = make_candidate()
    references = [
        make_reference(
            reference_id="ref-unusable",
            source_type="BOOKSTORE",
            source_url_id=None,
        ),
        make_reference(
            reference_id="ref-not-match",
            source_type="FAHASA",
            match_decision=MatchDecision.POSSIBLE_MATCH,
        ),
    ]

    decision = image_rules.select_preferred_image_reference(candidate, references)

    assert decision.outcome == Outcome.BLOCKED
    assert decision.rule_code == image_rules.IMAGE_REFERENCE_NONE_USABLE


# ---------------------------------------------------------------------------
# 10. Idempotency: rerunning selection over identical, unambiguous input
#     always yields the identical decision (no duplicate/varying result).
# ---------------------------------------------------------------------------


def test_selection_is_idempotent_across_repeated_calls():
    candidate = make_candidate()
    references = [
        make_reference(reference_id="ref-fahasa", source_type="FAHASA"),
        make_reference(reference_id="ref-bookstore", source_type="BOOKSTORE"),
    ]

    first = image_rules.select_preferred_image_reference(candidate, references)
    second = image_rules.select_preferred_image_reference(candidate, references)

    assert first.outcome == second.outcome == Outcome.AUTO_PASS
    assert first.rule_code == second.rule_code
    assert first.evidence["reference_id"] == second.evidence["reference_id"]


# ---------------------------------------------------------------------------
# Phase 3 edition safety: the selected reference must still correspond to
# the candidate's own verified identity, independent of source priority.
# ---------------------------------------------------------------------------


def test_selected_reference_with_conflicting_isbn_requires_review():
    candidate = make_candidate(verified_isbn="9786045123456")
    references = [
        make_reference(
            reference_id="ref-bookstore",
            source_type="BOOKSTORE",
            reference_isbn="9786049999999",
        ),
    ]

    decision = image_rules.select_preferred_image_reference(candidate, references)

    assert decision.outcome == Outcome.REVIEW_REQUIRED
    assert decision.rule_code == image_rules.IMAGE_REFERENCE_IDENTITY_CONFLICT


def test_selected_reference_with_materially_different_title_requires_review():
    candidate = make_candidate(verified_title="Đạo - Con Đường Không Lối")
    references = [
        make_reference(
            reference_id="ref-bookstore",
            source_type="BOOKSTORE",
            reference_title="Nhân Tố Enzyme - Phương Thức Sống Lành Mạnh",
        ),
    ]

    decision = image_rules.select_preferred_image_reference(candidate, references)

    assert decision.outcome == Outcome.REVIEW_REQUIRED
    assert decision.rule_code == image_rules.IMAGE_REFERENCE_IDENTITY_CONFLICT


def test_edition_only_isbn_difference_does_not_block_when_titles_agree():
    # CLAUDE.md 9.3: edition metadata differences alone don't invalidate
    # identity. Here the reference carries no ISBN at all (common for a
    # bookstore page that never listed one) and the title matches
    # exactly -- selection should proceed.
    candidate = make_candidate(verified_title="Quân Khu Nam Đồng", verified_isbn=None)
    references = [
        make_reference(
            reference_id="ref-bookstore",
            source_type="BOOKSTORE",
            reference_title="Quân Khu Nam Đồng",
            reference_isbn=None,
        ),
    ]

    decision = image_rules.select_preferred_image_reference(candidate, references)

    assert decision.outcome == Outcome.AUTO_PASS
    assert decision.evidence["reference_id"] == "ref-bookstore"
