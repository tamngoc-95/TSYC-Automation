"""--action REPLACE_APPROVED: versioned replacement of APPROVED content.
Validate everything first, write all or nothing, preserve the previous
version (audit file + process_logs), never leave stale translations."""
from __future__ import annotations

import json

import pytest

import prepare_product_content as ppc
from test_prepare_product_content_translation import (
    DE_GOOD,
    EN_GOOD,
    PRODUCT_CODE,
    make_repository,
    make_vi_content,
    rows,
    write_file,
)

REASON = "Cross-language consistency failure: German text dropped the page count."


def _approved(language: str, fields: dict) -> dict:
    return {**fields, "product_content_id": f"content-{language}", "internal_product_id": "internal-product-1",
            "content_language": language, "content_status": "APPROVED", "review_required": False,
            "generation_method": "AI_ASSISTED"}


def _repository():
    repository = make_repository(
        vi=make_vi_content(),
        extra_contents=[_approved("en", EN_GOOD), _approved("de", {**DE_GOOD, "long_description": "Alt."})],
    )
    repository.client.tables["process_logs"] = []
    return repository


def _run(repository, tmp_path, package, **overrides):
    kwargs = dict(
        repository=repository, product_code=PRODUCT_CODE, content_file=write_file(tmp_path, package, "pkg.json"),
        repair_reason=REASON, generation_method="AI_ASSISTED", non_interactive=True, confirm_repair=True,
        audit_dir=tmp_path / "audit",
    )
    kwargs.update(overrides)
    return ppc.run_replace_approved_action(**kwargs)


def test_valid_translation_replacement_is_written_and_previous_version_preserved(tmp_path):
    repository = _repository()
    result = _run(repository, tmp_path, {"de": {"long_description": DE_GOOD["long_description"]}})

    de = rows(repository, "de")[0]
    assert de["long_description"] == DE_GOOD["long_description"]
    assert de["content_status"] == "APPROVED" and de["review_required"] is False
    assert de["generation_method"] == "AI_ASSISTED"
    assert "REPLACE_APPROVED" in de["review_notes"] and REASON in de["review_notes"]
    audit = json.loads(result["audit_file"].read_text(encoding="utf-8"))
    assert audit["before"]["de"]["long_description"] == "Alt."
    log = repository.client.tables["process_logs"][0]
    assert log["error_details"]["previous"]["de"]["long_description"] == "Alt."
    assert rows(repository, "en")[0]["long_description"] == EN_GOOD["long_description"]  # untouched


def test_invalid_replacement_writes_nothing(tmp_path):
    repository = _repository()
    bad = {"de": {"long_description": DE_GOOD["long_description"] + " Der Preis beträgt 9,99 €."}}
    with pytest.raises(RuntimeError, match="nothing written"):
        _run(repository, tmp_path, bad)
    assert rows(repository, "de")[0]["long_description"] == "Alt."
    assert repository.client.tables["process_logs"] == []
    assert not (tmp_path / "audit").exists()


def test_replacing_vi_without_its_translations_is_refused(tmp_path):
    repository = _repository()
    with pytest.raises(RuntimeError, match="APPROVED translations of the old text"):
        _run(repository, tmp_path, {"vi": {"short_description": "Một mô tả ngắn mới cho sách tranh."}})
    assert rows(repository, "vi")[0]["short_description"].startswith("“Gấu con đi ngủ”")


def test_only_approved_rows_can_be_replaced(tmp_path):
    repository = _repository()
    rows(repository, "de")[0]["content_status"] = "DRAFTED"
    with pytest.raises(RuntimeError, match="only replaces APPROVED"):
        _run(repository, tmp_path, {"de": {"long_description": DE_GOOD["long_description"]}})


@pytest.mark.parametrize(
    "override, message",
    [
        ({"repair_reason": ""}, "--repair-reason"),
        ({"confirm_repair": False}, "--confirm-repair"),
        ({"generation_method": "RULE_BASED"}, "--generation-method"),
    ],
)
def test_required_arguments(tmp_path, override, message):
    with pytest.raises(RuntimeError, match=message):
        _run(_repository(), tmp_path, {"de": {"long_description": DE_GOOD["long_description"]}}, **override)


def test_unknown_language_or_field_is_refused(tmp_path):
    with pytest.raises(RuntimeError, match="only"):
        _run(_repository(), tmp_path, {"fr": {"long_description": "x"}})
    with pytest.raises(RuntimeError):
        _run(_repository(), tmp_path, {"de": {"regular_price": "9.99"}})
