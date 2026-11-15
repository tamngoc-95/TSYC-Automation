"""EN/DE translation drafts: attribute labels from the approved translation's
own details text, values always from the VI source (bridge 1.2.0).

Before 2026-11-15 every EN/DE copy kept the Vietnamese attribute labels
("Tác giả", "Nhà xuất bản", "Số trang") while TSYC_CONTENT_GUIDE 7a removes
those facts from the description: an English page would show them only
under Vietnamese labels.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.domain import multilingual_adapter as ma
from src.domain.rules import woo_presentation as wp
from src.services import polylang_bridge_writer as pbw

BRIDGE_PHP = Path(__file__).resolve().parents[1] / "wordpress" / "tsyc-polylang-bridge" / "tsyc-polylang-bridge.php"


def test_labels_come_from_the_approved_details_lines() -> None:
    details = "Author: Văn Thành Lê\nPublisher: Kim Đồng\nPages:  148"
    assert wp.localized_attribute_labels(details) == [
        ("Văn Thành Lê", "Author"), ("Kim Đồng", "Publisher"), ("148", "Pages")]


def test_german_gendered_label_is_kept_as_approved() -> None:
    assert ("Tống Linh", "Autorin") in wp.localized_attribute_labels("Autorin: Tống Linh\nSeiten: 64")


def test_a_value_with_two_labels_is_ambiguous_and_dropped() -> None:
    labels = wp.localized_attribute_labels("Author: Lab A\nIllustrator: Lab A\nPages: 64")
    assert labels == [("64", "Pages")]


def test_non_attribute_lines_are_ignored_never_guessed() -> None:
    assert wp.localized_attribute_labels("Book list:\n1. Volume one\n2. Volume two") == []
    assert wp.localized_attribute_labels(None) == []


def test_value_key_matches_the_bridge_normalization() -> None:
    assert wp.attribute_value_key("  Kim   ĐỒNG ") == wp.attribute_value_key("kim đồng")


def test_adapter_refuses_another_languages_row() -> None:
    with pytest.raises(ValueError):
        ma.build_attribute_labels({"content_language": "de", "product_details": "Verlag: X"}, "en")
    assert ma.build_attribute_labels({"content_language": "en", "product_details": "Publisher: X"}, "en") == (("X", "Publisher"),)


def _request(**overrides):
    values = dict(source_id=7, lang="en", request_id="tsyc-tr-en-0001", authorization_id="a", internal_product_id="b",
                  product_code="c", expected_source_modified_gmt="2026-11-15T00:00:00",
                  content={"name": "n", "description": "d", "short_description": "s"})
    return pbw.CreateRequest(**{**values, **overrides})


def test_body_omits_labels_when_empty_so_old_fingerprints_hold() -> None:
    assert "attribute_labels" not in _request().as_body()


def test_body_sends_labels_only_never_values_as_attributes() -> None:
    body = _request(attribute_labels=(("Kim Đồng", "Publisher"),)).as_body()
    assert body["attribute_labels"] == [{"value": "Kim Đồng", "label": "Publisher"}]
    assert "attributes" not in body and "attributes" not in body["content"]


def test_vocabulary_covers_every_contributor_role_label() -> None:
    from src.domain.rules import contributor_roles

    for lang in ("en", "de"):
        vocabulary = wp.ATTRIBUTE_LABEL_VOCABULARY[lang]
        for vi_label in contributor_roles.ROLE_ATTRIBUTE_LABELS.values():
            assert vi_label in vocabulary, (lang, vi_label)
        assert wp.AUTHOR_ATTRIBUTE_NAME in vocabulary


@pytest.mark.parametrize("vi_label", ["Định dạng", "Ngôn ngữ", "Bản quyền"])
def test_labels_with_vietnamese_word_values_are_never_relabelled(vi_label: str) -> None:
    # "Format: Bìa mềm" would be a half-translated fact: keep VI and report it.
    for lang in ("en", "de"):
        assert vi_label not in wp.ATTRIBUTE_LABEL_VOCABULARY[lang]


def test_vocabulary_is_labels_only_and_has_no_translation_language_gaps() -> None:
    assert set(wp.ATTRIBUTE_LABEL_VOCABULARY["en"]) == set(wp.ATTRIBUTE_LABEL_VOCABULARY["de"])
    assert ma.build_attribute_vocabulary("en")[0] == ("Tác giả", "Author", "Authors")
    with pytest.raises(ValueError):
        ma.build_attribute_vocabulary("vi")


def test_body_carries_the_vocabulary_with_plural_forms() -> None:
    body = _request(attribute_vocabulary=(("Minh họa", "Illustrator", "Illustrators"),)).as_body()
    assert body["attribute_vocabulary"] == [{"source_label": "Minh họa", "label": "Illustrator", "label_plural": "Illustrators"}]
    assert "attribute_vocabulary" not in _request().as_body()


def test_bridge_keeps_values_from_source_and_reports_unlocalized() -> None:
    php = BRIDGE_PHP.read_text(encoding="utf-8")
    assert "tsyc_bridge_localized_attributes(" in php
    assert "array_values($source->get_attributes('edit'))" in php
    assert "'ATTRIBUTE_LABELS_NOT_LOCALIZED'" in php
    # Optional fields, strictly validated; taxonomy attributes are never renamed.
    assert "$optional = array('attribute_labels', 'attribute_vocabulary');" in php
    assert "tsyc_bad_attribute_labels" in php
    assert "if ($attribute->is_taxonomy()) {" in php
    # Approved text first, vocabulary second; a colliding name is never used.
    body = php[php.index("function tsyc_bridge_localized_attributes"):]
    assert body.index("$by_value[$key]") < body.index("$by_source_label[$source_key]")
    assert "$name_count[tsyc_bridge_attribute_key($planned[$index])] === 1" in body
    # Fingerprint unchanged for a request without labels.
    assert "if (!empty($body[$field])) {" in php
    # Never a value from the request: set_options is not called by the bridge.
    assert "set_options(" not in php
