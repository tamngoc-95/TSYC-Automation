"""Regression tests for
src.domain.rules.identity_rules.assess_conflict_is_title_only_recoverable().

Covers scripts/correct_candidate_extraction.py's narrow IDENTITY_CONFLICT
recovery exception: a candidate whose conflict is caused ONLY by a bad
extracted_title (not a real ISBN/author/publisher disagreement) may enter
the extraction-correction path. This module never decides that a
correction *should* happen -- only whether one *may safely be attempted*.

Pure/offline: no live Supabase, no network. Plain dicts in, a
DecisionResult out.
"""

from __future__ import annotations

from src.domain.decisions import Outcome
from src.domain.identity_status import MatchDecision
from src.domain.rules import identity_rules as rules


def make_candidate(
    *,
    extracted_title: str = "Combo 4 cuốn truyện",
    extracted_author: str | None = None,
) -> dict:
    return {
        "candidate_id": "candidate-1",
        "extracted_title": extracted_title,
        "extracted_author": extracted_author,
        "possible_isbn": None,
    }


def make_reference(
    *,
    reference_id: str = "ref-1",
    reference_title: str = "Combo Sách Tiểu Thuyết Nổi Tiếng Của Thomas Harris",
    reference_author: str | None = None,
    reference_isbn: str | None = None,
    reference_publisher: str | None = None,
) -> dict:
    return {
        "reference_id": reference_id,
        "reference_title": reference_title,
        "reference_author": reference_author,
        "reference_isbn": reference_isbn,
        "reference_publisher": reference_publisher,
    }


# ---------------------------------------------------------------------------
# 7. IDENTITY_CONFLICT caused only by title mismatch -> recoverable
# ---------------------------------------------------------------------------


def test_title_only_conflict_is_recoverable():
    candidate = make_candidate()
    references = [make_reference()]

    decision = rules.assess_conflict_is_title_only_recoverable(candidate, references)

    assert decision.outcome == Outcome.AUTO_PASS
    assert decision.rule_code == rules.IDENTITY_CONFLICT_TITLE_ONLY_RECOVERABLE


def test_title_only_conflict_recoverable_with_multiple_agreeing_references():
    candidate = make_candidate()
    references = [
        make_reference(reference_id="ref-1", reference_publisher="NXB A"),
        make_reference(reference_id="ref-2", reference_publisher="NXB A"),
    ]

    decision = rules.assess_conflict_is_title_only_recoverable(candidate, references)

    assert decision.outcome == Outcome.AUTO_PASS


# ---------------------------------------------------------------------------
# 8. An ISBN conflict cannot use the recovery path
# ---------------------------------------------------------------------------


def test_isbn_conflict_blocks_recovery():
    candidate = make_candidate()
    candidate["possible_isbn"] = "9786045123456"
    references = [make_reference(reference_isbn="9786049999999")]

    decision = rules.assess_conflict_is_title_only_recoverable(candidate, references)

    assert decision.outcome == Outcome.BLOCKED
    assert decision.rule_code == rules.IDENTITY_CONFLICT_NOT_RECOVERABLE
    assert "ISBN" in decision.reason


# ---------------------------------------------------------------------------
# 9. A specific-author conflict cannot use the recovery path
# ---------------------------------------------------------------------------


def test_specific_author_conflict_blocks_recovery():
    candidate = make_candidate(extracted_author="Nguyễn Nhật Ánh")
    references = [make_reference(reference_author="Tô Hoài")]

    decision = rules.assess_conflict_is_title_only_recoverable(candidate, references)

    assert decision.outcome == Outcome.BLOCKED
    assert decision.rule_code == rules.IDENTITY_CONFLICT_NOT_RECOVERABLE
    assert "author" in decision.reason.lower()


def test_generic_author_on_reference_does_not_block_recovery():
    # A generic placeholder author ("Various authors" etc.) is not a
    # real disagreement -- CLAUDE.md 2.2, is_specific_author().
    candidate = make_candidate(extracted_author="Nguyễn Nhật Ánh")
    references = [make_reference(reference_author="Nhiều tác giả")]

    decision = rules.assess_conflict_is_title_only_recoverable(candidate, references)

    assert decision.outcome == Outcome.AUTO_PASS


def test_no_candidate_author_never_blocks_on_author():
    # The historical population's extracted_author is always None --
    # confirm that never blocks recovery on its own.
    candidate = make_candidate(extracted_author=None)
    references = [make_reference(reference_author="Thomas Harris")]

    decision = rules.assess_conflict_is_title_only_recoverable(candidate, references)

    assert decision.outcome == Outcome.AUTO_PASS


# ---------------------------------------------------------------------------
# 10. A publisher conflict (2+ references) cannot use the recovery path
# ---------------------------------------------------------------------------


def test_publisher_conflict_across_references_blocks_recovery():
    candidate = make_candidate()
    references = [
        make_reference(reference_id="ref-1", reference_publisher="NXB Trẻ"),
        make_reference(reference_id="ref-2", reference_publisher="NXB Kim Đồng"),
    ]

    decision = rules.assess_conflict_is_title_only_recoverable(candidate, references)

    assert decision.outcome == Outcome.BLOCKED
    assert decision.rule_code == rules.IDENTITY_CONFLICT_NOT_RECOVERABLE
    assert "publisher" in decision.reason.lower()


def test_single_reference_can_never_trigger_publisher_conflict():
    # Publisher conflict is structurally a multi-reference concept --
    # one reference alone can never trigger it, regardless of its
    # publisher value.
    candidate = make_candidate()
    references = [make_reference(reference_publisher="NXB Trẻ")]

    decision = rules.assess_conflict_is_title_only_recoverable(candidate, references)

    assert decision.outcome == Outcome.AUTO_PASS


# ---------------------------------------------------------------------------
# No usable evidence at all -> not recoverable
# ---------------------------------------------------------------------------


def test_no_usable_reference_blocks_recovery():
    candidate = make_candidate()
    references = [make_reference(reference_title=None)]  # unusable: no title

    decision = rules.assess_conflict_is_title_only_recoverable(candidate, references)

    assert decision.outcome == Outcome.BLOCKED
    assert decision.rule_code == rules.IDENTITY_CONFLICT_NOT_RECOVERABLE


def test_empty_reference_list_blocks_recovery():
    candidate = make_candidate()

    decision = rules.assess_conflict_is_title_only_recoverable(candidate, [])

    assert decision.outcome == Outcome.BLOCKED
    assert decision.rule_code == rules.IDENTITY_CONFLICT_NOT_RECOVERABLE


# ---------------------------------------------------------------------------
# Non-MATCH-decision references are still evaluated (this function only
# assesses evidence agreement, not match_decision bookkeeping) -- included
# to document that behavior explicitly.
# ---------------------------------------------------------------------------


def test_reference_match_decision_field_is_irrelevant_to_this_assessment():
    candidate = make_candidate()
    references = [make_reference(reference_id="ref-1")]
    references[0]["match_decision"] = MatchDecision.NO_MATCH

    decision = rules.assess_conflict_is_title_only_recoverable(candidate, references)

    assert decision.outcome == Outcome.AUTO_PASS
