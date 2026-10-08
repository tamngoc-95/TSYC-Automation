"""Regression tests for the 2026-10-02 Fast Track infrastructure upgrade:
lane semantics of the new derived states, run_batch dispatch for reference
discovery and (opt-in) EN/DE translation, and the metadata-only content
component (built, validated, disabled pending an owner decision)."""
from __future__ import annotations

import subprocess
import types
from typing import Any

import pytest

import run_batch
from pipeline_state import CandidateState, derive_candidate_state, load_candidate_bundle
from src.domain.rules import lane_rules, metadata_only_content

from support.fake_supabase import FakeSupabaseRepository

HIST_CODE = "FB-HIST-2026-IMG-005-CAN-0002"
CANDIDATE_ID = "fa57fa57-0000-0000-0000-000000000001"
PRODUCT_ID = "fa57fa57-0000-0000-0000-0000000000ff"


def _state(derived_state: str, **overrides: Any) -> CandidateState:
    values = {"candidate_code": HIST_CODE, "candidate_id": CANDIDATE_ID, "product_code": None,
              "derived_state": derived_state}
    values.update(overrides)
    return CandidateState(**values)


# --- lane semantics -------------------------------------------------------------


def test_discovery_pending_is_fast_track_and_dispatches_the_discoverer():
    state = _state("REFERENCE_DISCOVERY_PENDING_HISTORICAL")

    assert lane_rules.classify_lane(state) == lane_rules.FAST_TRACK
    kind, dispatch, _ = run_batch.decide_action(state, False)
    assert kind == "invoke"
    assert dispatch.script == "discover_reference_sources.py"
    assert dispatch.build_args(state) == [
        "--candidate-code", HIST_CODE, "--non-interactive", "--confirm-discover",
    ]


@pytest.mark.parametrize(
    "derived_state", ["CONTENT_SOURCE_UNAVAILABLE_HISTORICAL", "IMAGE_PRICE_LABEL_REPLACEMENT_NEEDED"]
)
def test_missing_enrichment_is_enrichment_needed_not_human_review(derived_state):
    state = _state(derived_state, blocked=True, blocked_reason="x")

    assert lane_rules.classify_lane(state) == lane_rules.ENRICHMENT_NEEDED
    assert run_batch.decide_action(state, False)[0] == "blocked"


def test_completed_not_found_attempt_stops_rediscovery():
    candidate = {
        "candidate_id": CANDIDATE_ID, "candidate_code": HIST_CODE, "raw_page_id": None,
        "candidate_type": "SINGLE_BOOK", "identity_status": "IDENTITY_PENDING",
        "extracted_title": "Nếu Nhịn Tiểu Thì Sao?", "extracted_author": "Lời: Hoàng Hoành; Tranh: Kẹo Bông",
        "conflict_fields": [], "source_evidence": {},
    }
    attempt = {
        "candidate_id": CANDIDATE_ID, "process_name": "reference_discovery", "status": "NOT_FOUND",
        "created_at": "2026-10-02T12:00:00+00:00",
        "error_details": {"rules_version": "1.0.0"},
    }
    repository = FakeSupabaseRepository(tables={"product_candidates": [candidate], "process_logs": [attempt]})

    state = derive_candidate_state(load_candidate_bundle(repository, HIST_CODE))

    assert state.derived_state == "CONTENT_SOURCE_UNAVAILABLE_HISTORICAL"
    assert "found no approved-source page" in state.blocked_reason
    assert "disabled pending the shop owner's decision" not in state.blocked_reason  # extracted author is not verified
    assert state.human_gate is False


# --- opt-in translation dispatch ---------------------------------------------------


def _ready_repository(*contents: dict[str, Any]) -> FakeSupabaseRepository:
    candidate = {"candidate_id": CANDIDATE_ID, "candidate_code": HIST_CODE, "batch_id": "b",
                 "identity_status": "IDENTITY_PENDING"}
    product = {"internal_product_id": PRODUCT_ID, "candidate_id": CANDIDATE_ID,
               "product_code": f"TSYC-{HIST_CODE}", "isbn": None, "weight_grams": None,
               "content_status": "APPROVED", "image_status": "APPROVED",
               "woocommerce_status": "READY_FOR_DRAFT"}
    vi = {"product_content_id": "vi", "internal_product_id": PRODUCT_ID, "content_language": "vi",
          "content_status": "APPROVED", "review_required": False}
    return FakeSupabaseRepository(tables={
        "product_candidates": [candidate], "internal_products": [product],
        "product_contents": [vi, *contents],
    })


