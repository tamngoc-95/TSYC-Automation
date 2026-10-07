"""Regression tests for retailer boilerplate found in APPROVED content
during the 2026-10-06 Fast Track Batch 5 (reference-discovery path):

  - short_description that is only the heading "Giới thiệu sách <title>"
    whose title ends in "?" (the old heading filter required no "?")
  - a trailing cross-sell "Mời các bạn tìm mua trọn bộ:" plus a list of
    the series' other titles
  - an encyclopedia footnote marker glued to the text ("...Việt Nam[3].")
"""
from __future__ import annotations

import re

import pytest

import prepare_product_content as ppc
from src.domain.rules import content_rules, storefront_text

TITLE = "Làm Bạn Cùng Con - Mẹ Vẫn Yêu Con Chứ?"

BODY = (
    "LÀM BẠN CÙNG CON là bộ 4 cuốn truyện nhỏ và dễ thương với bé mẫu giáo, "
    "nhưng mang lại nhiều bài học hữu ích cho cả bố mẹ.\n\n"
    "Henry Lo - tác giả bộ sách là chuyên gia Montessori quốc tế, có gần 20 năm "
    "kinh nghiệm giáo dục mầm non."
)

CROSS_SELL_TAIL = (
    "\n\nMời các bạn tìm mua trọn bộ:\n\n"
    "Làm bạn cùng con: Mẹ vẫn yêu con chứ?\n\n"
    "Làm bạn cùng con: Con ngoan mà!\n\n"
    "Làm bạn cùng con: Con không muốn đâu!"
)


# --- detection --------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        f"Giới thiệu sách {TITLE}",
        "Giới thiệu sách Đúng Là Tết",
        "Mời các bạn tìm mua trọn bộ:",
        "Mời quý độc giả đón đọc tập tiếp theo.",
        "Bạn có thể tìm mua trọn bộ tại các nhà sách.",
    ],
)
def test_retailer_heading_and_cross_sell_are_boilerplate(text):
    assert storefront_text.SOURCE_BOILERPLATE in storefront_text.find_text_defects(text)


@pytest.mark.parametrize(
    "text",
    [
        "Giới thiệu sách cho trẻ là một việc làm ý nghĩa của cha mẹ.",  # real sentence
        "Cuốn sách mời bạn đọc cùng suy ngẫm về hạnh phúc.",
        "Câu chuyện kể về Tâm và mẹ.",
        "Sách gồm Tập [1] và Tập [2].",  # spaced brackets are not footnotes
    ],
)
def test_ordinary_sentences_are_not_flagged(text):
    defects = storefront_text.find_text_defects(text)
    assert storefront_text.SOURCE_BOILERPLATE not in defects
    assert storefront_text.CITATION_MARKER not in defects


def test_glued_footnote_marker_is_detected_and_removed_without_changing_words():
    text = "Ông là Phó Bí thư Đảng ủy Đài Tiếng nói Việt Nam[3]. Hiện nay ông là nhà thơ."
    assert storefront_text.CITATION_MARKER in storefront_text.find_text_defects(text)

    repaired = storefront_text.repair_typography(text)
    assert repaired == "Ông là Phó Bí thư Đảng ủy Đài Tiếng nói Việt Nam. Hiện nay ông là nhà thơ."
    assert re.findall(r"[^\W\d]+", repaired) == re.findall(r"[^\W\d]+", text)


# --- normalization of new reference descriptions -----------------------


def test_normalized_description_drops_heading_and_cross_sell_list():
    raw = f"Giới thiệu sách {TITLE}\n\n{BODY}{CROSS_SELL_TAIL}"
    normalized = storefront_text.normalize_source_description(raw, titles=[TITLE])

    assert normalized == BODY
    assert "Giới thiệu sách" not in normalized
    assert "Mời các bạn" not in normalized
    assert "Con ngoan mà" not in normalized
    assert storefront_text.is_usable_source_description(normalized)


def test_cross_sell_followed_by_real_prose_keeps_the_prose():
    long_prose = "Đây là một đoạn mô tả thật dài về nội dung cuốn sách. " * 5
    text = f"{BODY}\n\nMời các bạn tìm mua trọn bộ.\n\n{long_prose.strip()}"
    assert storefront_text.drop_cross_sell_tail(text) == storefront_text.normalize_paragraphs(text)
    cleaned = storefront_text.remove_defective_sentences(text)
    assert "Mời các bạn" not in cleaned
    assert long_prose.strip()[:40] in cleaned


