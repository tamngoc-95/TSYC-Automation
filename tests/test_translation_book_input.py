"""Approved product_contents + recorded VI row -> runner BookInput (offline).

Protects the fail-closed loader between TSYC's records and the translation
draft runner: it never chooses between approved rows, never accepts a
translation older than its VI source, and renders through the production
renderer with localized headings.
"""
from __future__ import annotations

import pytest

import create_woocommerce_draft as cwd
from src.domain import language_product_mapping as lpm
from src.domain import multilingual_adapter as ma
from src.domain import translation_book_input as tbi

BOOK = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
CODE = "TSYC-FAHASA-2026-004-CAN-0001"


def vi_row(**overrides) -> lpm.LanguageProduct:
    values = dict(internal_product_id=BOOK, product_code=CODE, language_code="vi", woocommerce_product_id=5000,
                  origin=lpm.ORIGIN_TSYC_CREATED, mapping_status=lpm.RECORDED,
                  baseline_source=lpm.BASELINE_CREATE_RESPONSE, baseline_modified_gmt="2026-11-14T10:00:00",
                  baseline_text={"name": "Sách", "description": "", "short_description": ""})
    values.update(overrides)
    return lpm.LanguageProduct(**values)


def content(lang: str, **overrides) -> dict:
    row = {"product_content_id": f"content-{lang}", "internal_product_id": BOOK, "content_language": lang,
           "content_status": "APPROVED", "review_required": False,
           "approved_at": {"vi": "2026-11-01T08:00:00+00:00", "en": "2026-11-02T08:00:00.123456+00:00",
                           "de": "2026-11-02T09:00:00+00:00"}[lang],
           "product_name": {"vi": "Sách", "en": "Book", "de": "Buch"}[lang],
           "short_description": f"Short {lang}.", "long_description": f"Long {lang}.",
           "author_summary": None, "product_details": None}
    row.update(overrides)
    return row


def rows(**per_language) -> list[dict]:
    return [content(lang, **per_language.get(lang, {})) for lang in ("vi", "en", "de")]


def build(vi=None, content_rows=None):
    return tbi.build_book_input(vi or vi_row(), rows() if content_rows is None else content_rows, cwd.render_content)


def test_ready_book_carries_exactly_the_approved_translations():
    result = build()
    assert result.ready and result.refusal is None
    assert set(result.contents) == {"en", "de"}
    assert result.contents["en"]["product_content_id"] == "content-en"
    assert result.as_dict()["product_content_ids"] == {"en": "content-en", "de": "content-de"}


def test_rows_of_other_books_and_non_final_rows_are_ignored():
    noise = [content("en", internal_product_id=OTHER, product_content_id="other-en"),
             content("de", content_status="DRAFTED", product_content_id="draft-de"),
             content("de", review_required=True, product_content_id="review-de")]
    result = build(content_rows=rows() + noise)
    assert result.ready
    assert result.contents["en"]["product_content_id"] == "content-en"
    assert result.contents["de"]["product_content_id"] == "content-de"


@pytest.mark.parametrize("overrides", [
    {"mapping_status": lpm.REMOTE_REMOVED},
    {"origin": lpm.ORIGIN_POLYLANG_LINKED},
    {"language_code": "en"},
    {"woocommerce_product_id": None},
])
def test_only_a_recorded_tsyc_created_vi_row_is_a_source(overrides):
    assert build(vi=vi_row(**overrides)).refusal == tbi.VI_ROW_NOT_RECORDED


@pytest.mark.parametrize("overrides", [{"baseline_modified_gmt": None}, {"baseline_text": None}])
def test_a_vi_row_without_a_baseline_is_refused(overrides):
    assert build(vi=vi_row(**overrides)).refusal == tbi.VI_BASELINE_MISSING


def test_missing_vi_content_is_refused_before_translations():
    assert build(content_rows=[content("en"), content("de")]).refusal == tbi.VI_CONTENT_NOT_APPROVED


@pytest.mark.parametrize("missing", ["en", "de"])
def test_a_missing_translation_is_refused(missing):
    kept = [r for r in rows() if r["content_language"] != missing]
    assert build(content_rows=kept).refusal == tbi.TRANSLATION_NOT_APPROVED


def test_two_approved_rows_for_one_language_are_never_chosen_between():
    result = build(content_rows=rows() + [content("en", product_content_id="content-en-2")])
    assert result.refusal == tbi.AMBIGUOUS_APPROVED_CONTENT and not result.contents


def test_an_approved_row_without_an_id_is_refused():
    assert build(content_rows=rows(de={"product_content_id": ""})).refusal == tbi.CONTENT_ID_MISSING


@pytest.mark.parametrize("approved_at", [
    "2026-10-31T08:00:00+00:00",   # before the VI approval
    None,                          # never recorded
    "not a time",
    "2026-11-05T08:00:00",         # naive: never assumed UTC
])
def test_a_translation_not_provably_newer_than_vi_is_refused(approved_at):
    result = build(content_rows=rows(de={"approved_at": approved_at}))
    assert result.refusal == tbi.TRANSLATION_PREDATES_VI_APPROVAL


def test_fractional_seconds_are_compared_as_times_not_text():
    # As text, "...08:00:00.5+00:00" sorts after "...08:00:00+00:00" only by luck
    # of the characters; the parsed times decide. Same second, later fraction.
    same_second = rows(vi={"approved_at": "2026-11-02T08:00:00+00:00"},
                       en={"approved_at": "2026-11-02T08:00:00.5+00:00"},
                       de={"approved_at": "2026-11-02T08:00:00.000001+00:00"})
    assert build(content_rows=same_second).ready
    earlier = rows(vi={"approved_at": "2026-11-02T08:00:00.9+00:00"}, en={"approved_at": "2026-11-02T08:00:00.5+00:00"})
    assert build(content_rows=earlier).refusal == tbi.TRANSLATION_PREDATES_VI_APPROVAL


def test_content_the_text_payload_rejects_is_refused():
    assert build(content_rows=rows(en={"product_name": "  "})).refusal == tbi.CONTENT_NOT_RENDERABLE


def test_ready_contents_render_with_localized_headings_and_no_metadata_author():
    result = build(content_rows=rows(
        en={"author_summary": "The authors of this book are Hoài Anh and Mai Hoa.",
            "product_details": "Book list:\n1. Volume one"},
        de={"author_summary": "Dr. Shefali Tsabary ist klinische Psychologin.",
            "product_details": "Buchliste:\n1. Band eins"}))
    assert result.ready
    en = ma.build_text_payload(result.contents["en"], "en", cwd.render_content)
    de = ma.build_text_payload(result.contents["de"], "de", cwd.render_content)
    assert "<h3>Product details</h3>" in en["description"] and "Hoài Anh" not in en["description"]
    assert "<h3>Über den Autor / die Autorin</h3>" in de["description"]
    assert "<h3>Produktdetails</h3>" in de["description"]
    for payload in (en, de):
        assert "Tác giả" not in payload["description"] and "Thông tin sản phẩm" not in payload["description"]
        assert set(payload) == {"name", "description", "short_description"}


def test_summary_counts_every_refusal_code():
    summary = tbi.summarize([build(), build(vi=vi_row(baseline_text=None))])
    assert summary["books"] == 2 and summary["ready"] == 1
    assert summary["by_refusal"][tbi.VI_BASELINE_MISSING] == 1
    assert set(summary["by_refusal"]) == set(tbi.REFUSAL_CODES)
