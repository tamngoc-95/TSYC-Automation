"""False positives found in the 14 flagged drafts (2026-10-08) -- and the
real failures the same rules must still catch."""
from __future__ import annotations

import pytest

from src.domain.rules import multilingual_consistency as mc


def _evaluate(vi_long: str, en_long: str, *, facts: dict | None = None, vi_short: str = "", en_short: str = "",
              title: str = "Sách Mẫu"):
    vi = {"product_name": title, "short_description": vi_short or vi_long[:60], "long_description": vi_long}
    en = {"product_name": title, "short_description": en_short or en_long[:60], "long_description": en_long}
    return mc.evaluate_multilingual_consistency(
        vi=vi, translations={"en": en, "de": en}, verified_facts=facts or {"title": title},
    ).per_language.get("en", ())


BODY_EN = "This is a gentle story for the whole family, and it is told with warmth by the author of the book."
BODY_VI = "Đây là một câu chuyện nhẹ nhàng cho cả gia đình, được tác giả kể lại bằng tất cả sự ấm áp của mình."


def test_common_word_publisher_is_not_required_outside_publisher_context():
    facts = {"title": "Sách Mẫu", "publisher": "Thế Giới"}
    codes = _evaluate(BODY_VI + " Cuốn sách đưa bạn vào thế giới của trẻ thơ.",
                      BODY_EN + " The book takes you into the world of childhood.", facts=facts)
    assert mc.TRANSLATION_NAME_NOT_PRESERVED not in codes


def test_publisher_named_as_publisher_is_still_required():
    facts = {"title": "Sách Mẫu", "publisher": "Thế Giới"}
    codes = _evaluate(BODY_VI + " Sách do NXB Thế Giới phát hành.", BODY_EN + " It is published by World.", facts=facts)
    assert mc.TRANSLATION_NAME_NOT_PRESERVED in codes


def test_each_author_is_required_individually_not_the_joined_field():
    facts = {"title": "Sách Mẫu", "author": "Daniel J. Siegel, Tina Payne Bryson"}
    vi = BODY_VI + " Daniel J. Siegel, Tina Payne Bryson là hai tác giả."
    assert mc.TRANSLATION_NAME_NOT_PRESERVED not in _evaluate(
        vi, BODY_EN + " Daniel J. Siegel and Tina Payne Bryson are the authors.", facts=facts)
    assert mc.TRANSLATION_NAME_NOT_PRESERVED in _evaluate(
        vi, BODY_EN + " Daniel J. Siegel is the author.", facts=facts)


def test_honorific_is_not_part_of_the_required_name():
    facts = {"title": "Sách Mẫu", "author": "Tiến sĩ Shefali Tsabary"}
    codes = _evaluate(BODY_VI + " Tiến sĩ Shefali Tsabary viết cuốn sách này.",
                      BODY_EN + " Dr. Shefali Tsabary wrote this book.", facts=facts)
    assert mc.TRANSLATION_NAME_NOT_PRESERVED not in codes


@pytest.mark.parametrize(
    "vi_fragment, en_fragment",
    [
        ("5 năm sau, sách được tái bản.", "Five years later, the book was reissued."),
        ("Đây là 1 trong 4 cuốn sách.", "This is one of 4 books."),
        ("cậu bé mới 8,9 tuổi.", "a boy of only 8 or 9."),
    ],
)
def test_numbers_written_as_words_or_split_ranges_are_carried_over(vi_fragment, en_fragment):
    assert mc.TRANSLATION_DROPPED_FACT not in _evaluate(BODY_VI + " " + vi_fragment, BODY_EN + " " + en_fragment)


def test_a_really_dropped_number_still_fails():
    assert mc.TRANSLATION_DROPPED_FACT in _evaluate(BODY_VI + " Sách có 196 trang.", BODY_EN + " The book has many pages.")


