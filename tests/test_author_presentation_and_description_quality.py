"""Author presentation and storefront description quality (2026-10-16).

Protects: no metadata-style author repetition in VI/EN/DE descriptions;
the author is kept as structured metadata and the WooCommerce "Tác giả"
attribute; narrative author mentions are kept; natural description
structure; no invented age claims, superlatives or retailer copy. Offline.
"""
from __future__ import annotations

import pytest

import create_woocommerce_draft as cwd
import prepare_product_content as ppc
from src.domain.decisions import DecisionResult, Outcome
from src.domain.rules import content_rules, storefront_text, woo_presentation

AUTHOR = "Frances Hodgson Burnett"
PRODUCT = {
    "internal_product_id": "p1",
    "candidate_id": "c1",
    "product_code": "TSYC-FAHASA-2026-001-CAN-0007",
    "title": "Khu Vườn Bí Mật",
    "author": AUTHOR,
    "publisher": "Thông Tấn",
    "page_count": 56,
}
NARRATIVE_VI = (
    "Khu Vườn Bí Mật là tác phẩm kinh điển của nhà văn Frances Hodgson Burnett, "
    "được nhiều thế hệ độc giả yêu mến.\n\n"
    "Câu chuyện kể về Mary Lennox, một cô bé lớn lên thiếu vắng tình yêu thương của cha mẹ."
)

# --- duplicate author information, three languages ------------------------

AUTHOR_METADATA_CASES = [
    ("vi", "Tác giả: Frances Hodgson Burnett"),
    ("vi", "Tác giả của ấn phẩm là Frances Hodgson Burnett."),
    ("vi", "Tác giả của cuốn sách là L. Frank Baum."),
    ("en", "Author: Frances Hodgson Burnett"),
    ("en", "The author of this book is L. Frank Baum."),
    ("de", "Autorin: Frances Hodgson Burnett"),
    ("de", "Dieses Buch stammt von Thích Nhất Hạnh."),
    ("de", "Der Autor dieses Buches ist Stephen Guise."),
]


@pytest.mark.parametrize("language, line", AUTHOR_METADATA_CASES)
def test_metadata_author_line_is_detected_in_every_language(language, line):
    text = f"{line}\n\nCâu chuyện kể về một khu vườn bị lãng quên suốt nhiều năm."
    assert storefront_text.find_author_metadata(text) == [line]
    findings = storefront_text.find_content_defects({"long_description": text}, ("long_description",))
    assert storefront_text.AUTHOR_METADATA in findings["long_description"]


@pytest.mark.parametrize("language, line", AUTHOR_METADATA_CASES)
def test_metadata_author_line_is_removed_whole_and_nothing_else(language, line):
    rest = "Câu chuyện kể về một khu vườn bị lãng quên suốt nhiều năm."
    assert storefront_text.remove_author_metadata(f"{line}\n\n{rest}") == rest
    if line.endswith("."):
        # A template sentence followed by prose on the same line.
        assert storefront_text.remove_author_metadata(f"{line} {rest}") == rest


def test_label_running_into_prose_is_flagged_but_never_deleted():
    text = "Tác giả: Frances Hodgson Burnett Câu chuyện kể về một khu vườn bị lãng quên suốt nhiều năm. Mary lớn lên."
    assert storefront_text.find_author_metadata(text)
    # Where the name ends is not deterministic: nothing is removed, the
    # defect stays for human review.
    assert storefront_text.remove_author_metadata(text) == text


@pytest.mark.parametrize(
    "narrative",
    [
        NARRATIVE_VI,
        "The Secret Garden is the classic by Frances Hodgson Burnett, loved by generations of readers.",
        "Der geheime Garten ist der Klassiker von Frances Hodgson Burnett.",
        "CHO Hyun-Min là tác giả của cuốn sách, nằm trong bộ Giáo dục giới tính bản quyền Hàn Quốc.",
    ],
)
def test_narrative_author_mentions_are_kept(narrative):
    assert storefront_text.find_author_metadata(narrative) == []
    assert storefront_text.remove_author_metadata(narrative) == narrative
    assert not storefront_text.is_metadata_only_author_summary(narrative)


def test_author_metadata_is_not_a_defect_of_the_structured_details_field():
    content = {"product_details": "Tác giả: Frances Hodgson Burnett\nNhà xuất bản: Thông Tấn"}
    assert storefront_text.find_content_defects(content, ("product_details",)) == {}


@pytest.mark.parametrize("language", ["en", "de"])
def test_translation_with_author_line_is_not_auto_approved(language):
    passing = DecisionResult(outcome=Outcome.AUTO_PASS, rule_code="TRANSLATION_VALIDATION", reason="ok")
    line = "Author: Frances Hodgson Burnett" if language == "en" else "Autorin: Frances Hodgson Burnett"
    translation = {"long_description": f"{line}\n\nA story about a forgotten garden and healing friendship."}
    decision = ppc.with_translation_presentation_checks(passing, translation, PRODUCT)
    assert decision.outcome == Outcome.REVIEW_REQUIRED
    assert storefront_text.AUTHOR_METADATA in decision.reason