# --- orphaned retailer footer stub ------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Sách Gia Đình Tỉnh Thức của tác giả TS.",
        "Sách Mạnh Mẽ Như Dòng Sông của tác giả Sarah Noble",
    ],
)
def test_orphaned_retailer_footer_stub_is_boilerplate(text):
    assert storefront_text.SOURCE_BOILERPLATE in storefront_text.find_text_defects(text)


@pytest.mark.parametrize(
    "text",
    [
        "Sách của tác giả Nguyễn Nhật Ánh kể về tuổi thơ ở một làng quê miền Trung.",
        "Sách Gia Đình Tỉnh Thức của tác giả Shefali Tsabary là tài liệu hữu ích cho cha mẹ.",
    ],
)
def test_real_sentences_about_the_author_are_kept(text):
    assert storefront_text.SOURCE_BOILERPLATE not in storefront_text.find_text_defects(text)


def test_repair_removes_the_footer_stub_paragraph():
    long_description = f"{BODY}\n\nSách Gia Đình Tỉnh Thức của tác giả TS."
    cleaned = storefront_text.remove_defective_sentences(long_description)
    assert cleaned == BODY


# --- list lead-ins and rating widgets (Batch 20) -----------------------


@pytest.mark.parametrize("text", ["Hãy sẳn sàng để:", "Sách gồm các phần sau;"])
def test_prose_ending_on_a_colon_is_truncated(text):
    defects = storefront_text.find_content_defects({"short_description": text}, ["short_description"])
    assert storefront_text.TRUNCATED in defects["short_description"]


@pytest.mark.parametrize(
    "text",
    [
        "4,7*/5 với 1,607 lượt đánh giá trên trang Amazon.com",
        "4,2*/5 với 3,748 lượt đánh giá trên trang Goodreads.com",
        "Sách đạt 4.5/5 với hơn 2.000 lượt đánh giá.",
    ],
)
def test_rating_widgets_are_boilerplate(text):
    assert storefront_text.SOURCE_BOILERPLATE in storefront_text.find_text_defects(text)


def test_a_sentence_mentioning_five_parts_is_not_a_rating():
    text = "Cuốn sách gồm 5 phần, mỗi phần là một bài học về cuộc sống."
    assert storefront_text.SOURCE_BOILERPLATE not in storefront_text.find_text_defects(text)


# --- German closing quotes --------------------------------------------


@pytest.mark.parametrize(
    "text, complete",
    [
        ("„Mama, hast du mich dann trotzdem noch lieb?“", True),
        ("»Werde schnell groß!«", True),
        ("Ein Zitat endet hier.“", True),
        ("Der Text bricht hier ab „", False),  # stray opening quote
        ("Der Text bricht hier ab“", False),  # no sentence punctuation
    ],
)
def test_german_closing_quote_ends_a_sentence_only_after_punctuation(text, complete):
    assert storefront_text.ends_with_complete_sentence(text) is complete


# --- REPAIR of an already-APPROVED row ---------------------------------


def test_repair_plan_removes_heading_summary_and_cross_sell_tail():
    existing = {
        "product_name": TITLE,
        "short_description": f"Giới thiệu sách {TITLE}",
        "long_description": f"Giới thiệu sách {TITLE}\n\n{BODY}{CROSS_SELL_TAIL}",
        "seo_title": f"{TITLE} – Henry Lo",
        "seo_description": f"Giới thiệu sách {TITLE}",
        "author_summary": None,
        "product_details": None,
    }
    product = {"title": TITLE, "author": "Henry Lo, Yi-Ting Lee"}

    plan = ppc.plan_storefront_repair(existing, product, candidate={}, references=[])

    assert plan["outcome"] == ppc.REPAIR_OUTCOME_REPAIRABLE, plan["reason"]
    content = plan["content"]
    assert content["long_description"] == BODY
    assert content["short_description"].startswith("LÀM BẠN CÙNG CON là bộ 4 cuốn")
    assert "Giới thiệu sách" not in content["seo_description"]
    assert not storefront_text.find_content_defects(content, content_rules.CUSTOMER_FACING_FIELDS)
