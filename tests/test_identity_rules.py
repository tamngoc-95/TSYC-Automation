"""Offline tests for src/domain/rules/identity_rules.py.

No live Supabase/WooCommerce/Facebook dependency -- pure functions
operating on plain dicts.
"""
from __future__ import annotations

from src.domain.decisions import Outcome
from src.domain.identity_status import MatchDecision
from src.domain.rules import identity_rules as rules


# --- looks_like_valid_isbn / normalize_isbn ---------------------------


def test_valid_isbn13_prefix_978_accepted():
    assert rules.looks_like_valid_isbn("9786041234567") is True


def test_valid_isbn13_prefix_979_accepted():
    assert rules.looks_like_valid_isbn("9791234567897") is True


def test_893_prefixed_barcode_never_treated_as_isbn():
    """CLAUDE.md section 2.3: 893-prefixed identifiers are normally
    EAN/product barcodes, not ISBNs."""
    assert rules.looks_like_valid_isbn("8931234567890") is False


def test_isbn10_accepted():
    assert rules.looks_like_valid_isbn("0306406152") is True


def test_garbage_value_rejected():
    assert rules.looks_like_valid_isbn("not-an-isbn") is False
    assert rules.looks_like_valid_isbn(None) is False
    assert rules.looks_like_valid_isbn("") is False


# --- evaluate_single_reference_identity --------------------------------


def test_exact_isbn_match_auto_passes():
    candidate = {"extracted_title": "Doraemon", "possible_isbn": "978-604-1-23456-7"}
    reference = {"reference_title": "Doraemon", "reference_isbn": "9786041234567"}

    result = rules.evaluate_single_reference_identity(candidate, reference)

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.IDENTITY_EXACT_ISBN
    assert result.evidence["match_decision"] == MatchDecision.MATCH


def test_barcode_collision_does_not_trigger_isbn_match():
    """The regression this rule exists to fix: an 893-prefixed barcode
    identical on both sides must NOT be treated as an ISBN match --
    it should fall through to title/author comparison instead."""
    candidate = {
        "extracted_title": "Completely Different Book",
        "possible_isbn": "8931234567890",
    }
    reference = {
        "reference_title": "Some Other Book",
        "reference_isbn": "8931234567890",
    }

    result = rules.evaluate_single_reference_identity(candidate, reference)

    assert result.rule_code != rules.IDENTITY_EXACT_ISBN
    assert result.evidence["isbn_match"] is False


def test_exact_title_and_author_match_auto_passes():
    candidate = {
        "extracted_title": "Doraemon Tap 1",
        "extracted_author": "Fujiko F. Fujio",
    }
    reference = {
        "reference_title": "Doraemon Tap 1",
        "reference_author": "Fujiko F. Fujio",
    }

    result = rules.evaluate_single_reference_identity(candidate, reference)

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.IDENTITY_EXACT_TITLE_AUTHOR
    assert result.evidence["match_decision"] == MatchDecision.MATCH


def test_valid_isbn_conflict_is_confirmed_no_match():
    candidate = {"extracted_title": "Book A", "possible_isbn": "9786041234567"}
    reference = {"reference_title": "Book A", "reference_isbn": "9786049999999"}

    result = rules.evaluate_single_reference_identity(candidate, reference)

    assert result.outcome == Outcome.AUTO_REJECT
    assert result.rule_code == rules.IDENTITY_CONFIRMED_NO_MATCH
    assert result.evidence["match_decision"] == MatchDecision.NO_MATCH


def test_low_title_similarity_is_confirmed_no_match():
    candidate = {"extracted_title": "Doraemon Tap 1"}
    reference = {"reference_title": "Totally Unrelated Cooking Manual"}

    result = rules.evaluate_single_reference_identity(candidate, reference)

    assert result.outcome == Outcome.AUTO_REJECT
    assert result.rule_code == rules.IDENTITY_CONFIRMED_NO_MATCH


def test_strong_title_missing_author_is_insufficient_evidence():
    candidate = {"extracted_title": "Doraemon Tap 1", "extracted_author": None}
    reference = {"reference_title": "Doraemon Tap 1", "reference_author": None}

    result = rules.evaluate_single_reference_identity(candidate, reference)

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.IDENTITY_INSUFFICIENT_EVIDENCE
    assert result.evidence["match_decision"] == MatchDecision.POSSIBLE_MATCH


def test_moderate_similarity_is_insufficient_evidence():
    candidate = {"extracted_title": "Doraemon Tap 1", "extracted_author": "Fujio"}
    reference = {"reference_title": "Doraemon T1", "reference_author": "F."}

    result = rules.evaluate_single_reference_identity(candidate, reference)

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.IDENTITY_INSUFFICIENT_EVIDENCE


# --- evaluate_consensus_identity --------------------------------------


