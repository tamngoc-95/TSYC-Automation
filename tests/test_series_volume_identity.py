"""Deterministic series-volume identity (CLAUDE.md 9.1) and its false
positives. Real case 2026-10-06: FB-HIST-2026-IMG-001-CAN-0031 "Cơ Thể Con
Là Của Con!" vs Fahasa "Con Có Thể Tự Bảo Vệ Mình - Cơ Thể Con Là Của Con
(Tái Bản 2022)" was scored NO_MATCH by plain similarity."""
from __future__ import annotations

import pytest

from src.domain.identity_status import MatchDecision
from src.domain.rules import identity_rules as rules

CANDIDATE = {
    "candidate_type": "SINGLE_BOOK",
    "extracted_title": "Cơ Thể Con Là Của Con!",
    "extracted_author": "Dagmar Geisler",
}
REFERENCE = {
    "reference_title": "Con Có Thể Tự Bảo Vệ Mình - Cơ Thể Con Là Của Con (Tái Bản 2022)",
    "reference_author": "Dagmar Geisler",
    "reference_publisher": "Thế Giới",
}


def test_corroborated_series_volume_is_a_match():
    decision = rules.evaluate_single_reference_identity(dict(CANDIDATE), dict(REFERENCE))
    assert decision.evidence["match_decision"] == MatchDecision.MATCH
    assert decision.rule_code == rules.IDENTITY_SERIES_VOLUME_CONFIRMED


def test_equal_valid_isbn_corroborates_without_author():
    candidate = {**CANDIDATE, "extracted_author": None, "possible_isbn": "9786041234567"}
    reference = {**REFERENCE, "reference_author": None, "reference_isbn": "978-604-1-23456-7"}
    assert rules.evaluate_series_volume_identity(candidate, reference).is_auto_pass


@pytest.mark.parametrize(
    "candidate_changes, reference_changes, unmet",
    [
        # title alone never matches
        ({"extracted_author": None}, {"reference_author": None}, "no shared specific author"),
        # different author: same volume title, different book
        ({}, {"reference_author": "Jane Smith"}, "no shared specific author"),
        # a complete series never matches one volume
        ({}, {"reference_title": "Trọn Bộ Con Có Thể Tự Bảo Vệ Mình - Cơ Thể Con Là Của Con"}, "multi-volume"),
        ({}, {"reference_title": "Combo Con Có Thể Tự Bảo Vệ Mình - Cơ Thể Con Là Của Con"}, "multi-volume"),
        ({}, {"reference_title": "Con Có Thể Tự Bảo Vệ Mình (4 cuốn) - Cơ Thể Con Là Của Con"}, "multi-volume"),
        # nor a combo candidate one volume
        ({"candidate_type": "BOOK_COMBO"}, {}, "not a single book"),
        # stated volume numbers must agree
        ({"extracted_title": "Cơ Thể Con Là Của Con Tập 2"},
         {"reference_title": "Con Có Thể Tự Bảo Vệ Mình - Cơ Thể Con Là Của Con Tập 3"}, "volume number differs"),
        ({"extracted_title": "Cơ Thể Con Là Của Con Tập 2"},
         {"reference_title": "Con Có Thể Tự Bảo Vệ Mình - Cơ Thể Con Là Của Con"}, "not '<series> - <candidate title>'"),
        # the series name alone is not a volume
        ({"extracted_title": "Con Có Thể Tự Bảo Vệ Mình"}, {}, "not '<series> - <candidate title>'"),
        # near title (one word different) is not the exact volume
        ({"extracted_title": "Cơ Thể Của Con"}, {}, "not '<series> - <candidate title>'"),
        # conflicting identifiers / publisher
        ({"possible_isbn": "9786041234567"}, {"reference_isbn": "9786049999992"}, "valid ISBNs differ"),
        ({"extracted_publisher": "Kim Đồng"}, {}, "publisher differs"),
    ],
)
def test_series_volume_false_positives_are_rejected(candidate_changes, reference_changes, unmet):
    candidate = {**CANDIDATE, **candidate_changes}
    reference = {**REFERENCE, **reference_changes}

    decision = rules.evaluate_series_volume_identity(candidate, reference)

    assert not decision.is_auto_pass
    assert unmet in decision.reason
    single = rules.evaluate_single_reference_identity(dict(candidate), dict(reference))
    assert single.rule_code != rules.IDENTITY_SERIES_VOLUME_CONFIRMED


def test_retailer_code_is_not_an_isbn_corroboration():
    candidate = {**CANDIDATE, "extracted_author": None, "possible_isbn": "2421762043452"}
    reference = {**REFERENCE, "reference_author": None, "reference_isbn": "2421762043452"}
    assert not rules.evaluate_series_volume_identity(candidate, reference).is_auto_pass


@pytest.mark.parametrize(
    "title, numbers",
    [("Tập 3", {3}), ("Vol. 2 - X", {2}), ("Doraemon #12", {12}), ("Cơ Thể Con Là Của Con", set()),
     ("Tái Bản 2022", set())],
)
def test_volume_numbers(title, numbers):
    assert rules.volume_numbers(title) == numbers
