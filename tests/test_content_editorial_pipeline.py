"""Regression tests for the evidence-checked editorial pipeline (2026-10-17):
evidence ledger, explainable quality score + hard gates, bounded editorial
revisions with every version preserved, VI/EN/DE consistency, and writes
only through the official content writers, never over approved content.
Offline: fake Supabase, fake prepare_product_content runner."""
from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import run_content_editorial as editorial
from src.domain import content_evidence as evidence
from src.domain.rules import content_quality_score as quality
from support.fake_supabase import FakeSupabaseRepository

CODE = "FAHASA-2026-002-CAN-0099"
PRODUCT_CODE = f"TSYC-{CODE}"
CANDIDATE_ID = "cand-99"
PRODUCT_ID = "ip-99"
REFERENCE_ID = "ref-99"
TITLE = "Cổ Tích Thế Giới Hay Nhất - Bà Chúa Tuyết"
PREVIEW = "https://cdn1.fahasa.com/media/flashmagazine/images/page_images/ba_chua_tuyet/p1-390x510.jpg"
SOURCE = (
    "Ngày xửa ngày xưa, có một cô gái siêng năng chăm chỉ, còn người chị em cùng cha khác mẹ của cô lại "
    "chây lười, biếng nhác. Câu chuyện bắt đầu khi cô gái ấy làm rơi ống sợi xuống giếng…"
)
PRODUCT = {"title": TITLE, "author": "Phương Thúy, Cloud Pillow Studio", "publisher": "Kim Đồng"}

GOOD_VI = {
    "short_description": "Truyện cổ tích về cô gái siêng năng lạc xuống đáy giếng rồi đến ngôi nhà của Bà Chúa Tuyết.",
    "long_description": (
        "Trong nhà bà mẹ kế, cô gái siêng năng phải làm lụng quần quật, ngồi kéo sợi đến rớm máu đầu ngón tay, "
        "trong khi cô con gái ruột của bà lại chây lười, biếng nhác. Một lần cúi xuống giếng rửa ống sợi, cô "
        "tuột tay đánh rơi nó xuống đáy, và bị bà mẹ kế bắt phải xuống mò lên.\n\n"
        "Khi mở mắt, cô thấy mình nằm giữa một đồng cỏ đầy hoa. Cuối cùng, cô đến căn nhà nhỏ của Bà Chúa "
        "Tuyết, ở lại giũ đệm thật mạnh cho bà, để lông vũ bay khắp nơi như những bông hoa tuyết.\n\n"
        "Câu chuyện đặt cạnh nhau sự siêng năng và thói chây lười, một chủ đề giản dị để cha mẹ cùng con trò "
        "chuyện sau mỗi lần đọc."
    ),
    "seo_title": TITLE,
    "seo_description": "Cô gái siêng năng lạc xuống đáy giếng và đến nhà Bà Chúa Tuyết.",
}
GOOD_EN = {
    "product_name": f"{TITLE} (Vietnamese-language fairy tale)",
    "short_description": "A fairy tale about a hard-working girl who falls to the bottom of a well and reaches the house of Bà Chúa Tuyết.",
    "long_description": (
        "In her stepmother's house, the hard-working girl toils from morning to night, spinning until her "
        "fingertips bleed, while the stepmother's own daughter stays idle and lazy. One day, as she leans over "
        "the well to wash the spindle, it slips from her hand, and her stepmother orders her to fetch it.\n\n"
        "When she opens her eyes, she is lying in a meadow full of flowers. At last she reaches the little "
        "house of Bà Chúa Tuyết and shakes the bedding so hard that feathers fly like snowflakes.\n\n"
        "The story sets diligence and idleness side by side, a simple theme for parents and children to talk "
        "about after each reading."
    ),
}
GOOD_DE = {
    "product_name": f"{TITLE} (Märchen auf Vietnamesisch)",
    "short_description": "Ein Märchen über ein fleißiges Mädchen, das auf den Grund eines Brunnens fällt und zum Haus von Bà Chúa Tuyết gelangt.",
    "long_description": (
        "Im Haus ihrer Stiefmutter muss das fleißige Mädchen von früh bis spät schuften und spinnen, bis ihre "
        "Fingerspitzen bluten, während die leibliche Tochter faul und träge bleibt. Als sie sich über den "
        "Brunnen beugt, um die Spule zu waschen, rutscht ihr diese aus der Hand.\n\n"
        "Beim Erwachen liegt sie auf einer Wiese voller Blumen. Schließlich erreicht sie das kleine Haus von "
        "Bà Chúa Tuyết und schüttelt die Decken so kräftig, dass die Federn wie Schneeflocken fliegen.\n\n"
        "Die Geschichte stellt Fleiß und Faulheit einander gegenüber – ein schlichtes Thema, über das Eltern "
        "und Kinder nach dem Lesen ins Gespräch kommen können."
    ),
}


