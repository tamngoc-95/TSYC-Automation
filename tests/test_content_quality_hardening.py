"""Regression tests for the 2026-10-02 content-quality hardening.

Defects observed in APPROVED historical content (Woo drafts 4217/4219/
4221/4223):
  - ""Intelligence Quotient"" (CSV-escaped doubled quotes)
  - "... bản sắc riêng của mình không?." (stray period after "?")
  - "Chris Hadfiled" (author "Chris Hadfield" misspelled in the text)
  - quoted "người thấy tốt" (title "Người Thầy Tốt" misspelled)
  - "biết biết" (duplicated word -- advisory only: Vietnamese
    reduplication such as "song song"/"từ từ" is grammatical)
"""
from __future__ import annotations

import re
import unicodedata

import pytest

import prepare_product_content as ppc
from src.domain.rules import author_rules, content_rules, storefront_text


# --- typography: detection -------------------------------------------------


@pytest.mark.parametrize(
    "text, code",
    [
        ('IQ là viết tắt của ""Intelligence Quotient"", chỉ số thông minh.', storefront_text.DOUBLED_QUOTE),
        ("Chúng ta có dám sống khác không?. Có bao giờ bạn tự hỏi?", storefront_text.MALFORMED_PUNCTUATION),
        ("Thật tuyệt!. Sách hay.", storefront_text.MALFORMED_PUNCTUATION),
        ("Sách gồm truyện,. tranh và thơ.", storefront_text.MALFORMED_PUNCTUATION),
    ],
)
def test_typography_defects_are_detected(text, code):
    assert code in storefront_text.find_text_defects(text)


@pytest.mark.parametrize(
    "text",
    [
        "Bạn có biết không?... Câu trả lời ở trong sách.",  # ellipsis after "?"
        "Các kỹ năng: tô chữ, tô số, v.v., giúp bé làm quen.",  # "v.v.," is correct
        "“Gấu con đi ngủ” là câu chuyện nhẹ nhàng.",
        'Cuốn sách "Tư Duy Ngược" giúp bạn.',
        "Giá trị 1.5 lần.",
    ],
)
def test_legitimate_punctuation_is_not_a_defect(text):
    assert not (storefront_text.find_text_defects(text) & storefront_text.TYPOGRAPHY_DEFECT_CODES)


def test_repair_typography_is_deterministic_and_changes_no_word():
    source = 'IQ là ""Intelligence Quotient"". Không?. Hay!. Có,. hết.'
    repaired = storefront_text.repair_typography(source)

    assert repaired == 'IQ là "Intelligence Quotient". Không? Hay! Có. hết.'
    assert not storefront_text.find_text_defects(repaired) & storefront_text.TYPOGRAPHY_DEFECT_CODES
    # Only punctuation changed: the words themselves are identical.
    assert re.findall(r"\w+", repaired) == re.findall(r"\w+", source)


def test_repair_typography_keeps_ellipses():
    assert storefront_text.repair_typography("Vì sao?... Và,...") == "Vì sao?... Và,..."


def test_normalized_reference_description_is_typography_clean():
    """New content built from a reference never inherits the artifacts."""
    raw = (
        'IQ là viết tắt của ""Intelligence Quotient"", thường gọi là chỉ số thông minh. '
        "Chúng ta có dám đi ngược đám đông không?. Cuốn sách giúp trẻ phát triển tư duy."
    )
    normalized = storefront_text.normalize_source_description(raw)

    assert '""' not in normalized
    assert "?." not in normalized
    assert storefront_text.is_usable_source_description(normalized)


# --- duplicated words: advisory only --------------------------------------


def test_duplicated_word_is_advisory_never_a_defect():
    text = "Tôi biết biết rằng các mùa luôn luôn thay đổi, song song với cuộc sống."

    # Reported for an operator, legitimate reduplication included -- which
    # is exactly why it can never be a blocking defect.
    assert storefront_text.find_duplicated_words(text) == ["biết biết", "luôn luôn", "song song"]
    assert storefront_text.find_text_defects(text) == set()


def test_duplicated_word_across_paragraphs_is_not_reported():
    assert storefront_text.find_duplicated_words("Osho\n\nOsho là tác giả.") == []


# --- typography: REPAIR action ------------------------------------------------


def _approved_row(long_description: str, short_description: str) -> dict:
    return {
        "product_name": "Phát Triển IQ Cho Bé (5-6 tuổi)",
        "short_description": short_description,
        "long_description": long_description,
        "author_summary": None,
        "product_details": None,
        "seo_title": "Phát Triển IQ Cho Bé (5-6 tuổi)",
        "seo_description": short_description,
    }


def test_repair_plan_fixes_typography_on_approved_content():
    existing = _approved_row(
        long_description='IQ là viết tắt của ""Intelligence Quotient"", thường gọi là chỉ số thông minh.\n\n'
        "Bộ sách giúp bé làm quen với toán và tiếng Việt.",
        short_description='IQ là viết tắt của ""Intelligence Quotient"", thường gọi là chỉ số thông minh.',
    )
    product = {"title": "Phát Triển IQ Cho Bé (5-6 tuổi)", "author": None}

    plan = ppc.plan_storefront_repair(existing, product, candidate={}, references=[])

    assert plan["category"] == ppc.REPAIR_CATEGORY_TYPOGRAPHY
    assert plan["outcome"] == ppc.REPAIR_OUTCOME_REPAIRABLE
    assert '"Intelligence Quotient"' in plan["content"]["long_description"]
    assert '""' not in plan["content"]["short_description"]