def test_title_without_edition_suffix_is_not_mixed_language():
    title = "Tuổi Trẻ Đáng Giá Bao Nhiêu (Tái Bản 2021)"
    codes = _evaluate(BODY_VI + " Tuổi trẻ đáng giá bao nhiêu? được chia làm 3 phần.",
                      BODY_EN + " The author divides Tuổi trẻ đáng giá bao nhiêu? into 3 parts.",
                      facts={"title": title}, title=title)
    assert mc.TRANSLATION_MIXED_LANGUAGE not in codes


def test_quoted_vietnamese_title_inside_english_is_not_mixed_language():
    codes = _evaluate(BODY_VI + " Cuốn thứ hai có tên “Một năm để thay đổi tất cả”.",
                      BODY_EN + " Her second book is titled “Một năm để thay đổi tất cả”.")
    assert mc.TRANSLATION_MIXED_LANGUAGE not in codes


def test_untranslated_vietnamese_sentence_is_still_mixed_language():
    codes = _evaluate(BODY_VI, BODY_EN + " Cuốn sách này mang lại nhiều giá trị cho người đọc trẻ tuổi.")
    assert mc.TRANSLATION_MIXED_LANGUAGE in codes


def test_short_quoted_line_with_little_signal_is_not_wrong_language():
    codes = _evaluate(BODY_VI, BODY_EN, vi_short="“Mẹ ơi, nếu con gây chuyện, mẹ vẫn yêu con chứ ạ?”",
                      en_short="\"Mummy, if I make trouble, will you still love me?\"")
    assert mc.TRANSLATION_WRONG_LANGUAGE not in codes


@pytest.mark.parametrize(
    "en_fragment",
    [
        "All of this progress comes at a price.",
        "Use dashi stock made from kelp.",
    ],
)
def test_english_commerce_idioms_are_not_commerce(en_fragment):
    assert mc.TRANSLATION_COMMERCE_LANGUAGE not in _evaluate(BODY_VI, BODY_EN + " " + en_fragment)


@pytest.mark.parametrize(
    "de_fragment",
    [
        "All diese Fortschritte haben ihren Preis.",
        "2017 erhielt sie den Preis Psychologies-Fnac.",
        "Beikost liefert dem Kind Energie.",
        "Wir können die Freiheit selbst kosten.",
        "vor allem auf Kosten Ihrer Geduld.",
    ],
)
def test_german_commerce_idioms_are_not_commerce(de_fragment):
    result = mc.evaluate_multilingual_consistency(
        vi={"product_name": "Sách Mẫu", "short_description": BODY_VI, "long_description": BODY_VI},
        translations={"de": {"product_name": "Sách Mẫu", "short_description": "Eine Geschichte für die Familie.",
                             "long_description": "Eine Geschichte für die ganze Familie. " + de_fragment}},
        verified_facts={"title": "Sách Mẫu"},
    )
    assert mc.TRANSLATION_COMMERCE_LANGUAGE not in result.per_language.get("de", ())


@pytest.mark.parametrize(
    "fragment, language",
    [
        ("Order now at a special price.", "en"),
        ("Currently in stock.", "en"),
        ("Der Preis beträgt 9,99 €.", "de"),
        ("Die Kosten für den Versand übernehmen wir.", "de"),
        ("Lieferung in 2 Tagen.", "de"),
    ],
)
def test_real_commerce_wording_is_still_caught(fragment, language):
    text = (BODY_EN if language == "en" else "Eine Geschichte für die ganze Familie.") + " " + fragment
    result = mc.evaluate_multilingual_consistency(
        vi={"product_name": "Sách Mẫu", "short_description": BODY_VI, "long_description": BODY_VI},
        translations={language: {"product_name": "Sách Mẫu", "short_description": text[:60], "long_description": text}},
        verified_facts={"title": "Sách Mẫu"},
    )
    assert mc.TRANSLATION_COMMERCE_LANGUAGE in result.per_language.get(language, ())