def _args(translation_provider: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(dry_run=False, non_interactive=True, allow_woo_draft=False,
                                 batch_code=None, verbose=False,
                                 translation_provider=translation_provider)


def _translation(language: str, status: str) -> dict[str, Any]:
    return {"product_content_id": language, "internal_product_id": PRODUCT_ID,
            "content_language": language, "content_status": status, "review_required": status != "APPROVED"}


def test_without_translation_provider_ready_candidate_stops_as_before():
    calls: list[list[str]] = []
    report = run_batch.process_one_candidate(
        HIST_CODE, _args("none"), _ready_repository(),
        lambda argv: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""),
        lambda _p: True, None,
    )
    assert calls == []
    assert report.result == run_batch.MULTILINGUAL_CONTENT_REQUIRED


def test_claude_translation_is_dispatched_then_woo_draft_follows():
    repository = _ready_repository()
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess:
        calls.append(argv)
        if "TRANSLATE" in argv:
            repository.client.tables["product_contents"].extend(
                [_translation("en", "APPROVED"), _translation("de", "APPROVED")]
            )
        if argv[1].endswith("create_woocommerce_draft.py"):
            repository.client.tables["internal_products"][0]["woocommerce_status"] = "DRAFT_CREATED"
        return subprocess.CompletedProcess(argv, 0, "Result: PASS", "")

    report = run_batch.process_one_candidate(HIST_CODE, _args("claude"), repository, runner, lambda _p: True, None)

    scripts = [argv[1].rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for argv in calls if "audit_pipeline_state.py" not in argv[1]]
    assert scripts[:2] == ["prepare_product_content.py", "create_woocommerce_draft.py"]
    translate_argv = calls[0]
    assert translate_argv[translate_argv.index("--action") + 1] == "TRANSLATE"
    assert translate_argv[translate_argv.index("--translation-provider") + 1] == "claude"
    assert "publish" not in " ".join(translate_argv)
    assert report.actions_executed[0] == "prepare_product_content.py"


def test_declined_translation_is_never_retried_automatically():
    calls: list[list[str]] = []
    report = run_batch.process_one_candidate(
        HIST_CODE, _args("claude"), _ready_repository(_translation("en", "REVIEW_REQUIRED")),
        lambda argv: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""),
        lambda _p: True, None,
    )
    assert calls == []
    assert report.result == run_batch.MULTILINGUAL_CONTENT_REQUIRED
    assert "declined by validation" in (report.blocked_reason or "")


def test_translation_that_does_not_complete_stalls_without_retry():
    calls: list[list[str]] = []
    report = run_batch.process_one_candidate(
        HIST_CODE, _args("claude"), _ready_repository(),
        lambda argv: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""),
        lambda _p: True, None,
    )
    assert len(calls) == 1
    assert report.result == "STALLED"


def _filled_package(tmp_path, *, de: str = "Eine Beschreibung.") -> None:
    (tmp_path / f"{HIST_CODE}.json").write_text(
        __import__("json").dumps({
            "candidate_code": HIST_CODE,
            "description_en": "A description.", "short_description_en": "A summary.",
            "description_de": de, "short_description_de": "Eine Zusammenfassung." if de else "",
        }),
        encoding="utf-8",
    )


def test_package_file_fallback_dispatches_translate_when_package_is_filled(monkeypatch):
    monkeypatch.setattr(run_batch, "filled_package_available", lambda code: True)
    calls: list[list[str]] = []
    run_batch.process_one_candidate(
        HIST_CODE, _args("package-file"), _ready_repository(),
        lambda argv: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""),
        lambda _p: True, None,
    )
    translate_argv = calls[0]
    assert translate_argv[translate_argv.index("--action") + 1] == "TRANSLATE"
    assert translate_argv[translate_argv.index("--translation-provider") + 1] == "package-file"


def test_package_file_fallback_without_a_filled_package_stops_without_any_call(tmp_path, monkeypatch):
    monkeypatch.setattr(run_batch, "filled_package_available", lambda code: False)
    calls: list[list[str]] = []
    report = run_batch.process_one_candidate(
        HIST_CODE, _args("package-file"), _ready_repository(),
        lambda argv: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""),
        lambda _p: True, None,
    )
    assert calls == []
    assert report.result == run_batch.MULTILINGUAL_CONTENT_REQUIRED


