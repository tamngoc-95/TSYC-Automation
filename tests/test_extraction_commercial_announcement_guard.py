"""Regression tests for the commercial/preorder-announcement title guard
in src/domain/rules/extraction_rules.py.

Covers the production incident on FB-HIST-2026-002-CAN-0023: a Facebook
post ("Em nhận preorder sách mới 70€/ Combo 4 cuốn truyện của Thomas
Harris") whose TITLE_AUTHOR_PATTERN_2 match captured the entire
preorder-price announcement clause as the "title" instead of the actual
product description that follows it.

Two independent, generalizable mechanisms are covered:
  - strip_leading_commercial_announcement(): strips a leading
    "price-token / product" preamble before any title pattern runs.
  - looks_like_commercial_announcement() (wired into
    validate_extracted_identity()): a defense-in-depth rejection of any
    final title that still contains a price/currency token, regardless
    of which pattern or preprocessing path produced it.

Neither mechanism is a hardcoded exclusion for CAN-0023's exact wording
-- both key off the structural presence of a price/currency token, so a
legitimate title containing "combo" or a volume count (with no price
inside it) is never affected.

Pure/offline: no live Supabase, no network. Plain strings in, plain
values out.
"""

from __future__ import annotations

from src.domain.rules import extraction_rules as rules


# ---------------------------------------------------------------------------
# 1. The exact CAN-0023-shaped preorder sentence is not selected as title
# ---------------------------------------------------------------------------


def test_preorder_price_announcement_sentence_is_not_selected_as_title():
    text = (
        "Em nhận preorder sách mới 70€/ Combo 4 cuốn truyện của Thomas Harris\n"
        "Thanh lý truyện cũ 40€/ combo 4 cuốn (Có sẵn)\n"
        "Có bán lẻ tập ạ"
    )

    result = rules.extract_single_book_identity(text)

    assert result is not None
    assert result.extracted_title == "Combo 4 cuốn truyện"
    assert "€" not in result.extracted_title
    assert "preorder" not in result.extracted_title.lower()


# ---------------------------------------------------------------------------
# 2. A legitimate BOOK_COMBO title (contains "combo X cuốn", no price) still
#    works -- the guard must never reject a real combo title.
# ---------------------------------------------------------------------------


def test_legitimate_combo_title_with_no_price_is_unaffected():
    text = "Combo trọn bộ Doraemon của Fujiko F. Fujio, đầy đủ các tập."

    result = rules.extract_single_book_identity(text)

    assert result is not None
    assert result.extracted_title == "Combo trọn bộ Doraemon"
    assert result.extracted_author == "Fujiko F"


def test_combo_set_count_title_with_no_price_is_unaffected():
    text = "“Combo 14 cuốn Gieo hạt cùng vĩ nhân” của Trần Việt Quân."

    result = rules.extract_single_book_identity(text)

    assert result is not None
    assert result.extracted_title == "Combo 14 cuốn Gieo hạt cùng vĩ nhân"
    assert result.extracted_author == "Trần Việt Quân"


# ---------------------------------------------------------------------------
# 3. A bare price/currency line never becomes a title
# ---------------------------------------------------------------------------


def test_price_only_text_never_becomes_a_title():
    assert rules.extract_single_book_identity("70€") is None
    assert rules.extract_single_book_identity("450.000đ") is None


def test_title_with_embedded_price_token_is_rejected_by_validation():
    # No leading-slash preamble to strip -- the price sits inside the
    # only candidate title text available. The defense-in-depth
    # validation guard (not the preamble stripper) must catch this.
    assert rules.looks_like_commercial_announcement("Sách hay 50.000đ") is True
    assert rules.looks_like_commercial_announcement("Doraemon Tap 1") is False


# ---------------------------------------------------------------------------
# 4. A seller-action announcement line does not become a title
# ---------------------------------------------------------------------------


def test_seller_action_preamble_without_slash_still_blocks_price_title():
    # No "/" separator this time -- confirms the guard is not solely
    # dependent on the slash convention; a price token surviving into
    # the final captured title is rejected either way.
    text = "Thanh lý sách cũ 100.000đ của Nguyễn Nhật Ánh."

    result = rules.extract_single_book_identity(text)

    assert result is None


# ---------------------------------------------------------------------------
# 5. A normal SINGLE_BOOK title remains unchanged
# ---------------------------------------------------------------------------


def test_normal_single_book_title_is_unaffected():
    text = "“Cho tôi xin một vé đi tuổi thơ” của Nguyễn Nhật Ánh."

    result = rules.extract_single_book_identity(text)

    assert result is not None
    assert result.extracted_title == "Cho tôi xin một vé đi tuổi thơ"
    assert result.extracted_author == "Nguyễn Nhật Ánh"
    assert result.matched_pattern == "TITLE_AUTHOR_PATTERN_1"


def test_price_after_title_in_same_sentence_is_unaffected():
    # Regression guard: a price mentioned *after* the captured title
    # (never part of the title span itself) must not trigger rejection.
    text = "“Doraemon Tap 1” của Fujiko F. Fujio, sách còn mới 100%, giá 8€."

    result = rules.extract_single_book_identity(text)

    assert result is not None
    assert result.extracted_title == "Doraemon Tap 1"


# ---------------------------------------------------------------------------
# 6. A normal BOOK_COMBO (via run_automatic_extraction) remains unchanged
# ---------------------------------------------------------------------------


def test_run_automatic_extraction_combo_case_is_unaffected():
    from src.domain.decisions import Outcome

    text = "Combo trọn bộ Doraemon của Fujiko F. Fujio, đầy đủ các tập."

    result = rules.run_automatic_extraction(text)

    assert result.post_type == rules.PostType.COMBO
    assert result.decision.outcome == Outcome.AUTO_PASS
    assert len(result.candidates) == 1
    assert result.candidates[0].candidate_type == "BOOK_COMBO"
    assert result.candidates[0].extracted_title == "Combo trọn bộ Doraemon"


# ---------------------------------------------------------------------------
# strip_leading_commercial_announcement(): direct unit coverage
# ---------------------------------------------------------------------------


def test_strip_leading_commercial_announcement_removes_price_slash_preamble():
    text = "Em nhận preorder sách mới 70€/ Combo 4 cuốn truyện của Thomas Harris"

    result = rules.strip_leading_commercial_announcement(text)

    assert result == "Combo 4 cuốn truyện của Thomas Harris"


def test_strip_leading_commercial_announcement_leaves_text_without_price_untouched():
    text = "Sách mới/ Combo 4 cuốn truyện của Thomas Harris"

    result = rules.strip_leading_commercial_announcement(text)

    assert result == text


def test_strip_leading_commercial_announcement_leaves_text_without_slash_untouched():
    text = "Em nhận preorder sách mới 70€ Combo 4 cuốn truyện của Thomas Harris"

    result = rules.strip_leading_commercial_announcement(text)

    assert result == text


def test_strip_leading_commercial_announcement_never_empties_the_text():
    # Defensive: if stripping would leave nothing behind, return the
    # original text instead of an empty string.
    text = "50.000đ/"

    result = rules.strip_leading_commercial_announcement(text)

    assert result == text
