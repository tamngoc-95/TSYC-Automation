"""Offline tests for src/domain/rules/historical_text_cleaner.py.

Every fixture below is drawn directly from real records in the
historical export (record ids named in each test), captured via
line-by-line inspection of HistoryRecord.full_text during this fix --
not invented. No live Supabase/WooCommerce/Facebook/Claude dependency.
"""
from __future__ import annotations

from src.domain.rules.historical_text_cleaner import (
    KNOWN_LEADING_BOILERPLATE_LINES,
    clean_historical_facebook_text,
)


# --- leading boilerplate line removal --------------------------------


def test_mobile_upload_heading_is_dropped():
    """#1038-style: 'Tải lên từ di động' as a standalone first line."""
    text = "Tải lên từ di động\n#Preorder tuyển tập truyện trinh thám 5🌟 của Agatha Christie\n \n Tui rất sợ mấy truyện ntn, nhưng ma cũng rất mê😱😆😅"

    cleaned = clean_historical_facebook_text(text)

    assert not cleaned.startswith("Tải lên từ di động")
    assert "Tải lên từ di động" not in cleaned
    assert cleaned.startswith("#Preorder")


def test_desktop_photo_upload_heading_is_dropped():
    """#1482-style: 'Ảnh' as a standalone first line."""
    text = "Ảnh\n1. Nghệ Thuật Bán Hàng Cho Người Giàu 8€\n2. Phương Pháp Đầu Tư Warren Buffett 10€"

    cleaned = clean_historical_facebook_text(text)

    assert not cleaned.startswith("Ảnh\n")
    assert cleaned.split("\n")[0] != "Ảnh"


def test_sach_co_san_tai_duc_heading_is_dropped():
    """#1287/#1603-style TSYC opening marker, this export's exact
    casing/suffix ('tại Đức', distinct from extraction_rules.py's own
    'ở Đức' marker)."""
    text = "Sách Có Sẵn tại Đức"

    cleaned = clean_historical_facebook_text(text)

    assert cleaned == ""


def test_non_boilerplate_first_line_is_preserved():
    """A real, user-authored heading (e.g. a review-post header) must
    never be dropped -- only the three confirmed chrome lines are."""
    text = "Review sách hay🥰\nĐọc sách rất hay của tác giả nào đó."

    cleaned = clean_historical_facebook_text(text)

    assert cleaned.startswith("Review sách hay🥰")


def test_known_boilerplate_set_is_exactly_the_confirmed_three():
    assert KNOWN_LEADING_BOILERPLATE_LINES == {
        "tải lên từ di động",
        "ảnh",
        "sách có sẵn tại đức",
    }


# --- exact-repeated-sequence collapse ----------------------------------


def test_whole_two_paragraph_sequence_duplicate_is_collapsed():
    """#1267-style: [A, B, A, ' ', ' '+B] -> after boilerplate-drop and
    empty-paragraph removal, [A, B, A, B] -> collapses to [A, B]."""
    text = (
        "Tải lên từ di động\n"
        "Bác nào gửi tiền về Việt Nam mà gửi ít ít nhỏ lẻ thì dùng Tap Tap Send được nè. Tỉ giá giờ quá đẹp🤩🤩🤩1€=30000vnd\n"
        "Ai chưa dùng bh thì có thẻ dùng code THITAM18. Tiền thu được em dồn vào mua sách thư viện nha👍🏻\n"
        "Bác nào gửi tiền về Việt Nam mà gửi ít ít nhỏ lẻ thì dùng Tap Tap Send được nè. Tỉ giá giờ quá đẹp🤩🤩🤩1€=30000vnd\n"
        " \n"
        " Ai chưa dùng bh thì có thẻ dùng code THITAM18. Tiền thu được em dồn vào mua sách thư viện nha👍🏻"
    )

    cleaned = clean_historical_facebook_text(text)
    paragraphs = cleaned.split("\n")

    assert len(paragraphs) == 2
    assert paragraphs[0].startswith("Bác nào gửi tiền")
    assert paragraphs[1].startswith("Ai chưa dùng bh")


def test_whole_four_paragraph_sequence_duplicate_is_collapsed():
    """#1104-style multi-paragraph duplicate: [A, B, A, B] -> [A, B]."""
    text = (
        "Tải lên từ di động\n"
        "Đây là cuốn sách giúp mình vượt ra khỏi mọi nỗi đau. Biết ơn thiền sư Thích Nhất Hạnh đã viết nên cuốn sách tuyệt vời này♥️\n"
        "Mình có cả sách giấy cho ai yêu thích, giá chỉ 13,99€❤️\n"
        "Đây là cuốn sách giúp mình vượt ra khỏi mọi nỗi đau. Biết ơn thiền sư Thích Nhất Hạnh đã viết nên cuốn sách tuyệt vời này♥️\n"
        " \n"
        " Mình có cả sách giấy cho ai yêu thích, giá chỉ 13,99€❤️"
    )

    cleaned = clean_historical_facebook_text(text)
    paragraphs = cleaned.split("\n")

    assert len(paragraphs) == 2