def test_clean_translation_keeps_its_decision():
    passing = DecisionResult(outcome=Outcome.AUTO_PASS, rule_code="TRANSLATION_VALIDATION", reason="ok")
    translation = {"long_description": "The Secret Garden is the classic by Frances Hodgson Burnett."}
    assert ppc.with_translation_presentation_checks(passing, translation, PRODUCT) is passing


# --- author metadata retained (structured + attribute) ---------------------


def test_generated_draft_has_no_author_summary_but_keeps_author_details():
    draft = ppc.build_safe_draft(PRODUCT)
    assert draft["author_summary"] is None
    assert f"Tác giả: {AUTHOR}" in draft["product_details"]


def test_legacy_generated_draft_is_still_recognized_as_generic():
    generated = ppc.build_safe_draft(PRODUCT)
    legacy = {**generated, "author_summary": f"Tác giả của ấn phẩm là {AUTHOR}."}
    assert ppc.is_generic_safe_draft(content=legacy, generated=generated)


def test_title_line_summary_never_appends_the_author():
    assert ppc._title_sentence("Khu Vườn Bí Mật", AUTHOR) == "“Khu Vườn Bí Mật”."


def test_author_becomes_the_woo_attribute():
    content = {"product_details": f"Tác giả: {AUTHOR}\nNhà xuất bản: Thông Tấn\nSố trang: 56"}
    attributes = woo_presentation.build_attributes(PRODUCT, content)
    assert [(a["name"], a["options"]) for a in attributes] == [
        ("Tác giả", [AUTHOR]),
        ("Nhà xuất bản", ["Thông Tấn"]),
        ("Số trang", ["56"]),
    ]
    assert all(a["visible"] is True and a["variation"] is False for a in attributes)


def test_multiple_authors_are_separate_attribute_options():
    product = {**PRODUCT, "author": "Huỳnh Long, Mai Chi"}
    attributes = woo_presentation.build_attributes(product, {"product_details": None})
    assert attributes[0]["options"] == ["Huỳnh Long", "Mai Chi"]


def test_no_author_no_invented_attribute():
    assert woo_presentation.build_attributes({**PRODUCT, "author": None}, {"product_details": None}) == []


def test_description_has_no_author_section_or_details_list_when_attributes_carry_them():
    content = {
        "long_description": NARRATIVE_VI,
        "author_summary": f"Tác giả của ấn phẩm là {AUTHOR}.",
        "product_details": f"Tác giả: {AUTHOR}\nNhà xuất bản: Thông Tấn\nSố trang: 56",
    }
    description = cwd.build_description(content)
    assert "<h3>Tác giả</h3>" not in description
    assert "Thông tin sản phẩm" not in description
    assert "Tác giả:" not in description
    # The author is named once, in the narrative.
    assert description.count(AUTHOR) == 1


def test_substantive_author_note_is_still_rendered():
    note = "CHO Hyun-Min là tác giả của cuốn sách, nằm trong bộ Giáo dục giới tính bản quyền Hàn Quốc."
    description = cwd.build_description({"long_description": "Nội dung sách.", "author_summary": note})
    assert "<h3>Tác giả</h3>" in description and "CHO Hyun-Min" in description


def test_combo_list_details_stay_in_description_without_the_author_line():
    details = "Tác giả: Vãn Tình\nSố lượng: 6 cuốn\nDanh sách sách:\n1. Lấy Tình Thâm Mà Đổi Đầu Bạc"
    description = cwd.build_description({"long_description": "Bộ sách.", "product_details": details})
    assert "Thông tin sản phẩm" in description
    assert "Danh sách sách:" in description
    assert "Tác giả" not in description
    attributes = woo_presentation.build_attributes({"author": "Vãn Tình"}, {"product_details": details})
    assert [a["name"] for a in attributes] == ["Tác giả"]


def test_create_payload_carries_author_attribute():
    payload = cwd.build_woocommerce_payload(
        product={**PRODUCT, "product_code": "TSYC-FB-2026-001-CAN-0001"},
        content={
            "product_content_id": "pc1",
            "product_name": "Khu Vườn Bí Mật",
            "long_description": NARRATIVE_VI,
            "short_description": "Tác phẩm kinh điển về một khu vườn bị lãng quên.",
            "product_details": f"Tác giả: {AUTHOR}",
        },
        uploaded_media=[],
    )
    assert payload["attributes"][0] == {
        "name": "Tác giả", "position": 0, "visible": True, "variation": False, "options": [AUTHOR],
    }
    assert "Tác giả" not in payload["description"]


def test_repair_drops_author_metadata_lines_and_keeps_narrative():
    existing = {
        "content_status": "APPROVED",
        "product_name": "Khu Vườn Bí Mật",
        "long_description": f"Tác giả: {AUTHOR}\n\n{NARRATIVE_VI}",
        "short_description": f"Tác giả của ấn phẩm là {AUTHOR}. Tác phẩm kinh điển về một khu vườn bị lãng quên.",
        "seo_title": "Khu Vườn Bí Mật",
        "seo_description": "Tác phẩm kinh điển về một khu vườn bị lãng quên.",
    }
    plan = ppc.plan_storefront_repair(existing, PRODUCT, {}, [])
    assert plan["outcome"] == ppc.REPAIR_OUTCOME_REPAIRABLE
    assert plan["category"] == ppc.REPAIR_CATEGORY_STOCK_OR_BOILERPLATE
    assert plan["content"]["long_description"] == NARRATIVE_VI
    assert plan["content"]["short_description"] == "Tác phẩm kinh điển về một khu vườn bị lãng quên."


