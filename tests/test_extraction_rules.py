"""Offline tests for src/domain/rules/extraction_rules.py.

No live Supabase/WooCommerce/Facebook dependency -- pure functions
operating on plain strings. Covers post-type classification
(ONE_BOOK/MULTIPLE_BOOKS/COMBO/GENERAL_POST/AMBIGUOUS) via
run_automatic_extraction(), plus the single-book extraction contract
already pinned down separately (via create_candidates_from_cleaned_
posts.py's extract_book_identity() wrapper) in
tests/test_facebook_text_normalization.py.
"""
from __future__ import annotations

from src.domain.decisions import Outcome
from src.domain.rules import extraction_rules as rules


# --- run_automatic_extraction: ONE_BOOK ---------------------------------


def test_single_book_with_title_author_pattern_is_one_book():
    text = "“Doraemon Tap 1” của Fujiko F. Fujio, sách còn mới 100%."

    result = rules.run_automatic_extraction(text)

    assert result.post_type == rules.PostType.ONE_BOOK
    assert result.decision.outcome == Outcome.AUTO_PASS
    assert result.decision.rule_code == rules.EXTRACTION_ONE_BOOK
    assert len(result.candidates) == 1
    assert result.candidates[0].extracted_title == "Doraemon Tap 1"
    # The author regex's exclusion class stops at "." (protected,
    # unchanged behavior -- see tests/test_facebook_text_normalization.py).
    assert result.candidates[0].extracted_author == "Fujiko F"
    assert result.candidates[0].candidate_type == "SINGLE_BOOK"


def test_title_description_pattern_is_one_book_with_no_author():
    text = "Chuyện con mèo dạy hải âu bay là một cuốn sách rất hay cho trẻ em."

    result = rules.run_automatic_extraction(text)

    assert result.post_type == rules.PostType.ONE_BOOK
    assert result.decision.outcome == Outcome.AUTO_PASS
    assert result.candidates[0].extracted_author is None


# --- run_automatic_extraction: MULTIPLE_BOOKS ---------------------------


def test_explicit_numbered_list_is_multiple_books():
    text = (
        "Sách có sẵn:\n"
        "1. “Doraemon Tap 1” của Fujiko F. Fujio\n"
        "2. “Conan Tap 5” của Gosho Aoyama\n"
    )

    result = rules.run_automatic_extraction(text)

    assert result.post_type == rules.PostType.MULTIPLE_BOOKS
    assert result.decision.outcome == Outcome.AUTO_PASS
    assert result.decision.rule_code == rules.EXTRACTION_MULTIPLE_BOOKS
    assert len(result.candidates) == 2
    titles = {candidate.extracted_title for candidate in result.candidates}
    assert titles == {"Doraemon Tap 1", "Conan Tap 5"}
    assert all(candidate.candidate_type == "SINGLE_BOOK" for candidate in result.candidates)


def test_bullet_list_duplicate_titles_are_deduped():
    text = (
        "- “Doraemon Tap 1” của Fujiko F. Fujio\n"
        "- “Doraemon Tap 1” của Fujiko F. Fujio\n"
        "- “Conan Tap 5” của Gosho Aoyama\n"
    )

    result = rules.run_automatic_extraction(text)

    assert result.post_type == rules.PostType.MULTIPLE_BOOKS
    assert len(result.candidates) == 2


def test_list_with_fewer_than_two_resolvable_titles_is_ambiguous():
    # Two usable (non-price/timestamp/emoji) list items, but only one of
    # them contains a pattern extract_single_book_identity() can resolve
    # -- the other is plain prose with no title anchor.
    text = (
        "- “Doraemon Tap 1” của Fujiko F. Fujio\n"
        "- Sách hay không rõ tên tác giả hay tựa đề gì cả\n"
    )

    result = rules.run_automatic_extraction(text)

    assert result.post_type == rules.PostType.AMBIGUOUS
    assert result.decision.outcome == Outcome.REVIEW_REQUIRED
    assert result.decision.rule_code == rules.EXTRACTION_INSUFFICIENT_TITLE
    assert result.candidates == ()


# --- run_automatic_extraction: COMBO ------------------------------------


