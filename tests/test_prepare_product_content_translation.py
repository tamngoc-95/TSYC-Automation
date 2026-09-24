"""Tests for EN/DE translation content (CLAUDE_AUTOMATION.md section 9).

Covers src/domain/rules/translation_rules.py (deterministic validation)
and scripts/prepare_product_content.py --content-language en|de:
refusal without APPROVED vi content, no overwrite of APPROVED rows, no
duplicate rows, rejection of invented facts, internal_products never
touched by a translation write, and unchanged vi behavior.

Pure/offline: FakeSupabaseRepository only; no live Supabase/Woo/network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import prepare_product_content as ppc
from src.domain.rules import translation_rules

from support.fake_supabase import FakeSupabaseRepository


PRODUCT_ID = "internal-product-1"
CANDIDATE_ID = "candidate-1"
PRODUCT_CODE = "TSYC-FB-HIST-2026-002-CAN-0001"

VI_LONG = (
    "“Gấu con đi ngủ” của Lê Minh hiện có tại Tiệm Sách Yêu Con.\n\n"
    "Câu chuyện kể về chú gấu nhỏ chuẩn bị đi ngủ cùng mẹ, với tranh minh "
    "họa màu sắc dịu nhẹ.\n\nSách dày 32 trang."
)

EN_GOOD = {
    "product_name": "Little Bear Goes to Sleep (Gấu con đi ngủ)",
    "short_description": (
        "“Gấu con đi ngủ” by Lê Minh is available at Tiệm Sách Yêu Con. "
        "A gentle story about a little bear's bedtime."
    ),
    "long_description": (
        "“Gấu con đi ngủ” by Lê Minh is now available at Tiệm Sách Yêu Con.\n\n"
        "The story follows a little bear getting ready for bed with its "
        "mother, with softly coloured illustrations.\n\nThe book has 32 pages."
    ),
    "product_details": "Author: Lê Minh\nPages: 32",
    "seo_title": "Gấu con đi ngủ – Lê Minh",
    "seo_description": "Gấu con đi ngủ by Lê Minh at Tiệm Sách Yêu Con.",
}

DE_GOOD = {
    "product_name": "Kleiner Bär geht schlafen (Gấu con đi ngủ)",
    "short_description": (
        "„Gấu con đi ngủ“ von Lê Minh ist bei Tiệm Sách Yêu Con erhältlich. "
        "Eine sanfte Geschichte über die Schlafenszeit eines kleinen Bären."
    ),
    "long_description": (
        "„Gấu con đi ngủ“ von Lê Minh ist jetzt bei Tiệm Sách Yêu Con "
        "erhältlich.\n\nDie Geschichte erzählt, wie sich ein kleiner Bär mit "
        "seiner Mutter auf das Schlafengehen vorbereitet, mit sanft "
        "kolorierten Illustrationen.\n\nDas Buch hat 32 Seiten."
    ),
    "product_details": "Autor: Lê Minh\nSeiten: 32",
    "seo_title": "Gấu con đi ngủ – Lê Minh",
    "seo_description": "Gấu con đi ngủ von Lê Minh bei Tiệm Sách Yêu Con.",
}


def make_product(**overrides: Any) -> dict[str, Any]:
    product = {
        "internal_product_id": PRODUCT_ID,
        "candidate_id": CANDIDATE_ID,
        "product_code": PRODUCT_CODE,
        "title": "Gấu con đi ngủ",
        "author": "Lê Minh",
        "isbn": None,
        "publisher": None,
        "page_count": 32,
        "weight_grams": None,
        "length_cm": None,
        "width_cm": None,
        "height_cm": None,
        "content_status": "APPROVED",
        "is_active": True,
        "created_at": "2026-08-01T00:00:00+00:00",
    }
    product.update(overrides)
    return product


def make_vi_content(**overrides: Any) -> dict[str, Any]:
    content = {
        "product_content_id": "content-vi",
        "internal_product_id": PRODUCT_ID,
        "content_language": "vi",
        "product_name": "Gấu con đi ngủ",
        "short_description": (
            "“Gấu con đi ngủ” của Lê Minh là ấn phẩm đang có tại Tiệm Sách "
            "Yêu Con. Câu chuyện nhẹ nhàng về giờ đi ngủ của chú gấu nhỏ."
        ),
        "long_description": VI_LONG,
        "author_summary": None,
        "product_details": "Tác giả: Lê Minh\nSố trang: 32",
        "seo_title": "Gấu con đi ngủ – Lê Minh",
        "seo_description": "Gấu con đi ngủ của Lê Minh tại Tiệm Sách Yêu Con.",
        "content_status": "APPROVED",
        "review_required": False,
        "generation_method": "RULE_BASED",
    }
    content.update(overrides)
    return content


def make_repository(
    *,
    vi: dict[str, Any] | None = None,
    extra_contents: list[dict[str, Any]] | None = None,
    candidate_type: str = "SINGLE_BOOK",
) -> FakeSupabaseRepository:
    contents = ([vi] if vi is not None else []) + list(extra_contents or [])
    return FakeSupabaseRepository(
        tables={
            "internal_products": [make_product()],
            "product_contents": contents,
            "product_candidates": [
                {
                    "candidate_id": CANDIDATE_ID,
                    "candidate_code": "FB-HIST-2026-002-CAN-0001",
                    "candidate_type": candidate_type,
                }
            ],
        }
    )


def write_file(tmp_path: Path, payload: dict[str, Any], name: str = "t.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def rows(repository: FakeSupabaseRepository, language: str) -> list[dict[str, Any]]:
    return [
        row
        for row in repository.client.tables["product_contents"]
        if row["content_language"] == language
    ]


def run(repository, action, language, tmp_path=None, payload=None, non_interactive=True):
    content_file = write_file(tmp_path, payload) if payload is not None else None
    return ppc.run_translation_action(
        repository=repository,
        action=action,
        language=language,
        product_code=PRODUCT_CODE,
        content_file=content_file,
        non_interactive=non_interactive,
    )


def evaluate(translation, language="en", candidate_type="SINGLE_BOOK", **vi_overrides):
    return translation_rules.evaluate_translation(
        language=language,
        translation=translation,
        vi_content=make_vi_content(**vi_overrides),
        product=make_product(),
        candidate_type=candidate_type,
    )


# --------------------------------------------------------------------------
# translation_rules.evaluate_translation
# --------------------------------------------------------------------------


class TestEvaluateTranslation:
    @pytest.mark.parametrize("language,translation", [("en", EN_GOOD), ("de", DE_GOOD)])
    def test_faithful_translation_passes(self, language, translation):
        result = evaluate(translation, language=language)
        assert result.is_auto_pass, result.reason

    @pytest.mark.parametrize(
        "addition,expected",
        [
            (" ISBN 9786041234567.", "isbn"),
            (" Suitable for ages 3 to 6.", "age_recommendation"),
            (" An award-winning bestseller.", "award"),
            (" Free shipping on every order.", "shipping"),
            (" Only 2 left in stock.", "stock"),
            (" Published by Kim Dong Publishing.", "publisher"),
            (" It helps children develop language skills.", "educational_benefit"),
            (" Special anniversary edition.", "edition"),
            (" Size 20 x 20 cm, weight 250 grams.", "dimensions"),
        ],
    )
    def test_invented_fact_is_rejected(self, addition, expected):
        translation = {**EN_GOOD, "long_description": EN_GOOD["long_description"] + addition}
        result = evaluate(translation)
        assert not result.is_auto_pass
        assert expected in result.reason

    def test_invented_number_is_rejected(self):
        translation = {**EN_GOOD, "long_description": EN_GOOD["long_description"].replace("32", "48")}
        result = evaluate(translation)
        assert not result.is_auto_pass
        assert "48" in result.reason

    def test_german_invented_age_is_rejected(self):
        translation = {**DE_GOOD, "short_description": DE_GOOD["short_description"] + " Ab 3 Jahren."}
        result = evaluate(translation, language="de")
        assert not result.is_auto_pass
        assert "age_recommendation" in result.reason

    def test_internal_workflow_language_is_rejected(self):
        translation = {**EN_GOOD, "short_description": EN_GOOD["short_description"] + " Pending manager review."}
        result = evaluate(translation)
        assert not result.is_auto_pass
        assert "workflow" in result.reason.lower()

    def test_german_workflow_language_is_rejected(self):
        translation = {**DE_GOOD, "long_description": DE_GOOD["long_description"] + " Wird später ergänzt."}
        result = evaluate(translation, language="de")
        assert not result.is_auto_pass

    def test_untranslated_vietnamese_is_rejected(self):
        translation = {**EN_GOOD, "long_description": VI_LONG}
        result = evaluate(translation)
        assert not result.is_auto_pass
        assert "not localized" in result.reason

    def test_renamed_product_is_rejected(self):
        translation = {**EN_GOOD, "product_name": "Little Bear Goes to Sleep"}
        result = evaluate(translation)
        assert not result.is_auto_pass
        assert "product_name" in result.reason

    def test_individual_product_described_as_set_is_rejected(self):
        translation = {**EN_GOOD, "short_description": EN_GOOD["short_description"] + " A complete set for bedtime."}
        result = evaluate(translation)
        assert not result.is_auto_pass
        assert "set/combo" in result.reason

    def test_combo_source_requires_combo_translation(self):
        result = evaluate(EN_GOOD, candidate_type="BOOK_COMBO")
        assert not result.is_auto_pass
        assert "combo/set" in result.reason

    def test_padded_plot_is_rejected_by_length_ratio(self):
        padding = " The little bear looks out of the window at the moon with its mother." * 4
        translation = {**EN_GOOD, "long_description": EN_GOOD["long_description"] + padding}
        result = evaluate(translation)
        assert not result.is_auto_pass
        assert "length ratio" in result.reason

    def test_placeholder_copy_is_rejected(self):
        translation = {**EN_GOOD, "short_description": "Book."}
        result = evaluate(translation)
        assert not result.is_auto_pass

    def test_missing_required_field_is_rejected(self):
        translation = {**EN_GOOD, "long_description": ""}
        result = evaluate(translation)
        assert not result.is_auto_pass

    def test_unapproved_vi_source_is_rejected(self):
        result = evaluate(EN_GOOD, content_status="DRAFTED", review_required=True)
        assert not result.is_auto_pass

    def test_unsupported_language_is_rejected(self):
        result = evaluate(EN_GOOD, language="fr")
        assert not result.is_auto_pass


# --------------------------------------------------------------------------
# prepare_product_content.py --content-language en/de
# --------------------------------------------------------------------------


class TestTranslationRefusals:
    def test_save_refused_without_vi_content(self, tmp_path):
        repository = make_repository(vi=None)
        with pytest.raises(RuntimeError, match="no APPROVED Vietnamese content"):
            run(repository, "SAVE", "en", tmp_path, EN_GOOD)
        assert rows(repository, "en") == []

    def test_save_refused_when_vi_not_approved(self, tmp_path):
        repository = make_repository(
            vi=make_vi_content(content_status="DRAFTED", review_required=True)
        )
        with pytest.raises(RuntimeError, match="no APPROVED Vietnamese content"):
            run(repository, "SAVE", "de", tmp_path, DE_GOOD)
        assert rows(repository, "de") == []

    def test_save_requires_non_interactive(self, tmp_path):
        repository = make_repository(vi=make_vi_content())
        with pytest.raises(RuntimeError, match="--non-interactive"):
            run(repository, "SAVE", "en", tmp_path, EN_GOOD, non_interactive=False)

    def test_save_requires_content_file(self):
        repository = make_repository(vi=make_vi_content())
        with pytest.raises(RuntimeError, match="--content-file"):
            run(repository, "SAVE", "en")

    @pytest.mark.parametrize("action", ["REVISE", "AUTO_REVISE"])
    def test_vi_only_actions_refused_for_translations(self, action, tmp_path):
        repository = make_repository(vi=make_vi_content())
        with pytest.raises(RuntimeError, match="not supported"):
            run(repository, action, "en", tmp_path, EN_GOOD)

    def test_unknown_field_refused(self, tmp_path):
        repository = make_repository(vi=make_vi_content())
        with pytest.raises(RuntimeError, match="unsupported field"):
            run(repository, "SAVE", "en", tmp_path, {**EN_GOOD, "regular_price": "100"})
        assert rows(repository, "en") == []

    def test_save_refuses_to_overwrite_approved_translation(self, tmp_path):
        approved_en = {
            **EN_GOOD,
            "product_content_id": "content-en",
            "internal_product_id": PRODUCT_ID,
            "content_language": "en",
            "content_status": "APPROVED",
            "review_required": False,
        }
        repository = make_repository(vi=make_vi_content(), extra_contents=[dict(approved_en)])
        with pytest.raises(RuntimeError, match="APPROVED"):
            run(repository, "SAVE", "en", tmp_path, {**EN_GOOD, "short_description": "Changed text " * 5})
        assert rows(repository, "en") == [approved_en]

    def test_approve_refuses_approved_translation(self):
        approved_de = {
            **DE_GOOD,
            "product_content_id": "content-de",
            "internal_product_id": PRODUCT_ID,
            "content_language": "de",
            "content_status": "APPROVED",
            "review_required": False,
        }
        repository = make_repository(vi=make_vi_content(), extra_contents=[dict(approved_de)])
        with pytest.raises(RuntimeError, match="APPROVED"):
            run(repository, "APPROVE", "de")
        assert rows(repository, "de") == [approved_de]


class TestTranslationWrites:
    def test_preview_writes_nothing(self, tmp_path):
        repository = make_repository(vi=make_vi_content())
        before = json.dumps(repository.client.tables, sort_keys=True, default=str)
        assert run(repository, "PREVIEW", "en", tmp_path, EN_GOOD) is None
        assert json.dumps(repository.client.tables, sort_keys=True, default=str) == before

    def test_save_creates_drafted_row_and_never_touches_internal_product(self, tmp_path):
        vi = make_vi_content()
        repository = make_repository(vi=dict(vi))
        run(repository, "SAVE", "en", tmp_path, EN_GOOD)

        en_rows = rows(repository, "en")
        assert len(en_rows) == 1
        assert en_rows[0]["content_status"] == "DRAFTED"
        assert en_rows[0]["review_required"] is True
        assert en_rows[0]["generation_method"] == "AI_ASSISTED"
        assert "provenance=" in en_rows[0]["review_notes"]
        assert "content-vi" in en_rows[0]["review_notes"]
        assert not {"regular_price", "sale_price", "price", "status"} & set(en_rows[0])
        assert rows(repository, "vi") == [vi]
        assert repository.client.tables["internal_products"][0]["content_status"] == "APPROVED"

    def test_repeat_save_updates_in_place_without_duplicate(self, tmp_path):
        repository = make_repository(vi=make_vi_content())
        run(repository, "SAVE", "de", tmp_path, DE_GOOD)
        first_id = rows(repository, "de")[0]["product_content_id"]
        revised = {**DE_GOOD, "short_description": DE_GOOD["short_description"] + " Schön illustriert."}
        run(repository, "SAVE", "de", tmp_path, revised)

        de_rows = rows(repository, "de")
        assert len(de_rows) == 1
        assert de_rows[0]["product_content_id"] == first_id
        assert de_rows[0]["short_description"] == revised["short_description"]

    def test_approve_passes_valid_translation(self, tmp_path):
        repository = make_repository(vi=make_vi_content())
        run(repository, "SAVE", "en", tmp_path, EN_GOOD)
        run(repository, "APPROVE", "en")

        en_row = rows(repository, "en")[0]
        assert en_row["content_status"] == "APPROVED"
        assert en_row["review_required"] is False
        assert en_row["approved_at"]
        assert repository.client.tables["internal_products"][0]["content_status"] == "APPROVED"

    def test_approve_failure_is_review_required_for_that_language_only(self, tmp_path):
        repository = make_repository(vi=make_vi_content())
        run(repository, "SAVE", "de", tmp_path, DE_GOOD)
        invented = {**EN_GOOD, "long_description": EN_GOOD["long_description"] + " Winner of a national award."}
        run(repository, "SAVE", "en", tmp_path, invented)
        run(repository, "APPROVE", "en")
        run(repository, "APPROVE", "de")

        en_row = rows(repository, "en")[0]
        assert en_row["content_status"] == "REVIEW_REQUIRED"
        assert en_row["review_required"] is True
        assert "CONTENT_REVIEW_REQUIRED (en)" in en_row["review_notes"]
        assert rows(repository, "de")[0]["content_status"] == "APPROVED"
        assert rows(repository, "vi")[0]["content_status"] == "APPROVED"

    def test_approve_requires_non_interactive_without_write(self, tmp_path):
        repository = make_repository(vi=make_vi_content())
        run(repository, "SAVE", "en", tmp_path, EN_GOOD)
        with pytest.raises(RuntimeError, match="--non-interactive"):
            run(repository, "APPROVE", "en", non_interactive=False)
        assert rows(repository, "en")[0]["content_status"] == "DRAFTED"

    def test_approve_rechecks_vi_still_approved(self, tmp_path):
        repository = make_repository(vi=make_vi_content())
        run(repository, "SAVE", "en", tmp_path, EN_GOOD)
        rows(repository, "vi")[0]["content_status"] = "REVIEW_REQUIRED"
        with pytest.raises(RuntimeError, match="no APPROVED Vietnamese content"):
            run(repository, "APPROVE", "en")
        assert rows(repository, "en")[0]["content_status"] == "DRAFTED"


class TestVietnameseBehaviorUnchanged:
    def test_get_existing_content_defaults_to_vi(self):
        en_row = {
            **EN_GOOD,
            "product_content_id": "content-en",
            "internal_product_id": PRODUCT_ID,
            "content_language": "en",
            "content_status": "DRAFTED",
        }
        repository = make_repository(vi=None, extra_contents=[en_row])
        assert ppc.get_existing_content(repository, PRODUCT_ID) is None

    def test_translation_rows_do_not_make_vi_selectable(self):
        # vi APPROVED + en DRAFTED: the vi generator must still skip this
        # product (it only ever looks at the vi row).
        en_row = {
            **EN_GOOD,
            "product_content_id": "content-en",
            "internal_product_id": PRODUCT_ID,
            "content_language": "en",
            "content_status": "DRAFTED",
        }
        repository = make_repository(vi=make_vi_content(), extra_contents=[en_row])
        assert ppc.select_product_for_review(repository, PRODUCT_CODE) is None

    def test_vi_save_content_still_writes_vi_and_generator_version(self):
        repository = make_repository(vi=None)
        product = make_product(content_status="PENDING")
        row = ppc.save_content(
            repository=repository,
            product=product,
            existing=None,
            content=ppc.build_safe_draft(product),
            approve=False,
        )
        assert row["content_language"] == "vi"
        assert row["generator_version"] == ppc.GENERATOR_VERSION == "1.5.0"
