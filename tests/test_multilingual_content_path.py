"""Tests for the multilingual generation path (CLAUDE_AUTOMATION.md 9):
APPROVED vi package -> translation provider -> cross-language
consistency validator -> existing translation SAVE/APPROVE persistence.

Pure/offline: FakeSupabaseRepository, FakeTranslationProvider, and a fake
Claude client only. No live Supabase/Woo/network/credentials.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import prepare_product_content as ppc
import run_batch
from pipeline_state import CandidateState
from src.domain import content_package as cp
from src.domain.rules import lane_rules
from src.domain.rules import multilingual_consistency as mc
from src.services import translation_provider as tp

from test_prepare_product_content_translation import (
    DE_GOOD,
    EN_GOOD,
    PRODUCT_CODE,
    make_product,
    make_repository,
    make_vi_content,
    rows,
)

CANDIDATE_CODE = "FB-HIST-2026-002-CAN-0001"
CANDIDATE = {"candidate_code": CANDIDATE_CODE, "candidate_type": "SINGLE_BOOK"}

EN = {k: EN_GOOD[k] for k in ("product_name", "short_description", "long_description")}
DE = {k: DE_GOOD[k] for k in ("product_name", "short_description", "long_description")}


def build_package(contents=None):
    return cp.build_content_package(
        candidate=CANDIDATE,
        product=make_product(),
        contents=contents if contents is not None else [make_vi_content()],
    )


def check(en=None, de=None):
    return mc.evaluate_multilingual_consistency(
        vi=build_package().vietnamese(),
        translations={"en": en or EN, "de": de or DE},
        verified_facts=build_package().verified_facts,
    )


def approved(language, fields):
    return {
        **fields,
        "content_language": language,
        "content_status": "APPROVED",
        "review_required": False,
    }


# --------------------------------------------------------------------------
# Content package
# --------------------------------------------------------------------------


class TestContentPackage:
    def test_approved_vi_builds_package(self):
        package = build_package()
        assert package.candidate_code == CANDIDATE_CODE
        assert package.product_title == "Gấu con đi ngủ"
        assert package.sellable_unit == "SINGLE_BOOK"
        assert package.description_vi == make_vi_content()["long_description"]
        assert package.content_status_vi == "APPROVED"
        assert package.content_status_en is None and package.description_en is None
        assert package.multilingual_status == cp.CONTENT_VI_READY
        assert "internal_product.author" in package.facts_used
        assert package.content_source == "product_contents:content-vi (vi APPROVED)"

    def test_drafted_vi_is_refused(self):
        with pytest.raises(cp.ContentPackageRefused) as error:
            build_package([make_vi_content(content_status="DRAFTED", review_required=True)])
        assert error.value.code == cp.VI_CONTENT_NOT_APPROVED

    def test_missing_vi_is_refused(self):
        with pytest.raises(cp.ContentPackageRefused) as error:
            build_package([approved("en", EN)])
        assert error.value.code == cp.VI_CONTENT_NOT_APPROVED

    def test_json_round_trip(self):
        package = build_package().with_translation(
            "en", product_title=EN["product_name"],
            short_description=EN["short_description"], description=EN["long_description"],
        )
        payload = json.loads(package.to_json())
        assert payload["description_en"] == EN["long_description"]
        assert set(payload) >= {
            "candidate_code", "product_title", "sellable_unit",
            "description_vi", "description_en", "description_de",
            "short_description_vi", "short_description_en", "short_description_de",
            "content_source", "facts_used",
            "content_status_vi", "content_status_en", "content_status_de",
            "multilingual_status",
        }
        assert cp.ContentPackage.from_dict(payload) == package

    def test_from_dict_rejects_unknown_fields(self):
        payload = {**build_package().to_dict(), "regular_price": "100"}
        with pytest.raises(ValueError, match="regular_price"):
            cp.ContentPackage.from_dict(payload)

    def test_multilingual_status(self):
        vi = make_vi_content()
        assert build_package([vi, approved("en", EN)]).multilingual_status == cp.CONTENT_EN_READY
        assert (
            build_package([vi, approved("en", EN), approved("de", DE)]).multilingual_status
            == cp.MULTILINGUAL_CONTENT_READY
        )


# --------------------------------------------------------------------------
# Cross-language consistency validator
# --------------------------------------------------------------------------


class TestConsistencyValidator:
    def test_clean_translation_passes(self):
        result = check()
        assert result.passed, result.failures
        assert result.reason_codes == ()

    @pytest.mark.parametrize(
        "addition",
        [" ISBN 9786041234567.", " Suitable for ages 3 to 6.", " Published in 2019."],
    )
    def test_added_fact_fails(self, addition):
        result = check(en={**EN, "long_description": EN["long_description"] + addition})
        assert not result.passed
        assert mc.TRANSLATION_ADDED_FACT in result.per_language["en"]
        assert result.language_passed("de")

    def test_dropped_number_fails(self):
        result = check(de={**DE, "long_description": DE["long_description"].replace("\n\nDas Buch hat 32 Seiten.", "")})
        assert mc.TRANSLATION_DROPPED_FACT in result.per_language["de"]

    def test_dropped_author_fails(self):
        result = check(en={**EN, "short_description": EN["short_description"].replace(" by Lê Minh", ""),
                           "long_description": EN["long_description"].replace(" by Lê Minh", "")})
        assert mc.TRANSLATION_NAME_NOT_PRESERVED in result.per_language["en"]

    def test_listed_transliteration_is_accepted(self):
        en = {**EN, "short_description": EN["short_description"].replace("Lê Minh", "Le Minh"),
              "long_description": EN["long_description"].replace("Lê Minh", "Le Minh")}
        result = mc.evaluate_multilingual_consistency(
            vi=build_package().vietnamese(),
            translations={"en": en, "de": DE},
            verified_facts=build_package().verified_facts,
            name_transliterations={"Lê Minh": ["Le Minh"]},
        )
        assert result.passed, result.failures

    def test_dropped_title_fails(self):
        result = check(de={**DE, "product_name": "Kleiner Bär geht schlafen"})
        assert mc.TRANSLATION_TITLE_NOT_PRESERVED in result.per_language["de"]

    @pytest.mark.parametrize(
        "language,addition",
        [
            ("en", " Free shipping within Germany."),
            ("en", " Only one left in stock!"),
            ("en", " Pre-order now."),
            ("de", " Versandkostenfrei innerhalb Deutschlands."),
            ("de", " Der Preis ist günstig."),
            ("de", " Lieferung in 2 Tagen."),
        ],
    )
    def test_commerce_language_fails(self, language, addition):
        base = EN if language == "en" else DE
        translated = {**base, "short_description": base["short_description"] + addition}
        result = check(**{language: translated})
        assert mc.TRANSLATION_COMMERCE_LANGUAGE in result.per_language[language]

    def test_wrong_language_fails(self):
        result = check(en={**EN, "short_description": DE["short_description"], "long_description": DE["long_description"]})
        assert mc.TRANSLATION_WRONG_LANGUAGE in result.per_language["en"]

    def test_untranslated_vietnamese_fails(self):
        vi = make_vi_content()
        result = check(de={**DE, "long_description": vi["long_description"]})
        assert set(result.per_language["de"]) & {mc.TRANSLATION_WRONG_LANGUAGE, mc.TRANSLATION_MIXED_LANGUAGE}

    def test_concatenated_languages_fail(self):
        mixed = EN["long_description"] + "\n\n" + DE["long_description"]
        result = check(en={**EN, "long_description": mixed})
        assert mc.TRANSLATION_MIXED_LANGUAGE in result.per_language["en"]

    def test_language_labels_fail(self):
        labelled = "EN: " + EN["short_description"]
        result = check(en={**EN, "short_description": labelled})
        assert mc.TRANSLATION_MIXED_LANGUAGE in result.per_language["en"]

    def test_empty_field_fails(self):
        result = check(de={**DE, "short_description": ""})
        assert mc.TRANSLATION_EMPTY_FIELD in result.per_language["de"]

    def test_length_ratio_fails(self):
        padding = " The little bear looks out of the window at the moon with its mother." * 5
        result = check(en={**EN, "long_description": EN["long_description"] + padding})
        assert mc.TRANSLATION_LENGTH_RATIO in result.per_language["en"]


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------


class TestProviders:
    def test_fake_provider_is_deterministic(self):
        provider = tp.FakeTranslationProvider({"en": EN, "de": DE})
        first = provider.translate(build_package())
        second = provider.translate(build_package())
        assert first == second
        assert first.translations["en"] == EN

    def test_package_file_provider_reads_filled_package(self, tmp_path):
        package = build_package()
        filled = package.with_translation("en", product_title=EN["product_name"],
                                          short_description=EN["short_description"],
                                          description=EN["long_description"])
        filled = filled.with_translation("de", product_title=DE["product_name"],
                                         short_description=DE["short_description"],
                                         description=DE["long_description"])
        (tmp_path / f"{CANDIDATE_CODE}.json").write_text(
            json.dumps({**filled.to_dict(), "generation_method": "HYBRID"}, ensure_ascii=False),
            encoding="utf-8",
        )
        result = tp.PackageFileTranslationProvider(tmp_path).translate(package)
        assert result.translations == {"en": EN, "de": DE}
        assert result.generation_method == "HYBRID"

    def test_package_file_provider_refuses_stale_vietnamese(self, tmp_path):
        stale = build_package([make_vi_content(long_description="Nội dung cũ " * 10)])
        (tmp_path / f"{CANDIDATE_CODE}.json").write_text(stale.to_json(), encoding="utf-8")
        with pytest.raises(tp.TranslationProviderError) as error:
            tp.PackageFileTranslationProvider(tmp_path).translate(build_package())
        assert error.value.code == tp.PACKAGE_STALE

    def test_package_file_provider_missing_file(self, tmp_path):
        with pytest.raises(tp.TranslationProviderError) as error:
            tp.PackageFileTranslationProvider(tmp_path).translate(build_package())
        assert error.value.code == tp.PACKAGE_FILE_MISSING

    def test_package_path_rejects_traversal(self, tmp_path):
        with pytest.raises(tp.TranslationProviderError):
            tp.package_file_path("../evil", tmp_path)

    def test_claude_provider_refuses_without_key(self, monkeypatch):
        monkeypatch.delenv(tp.API_KEY_ENV_VAR, raising=False)
        with pytest.raises(tp.ClaudeProviderConfigurationError):
            tp.ClaudeTranslationProvider()

    def test_claude_provider_with_fake_client(self):
        parsed = SimpleNamespace(en=SimpleNamespace(**EN), de=SimpleNamespace(**DE))
        calls = []

        class FakeMessages:
            def parse(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(stop_reason="end_turn", parsed_output=parsed)

        provider = tp.ClaudeTranslationProvider(client=SimpleNamespace(messages=FakeMessages()))
        result = provider.translate(build_package())
        assert result.translations == {"en": EN, "de": DE}
        assert result.generation_method == "AI_ASSISTED"
        assert calls[0]["output_format"] is tp._ClaudeTranslationSchema
        assert "Gấu con đi ngủ" in calls[0]["messages"][0]["content"]

    def test_claude_refusal_is_provider_error(self):
        class FakeMessages:
            def parse(self, **kwargs):
                return SimpleNamespace(stop_reason="refusal", parsed_output=None)

        provider = tp.ClaudeTranslationProvider(client=SimpleNamespace(messages=FakeMessages()))
        with pytest.raises(tp.TranslationProviderError) as error:
            provider.translate(build_package())
        assert error.value.code == tp.PROVIDER_REFUSED


# --------------------------------------------------------------------------
# prepare_product_content.py --action TRANSLATE / EXPORT_PACKAGE
# --------------------------------------------------------------------------


def translate(repository, provider, non_interactive=True):
    return ppc.run_translate_action(
        repository=repository,
        product_code=PRODUCT_CODE,
        provider=provider,
        non_interactive=non_interactive,
    )


class TestTranslateAction:
    def test_end_to_end_approves_en_and_de_via_existing_path(self):
        vi = make_vi_content()
        repository = make_repository(vi=dict(vi))
        report = translate(repository, tp.FakeTranslationProvider({"en": EN, "de": DE}))

        assert report["consistency"] == mc.PASS
        assert report["statuses"] == {"en": "APPROVED", "de": "APPROVED"}
        for language, fields in (("en", EN), ("de", DE)):
            [row] = rows(repository, language)
            assert row["content_status"] == "APPROVED"
            assert row["review_required"] is False
            assert row["generator_name"] == ppc.TRANSLATION_GENERATOR_NAME
            assert row["long_description"] == fields["long_description"]
            assert not {"regular_price", "sale_price", "price", "status"} & set(row)
        assert rows(repository, "vi") == [vi]
        assert repository.client.tables["internal_products"][0]["content_status"] == "APPROVED"
        assert lane_rules.has_multilingual_content(repository.client.tables["product_contents"])

    def test_consistency_failure_is_review_required_for_that_language_only(self):
        repository = make_repository(vi=make_vi_content())
        bad_en = {**EN, "short_description": EN["short_description"] + " Free shipping."}
        report = translate(repository, tp.FakeTranslationProvider({"en": bad_en, "de": DE}))

        assert report["consistency"] == mc.FAIL
        assert mc.TRANSLATION_COMMERCE_LANGUAGE in report["reason_codes"]
        [en_row] = rows(repository, "en")
        assert en_row["content_status"] == "REVIEW_REQUIRED"
        assert en_row["review_required"] is True
        assert "CONTENT_REVIEW_REQUIRED (en)" in en_row["review_notes"]
        assert mc.TRANSLATION_COMMERCE_LANGUAGE in en_row["review_notes"]
        assert rows(repository, "de")[0]["content_status"] == "APPROVED"

    def test_refused_without_approved_vi_and_provider_not_called(self):
        repository = make_repository(vi=make_vi_content(content_status="DRAFTED", review_required=True))
        provider = tp.FakeTranslationProvider({"en": EN, "de": DE})
        with pytest.raises(cp.ContentPackageRefused, match=cp.VI_CONTENT_NOT_APPROVED):
            translate(repository, provider)
        assert provider.calls == []
        assert rows(repository, "en") == [] and rows(repository, "de") == []

    def test_requires_non_interactive(self):
        repository = make_repository(vi=make_vi_content())
        with pytest.raises(RuntimeError, match="--non-interactive"):
            translate(repository, tp.FakeTranslationProvider({"en": EN, "de": DE}), non_interactive=False)
        assert rows(repository, "en") == []

    def test_approved_translation_is_never_overwritten(self):
        approved_en = {
            **approved("en", EN),
            "product_content_id": "content-en",
            "internal_product_id": "internal-product-1",
        }
        repository = make_repository(vi=make_vi_content(), extra_contents=[dict(approved_en)])
        changed_en = {**EN, "short_description": EN["short_description"] + " Lovely."}
        report = translate(repository, tp.FakeTranslationProvider({"en": changed_en, "de": DE}))
        assert rows(repository, "en") == [approved_en]
        assert set(report["statuses"]) == {"de"}

    def test_rejected_translation_refuses_before_provider_call(self):
        rejected_de = {**DE, "product_content_id": "content-de", "internal_product_id": "internal-product-1",
                       "content_language": "de", "content_status": "REJECTED"}
        repository = make_repository(vi=make_vi_content(), extra_contents=[rejected_de])
        provider = tp.FakeTranslationProvider({"en": EN, "de": DE})
        with pytest.raises(RuntimeError, match="REJECTED"):
            translate(repository, provider)
        assert provider.calls == []

    def test_export_package_writes_file_and_never_overwrites(self, tmp_path):
        repository = make_repository(vi=make_vi_content())
        before = json.dumps(repository.client.tables, sort_keys=True, default=str)
        path = ppc.run_export_package_action(repository, PRODUCT_CODE, tmp_path)
        assert path.name == f"{CANDIDATE_CODE}.json"
        assert json.loads(path.read_text(encoding="utf-8"))["product_title"] == "Gấu con đi ngủ"
        assert json.dumps(repository.client.tables, sort_keys=True, default=str) == before
        with pytest.raises(RuntimeError, match="overwrite"):
            ppc.run_export_package_action(repository, PRODUCT_CODE, tmp_path)


# --------------------------------------------------------------------------
# Lane classifier / Woo dispatch guard / package agreement
# --------------------------------------------------------------------------


class TestLaneGuardAgreement:
    @pytest.mark.parametrize(
        "translations,ready",
        [
            ([], False),
            ([("en", EN)], False),
            ([("de", DE)], False),
            ([("en", EN), ("de", DE)], True),
        ],
    )
    def test_lane_guard_and_package_agree(self, translations, ready):
        contents = [make_vi_content()] + [approved(language, fields) for language, fields in translations]
        state = CandidateState(
            candidate_code=CANDIDATE_CODE,
            candidate_id="candidate-1",
            product_code=PRODUCT_CODE,
            derived_state="READY_FOR_DRAFT_HISTORICAL",
        )
        bundle = {"contents": contents}

        assert lane_rules.has_multilingual_content(contents) is ready
        assert run_batch.multilingual_content_missing(state, bundle) is (not ready)
        assert (build_package(contents).multilingual_status == cp.MULTILINGUAL_CONTENT_READY) is ready
        expected_lane = lane_rules.READY_FOR_DRAFT if ready else lane_rules.MULTILINGUAL_CONTENT
        assert (
            lane_rules.classify_lane(
                state,
                multilingual_ready=lane_rules.has_multilingual_content(contents),
                vi_content_approved=lane_rules.has_approved_vi_content(contents),
            )
            == expected_lane
        )


# --------------------------------------------------------------------------
# No secrets in packages, reports, rows or logs
# --------------------------------------------------------------------------


class TestNoSecrets:
    def test_no_env_values_leak(self, monkeypatch, capsys):
        secret = "sk-ant-test-SECRET-value-0123456789"
        monkeypatch.setenv(tp.API_KEY_ENV_VAR, secret)
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", secret + "-supabase")

        parsed = SimpleNamespace(en=SimpleNamespace(**EN), de=SimpleNamespace(**DE))

        class FakeMessages:
            def parse(self, **kwargs):
                return SimpleNamespace(stop_reason="end_turn", parsed_output=parsed)

        provider = tp.ClaudeTranslationProvider(client=SimpleNamespace(messages=FakeMessages()))
        assert secret not in json.dumps(vars(provider), default=str)

        repository = make_repository(vi=make_vi_content())
        report = translate(repository, provider)
        captured = capsys.readouterr()

        haystacks = [
            json.dumps(report, ensure_ascii=False, default=str),
            build_package(repository.client.tables["product_contents"]).to_json(),
            json.dumps(repository.client.tables, ensure_ascii=False, default=str),
            captured.out,
            captured.err,
        ]
        for haystack in haystacks:
            assert "SECRET" not in haystack
