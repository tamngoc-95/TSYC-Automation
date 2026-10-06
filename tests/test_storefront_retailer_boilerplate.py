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