def test_filled_package_requires_both_languages(tmp_path):
    _filled_package(tmp_path)
    assert run_batch.filled_package_available(HIST_CODE, tmp_path) is True
    _filled_package(tmp_path, de="")
    assert run_batch.filled_package_available(HIST_CODE, tmp_path) is False
    assert run_batch.filled_package_available("FB-HIST-MISSING", tmp_path) is False


def test_package_file_fallback_never_retries_a_declined_translation(monkeypatch):
    monkeypatch.setattr(run_batch, "filled_package_available", lambda code: True)
    calls: list[list[str]] = []
    run_batch.process_one_candidate(
        HIST_CODE, _args("package-file"), _ready_repository(_translation("de", "REVIEW_REQUIRED")),
        lambda argv: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""),
        lambda _p: True, None,
    )
    assert calls == []


def test_run_batch_cli_accepts_translation_provider_flag():
    args = run_batch.parse_arguments(["--candidate-code", HIST_CODE, "--max-candidates", "1",
                                      "--translation-provider", "claude"])
    assert args.translation_provider == "claude"
    assert run_batch.parse_arguments(["--candidate-code", HIST_CODE, "--max-candidates", "1"]).translation_provider == "none"


# --- metadata-only content (disabled) ---------------------------------------------


VERIFIED = {"verified_title": "Cây Cam Ngọt Của Tôi", "verified_author": "José Mauro de Vasconcelos",
            "verified_publisher": "NXB Hội Nhà Văn"}


def test_metadata_only_auto_approval_is_disabled_by_default():
    assert metadata_only_content.METADATA_ONLY_AUTO_APPROVAL_ENABLED is False
    decision = metadata_only_content.metadata_only_path_available(VERIFIED)
    assert decision.rule_code == metadata_only_content.METADATA_ONLY_DISABLED


def test_extracted_only_metadata_is_never_sufficient():
    candidate = {"extracted_title": "Tia Nắng Bé Con", "extracted_author": "ThS. Ngô Nam"}
    assert metadata_only_content.evaluate_metadata_only_sufficiency(candidate).rule_code == (
        metadata_only_content.METADATA_ONLY_INSUFFICIENT
    )


def test_author_field_with_contributor_roles_is_not_used():
    facts = metadata_only_content.verified_facts(
        {"verified_title": "Bóng Tối Bao La", "verified_author": "Chris Hadfield; Thư Vũ (dịch)"}
    )
    assert facts["authors"] == []


def test_metadata_only_templates_state_only_verified_facts():
    facts = metadata_only_content.verified_facts(VERIFIED)
    content = metadata_only_content.build_metadata_only_content(facts)

    assert content["vi"]["long_description"] == (
        "“Cây Cam Ngọt Của Tôi” là cuốn sách của tác giả José Mauro de Vasconcelos. "
        "Sách do NXB Hội Nhà Văn phát hành."
    )
    assert content["en"]["short_description"] == "“Cây Cam Ngọt Của Tôi” is a book by José Mauro de Vasconcelos."
    assert content["de"]["long_description"].startswith("„Cây Cam Ngọt Của Tôi“ ist ein Buch von")
    for language in ("vi", "en", "de"):
        assert metadata_only_content.validate_metadata_only_content(VERIFIED, language, content[language]).is_auto_pass
        text = content[language]["long_description"].lower()
        for forbidden in ("tuổi", "age", "alter", "tiếng việt", "vietnam", "tiệm sách"):
            assert forbidden not in text


def test_metadata_only_validator_rejects_any_added_claim():
    content = metadata_only_content.build_metadata_only_content(metadata_only_content.verified_facts(VERIFIED))["vi"]
    padded = dict(content, long_description=content["long_description"] + " Cuốn sách phù hợp cho trẻ từ 6 tuổi.")

    assert not metadata_only_content.validate_metadata_only_content(VERIFIED, "vi", padded).is_auto_pass


def test_enabling_the_owner_flag_unlocks_the_draft_safe_path(monkeypatch):
    monkeypatch.setattr(metadata_only_content, "METADATA_ONLY_AUTO_APPROVAL_ENABLED", True)
    assert metadata_only_content.metadata_only_path_available(VERIFIED).is_auto_pass
