"""Regression tests for the Facebook invisible-obfuscation title-extraction bug.

Covers the defect found during the first Phase C batch-orchestrator pilot:
Facebook prepends scrambled per-character post metadata to collected post
text, joining each character with U+034F COMBINING GRAPHEME JOINER -- a
Unicode joiner explicitly defined to render with zero visible width.

That joiner carries General Category "Mn" (Mark, Nonspacing), not "Cf"
(Format) or "Cc" (Control), so scripts/clean_facebook_raw_pages.py's
original invisible-character filter left it in place. Each obfuscated
noise character plus its trailing joiner then counted as two characters
instead of one, which defeated is_single_character_noise() and let the
scrambled prefix survive into cleaned_text. From there,
scripts/create_candidates_from_cleaned_posts.py's anchored title regexes
(which match at the very start of the string) missed the real title and
fell through to a weaker mid-text pattern, producing a garbage candidate
title (observed in production candidate FB-2026-001-CAN-0009).

Pure/offline: no live Supabase, no live browser/Playwright, no network
calls. This file contains no literal invisible/combining Unicode
characters -- the offending joiner is always built via chr(0x034F) (see
JOINER below), never typed directly, so it stays visible, greppable, and
immune to silent mangling by an editor, formatter, or copy/paste. Fixture
base characters are captured verbatim from the actual raw_pages.raw_text /
cleaned_text collected for raw_page_id
c15803a9-77ae-457e-88ac-f2a1c3f10ec9 (production candidate
FB-2026-001-CAN-0009), or are a minimal synthetic construction of the same
shape.
"""

from __future__ import annotations

import unicodedata

import clean_facebook_raw_pages as cfrp
import create_candidates_from_cleaned_posts as ccfp


# ---------------------------------------------------------------------------
# Exact observed prefix / code points
# ---------------------------------------------------------------------------

# COMBINING GRAPHEME JOINER -- Facebook's own zero-width per-character
# obfuscation joiner. Built via chr(0x034F) rather than typed as a literal
# invisible character, so the defect this file tests for stays visible,
# greppable, and immune to silent mangling by an editor, formatter, or
# copy/paste (the source file contains no literal invisible characters
# at all).
JOINER = chr(0x034F)

# The exact base characters of Facebook's scrambled per-character
# id/timestamp obfuscation string, captured in order from the real
# collected post (raw_pages.raw_text / cleaned_text for raw_page_id
# c15803a9-77ae-457e-88ac-f2a1c3f10ec9, production candidate
# FB-2026-001-CAN-0009). In the actual post each of these characters
# appears on its own line immediately followed by JOINER.
_NOISE_BASE_CHARACTERS = (
    "nopodsertSf9196ihtfitná3ci3fmf05u5m5gmh380128a096m6t51img"
)

# The joiner-obfuscated id/timestamp segment alone, matching the shape of
# raw_pages.cleaned_text as actually stored in production for
# FB-2026-001-CAN-0009 before this fix: the surrounding Facebook chrome
# lines ("Tiệm sách Yêu Con ở Đức", "·", etc.) were already stripped
# correctly by the pre-existing exact-line-match rules, which are
# unrelated to this bug -- only these single-character-plus-joiner lines
# survived.
SCRAMBLED_ID_SEGMENT = (
    "\n".join(f"{char}{JOINER}" for char in _NOISE_BASE_CHARACTERS) + "\n"
)

# Verbatim admin-info obfuscation segment captured from the real collected
# post (raw_pages.raw_text for the same raw_page_id), immediately before
# the real post content begins: the same scrambled id, still wrapped in
# the surrounding Facebook chrome lines that a full clean_facebook_text()
# pass is expected to strip via its existing (unrelated, unaffected-by-
# this-fix) exact-line-match rules.
OBFUSCATION_NOISE_SEGMENT = (
    "Tiệm sách Yêu Con ở Đức\n"
    "Yêu Con\n"
    "·\n"
    "Yêu thích\n"
    "·\n"
    "Quản trị viên\n"
    "·\n"
    + SCRAMBLED_ID_SEGMENT
    + "·\n"
)

REAL_POST_BODY = (
    "Bộ sách Jadoo IQ là một bộ sách tương "
    "tác rất thú vị dành cho trẻ mầm non "
    "và đầu tiểu học, kết hợp giữa "
    "kể chuyện, quan sát, tư duy logic và những "
    "bài học nhỏ về ứng xử trong cuộc "
    "sống hằng ngày."
)

RAW_TEXT_FIXTURE = (
    "Bài viết của Yêu Con\n"
    "Facebook\n"
    "Facebook\n"
    + OBFUSCATION_NOISE_SEGMENT
    + REAL_POST_BODY
    + "\nChưa có bình luận nào\nHãy là người đầu tiên bình luận.\n"
)