def _evidence() -> list[dict[str, Any]]:
    return [
        {"id": "E1", "kind": "VERIFIED_FACT", "source_type": "REFERENCE_DESCRIPTION", "source_ref": REFERENCE_ID,
         "statement": "Cô gái siêng năng; người chị em chây lười; rơi ống sợi xuống giếng.",
         "anchors": ["siêng năng", "chây lười", "ống sợi"]},
        {"id": "E2", "kind": "VERIFIED_FACT", "source_type": "PREVIEW_PAGE", "source_ref": PREVIEW, "legibility": "READABLE",
         "statement": "Mẹ kế bắt xuống giếng; đồng cỏ đầy hoa; Bà Chúa Tuyết nhờ giũ đệm, lông vũ bay như hoa tuyết.",
         "anchors": ["mẹ kế", "đồng cỏ", "Bà Chúa Tuyết", "lông vũ", "kéo sợi", "ngón tay"]},
        {"id": "E3", "kind": "UNKNOWN", "source_type": "REFERENCE_DESCRIPTION", "source_ref": REFERENCE_ID,
         "statement": "Nguồn gốc truyện chưa xác minh.", "anchors": ["Andersen"]},
    ]


def _package(versions=None, localizations=None, items=None) -> dict[str, Any]:
    return {
        "product_code": PRODUCT_CODE,
        "evidence": items if items is not None else _evidence(),
        "versions": versions if versions is not None else [{"stage": "DRAFT", "content": dict(GOOD_VI)}],
        "localizations": localizations if localizations is not None else {"en": dict(GOOD_EN), "de": dict(GOOD_DE)},
    }


def _approved_images() -> dict[str, dict[str, Any]]:
    return {PREVIEW: {"classification": "APPROVED", "visual_review": {"relevant_preview": True}}}


def _repository(vi_status="REVIEW_REQUIRED", woo_status="NOT_CREATED", records=None) -> FakeSupabaseRepository:
    return FakeSupabaseRepository(
        tables={
            "internal_products": [
                {"internal_product_id": PRODUCT_ID, "candidate_id": CANDIDATE_ID, "product_code": PRODUCT_CODE,
                 "woocommerce_status": woo_status, **PRODUCT}
            ],
            "product_candidates": [
                {"candidate_id": CANDIDATE_ID, "candidate_code": CODE, "candidate_type": "SINGLE_BOOK",
                 "source_evidence": {"fahasa_image_review_records": records if records is not None else [
                     {"image_url": PREVIEW, "classification": "APPROVED", "visual_review": {"relevant_preview": True}}
                 ]}}
            ],
            "product_references": [
                {"reference_id": REFERENCE_ID, "candidate_id": CANDIDATE_ID, "match_decision": "MATCH",
                 "reference_description": SOURCE}
            ],
            "product_contents": [
                {"product_content_id": "pc-vi", "internal_product_id": PRODUCT_ID, "content_language": "vi",
                 "content_status": vi_status, "product_name": TITLE, "review_required": vi_status != "APPROVED",
                 "short_description": "Bản nháp.", "long_description": "Bản nháp cũ."}
            ],
        }
    )


class FakeWriter:
    """Simulates prepare_product_content.py's REVISE/APPROVE/EXPORT/TRANSLATE effects."""

    def __init__(self, repository, package_dir: Path, fail_on: str | None = None):
        self.repository = repository
        self.package_dir = package_dir
        self.fail_on = fail_on
        self.calls: list[str] = []
        self.translate_status: dict[str, str] = {}

    def _rows(self):
        return self.repository.client.tables["product_contents"]

    def __call__(self, arguments: list[str]) -> subprocess.CompletedProcess:
        action = arguments[arguments.index("--action") + 1]
        self.calls.append(action)
        if action == self.fail_on:
            return subprocess.CompletedProcess(arguments, 1, stdout="Product content processing failed.", stderr="")
        vi = next(row for row in self._rows() if row["content_language"] == "vi")
        if action == "REVISE":
            vi.update(json.loads(Path(arguments[arguments.index("--content-file") + 1]).read_text(encoding="utf-8")))
            vi["content_status"], vi["generation_method"] = "DRAFTED", "AI_ASSISTED"
        elif action == "APPROVE":
            vi["content_status"], vi["review_required"] = "APPROVED", False
        elif action == "EXPORT_PACKAGE":
            path = self.package_dir / f"{CODE}.json"
            if path.exists():  # the real writer never overwrites a package
                return subprocess.CompletedProcess(arguments, 1, stdout="Refusing to overwrite", stderr="")
            path.write_text(json.dumps({"candidate_code": CODE}), encoding="utf-8")
        elif action == "TRANSLATE":
            exported = json.loads((self.package_dir / f"{CODE}.json").read_text(encoding="utf-8"))
            for language in ("en", "de"):
                row = next((r for r in self._rows() if r["content_language"] == language), None)
                if row is None:
                    row = {"internal_product_id": PRODUCT_ID, "content_language": language}
                    self._rows().append(row)
                if row.get("content_status") != "APPROVED":
                    row["long_description"] = exported[f"description_{language}"]
                    row["content_status"] = self.translate_status.get(language, "APPROVED")
        return subprocess.CompletedProcess(arguments, 0, stdout="ok", stderr="")


