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


# --- normalize_publisher / publishers_conflict --------------------------


def test_normalize_publisher_strips_leading_nxb():
    assert rules.normalize_publisher("NXB Hội Nhà Văn") == rules.normalize_text(
        "Hội Nhà Văn"
    )


def test_normalize_publisher_does_not_corrupt_a_real_name_containing_similar_tokens():
    """'Nhã Nam' must never be mangled by stripping a bare 'nha' token --
    only whole leading legal-form phrases are stripped."""
    assert rules.normalize_publisher("Nhã Nam") == rules.normalize_text("Nhã Nam")


def test_publishers_conflict_false_for_single_distinct_value_repeated():
    assert rules.publishers_conflict(["Trẻ", "Trẻ", None, ""]) is False


def test_publishers_conflict_false_after_legal_prefix_normalization():
    assert rules.publishers_conflict(["NXB Hội Nhà Văn", "Hội Nhà Văn"]) is False


def test_publishers_conflict_true_for_materially_different_values():
    assert (
        rules.publishers_conflict(["Hội Nhà Văn", "NXB Văn Hoá Sài Gòn"]) is True
    )


def test_publishers_conflict_false_for_all_empty():
    assert rules.publishers_conflict([None, "", None]) is False


# --- is_reference_evaluable --------------------------------------------


def test_reference_with_title_is_evaluable():
    assert rules.is_reference_evaluable({"reference_title": "Some Book"}) is True


def test_reference_without_title_is_not_evaluable():
    assert rules.is_reference_evaluable({"reference_title": None}) is False
    assert rules.is_reference_evaluable({"reference_title": ""}) is False
    assert rules.is_reference_evaluable({"reference_title": "   "}) is False
    assert rules.is_reference_evaluable({}) is False


# --- evaluate_candidate_identity: cumulative aggregate -------------------
#
# These protect the exact live-incident shapes found on the TSYC
# historical identity pilot (CAN-0015, CAN-0039) plus the newly hardened
# ISBN-validation and publisher-conflict gates (Phase 10 A/B/E/G/H/I).


def _candidate(title, author=None, isbn=None):
    return {
        "extracted_title": title,
        "extracted_author": author,
        "possible_isbn": isbn,
    }


def _reference(**overrides):
    reference = {
        "reference_id": "ref-1",
        "reference_title": None,
        "reference_author": None,
        "reference_isbn": None,
        "reference_publisher": None,
        "reference_page_count": None,
    }
    reference.update(overrides)
    return reference


def test_A_good_possible_match_then_empty_reference_does_not_regress():
    """CAN-0039's exact failure shape: a real reference that produced a
    valid POSSIBLE_MATCH, plus a second reference whose crawl came back
    completely empty. The empty reference must never flip this to
    NO_MATCH/CONFLICT -- regardless of which order they are supplied in."""
    candidate = _candidate("Nỗi Buồn Chiến Tranh")
    good = _reference(
        reference_id="netabooks",
        reference_title="Nỗi Buồn Chiến Tranh",
        reference_author="Bảo Ninh",
        reference_publisher="Trẻ",
        reference_page_count=348,
    )
    empty = _reference(reference_id="fahasa-empty")

    for references in ([good, empty], [empty, good]):
        result = rules.evaluate_candidate_identity(candidate, references)

        assert result.outcome == Outcome.REVIEW_REQUIRED
        assert result.evidence["match_decision"] == MatchDecision.POSSIBLE_MATCH
        assert result.evidence["has_genuine_conflict"] is False
        assert result.evidence["usable_reference_count"] == 1
        assert result.evidence["unusable_reference_count"] == 1


def test_B_empty_reference_alone_is_not_evaluable_never_confident_no_match():
    candidate = _candidate("Any Title At All")
    result = rules.evaluate_candidate_identity(candidate, [_reference()])

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.IDENTITY_NO_USABLE_EVIDENCE
    assert result.evidence["has_genuine_conflict"] is False
    assert result.evidence["usable_reference_count"] == 0
    assert result.evidence["unusable_reference_count"] == 1


