"""
Editorial pipeline for storefront descriptions: evidence -> Vietnamese draft
-> bounded editorial revisions -> quality score + hard gates -> EN/DE
localization -> the official content writers (CLAUDE.md 15, 15.3, 15.4).

For each EXACT --product-code (bounded by --max-products), serially, from
its editorial package <package-dir>/<product_code>.json:

  {"product_code": "...",
   "evidence": [ ...src.domain.content_evidence items... ],
   "versions": [ {"stage": "DRAFT", "content": {...}},
                 {"stage": "EDIT", "editor_changes": [...], "content": {...}}, ...],
   "localizations": {"en": {"product_name", "short_description", "long_description"},
                     "de": {...}}}

  1. evidence: validate_evidence (anchors must occur in the cited source;
     preview/cover evidence must cite an APPROVED, READABLE reviewed image);
     INSUFFICIENT evidence isolates the product (a shorter accurate text is
     preferred to invented depth, but no narrative evidence at all means
     there is nothing to write from).
  2. versions: the first is the DRAFT, then at most MAX_EDITORIAL_ATTEMPTS
     editorial revisions (EDIT). Every version is scored
     (content_quality_score) and kept with its defects, so the draft and
     every revision stay auditable. The LAST version is the candidate: it
     must pass every hard gate (existing approval validators + evidence
     support) and reach TARGET_SCORE, otherwise EDITORIAL_FAILED isolates
     the product with the exact defects.
  3. localizations: each EN/DE text must pass its hard gates, the exact
     translation_rules gate TRANSLATE applies, and the target score, and
     the VI/EN/DE package must pass multilingual_consistency -- all
     checked before any write.
  4. apply (--confirm-apply): audit file + process_logs entry with every
     version and score; then the official writers --
     prepare_product_content.py REVISE (AI_ASSISTED) + APPROVE for vi,
     EXPORT_PACKAGE + TRANSLATE (package-file) for en/de, each re-read.

Never overwrites APPROVED content (an approved vi row that differs is
VI_ALREADY_APPROVED -- replacing approved text is REPLACE_APPROVED's
separate, recorded path), never touches WooCommerce, prices, identity or
images. Idempotent: a re-run of an applied package is a NO_OP. Dry run
(full evaluation, no write) unless --confirm-apply.

Usage:
  .venv/Scripts/python.exe scripts/run_content_editorial.py \
      --product-code TSYC-FAHASA-2026-002-CAN-0001 --max-products 1
  ... --non-interactive --confirm-apply
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain import content_evidence as evidence  # noqa: E402
from src.domain.content_package import build_content_package  # noqa: E402
from src.domain.content_status import ContentStatus  # noqa: E402
from src.domain.rules import content_quality_score as quality  # noqa: E402
from src.domain.rules import multilingual_consistency, translation_rules  # noqa: E402
from src.services.translation_provider import DEFAULT_PACKAGE_DIR, package_file_path  # noqa: E402

configure_utf8_console()

EDITOR_NAME = "content_editorial_pipeline"
EDITOR_VERSION = "1.0.0"
MAX_PRODUCTS_HARD_CAP = 10
MAX_EDITORIAL_ATTEMPTS = 3
DEFAULT_EDITORIAL_DIR = PROJECT_ROOT / "data" / "processed" / "editorial_packages"
EDITORIAL_AUDIT_DIR = PROJECT_ROOT / "data" / "processed" / "editorial_audit"
VI_FIELDS = ("short_description", "long_description", "seo_title", "seo_description")
LOCALIZED_FIELDS = ("product_name", "short_description", "long_description")
LANGUAGES = ("en", "de")

READY = "READY"
DRY_RUN = "DRY_RUN"
APPLIED = "APPLIED"
NO_OP = "NO_OP"
LOCAL_NOT_READY = "LOCAL_NOT_READY"
PACKAGE_INVALID = "PACKAGE_INVALID"
INVALID_EVIDENCE = "INVALID_EVIDENCE"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
TOO_MANY_ATTEMPTS = "TOO_MANY_EDITORIAL_ATTEMPTS"
EDITORIAL_FAILED = "EDITORIAL_FAILED"
LOCALIZATION_FAILED = "LOCALIZATION_FAILED"
VI_ALREADY_APPROVED = "VI_ALREADY_APPROVED"
WRITER_FAILED = "WRITER_FAILED"

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _subprocess_runner(arguments: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "prepare_product_content.py"), *arguments],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=PROJECT_ROOT,
    )


# --- loading ---------------------------------------------------------------


def load_context(repository: Any, product_code: str) -> dict[str, Any]:
    """Product, candidate, content rows, sources, approved images, siblings."""
    products = repository.client.table("internal_products").select("*").eq("product_code", product_code).execute().data or []
    if len(products) != 1:
        raise RuntimeError(f"product_code did not resolve to exactly one product: {product_code}")
    product = products[0]
    candidates = (
        repository.client.table("product_candidates").select("*").eq("candidate_id", product["candidate_id"]).execute().data
        or []
    )
    if len(candidates) != 1:
        raise RuntimeError("The product's candidate was not found.")
    candidate = candidates[0]
    contents = (
        repository.client.table("product_contents").select("*")
        .eq("internal_product_id", product["internal_product_id"]).execute().data
        or []
    )
    references = (
        repository.client.table("product_references").select("*")
        .eq("candidate_id", candidate["candidate_id"]).execute().data
        or []
    )
    source_texts = {
        str(reference["reference_id"]): str(reference.get("reference_description") or "")
        for reference in references
        if reference.get("match_decision") == "MATCH" and reference.get("reference_description")
    }
    for field in ("title", "author", "publisher", "translator", "page_count", "series_name"):
        if product.get(field) not in (None, ""):
            source_texts[f"product.{field}"] = str(product[field])
    records = (candidate.get("source_evidence") or {}).get("fahasa_image_review_records") or []
    approved_images = {
        str(record.get("image_url")): record for record in records
        if isinstance(record, dict) and record.get("classification") == "APPROVED"
    }
    siblings = [
        row.get("long_description") or ""
        for row in (
            repository.client.table("product_contents").select("internal_product_id, long_description")
            .eq("content_language", "vi").execute().data
            or []
        )
        if row.get("internal_product_id") != product["internal_product_id"]
    ]
    return {
        "product": product,
        "candidate": candidate,
        "contents": {row["content_language"]: row for row in contents},
        "source_texts": source_texts,
        "approved_images": approved_images,
        "siblings": siblings,
    }


def validate_package(package: Any, product_code: str) -> list[str]:
    if not isinstance(package, dict):
        return ["package root is not an object"]
    problems = []
    if package.get("product_code") != product_code:
        problems.append(f"package product_code {package.get('product_code')!r} != {product_code!r}")
    versions = package.get("versions")
    if not isinstance(versions, list) or not versions:
        return problems + ["versions missing"]
    if versions[0].get("stage") != "DRAFT":
        problems.append("the first version must be the DRAFT")
    for index, version in enumerate(versions):
        content = version.get("content") if isinstance(version, dict) else None
        if not isinstance(content, dict) or not str(content.get("long_description") or "").strip():
            problems.append(f"version {index}: content.long_description missing")
        elif set(content) - set(VI_FIELDS):
            problems.append(f"version {index}: fields outside {VI_FIELDS}: {sorted(set(content) - set(VI_FIELDS))}")
        if index and version.get("stage") != "EDIT":
            problems.append(f"version {index}: stage must be EDIT")
    for language in LANGUAGES:
        localized = (package.get("localizations") or {}).get(language)
        if not isinstance(localized, dict) or any(not str(localized.get(f) or "").strip() for f in LOCALIZED_FIELDS):
            problems.append(f"localizations.{language} needs {LOCALIZED_FIELDS}")
    return problems


# --- evaluation --------------------------------------------------------------


def evaluate_package(context: dict[str, Any], package: dict[str, Any]) -> dict[str, Any]:
    """Pure over the loaded context: every version and localization scored."""
    product = context["product"]
    vi_row = context["contents"].get("vi") or {}
    product_name = vi_row.get("product_name") or product.get("title")
    items = package.get("evidence") or []
    reference_texts = [text for key, text in context["source_texts"].items() if not key.startswith("product.")]
    report: dict[str, Any] = {"evidence_problems": [], "evidence_strength": None, "versions": [], "localizations": {}}

    problems = evidence.validate_evidence(items, context["source_texts"], context["approved_images"])
    report["evidence_problems"] = problems
    report["evidence_strength"] = evidence.evidence_strength(items)
    if problems:
        return {**report, "outcome": INVALID_EVIDENCE}
    if report["evidence_strength"] == evidence.INSUFFICIENT:
        return {**report, "outcome": INSUFFICIENT_EVIDENCE}
    edits = len(package["versions"]) - 1
    if edits > MAX_EDITORIAL_ATTEMPTS:
        return {**report, "outcome": TOO_MANY_ATTEMPTS}

    for index, version in enumerate(package["versions"]):
        content = {**version["content"], "product_name": product_name}
        result = quality.score_content(
            content, product, "vi", source_texts=reference_texts, evidence_items=items, sibling_texts=context["siblings"]
        )
        report["versions"].append(
            {
                "index": index,
                "stage": version.get("stage"),
                "editor_changes": version.get("editor_changes") or [],
                "content": version["content"],
                "score": result["score"],
                "defects": result["defects"],
                "hard_gate_failures": result["hard_gate_failures"],
                "dimensions": result["dimensions"],
                "passed": result["passed"],
            }
        )
    final = report["versions"][-1]
    if not final["passed"]:
        return {**report, "outcome": EDITORIAL_FAILED}

    final_vi = {**package["versions"][-1]["content"], "product_name": product_name}
    approved_vi = {**final_vi, "content_status": ContentStatus.APPROVED, "review_required": False}
    vi_ratio = final["dimensions"]["specificity"]["ratio"]
    localized_failures = False
    for language in LANGUAGES:
        localized = package["localizations"][language]
        result = quality.score_content(
            localized, product, language, sibling_texts=(), reference_specificity=vi_ratio, enforce_originality=False
        )
        # The exact translation gate TRANSLATE will apply (fact categories,
        # numbers, combo/individual distinction, language, length ratio),
        # run first so the editorial loop repairs it before any write.
        translation_check = translation_rules.evaluate_translation(
            language=language,
            translation=localized,
            vi_content=approved_vi,
            product=product,
            candidate_type=context["candidate"].get("candidate_type"),
        )
        gates = list(result["hard_gate_failures"])
        if not translation_check.is_auto_pass:
            gates.append(f"[{translation_check.rule_code}] {translation_check.reason}")
        report["localizations"][language] = {
            "score": result["score"], "defects": result["defects"],
            "hard_gate_failures": gates, "passed": result["passed"] and not gates,
        }
        localized_failures |= bool(gates) or not result["passed"]

    translated = build_content_package(
        candidate=context["candidate"],
        product=product,
        contents=[{**final_vi, "content_language": "vi", "content_status": ContentStatus.APPROVED, "review_required": False}],
    )
    for language in LANGUAGES:
        localized = package["localizations"][language]
        translated = translated.with_translation(
            language,
            product_title=localized["product_name"],
            short_description=localized["short_description"],
            description=localized["long_description"],
        )
    consistency = multilingual_consistency.evaluate_package(translated)
    report["consistency"] = {"status": consistency.status, "failures": list(consistency.failures)}
    if localized_failures or consistency.status != multilingual_consistency.PASS:
        return {**report, "outcome": LOCALIZATION_FAILED}
    return {**report, "outcome": READY}


# --- apply ---------------------------------------------------------------------


def _same_text(row: dict[str, Any] | None, content: dict[str, Any], fields: Sequence[str]) -> bool:
    return bool(row) and all(" ".join(str(row.get(f) or "").split()) == " ".join(str(content.get(f) or "").split()) for f in fields)


def _run(runner: Runner, arguments: list[str]) -> None:
    completed = runner(arguments)
    if completed.returncode != 0:
        tail = (completed.stdout or "")[-600:] + (completed.stderr or "")[-300:]
        raise RuntimeError(f"prepare_product_content.py {' '.join(arguments[:4])} failed: {tail}")


def apply_package(
    repository: Any,
    context: dict[str, Any],
    package: dict[str, Any],
    evaluation: dict[str, Any],
    runner: Runner,
    audit_dir: Path,
    package_dir: Path,
) -> dict[str, Any]:
    product = context["product"]
    product_code = product["product_code"]
    final_vi = package["versions"][-1]["content"]
    audit_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit_path = audit_dir / f"{product_code}_editorial_{stamp}.json"
    audit_path.write_text(
        json.dumps(
            {"product_code": product_code, "editor": f"{EDITOR_NAME}/{EDITOR_VERSION}", "recorded_at": utc_now(),
             "evidence": package.get("evidence"), "evaluation": evaluation, "localizations": package["localizations"]},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    repository.write_process_log(
        message=(
            f"Editorial review of {product_code}: {len(evaluation['versions'])} version(s), "
            f"scores {[v['score'] for v in evaluation['versions']]}, final passed; "
            f"en {evaluation['localizations']['en']['score']}, de {evaluation['localizations']['de']['score']}."
        ),
        process_name=EDITOR_NAME,
        candidate_id=product.get("candidate_id"),
        process_step="EDITORIAL_REVIEW",
        log_level="INFO",
        status=READY,
        error_details={
            "audit_file": audit_path.name,
            "evidence_strength": evaluation["evidence_strength"],
            "versions": [{k: v[k] for k in ("stage", "score", "defects")} for v in evaluation["versions"]],
        },
    )

    content_file = audit_dir / f"{product_code}_final_vi_{stamp}.json"
    content_file.write_text(json.dumps(final_vi, ensure_ascii=False, indent=2), encoding="utf-8")
    vi_row = context["contents"].get("vi")
    if not (vi_row and vi_row.get("content_status") == ContentStatus.APPROVED):
        if not _same_text(vi_row, final_vi, VI_FIELDS):
            _run(runner, ["--product-code", product_code, "--action", "REVISE", "--content-file", str(content_file),
                          "--non-interactive", "--confirm-revise", "--generation-method", "AI_ASSISTED"])
        _run(runner, ["--product-code", product_code, "--action", "APPROVE", "--non-interactive"])
    refreshed = load_context(repository, product_code)["contents"]
    if (refreshed.get("vi") or {}).get("content_status") != ContentStatus.APPROVED:
        return {"result": WRITER_FAILED, "detail": f"vi is {(refreshed.get('vi') or {}).get('content_status')!r} after APPROVE",
                "audit_file": str(audit_path)}

    if any((refreshed.get(language) or {}).get("content_status") != ContentStatus.APPROVED for language in LANGUAGES):
        path = package_file_path(context["candidate"]["candidate_code"], package_dir)
        # EXPORT_PACKAGE never overwrites a package; a re-run (e.g. after a
        # translation was sent to review) refills the existing one, and the
        # package-file provider still refuses it if its vi side is stale.
        if not path.exists():
            _run(runner, ["--product-code", product_code, "--action", "EXPORT_PACKAGE", "--non-interactive"])
        exported = json.loads(path.read_text(encoding="utf-8"))
        for language in LANGUAGES:
            localized = package["localizations"][language]
            exported[f"product_title_{language}"] = localized["product_name"]
            exported[f"short_description_{language}"] = localized["short_description"]
            exported[f"description_{language}"] = localized["long_description"]
        exported["generation_method"] = "AI_ASSISTED"
        path.write_text(json.dumps(exported, ensure_ascii=False, indent=2), encoding="utf-8")
        _run(runner, ["--product-code", product_code, "--action", "TRANSLATE", "--translation-provider", "package-file",
                      "--non-interactive"])
        refreshed = load_context(repository, product_code)["contents"]
    statuses = {language: (refreshed.get(language) or {}).get("content_status") for language in ("vi", *LANGUAGES)}
    return {
        "result": APPLIED if all(status == ContentStatus.APPROVED for status in statuses.values()) else WRITER_FAILED,
        "statuses": statuses,
        "audit_file": str(audit_path),
    }


def run_editorial(
    repository: Any,
    product_code: str,
    package_dir: Path,
    dry_run: bool,
    runner: Runner = _subprocess_runner,
    audit_dir: Path = EDITORIAL_AUDIT_DIR,
    translation_package_dir: Path = DEFAULT_PACKAGE_DIR,
) -> dict[str, Any]:
    report: dict[str, Any] = {"product_code": product_code, "result": None}
    try:
        context = load_context(repository, product_code)
    except RuntimeError as error:
        report.update(result=LOCAL_NOT_READY, detail=str(error))
        return report
    path = package_dir / f"{product_code}.json"
    try:
        package = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        report.update(result=PACKAGE_INVALID, detail=f"{path.name}: {type(error).__name__}")
        return report
    problems = validate_package(package, product_code)
    if problems:
        report.update(result=PACKAGE_INVALID, detail="; ".join(problems))
        return report

    vi_row = context["contents"].get("vi") or {}
    final_vi = package["versions"][-1]["content"]
    if vi_row.get("content_status") == ContentStatus.APPROVED and not _same_text(vi_row, final_vi, VI_FIELDS):
        report.update(result=VI_ALREADY_APPROVED, detail="approved Vietnamese content is never overwritten here")
        return report

    evaluation = evaluate_package(context, package)
    report.update(
        outcome=evaluation["outcome"],
        evidence_strength=evaluation["evidence_strength"],
        evidence_problems=evaluation["evidence_problems"],
        versions=[
            {k: v[k] for k in ("index", "stage", "score", "defects", "hard_gate_failures", "passed")}
            for v in evaluation["versions"]
        ],
        localizations=evaluation["localizations"],
        consistency=evaluation.get("consistency"),
    )
    if evaluation["outcome"] != READY:
        report["result"] = evaluation["outcome"]
        return report
    if all((context["contents"].get(l) or {}).get("content_status") == ContentStatus.APPROVED for l in ("vi", *LANGUAGES)):
        report["result"] = NO_OP
        return report
    if dry_run:
        report["result"] = DRY_RUN
        return report
    try:
        report.update(apply_package(repository, context, package, evaluation, runner, audit_dir, translation_package_dir))
    except RuntimeError as error:
        report.update(result=WRITER_FAILED, detail=str(error))
    return report


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evidence-checked editorial pipeline for exact products' descriptions.")
    parser.add_argument("--product-code", action="append", required=True, help="Exact internal product code. Repeat.")
    parser.add_argument("--max-products", type=int, required=True)
    parser.add_argument("--package-dir", type=Path, default=DEFAULT_EDITORIAL_DIR)
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--confirm-apply", action="store_true", help="Required to write; otherwise a dry run.")
    args = parser.parse_args(argv)
    if not 1 <= args.max_products <= MAX_PRODUCTS_HARD_CAP:
        parser.error(f"--max-products must be between 1 and {MAX_PRODUCTS_HARD_CAP}.")
    if len(set(args.product_code)) != len(args.product_code):
        parser.error("--product-code values must be unique.")
    if len(args.product_code) > args.max_products:
        parser.error("More --product-code values than --max-products.")
    return args


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_arguments()
    from src.repositories.supabase_repository import SupabaseRepository

    repository = SupabaseRepository()
    for product_code in args.product_code:  # serial: one product at a time
        report = run_editorial(repository, product_code, args.package_dir, dry_run=not args.confirm_apply)
        print(f"CONTENT_EDITORIAL_RESULT {json.dumps(report, ensure_ascii=False, default=str)}")


if __name__ == "__main__":
    main()