def _write_package(directory: Path, package: dict[str, Any]) -> None:
    (directory / f"{PRODUCT_CODE}.json").write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")


def _run(tmp_path, repository, package, dry_run=False, writer=None):
    packages, audit, translations = tmp_path / "packages", tmp_path / "audit", tmp_path / "translations"
    for directory in (packages, audit, translations):
        directory.mkdir(exist_ok=True)
    _write_package(packages, package)
    writer = writer or FakeWriter(repository, translations)
    report = editorial.run_editorial(
        repository, PRODUCT_CODE, packages, dry_run=dry_run, runner=writer,
        audit_dir=audit, translation_package_dir=translations,
    )
    return report, writer, audit


# --- quality score -------------------------------------------------------------------------


def _score(content, **kwargs):
    return quality.score_content({**content, "product_name": TITLE}, PRODUCT, "vi", source_texts=[SOURCE], **kwargs)


def test_source_backed_description_passes_gates_and_target():
    result = _score(GOOD_VI, evidence_items=_evidence())
    assert result["hard_gate_failures"] == []
    assert result["score"] >= quality.TARGET_SCORE and result["passed"]
    assert set(result["dimensions"]) == set(quality.DIMENSION_WEIGHTS)
    assert all(d["score"] <= d["max"] for d in result["dimensions"].values())


def test_generic_filler_and_template_phrasing_lose_points_with_named_defects():
    generic = dict(GOOD_VI, long_description=(
        f"{TITLE} là cuốn sách của tác giả Phương Thúy, do Nhà xuất bản Kim Đồng ấn hành. Cuốn sách đưa bé bước vào "
        "thế giới lung linh với những điều kỳ diệu trên từng trang sách.\n\n"
        "Cuốn sách giúp bé phát triển tư duy, rèn luyện kỹ năng quan sát và nâng cao khả năng tập trung. "
        "Đây là cuốn sách phù hợp cho mọi bé, đồng thời là món quà ý nghĩa dành cho gia đình."
    ))
    result = _score(generic)
    assert result["score"] < quality.TARGET_SCORE
    for defect in (quality.GENERIC_FILLER, quality.METADATA_OPENING, quality.CLOSING_CLICHE, quality.BENEFIT_STACKING):
        assert defect in result["defects"]
    assert any("UNSUPPORTED_BENEFIT" in failure for failure in quality.hard_gates(
        {**generic, "product_name": TITLE}, PRODUCT, "vi", [SOURCE], _evidence()
    ))


def test_repeated_author_label_is_a_hard_gate():
    labelled = dict(GOOD_VI, long_description=GOOD_VI["long_description"] + "\n\nTác giả: Phương Thúy")
    result = _score(labelled, evidence_items=_evidence())
    assert result["hard_gate_failures"] and not result["passed"]


def test_template_reuse_against_sibling_products_is_detected():
    result = _score(GOOD_VI, evidence_items=_evidence(), sibling_texts=[GOOD_VI["long_description"]])
    assert quality.TEMPLATE_REUSE in result["defects"]


def test_title_character_name_is_not_internal_repetition():
    result = _score(GOOD_VI, evidence_items=_evidence())
    assert quality.INTERNAL_REPETITION not in result["defects"]  # "của Bà Chúa Tuyết" twice is the protagonist


def test_localization_inherits_vietnamese_specificity():
    result = quality.score_content(GOOD_EN, PRODUCT, "en", reference_specificity=1.0, enforce_originality=False)
    assert result["dimensions"]["specificity"]["ratio"] == 1.0 and result["passed"]