def test_E_strong_pass_with_only_unusable_extra_reference_preserves_match():
    candidate = _candidate("Doraemon Tap 1", author="Fujiko F. Fujio")
    strong = _reference(
        reference_id="r1", reference_title="Doraemon Tap 1", reference_author="Fujiko F. Fujio"
    )
    unusable = _reference(reference_id="r2")

    result = rules.evaluate_candidate_identity(candidate, [strong, unusable])

    assert result.outcome == Outcome.AUTO_PASS
    assert result.evidence["has_genuine_conflict"] is False
    assert result.evidence["matching_reference_id"] == "r1"


def test_strong_pass_plus_genuine_no_match_is_a_real_conflict():
    """A different, well-populated reference confidently disagreeing
    with an otherwise-strong match is exactly the case that must stop
    for review -- unlike an empty/unusable reference (test_B/E)."""
    candidate = _candidate("Doraemon Tap 1", author="Fujiko F. Fujio")
    strong = _reference(
        reference_id="r1", reference_title="Doraemon Tap 1", reference_author="Fujiko F. Fujio"
    )
    disagreeing = _reference(
        reference_id="r2", reference_title="Totally Unrelated Cooking Manual"
    )

    result = rules.evaluate_candidate_identity(candidate, [strong, disagreeing])

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.IDENTITY_CONFLICTING_CREDIBLE_SOURCES
    assert result.evidence["has_genuine_conflict"] is True


def test_abbreviated_candidate_title_with_two_corroborating_long_titles_is_not_a_false_reject():
    """Live incident: historical candidate CAN-0004 ('Power vs. Force',
    a short Facebook-extracted title). Both of its registered references
    carry the full, much longer official subtitle and individually
    AUTO_REJECT (title_similarity < 0.60 against the short candidate
    title) -- but they closely agree with EACH OTHER. That must not
    collapse to a confident AUTO_REJECT/CONFLICT; it must become
    REVIEW_REQUIRED, since the low similarity is an artifact of the
    candidate's own abbreviated title, not evidence of a different book
    (CLAUDE.md 5.2)."""
    candidate = _candidate("Power vs. Force")
    ref_a = _reference(
        reference_id="fahasa",
        reference_title=(
            "Power Vs Force - Trường Năng Lượng Và Những Nhân Tố Quyết "
            "Định Hành Vi Của Con Người (Tái Bản)"
        ),
        reference_author="David R Hawkins",
        reference_publisher="NXB Thế Giới",
        reference_page_count=398,
    )
    ref_b = _reference(
        reference_id="neta",
        reference_title=(
            "Power Vs Force - Trường Năng Lượng Và Những Nhân Tố Quyết "
            "Định Tinh Thần, Sức Khỏe Con Người (Bìa Cứng)"
        ),
        reference_author="David R. Hawkins",
        reference_publisher="Thế Giới",
        reference_page_count=398,
    )

    result = rules.evaluate_candidate_identity(candidate, [ref_a, ref_b])

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.outcome != Outcome.AUTO_REJECT
    assert result.rule_code == rules.IDENTITY_INSUFFICIENT_EVIDENCE
    assert result.evidence["has_genuine_conflict"] is False


def test_two_genuinely_unrelated_rejecting_references_still_confirm_no_match():
    """The guard above must not swallow a real rejection: two references
    that reject the candidate AND disagree with each other is still a
    confident, genuine AUTO_REJECT."""
    candidate = _candidate("Some Book")
    ref_a = _reference(reference_id="a", reference_title="Completely Different Novel About Ships")
    ref_b = _reference(reference_id="b", reference_title="A Cookbook For Weekend Brunch")

    result = rules.evaluate_candidate_identity(candidate, [ref_a, ref_b])

    assert result.outcome == Outcome.AUTO_REJECT
    assert result.evidence["has_genuine_conflict"] is True


def test_single_rejecting_reference_is_still_a_confident_reject():
    """A lone rejecting reference has nothing to corroborate against --
    the guard only applies with 2+ rejecting references."""
    candidate = _candidate("Doraemon Tap 1")
    lone_reject = _reference(reference_id="r1", reference_title="Totally Unrelated Cooking Manual")

    result = rules.evaluate_candidate_identity(candidate, [lone_reject])

    assert result.outcome == Outcome.AUTO_REJECT
    assert result.evidence["has_genuine_conflict"] is True