def test_bullet_list_whole_sequence_duplicate_is_collapsed():
    """#1064-style: an entire numbered list repeated once, restoring
    each bullet's FULL original content (never truncated) instead of
    the truncated, mis-anchored fragment a duplicated/un-cleaned list
    produced before this fix."""
    text = (
        "Tải lên từ di động\n"
        "Thanh lý:\n"
        " 1. Rich Habits – Thói quen thành công của những triệu phú tự thân 9e\n"
        " 2. Ai che lưng cho bạn 9e\n"
        " 3. Sức mạnh của thói quen 9e\n"
        "Thanh lý:\n"
        " \n"
        " 1. Rich Habits – Thói quen thành công của những triệu phú tự thân 9e\n"
        " 2. Ai che lưng cho bạn 9e\n"
        " 3. Sức mạnh của thói quen 9e"
    )

    cleaned = clean_historical_facebook_text(text)
    paragraphs = cleaned.split("\n")

    assert paragraphs == [
        "Thanh lý:",
        "1. Rich Habits – Thói quen thành công của những triệu phú tự thân 9e",
        "2. Ai che lưng cho bạn 9e",
        "3. Sức mạnh của thói quen 9e",
    ]


def test_non_duplicate_paragraphs_are_never_collapsed():
    """#1038/#1482-style: genuinely distinct paragraphs (no repeated
    whole sequence) must survive completely unchanged."""
    text = "Ảnh\n1. Nghệ Thuật Bán Hàng Cho Người Giàu 8€\n2. Phương Pháp Đầu Tư Warren Buffett 10€\n3. Người Nam Châm 6€"

    cleaned = clean_historical_facebook_text(text)
    paragraphs = cleaned.split("\n")

    assert len(paragraphs) == 3
    assert paragraphs == [
        "1. Nghệ Thuật Bán Hàng Cho Người Giàu 8€",
        "2. Phương Pháp Đầu Tư Warren Buffett 10€",
        "3. Người Nam Châm 6€",
    ]


def test_odd_paragraph_count_prevents_a_false_collapse():
    """A safety property of the collapse algorithm itself: with an odd
    paragraph count there is no possible exact first-half/second-half
    split, so nothing is ever collapsed, however similar consecutive
    paragraphs may look."""
    text = "A\nA\nB"

    cleaned = clean_historical_facebook_text(text)

    assert cleaned == "A\nA\nB"


def test_paragraphs_sharing_only_a_common_prefix_are_preserved():
    """#1603-style: a short quoted-title line (T) followed by a longer
    paragraph that happens to START with the same phrase (D1) are NOT
    exact duplicates of each other -- Phase 3 requirement: only a
    materially IDENTICAL repeated block collapses, never a shared-
    prefix pair. Both paragraphs must survive."""
    text = (
        "Sách Có Sẵn tại Đức\n"
        "#Có Sẵn\n"
        "“Chữa lành đứa trẻ trong bạn” của Charles Whitfield\n"
        "“Chữa lành đứa trẻ trong bạn” của Charles Whitfield là một cuốn sách đầy ý nghĩa."
    )

    cleaned = clean_historical_facebook_text(text)
    paragraphs = cleaned.split("\n")

    assert paragraphs == [
        "#Có Sẵn",
        "“Chữa lành đứa trẻ trong bạn” của Charles Whitfield",
        "“Chữa lành đứa trẻ trong bạn” của Charles Whitfield là một cuốn sách đầy ý nghĩa.",
    ]


# --- content preservation -------------------------------------------------


def test_price_isbn_and_combo_wording_are_preserved():
    text = "Tải lên từ di động\nCombo 3 cuốn sách ISBN 9786045123456 giá 99.000đ, freeship toàn quốc"

    cleaned = clean_historical_facebook_text(text)

    assert "Combo" in cleaned
    assert "ISBN" in cleaned
    assert "9786045123456" in cleaned
    assert "99.000đ" in cleaned
    assert "freeship" in cleaned


def test_vietnamese_diacritics_are_preserved():
    text = "Tải lên từ di động\nĐây là một cuốn sách rất hay dành cho các bạn nhỏ."

    cleaned = clean_historical_facebook_text(text)

    assert "Đây" in cleaned
    assert "hay" in cleaned
    assert "dành" in cleaned


def test_generic_extraction_vocabulary_words_are_never_stripped():
    """sách / của / by / bộ sách / combo must never be removed --
    extraction_rules.py's own patterns depend on them."""
    text = "Tải lên từ di động\nCombo bộ sách hay của tác giả ABC, order by inbox"

    cleaned = clean_historical_facebook_text(text)

    for word in ("sách", "của", "by", "bộ sách", "Combo"):
        assert word in cleaned


def test_u034f_invisible_joiner_is_still_stripped():
    """Regression protection for CLAUDE.md section 12 -- reuses
    clean_facebook_raw_pages.normalize_unicode_text unchanged, so this
    must still work exactly as it does for the live pipeline."""
    text = "Tải lên từ di động\nS͏ách hay của tác giả X"

    cleaned = clean_historical_facebook_text(text)

    assert "͏" not in cleaned
    assert "Sách" in cleaned


def test_empty_input_produces_empty_output():
    assert clean_historical_facebook_text("") == ""
    assert clean_historical_facebook_text("   \n  \n ") == ""


def test_pure_function_repeated_call_is_identical():
    text = "Tải lên từ di động\nA\nB\nA\nB"

    first = clean_historical_facebook_text(text)
    second = clean_historical_facebook_text(text)

    assert first == second