# --- evidence ledger -------------------------------------------------------------------------


def test_unsupported_names_numbers_and_unknown_facts_are_hard_gates():
    text = GOOD_VI["long_description"] + " Truyện của Andersen có 12 nhân vật, trong đó có Hoàng Tử Gió."
    issues = evidence.find_unsupported_statements(text, _evidence(), PRODUCT, [SOURCE])
    assert evidence.USES_UNKNOWN_FACT in issues and "Andersen" in issues[evidence.USES_UNKNOWN_FACT]
    assert issues[evidence.UNSUPPORTED_NUMBER] == ["12"]
    assert {"Hoàng", "Tử", "Gió"} <= set(issues[evidence.UNSUPPORTED_NAME])


def test_reference_anchors_must_occur_in_the_cited_source():
    items = _evidence()
    items[0]["anchors"].append("hoàng tử")
    problems = evidence.validate_evidence(items, {REFERENCE_ID: SOURCE}, _approved_images())
    assert any("anchors not found" in problem for problem in problems)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"legibility": "PARTLY_READABLE"}, "READABLE"),
        ({"source_ref": "https://cdn1.fahasa.com/other.jpg"}, "APPROVED reviewed image"),
    ],
)
def test_preview_evidence_must_be_approved_and_readable(change, expected):
    items = _evidence()
    items[1].update(change)
    problems = evidence.validate_evidence(items, {REFERENCE_ID: SOURCE}, _approved_images())
    assert any(expected in problem for problem in problems)


def test_preview_not_reviewed_as_relevant_is_not_evidence():
    images = {PREVIEW: {"classification": "APPROVED", "visual_review": {"relevant_preview": None}}}
    problems = evidence.validate_evidence(_evidence(), {REFERENCE_ID: SOURCE}, images)
    assert any("not reviewed as relevant" in problem for problem in problems)


def test_evidence_strength_bands():
    metadata_only = [{"id": "E1", "kind": "VERIFIED_FACT", "source_type": "PRODUCT_METADATA"}]
    assert evidence.evidence_strength(metadata_only) == evidence.INSUFFICIENT
    assert evidence.evidence_strength(_evidence()[:1]) == evidence.THIN
    assert evidence.evidence_strength(_evidence()) == evidence.ADEQUATE


# --- editorial runner ------------------------------------------------------------------------


def test_missing_reference_text_isolates_instead_of_inventing(tmp_path):
    metadata_only = [{"id": "E1", "kind": "VERIFIED_FACT", "source_type": "PRODUCT_METADATA",
                      "source_ref": "product.title", "statement": TITLE, "anchors": []}]
    report, writer, _ = _run(tmp_path, _repository(), _package(items=metadata_only))
    assert report["result"] == editorial.INSUFFICIENT_EVIDENCE
    assert writer.calls == []


def test_every_version_is_scored_and_preserved_and_the_last_one_is_applied(tmp_path):
    weak = dict(GOOD_VI, long_description=GOOD_VI["long_description"].replace(
        "Câu chuyện đặt cạnh nhau", "Đây là cuốn sách phù hợp cho mọi bé, món quà ý nghĩa. Câu chuyện đặt cạnh nhau"
    ))
    versions = [{"stage": "DRAFT", "content": weak}, {"stage": "EDIT", "editor_changes": ["stock closing removed"], "content": dict(GOOD_VI)}]
    repository = _repository()
    report, writer, audit = _run(tmp_path, repository, _package(versions=versions))
    assert report["result"] == editorial.APPLIED, report
    assert [v["stage"] for v in report["versions"]] == ["DRAFT", "EDIT"]
    assert report["versions"][0]["score"] < report["versions"][1]["score"]
    assert writer.calls == ["REVISE", "APPROVE", "EXPORT_PACKAGE", "TRANSLATE"]
    vi = next(row for row in repository.client.tables["product_contents"] if row["content_language"] == "vi")
    assert vi["long_description"] == GOOD_VI["long_description"] and vi["generation_method"] == "AI_ASSISTED"
    [audit_file] = list(audit.glob("*_editorial_*.json"))
    saved = json.loads(audit_file.read_text(encoding="utf-8"))
    assert [v["content"]["long_description"] for v in saved["evaluation"]["versions"]] == [
        weak["long_description"], GOOD_VI["long_description"]
    ]
    assert repository.client.tables["process_logs"][-1]["process_step"] == "EDITORIAL_REVIEW"