# --- person-name parsing ----------------------------------------------------


@pytest.mark.parametrize(
    "field, expected",
    [
        ("Chris Hadfield; Anh Em Nhà Fan (minh họa); Thư Vũ (dịch)", ["Chris Hadfield", "Anh Em Nhà Fan", "Thư Vũ"]),
        ("Lời: Tạ Như; Tranh: Hình Bắc Ninh; Người dịch: Thùy Dương", ["Tạ Như", "Hình Bắc Ninh", "Thùy Dương"]),
        ("ThS. Ngô Nam; Minh họa: Khánh Chi", ["Ngô Nam", "Khánh Chi"]),
        ("Isabel M. Arques & Angela Pelaez-Vargas; Minh Vũ (dịch)", ["Isabel M. Arques", "Angela Pelaez-Vargas", "Minh Vũ"]),
        ("Antonella Abbatiello; Phạm P... , Vũ Hà Tường (dịch)", ["Antonella Abbatiello", "Vũ Hà Tường"]),
        ("Sasaki Mio (lời và tranh)", ["Sasaki Mio"]),
        (None, []),
    ],
)
def test_split_person_names(field, expected):
    assert author_rules.split_person_names(field) == expected


def test_person_name_key_folds_hyphen_and_diacritics():
    assert author_rules.person_name_key("Ji-Yoon Shin") == author_rules.person_name_key("Ji Yoon Shin")
    assert author_rules.person_name_key("Ngô Nam") == author_rules.person_name_key("Ngo Nam")


# --- name / title consistency ---------------------------------------------


HADFIELD_PRODUCT = {
    "title": "Bóng Tối Bao La",
    "author": "Chris Hadfield; Anh Em Nhà Fan (minh họa); Thư Vũ (dịch)",
}


def test_author_transposition_is_flagged():
    content = {
        "long_description": "Câu chuyện có thật về phi hành gia Chris Hadfiled thời thơ ấu.",
    }

    findings = content_rules.find_name_inconsistencies(content, HADFIELD_PRODUCT)

    assert findings == {"long_description": ["Chris Hadfiled"]}
    decision = content_rules.evaluate_name_consistency(content, HADFIELD_PRODUCT)
    assert decision.outcome == "REVIEW_REQUIRED"
    assert decision.rule_code == content_rules.CONTENT_NAME_INCONSISTENCY


def test_correct_author_spelling_passes():
    content = {"long_description": "Câu chuyện có thật về phi hành gia Chris Hadfield thời thơ ấu."}
    assert content_rules.evaluate_name_consistency(content, HADFIELD_PRODUCT).is_auto_pass


def test_nfd_text_is_not_mistaken_for_a_misspelling():
    product = {"title": "Người Mẹ Tốt Hơn Là Người Thầy Tốt", "author": "Doãn Kiến Lợi"}
    text = unicodedata.normalize("NFD", "Sổ tay nuôi dạy con của thạc sĩ Doãn Kiến Lợi.")
    assert content_rules.evaluate_name_consistency({"long_description": text}, product).is_auto_pass


def test_quoted_title_with_wrong_diacritic_is_flagged():
    product = {"title": "Người Mẹ Tốt Hơn Là Người Thầy Tốt", "author": "Doãn Kiến Lợi"}
    content = {
        "long_description": "\"Người mẹ tốt hơn là người thấy tốt\" đề cập đến nhiều vấn đề nuôi dạy con.",
    }

    findings = content_rules.find_name_inconsistencies(content, product)

    assert findings == {"long_description": ["Người mẹ tốt hơn là người thấy tốt"]}


def test_quoted_title_with_only_case_difference_passes():
    product = {"title": "Người Mẹ Tốt Hơn Là Người Thầy Tốt", "author": None}
    content = {"long_description": "\"Người mẹ tốt hơn là người thầy tốt\" là cuốn sổ tay nuôi dạy con."}
    assert content_rules.evaluate_name_consistency(content, product).is_auto_pass


def test_single_word_names_and_unrelated_words_are_not_flagged():
    product = {"title": "Đảo", "author": "Wanderer"}
    content = {"long_description": "Một hòn đảo nhỏ. Wandere là từ khác."}
    assert content_rules.evaluate_name_consistency(content, product).is_auto_pass


def test_approval_gate_rejects_misspelled_author_when_product_is_given():
    content = {
        "short_description": "Phi hành gia Chris Hadfiled kể lại tuổi thơ.",
        "long_description": "Phi hành gia Chris Hadfiled kể lại tuổi thơ của mình.",
        "author_summary": None,
        "seo_description": "Phi hành gia Chris Hadfiled kể lại tuổi thơ.",
    }
    existing = dict(content, content_status="DRAFTED")
    generated = {"short_description": "x", "long_description": "y", "author_summary": "z", "seo_description": "w"}

    with pytest.raises(RuntimeError, match="CONTENT_NAME_INCONSISTENCY"):
        ppc.validate_approval_content(existing=existing, content=content, generated=generated, product=HADFIELD_PRODUCT)

    # Without a product the pre-existing gate is unchanged.
    ppc.validate_approval_content(existing=existing, content=content, generated=generated)