def test_combo_keyword_without_list_is_combo():
    text = "Combo trọn bộ Doraemon của Fujiko F. Fujio, đầy đủ các tập."

    result = rules.run_automatic_extraction(text)

    assert result.post_type == rules.PostType.COMBO
    assert result.decision.outcome == Outcome.AUTO_PASS
    assert result.decision.rule_code == rules.EXTRACTION_COMBO
    assert len(result.candidates) == 1
    assert result.candidates[0].candidate_type == "BOOK_COMBO"


def test_bare_bo_sach_alone_is_not_combo_evidence():
    """CLAUDE.md/module docstring: "bo sach" ("book set") alone is a
    normal single-product name, not bundle-deal evidence -- see
    extraction_rules.py's _COMBO_KEYWORDS comment."""
    text = "Bộ sách Doraemon là một cuốn sách hay cho các bé."

    result = rules.run_automatic_extraction(text)

    assert result.post_type != rules.PostType.COMBO


def test_combo_language_with_explicit_list_is_ambiguous():
    text = (
        "Combo trọn bộ:\n"
        "1. “Doraemon Tap 1” của Fujiko F. Fujio\n"
        "2. “Conan Tap 5” của Gosho Aoyama\n"
    )

    result = rules.run_automatic_extraction(text)

    assert result.post_type == rules.PostType.AMBIGUOUS
    assert result.decision.outcome == Outcome.REVIEW_REQUIRED
    assert result.decision.rule_code == rules.EXTRACTION_AMBIGUOUS
    assert result.candidates == ()


def test_combo_language_with_no_extractable_title_is_ambiguous():
    text = "Combo trọn bộ giảm giá sốc, số lượng có hạn, inbox ngay!"

    result = rules.run_automatic_extraction(text)

    assert result.post_type == rules.PostType.AMBIGUOUS
    assert result.decision.outcome == Outcome.REVIEW_REQUIRED
    assert result.decision.rule_code == rules.EXTRACTION_INSUFFICIENT_TITLE


# --- run_automatic_extraction: GENERAL_POST / AMBIGUOUS ------------------


def test_empty_text_is_general_post():
    result = rules.run_automatic_extraction("")

    assert result.post_type == rules.PostType.GENERAL_POST
    assert result.decision.outcome == Outcome.AUTO_REJECT
    assert result.decision.rule_code == rules.EXTRACTION_GENERAL_POST
    assert result.candidates == ()


def test_no_book_signal_is_general_post():
    text = "Chúc mừng sinh nhật shop tròn 5 tuổi! Cảm ơn mọi người đã ủng hộ."

    result = rules.run_automatic_extraction(text)

    assert result.post_type == rules.PostType.GENERAL_POST
    assert result.decision.outcome == Outcome.AUTO_REJECT
    assert result.decision.rule_code == rules.EXTRACTION_GENERAL_POST


def test_book_signal_without_reliable_title_is_ambiguous():
    text = "Sách hay quá mọi người ơi, ai cũng nên đọc cuốn này nhé!!!"

    result = rules.run_automatic_extraction(text)

    assert result.post_type == rules.PostType.AMBIGUOUS
    assert result.decision.outcome == Outcome.REVIEW_REQUIRED
    assert result.decision.rule_code == rules.EXTRACTION_INSUFFICIENT_TITLE
    assert result.candidates == ()


# --- helper functions -----------------------------------------------------


def test_detect_combo_evidence_matches_set_count_phrase():
    assert rules.detect_combo_evidence("bộ 10 cuốn doraemon giá tốt") is True


def test_detect_combo_evidence_false_for_unrelated_text():
    assert rules.detect_combo_evidence("một cuốn sách hay cho bé") is False


def test_split_list_items_only_matches_explicit_markers():
    text = (
        "1. Doraemon Tap 1\n"
        "- Conan Tap 5\n"
        "Đây không phải là một mục danh sách.\n"
    )

    items = rules.split_list_items(text)

    assert items == ["Doraemon Tap 1", "Conan Tap 5"]


def test_is_unusable_title_line_rejects_price_timestamp_and_emoji_only():
    assert rules.is_unusable_title_line("50.000đ") is True
    assert rules.is_unusable_title_line("2 giờ trước") is True
    assert rules.is_unusable_title_line("📚📚📚") is True
    assert rules.is_unusable_title_line("") is True
    assert rules.is_unusable_title_line("Doraemon Tap 1") is False


def test_extract_single_book_identity_returns_none_for_unresolvable_text():
    assert rules.extract_single_book_identity("50.000đ") is None
    assert rules.extract_single_book_identity("") is None