def test_failing_final_version_is_isolated_with_its_defects(tmp_path):
    bad = dict(GOOD_VI, long_description=GOOD_VI["long_description"] + " Truyện có 12 chương.")
    report, writer, _ = _run(tmp_path, _repository(), _package(versions=[{"stage": "DRAFT", "content": bad}]))
    assert report["result"] == editorial.EDITORIAL_FAILED
    assert any("UNSUPPORTED_NUMBER" in failure for failure in report["versions"][0]["hard_gate_failures"])
    assert writer.calls == []


def test_more_than_three_editorial_attempts_are_refused(tmp_path):
    versions = [{"stage": "DRAFT", "content": dict(GOOD_VI)}] + [{"stage": "EDIT", "content": dict(GOOD_VI)}] * 4
    report, writer, _ = _run(tmp_path, _repository(), _package(versions=versions))
    assert report["result"] == editorial.TOO_MANY_ATTEMPTS and writer.calls == []


def test_cross_language_inconsistency_blocks_every_write(tmp_path):
    en = dict(GOOD_EN, long_description=GOOD_EN["long_description"] + " The book has 32 pages.")
    report, writer, _ = _run(tmp_path, _repository(), _package(localizations={"en": en, "de": dict(GOOD_DE)}))
    assert report["result"] == editorial.LOCALIZATION_FAILED
    assert report["consistency"]["status"] != "PASS"
    assert writer.calls == []


def test_translation_gate_runs_before_any_write(tmp_path):
    # "set" reads as a combo for an individual book (translation_rules check 8).
    de = dict(GOOD_DE)
    en = dict(GOOD_EN, long_description=GOOD_EN["long_description"].replace(
        "The story sets diligence", "The text is set beside the pictures; the story places diligence"
    ))
    report, writer, _ = _run(tmp_path, _repository(), _package(localizations={"en": en, "de": de}))
    assert report["result"] == editorial.LOCALIZATION_FAILED
    assert any("set/combo" in failure for failure in report["localizations"]["en"]["hard_gate_failures"])
    assert writer.calls == []


def test_approved_or_published_content_is_never_overwritten(tmp_path):
    report, writer, _ = _run(tmp_path, _repository(vi_status="APPROVED", woo_status="PUBLISHED"), _package())
    assert report["result"] == editorial.VI_ALREADY_APPROVED and writer.calls == []


def test_repeated_execution_is_idempotent(tmp_path):
    repository = _repository()
    first, _, _ = _run(tmp_path, repository, _package())
    assert first["result"] == editorial.APPLIED
    second, writer, _ = _run(tmp_path, repository, _package())
    assert second["result"] == editorial.NO_OP and writer.calls == []


def test_rerun_after_a_translation_went_to_review_refills_the_existing_package(tmp_path):
    repository = _repository()
    (tmp_path / "translations").mkdir()
    first_writer = FakeWriter(repository, tmp_path / "translations")
    first_writer.translate_status = {"de": "REVIEW_REQUIRED"}
    first, _, _ = _run(tmp_path, repository, _package(), writer=first_writer)
    assert first["result"] == editorial.WRITER_FAILED and first["statuses"]["de"] == "REVIEW_REQUIRED"

    second, writer, _ = _run(tmp_path, repository, _package())
    assert second["result"] == editorial.APPLIED, second
    assert writer.calls == ["TRANSLATE"]  # vi untouched, existing package reused, no overwrite attempt


def test_writer_failure_is_reported_not_retried(tmp_path):
    repository = _repository()
    writer = FakeWriter(repository, tmp_path / "translations", fail_on="APPROVE")
    (tmp_path / "translations").mkdir()
    report, _, _ = _run(tmp_path, repository, _package(), writer=writer)
    assert report["result"] == editorial.WRITER_FAILED
    assert writer.calls == ["REVISE", "APPROVE"]


def test_dry_run_writes_nothing(tmp_path):
    repository = _repository()
    report, writer, audit = _run(tmp_path, repository, _package(), dry_run=True)
    assert report["result"] == editorial.DRY_RUN and writer.calls == [] and not list(audit.iterdir())


def test_package_shape_and_bounds_are_enforced(tmp_path):
    package = _package(versions=[{"stage": "EDIT", "content": dict(GOOD_VI)}])
    report, _, _ = _run(tmp_path, _repository(), package)
    assert report["result"] == editorial.PACKAGE_INVALID
    extra = copy.deepcopy(_package())
    extra["versions"][0]["content"]["product_name"] = "Khác"
    assert _run(tmp_path, _repository(), extra)[0]["result"] == editorial.PACKAGE_INVALID
    with pytest.raises(SystemExit):
        editorial.parse_arguments(["--product-code", PRODUCT_CODE, "--max-products", "11"])