def test_G_unvalidated_isbn_never_becomes_the_consensus_isbn():
    """Two sources agreeing on title/author but one carrying a
    barcode-shaped, non-ISBN value must still AUTO_PASS (consensus
    doesn't need ISBN), and that value must never appear in
    valid_isbn_values -- the write layer must not promote it."""
    candidate = _candidate("Quân khu Nam Đồng")
    with_bad_isbn = _reference(
        reference_id="neta",
        reference_title="Quân Khu Nam Đồng",
        reference_author="Bình Ca",
        reference_isbn="2396043028889",  # does not start with 978/979
        reference_publisher="Trẻ",
        reference_page_count=440,
    )
    clean = _reference(
        reference_id="fahasa",
        reference_title="Quân Khu Nam Đồng",
        reference_author="Bình Ca",
        reference_publisher="Trẻ",
        reference_page_count=440,
    )

    result = rules.evaluate_candidate_identity(candidate, [with_bad_isbn, clean])

    assert result.outcome == Outcome.AUTO_PASS
    assert result.evidence["valid_isbn_values"] == []
    assert result.evidence.get("isbn_conflict") is False


def test_H_two_conflicting_valid_isbns_requires_review():
    candidate = _candidate("Some Book")
    ref_a = _reference(
        reference_id="a", reference_title="Some Book", reference_isbn="9786041234567"
    )
    ref_b = _reference(
        reference_id="b", reference_title="Some Book", reference_isbn="9786049999999"
    )

    result = rules.evaluate_candidate_identity(candidate, [ref_a, ref_b])

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.IDENTITY_CONFLICTING_CREDIBLE_SOURCES
    assert result.evidence["isbn_conflict"] is True
    assert result.evidence["has_genuine_conflict"] is True


def test_I_publisher_conflict_blocks_auto_pass_no_silent_winner():
    candidate = _candidate("Phía Sau Nghi Can X")
    ref_a = _reference(
        reference_id="neta",
        reference_title="Phía Sau Nghi Can X",
        reference_author="Higashino Keigo",
        reference_publisher="Hội Nhà Văn",
    )
    ref_b = _reference(
        reference_id="fahasa",
        reference_title="Phía Sau Nghi Can X",
        reference_author="Higashino Keigo",
        reference_publisher="NXB Văn Hoá Sài Gòn",
    )

    result = rules.evaluate_candidate_identity(candidate, [ref_a, ref_b])

    assert result.outcome == Outcome.REVIEW_REQUIRED
    assert result.rule_code == rules.IDENTITY_CONFLICTING_CREDIBLE_SOURCES
    assert result.evidence["publisher_conflict"] is True
    assert result.evidence["has_genuine_conflict"] is True


def test_consensus_two_agreeing_sources_still_auto_passes_with_hardening():
    """The hardening must not weaken the existing, working consensus
    path: two independent, fully agreeing sources still AUTO_PASS."""
    candidate = _candidate("Đời ngắn đừng ngủ dài")
    ref_a = _reference(
        reference_id="a",
        reference_title="Đời Ngắn Đừng Ngủ Dài",
        reference_author="Robin Sharma",
        reference_publisher="Trẻ",
        reference_page_count=228,
    )
    ref_b = _reference(
        reference_id="b",
        reference_title="Đời Ngắn Đừng Ngủ Dài",
        reference_author="Robin Sharma",
        reference_publisher="Trẻ",
        reference_page_count=228,
    )

    result = rules.evaluate_candidate_identity(candidate, [ref_a, ref_b])

    assert result.outcome == Outcome.AUTO_PASS
    assert result.rule_code == rules.IDENTITY_EXACT_TITLE_AUTHOR


def test_evaluate_candidate_identity_is_order_independent():
    """Same reference set, different order, must yield the identical
    decision -- true idempotency depends on this."""
    candidate = _candidate("Nỗi Buồn Chiến Tranh")
    good = _reference(
        reference_id="netabooks",
        reference_title="Nỗi Buồn Chiến Tranh",
        reference_author="Bảo Ninh",
    )
    empty = _reference(reference_id="fahasa-empty")

    result_1 = rules.evaluate_candidate_identity(candidate, [good, empty])
    result_2 = rules.evaluate_candidate_identity(candidate, [empty, good])

    assert result_1.outcome == result_2.outcome
    assert result_1.rule_code == result_2.rule_code
    assert result_1.confidence == result_2.confidence