def test_obfuscation_joiner_is_u034f_combining_grapheme_joiner() -> None:
    """Pin down the exact offending code point by name, not just by value."""
    joiner_positions = [
        char
        for char in OBFUSCATION_NOISE_SEGMENT
        if unicodedata.category(char) == "Mn"
    ]

    assert joiner_positions, "fixture must contain the obfuscation joiner"

    for char in joiner_positions:
        assert ord(char) == 0x034F
        assert unicodedata.name(char) == "COMBINING GRAPHEME JOINER"


def test_remove_invisible_unicode_characters_strips_the_joiner() -> None:
    noisy = f"S{JOINER}"

    result = cfrp.remove_invisible_unicode_characters(noisy)

    assert result == "S"
    assert JOINER not in result


def test_clean_facebook_text_removes_full_obfuscation_prefix() -> None:
    """The full noise segment must disappear, not just its joiners."""
    cleaned = cfrp.clean_facebook_text(RAW_TEXT_FIXTURE)

    assert JOINER not in cleaned
    assert cleaned.startswith("Bộ sách Jadoo IQ")


def test_clean_facebook_text_validates_as_clean() -> None:
    cleaned = cfrp.clean_facebook_text(RAW_TEXT_FIXTURE)

    status, warnings = cfrp.validate_cleaned_text(
        raw_text=RAW_TEXT_FIXTURE,
        cleaned_text=cleaned,
    )

    assert status == "CLEANED"
    assert warnings == []


def test_extractor_identifies_correct_leading_title_after_cleaning() -> None:
    """End-to-end: raw text with the real defect shape -> correct title."""
    cleaned = cfrp.clean_facebook_text(RAW_TEXT_FIXTURE)

    extraction = ccfp.extract_book_identity(cleaned)

    assert extraction["extracted_title"] == "Bộ sách Jadoo IQ"
    assert extraction["matched_pattern"] == "TITLE_DESCRIPTION_PATTERN_2"


def test_extractor_defensive_normalization_on_uncleaned_joiner_text() -> None:
    """The extractor's own defensive normalization must not choke on the
    joiner even if it receives a cleaned_text row that was produced by the
    pre-fix cleaner (i.e. Facebook chrome lines already stripped by the
    unrelated exact-line-match rules, but the scrambled joiner-obfuscated
    id/timestamp characters still present -- exactly the shape stored for
    production candidate FB-2026-001-CAN-0009 before this fix).
    """
    still_noisy_cleaned_text = SCRAMBLED_ID_SEGMENT + REAL_POST_BODY

    extraction = ccfp.extract_book_identity(still_noisy_cleaned_text)

    # The invisible joiner itself must never survive into an extracted
    # field, even though full noise-word removal is the fixed cleaner's
    # job (this defensive call only strips invisible characters, it does
    # not re-run line-based noise filtering).
    assert JOINER not in (extraction["extracted_title"] or "")
    assert extraction["extracted_title"] is not None
    assert extraction["extracted_title"].endswith("Bộ sách Jadoo IQ")


def test_vietnamese_diacritics_survive_normalization() -> None:
    """The fix must not damage legitimate Vietnamese Unicode text."""
    sample = (
        "ạấẩầẫậắằẳẵặ"
        "ẹẻẽếềểễệ"
        "ỉịọỏốồổỗộ"
        "ớờởỡợ"
        "ụủứừửữự"
        "ỳỵỷỹ"
    )

    result = cfrp.normalize_unicode_text(sample)

    assert result == sample


def test_ordinary_clean_text_is_unchanged_by_the_fix() -> None:
    """A post with no invisible obfuscation characters must clean identically
    to before this fix (no regression on the common case).
    """
    raw_text = (
        "Sách có sẵn ở Đức\n"
        "Chiếc lá cuối cùng của O. Henry là một tác phẩm kinh điển.\n"
        "Chưa có bình luận nào\n"
    )

    cleaned = cfrp.clean_facebook_text(raw_text)

    assert cleaned == (
        "Sách có sẵn ở Đức\n"
        "Chiếc lá cuối cùng của O. Henry là một tác phẩm kinh điển."
    )


def test_no_regression_for_title_author_pattern_extraction() -> None:
    """Existing TITLE_AUTHOR_PATTERN extraction fixture must still work."""
    cleaned_text = (
        "“Cho tôi xin một vé đi tuổi thơ” của Nguyễn Nhật Ánh."
    )

    extraction = ccfp.extract_book_identity(cleaned_text)

    assert extraction["extracted_title"] == "Cho tôi xin một vé đi tuổi thơ"
    assert extraction["extracted_author"] == "Nguyễn Nhật Ánh"
    assert extraction["matched_pattern"] == "TITLE_AUTHOR_PATTERN_1"
