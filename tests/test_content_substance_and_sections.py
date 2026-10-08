"""Content-quality gate from the 2026-10-08 review of the Fast Track drafts:
tables of contents / praise / press sections, retailer promotion and
navigation, copied first-person author prefaces, thin descriptions."""
from __future__ import annotations

import pytest

import prepare_product_content as ppc
from src.domain.rules import content_rules, storefront_text

BODY = (
    "Khi nuôi dạy con cái, ta thường xuyên bắt gặp mình đứng trước cuộc chiến "
    "giữa lý trí và con tim. Cuốn sách giúp cha mẹ nhìn con như một tâm hồn "
    "riêng biệt và điều chỉnh cách nuôi dạy theo nhu cầu của con."
)
AUTHOR_INFO = "Thông tin tác giả Shefali Tsabary\n\nNhận bằng tiến sĩ Tâm lý lâm sàng, Đại học Columbia."


@pytest.mark.parametrize(
    "heading, block",
    [
        ("Mục lục sách", "Lời tựa\n\nChương 1: Một con người đích thực\n\nChương 2: Mục đích tâm linh"),
        ("Lời khen cho cuốn sách 8 Loại Hình Thông Minh", "“Một cuốn sách hay.” - Jim Daly"),
        ("Người nổi tiếng nói về cuốn sách Hiểu Về Trái Tim", "“Rất ý nghĩa!” - Nhà báo A"),
        ("Báo chí nói gì về Hiểu Về Trái Tim", "“Một cuốn sách đặc biệt.” – baomoi.vn"),
        ("MỘT SỐ ĐÁNH GIÁ VỀ CUỐN SÁCH", "“Cuốn sách sáng suốt.” - The Guardian"),
    ],
)
def test_third_party_sections_are_detected_and_dropped_up_to_author_info(heading, block):
    text = f"{BODY}\n\n{heading}\n\n{block}\n\n{AUTHOR_INFO}"
    assert storefront_text.THIRD_PARTY_SECTION in storefront_text.find_content_defects(
        {"long_description": text}, ["long_description"]
    )["long_description"]
    assert storefront_text.drop_third_party_sections(text) == f"{BODY}\n\n{AUTHOR_INFO}"


def test_section_heading_words_inside_prose_are_not_sections():
    text = f"{BODY} Mục lục của sách được sắp xếp theo độ tuổi của trẻ."
    assert not storefront_text.has_third_party_section(text)


@pytest.mark.parametrize(
    "sentence",
    [
        "“Về nhà thôi!” - cuốn sách hot nhất những tháng cuối năm 2021.",
        "Là bộ sách mà mọi em bé 5+ đều nhất định cần có!",
        "Đây là cuốn sách mà bạn không nên bỏ qua.",
        "Cuốn sách đã chính thức trở lại kệ sách nhà Sư Tử Bé rồi đây!",
        "Hộp Háo Hức tháng 11 mang tới cuốn sách Cùng Giúp Báo Đốm Khỏi Ốm.",
        "Mừng tuổi ngay mỗi độc giả nhí một bảng sticker Xuân vui vẻ!",
        "Xem thêm",
    ],
)
def test_promotion_and_navigation_are_boilerplate(sentence):
    assert storefront_text.SOURCE_BOILERPLATE in storefront_text.find_text_defects(sentence)


def test_first_person_author_preface_is_not_auto_approved():
    preface = (
        "Cuốn sách này thực sự đã giúp đỡ cho hàng triệu độc giả, trong đó có tôi. "
        "Nếu không có những ý niệm này thì chưa chắc tôi đã có được cuộc hôn nhân "
        "hạnh phúc. Tôi có thể hiểu những phản ứng của vợ tôi theo cách khách quan hơn."
    )
    decision = content_rules.evaluate_description_substance({"long_description": preface})
    assert not decision.is_auto_pass
    assert storefront_text.FIRST_PERSON_SOURCE in decision.evidence["codes"]


def test_quoted_first_person_and_third_person_text_pass():
    text = (
        f"{BODY} Tác giả chia sẻ: “Tôi đã từng quyết lòng ra đi tìm hạnh phúc. "
        "Tôi tin rằng nó có thật và tôi đã tìm thấy con đường.”"
    )
    assert content_rules.evaluate_description_substance({"long_description": text}).is_auto_pass


def test_thin_description_is_not_auto_approved():
    aphorism = (
        "Hạnh phúc không phải là thứ được định đoạt bằng tiền. Hạnh phúc phải được "
        "định đoạt bằng tâm thế của mỗi chúng ta."
    )
    decision = content_rules.evaluate_description_substance({"long_description": aphorism})
    assert not decision.is_auto_pass
    assert storefront_text.THIN_DESCRIPTION in decision.evidence["codes"]


def test_repair_drops_a_table_of_contents_block():
    existing = {
        "product_name": "Làm Cha Mẹ Tỉnh Thức",
        "short_description": BODY,
        "long_description": f"{BODY}\n\nMục lục sách\n\nLời tựa\n\nChương 1: Một con người\n\n{AUTHOR_INFO}",
        "seo_title": "Làm Cha Mẹ Tỉnh Thức – Shefali Tsabary",
        "seo_description": BODY[:120] + ".",
        "author_summary": None,
        "product_details": None,
    }
    plan = ppc.plan_storefront_repair(existing, {"title": "Làm Cha Mẹ Tỉnh Thức", "author": "Shefali Tsabary"}, {}, [])
    assert plan["outcome"] == ppc.REPAIR_OUTCOME_REPAIRABLE, plan["reason"]
    assert "Chương 1" not in plan["content"]["long_description"]
    assert plan["content"]["long_description"].endswith("Đại học Columbia.")