def test_consensus_isbn_conflict_requires_review():
    """Multiple credible sources disagreeing is exactly the scenario
    IDENTITY_CONFLICTING_CREDIBLE_SOURCES exists for."""
    result = rules.evaluate_consensus_identity(
        isbn_conflict=True,
        author_conflict=False,
        page_count_conflict=False,
        matching_reference_count=2,
        has_specific_author=True,
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.IDENTITY_CONFLICTING_CREDIBLE_SOURCES
    assert result.evidence["match_decision"] == MatchDecision.MANUAL_REVIEW


def test_consensus_author_conflict_requires_review():
    result = rules.evaluate_consensus_identity(
        isbn_conflict=False,
        author_conflict=True,
        page_count_conflict=False,
        matching_reference_count=2,
        has_specific_author=True,
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.IDENTITY_CONFLICTING_CREDIBLE_SOURCES


def test_consensus_multi_source_with_specific_author_auto_passes():
    result = rules.evaluate_consensus_identity(
        isbn_conflict=False,
        author_conflict=False,
        page_count_conflict=False,
        matching_reference_count=2,
        has_specific_author=True,
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.IDENTITY_EXACT_TITLE_AUTHOR


def test_consensus_multi_source_canonical_title_auto_passes():
    result = rules.evaluate_consensus_identity(
        isbn_conflict=False,
        author_conflict=False,
        page_count_conflict=False,
        matching_reference_count=2,
        has_specific_author=False,
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.IDENTITY_EXACT_CANONICAL_TITLE


def test_consensus_single_source_is_insufficient_evidence():
    result = rules.evaluate_consensus_identity(
        isbn_conflict=False,
        author_conflict=False,
        page_count_conflict=False,
        matching_reference_count=1,
        has_specific_author=False,
        max_individual_confidence=0.5,
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.IDENTITY_INSUFFICIENT_EVIDENCE
    assert result.evidence["match_decision"] == MatchDecision.POSSIBLE_MATCH


# --- evaluate_series_volume_match --------------------------------------


def test_series_volume_match_with_common_prefix_auto_passes():
    result = rules.evaluate_series_volume_match(
        candidate_title="Doraemon - Tap 5",
        reference_title="Tap 5",
        series_prefix="Doraemon",
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.IDENTITY_SERIES_VOLUME_MATCH


def test_series_volume_direct_match_auto_passes():
    result = rules.evaluate_series_volume_match(
        candidate_title="Doraemon Tap 5",
        reference_title="Doraemon Tap 5",
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.IDENTITY_SERIES_VOLUME_MATCH


def test_series_volume_weak_match_requires_review():
    result = rules.evaluate_series_volume_match(
        candidate_title="Doraemon Tap 5",
        reference_title="Conan Tap 12",
        series_prefix="Doraemon",
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.IDENTITY_INSUFFICIENT_EVIDENCE


def test_series_volume_missing_title_requires_review():
    result = rules.evaluate_series_volume_match(
        candidate_title=None,
        reference_title="Tap 5",
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED


# --- evaluate_edition_metadata_difference ------------------------------


def test_edition_metadata_difference_auto_passes_when_identity_confirmed():
    result = rules.evaluate_edition_metadata_difference(
        identity_confirmed=True,
        differing_fields=["isbn", "page_count", "weight_grams"],
    )

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.IDENTITY_EDITION_METADATA_DIFFERENCE
    assert len(result.warnings) == 3


def test_edition_metadata_difference_requires_review_without_confirmed_identity():
    result = rules.evaluate_edition_metadata_difference(
        identity_confirmed=False,
        differing_fields=["isbn"],
    )

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.IDENTITY_INSUFFICIENT_EVIDENCE


# --- evaluate_combo_identity --------------------------------------------


def _passing(rule_code: str):
    from src.domain.decisions import DecisionResult

    return DecisionResult(outcome=Outcome.AUTO_PASS, rule_code=rule_code, reason="ok")


def _failing(rule_code: str):
    from src.domain.decisions import DecisionResult

    return DecisionResult(
        outcome=Outcome.REVIEW_REQUIRED, rule_code=rule_code, reason="not sure"
    )


def test_combo_complete_match_auto_passes():
    members = [_passing("A"), _passing("B"), _passing("C")]

    result = rules.evaluate_combo_identity(members)

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.IDENTITY_COMBO_COMPLETE_MATCH
    assert result.evidence["matched_count"] == 3


def test_combo_single_ambiguity_requires_review():
    """One matched volume out of three is not enough to establish
    identity for the whole combo/set."""
    members = [_passing("A"), _failing("B"), _failing("C")]

    result = rules.evaluate_combo_identity(members)

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.IDENTITY_COMBO_SINGLE_AMBIGUITY
    assert result.evidence["matched_count"] == 1
    assert result.evidence["member_count"] == 3


def test_combo_no_members_requires_review():
    result = rules.evaluate_combo_identity([])

    assert result.outcome == Outcome.REVIEW_REQUIRED