# --- natural description structure / no invented content --------------------


def _issues(long_description, **kwargs):
    return content_rules.find_description_quality_issues({"long_description": long_description}, PRODUCT, **kwargs)


def test_natural_multi_paragraph_description_passes():
    decision = content_rules.evaluate_description_quality(
        {"long_description": NARRATIVE_VI, "short_description": "Tác phẩm kinh điển."},
        PRODUCT,
        source_texts=["Khu Vườn Bí Mật kể về cô bé Mary Lennox."],
        enforce_originality=True,
    )
    assert decision.is_auto_pass


def test_wall_of_text_is_flagged():
    single = " ".join(["Câu chuyện kể về một khu vườn bị lãng quên và những đứa trẻ."] * 14)
    assert content_rules.QUALITY_WALL_OF_TEXT in _issues(single)


def test_repetitive_sales_language_is_flagged():
    text = "Cuốn sách tuyệt vời, hấp dẫn, vô cùng cảm động.\n\nĐây là món quà ý nghĩa, phù hợp cho mọi gia đình."
    assert content_rules.QUALITY_SALES_LANGUAGE in _issues(text)


def test_one_closing_recommendation_is_not_sales_language():
    assert content_rules.QUALITY_SALES_LANGUAGE not in _issues(NARRATIVE_VI + "\n\nĐây là cuốn sách phù hợp cho các em nhỏ.")


def test_title_words_never_count_as_hype():
    product = {**PRODUCT, "title": "Cổ Tích Thế Giới Hay Nhất - Tuyệt Vời Nhất"}
    issues = content_rules.find_description_quality_issues(
        {"long_description": "Cổ Tích Thế Giới Hay Nhất - Tuyệt Vời Nhất là tuyển tập truyện cổ tích."},
        product,
        source_texts=[],
    )
    assert content_rules.QUALITY_EXAGGERATED_CLAIM not in issues


def test_unverifiable_superlative_is_flagged():
    issues = _issues("Đây là cuốn sách hay nhất mọi thời đại về thiên nhiên.", source_texts=["Một câu chuyện về khu vườn."])
    assert content_rules.QUALITY_EXAGGERATED_CLAIM in issues


def test_age_recommendation_requires_a_source():
    text = "Cuốn sách dành cho trẻ từ 6 tuổi trở lên."
    assert content_rules.QUALITY_UNVERIFIED_AGE in _issues(text, source_texts=["Sách kể về khu vườn."])
    assert content_rules.QUALITY_UNVERIFIED_AGE not in _issues(text, source_texts=["Sách dành cho bé 6 tuổi trở lên."])
    # Unknown sources are not evidence either way: the check is skipped.
    assert content_rules.QUALITY_UNVERIFIED_AGE not in _issues(text, source_texts=None)


def test_character_age_is_not_an_age_recommendation():
    assert content_rules.QUALITY_UNVERIFIED_AGE not in _issues("Mary là cô bé 9 tuổi.", source_texts=[])


def test_long_verbatim_retailer_copy_is_flagged_for_live_products_only():
    source = " ".join(f"từ{index}" for index in range(60))
    copied = f"Mở đầu. {source} Kết thúc."
    assert content_rules.QUALITY_VERBATIM_COPY in _issues(copied, source_texts=[source], enforce_originality=True)
    # FB-HIST draft-safe content reuses a normalized reference by design.
    assert content_rules.QUALITY_VERBATIM_COPY not in _issues(copied, source_texts=[source], enforce_originality=False)


def test_heavy_author_repetition_is_flagged():
    text = "\n\n".join(f"{AUTHOR} viết đoạn {index}." for index in range(6))
    assert content_rules.QUALITY_AUTHOR_REPETITION in _issues(text)


def test_approval_gate_uses_quality_only_when_sources_are_supplied():
    generated = ppc.build_safe_draft(PRODUCT)
    content = {
        **generated,
        "long_description": NARRATIVE_VI + "\n\nĐây là cuốn sách hay nhất mọi thời đại cho trẻ từ 6 tuổi trở lên.",
        "short_description": "Tác phẩm kinh điển về một khu vườn bị lãng quên.",
        "seo_description": "Tác phẩm kinh điển.",
    }
    existing = {"content_status": "DRAFTED", **content}
    ppc.validate_approval_content(existing=existing, content=content, generated=generated, product=PRODUCT)
    with pytest.raises(RuntimeError, match="CONTENT_DESCRIPTION_QUALITY"):
        ppc.validate_approval_content(
            existing=existing, content=content, generated=generated, product=PRODUCT,
            description_quality={"source_texts": ["Câu chuyện về Mary Lennox."], "enforce_originality": True},
        )
