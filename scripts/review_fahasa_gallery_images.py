"""
Review every collected Fahasa image of exact candidates (CLAUDE.md 14.8.2)
and add the approved ones to the candidate's gallery.

For each EXACT --candidate-code (bounded by --max-candidates), serially:

  1. optional --visual-reviews JSON: persisted visual verdicts, one per
     image URL ({candidate_code, image_url, depicts_candidate, edition_matches,
     price_label_visible, misleading_transformation, relevant_preview,
     visual_duplicate, image_kind, reviewed_by, reviewed_at, notes}). A
     verdict must name a
     registered FAHASA image of THIS candidate; it is merged into
     product_candidates.source_evidence.fahasa_image_reviews. The cover's
     existing source_evidence.fahasa_cover_review is never overwritten.
  2. every FAHASA image of the candidate gets a machine-readable record
     (image_rules.review_fahasa_image): APPROVED / REVIEW_REQUIRED /
     REJECTED with explicit reasons, page role, image kind, duplicate
     check (find_duplicate_images) -- stored in
     source_evidence.fahasa_image_review_records.
  3. apply, never touching the selected main image:
       APPROVED non-main  -> VALIDATED, SUPPLIER_APPROVED, publish eligible,
                             never selected as main (gallery image)
       REJECTED non-main  -> REJECTED, not publish eligible
       REVIEW_REQUIRED    -> unchanged (an unknown result is never approved)
     Only when the candidate already has a VALIDATED, publish-eligible
     selected main image whose own record is not REJECTED. A rejected main
     cover isolates the candidate (MAIN_COVER_FAILED) with no gallery
     change; no main yet (NO_APPROVED_MAIN) leaves main selection to the
     normal image stage (pipeline_state / run_batch).

Images from other sources and the main image are never modified. No
WooCommerce, price, identity or content writes. Idempotent: a re-run with
the same verdicts changes nothing. Dry run unless --confirm-apply.

Usage:
  .venv/Scripts/python.exe scripts/review_fahasa_gallery_images.py \
      --candidate-code FAHASA-2026-002-CAN-0001 --max-candidates 1 \
      --visual-reviews data/output/fahasa_gallery/visual_reviews_<date>.json
  ... --non-interactive --confirm-apply
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import collect_fahasa_gallery_images as collector  # noqa: E402
from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain.image_status import ImageStatus  # noqa: E402
from src.domain.rights_status import PUBLISHABLE_RIGHTS_STATUSES  # noqa: E402
from src.domain.rules import image_rules  # noqa: E402

configure_utf8_console()

REVIEWER_NAME = "fahasa_gallery_image_reviewer"
REVIEWER_VERSION = "1.0.0"
MAX_CANDIDATES_HARD_CAP = 10

REVIEWED = "REVIEWED"
DRY_RUN = "DRY_RUN"
LOCAL_NOT_READY = "LOCAL_NOT_READY"
MAIN_COVER_FAILED = "MAIN_COVER_FAILED"
NO_APPROVED_MAIN = "NO_APPROVED_MAIN"

VERDICT_BOOL_FIELDS = (
    "depicts_candidate",
    "edition_matches",
    "price_label_visible",
    "misleading_transformation",
    "relevant_preview",
    "visual_duplicate",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _asset_path(url: str | None) -> str:
    return urlsplit(str(url or "")).path


def validate_verdict(verdict: Any) -> list[str]:
    """Problems with one visual verdict; [] when it is well formed."""
    if not isinstance(verdict, dict):
        return ["verdict is not an object"]
    problems = []
    if not str(verdict.get("candidate_code") or "").strip():
        problems.append("candidate_code missing")
    if not str(verdict.get("image_url") or "").strip():
        problems.append("image_url missing")
    if not str(verdict.get("reviewed_by") or "").strip():
        problems.append("reviewed_by missing")
    for field in VERDICT_BOOL_FIELDS:
        if verdict.get(field) not in (True, False, None):
            problems.append(f"{field} must be true, false or null")
    kind = verdict.get("image_kind")
    if kind is not None and kind not in image_rules.FAHASA_REVIEW_IMAGE_KINDS:
        problems.append(f"image_kind {kind!r} is not one of {sorted(image_rules.FAHASA_REVIEW_IMAGE_KINDS)}")
    return problems


def merge_verdicts(
    source_evidence: dict[str, Any],
    verdicts: list[dict[str, Any]],
    images: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(new source_evidence, refused verdicts). A verdict replaces an older
    one for the same asset; the cover review is never overwritten."""
    evidence = dict(source_evidence or {})
    registered_paths = {_asset_path(image.get("source_url")) for image in images}
    cover_review = evidence.get("fahasa_cover_review")
    cover_path = _asset_path(cover_review.get("image_url")) if isinstance(cover_review, dict) else None
    reviews = {
        _asset_path(review.get("image_url")): review
        for review in (evidence.get("fahasa_image_reviews") or [])
        if isinstance(review, dict)
    }
    refused: list[dict[str, Any]] = []
    for verdict in verdicts:
        problems = validate_verdict(verdict)
        path = _asset_path(verdict.get("image_url")) if isinstance(verdict, dict) else ""
        if not problems and path not in registered_paths:
            problems = ["image_url is not a registered FAHASA image of this candidate"]
        if not problems and path == cover_path:
            problems = ["the cover's existing fahasa_cover_review is never overwritten"]
        if problems:
            refused.append({"image_url": (verdict or {}).get("image_url") if isinstance(verdict, dict) else None,
                            "problems": problems})
            continue
        reviews[path] = {
            key: verdict.get(key)
            for key in (
                "candidate_code", "image_url", *VERDICT_BOOL_FIELDS, "image_kind", "reviewed_by", "reviewed_at", "notes"
            )
        }
    evidence["fahasa_image_reviews"] = list(reviews.values())
    return evidence, refused


def plan_image_update(image: dict[str, Any], record: dict[str, Any]) -> dict[str, Any] | None:
    """The exact column changes for one non-main image, or None (no-op)."""
    classification = record["classification"]
    if classification == image_rules.FAHASA_REVIEW_APPROVED:
        target = {
            "image_status": ImageStatus.VALIDATED,
            "usage_rights_status": record["rights_status"],
            "is_publish_eligible": True,
            "is_selected_main_image": False,
            "is_main_image_candidate": False,
            "image_role": record["image_kind"],
        }
    elif classification == image_rules.FAHASA_REVIEW_REJECTED:
        target = {
            "image_status": ImageStatus.REJECTED,
            "is_publish_eligible": False,
            "is_selected_main_image": False,
        }
    else:
        return None
    changes = {key: value for key, value in target.items() if image.get(key) != value}
    return changes or None


def _load(repository: Any, candidate_code: str) -> dict[str, Any]:
    target = collector.resolve_fahasa_target(repository, candidate_code)
    candidate_id = target["candidate"]["candidate_id"]
    target["candidate"] = (
        repository.client.table("product_candidates").select("*").eq("candidate_id", candidate_id).execute().data
    )[0]
    target["images"] = (
        repository.client.table("product_images").select("*").eq("candidate_id", candidate_id).execute().data or []
    )
    return target


def run_review(
    repository: Any,
    candidate_code: str,
    verdicts: list[dict[str, Any]],
    dry_run: bool,
) -> dict[str, Any]:
    report: dict[str, Any] = {"candidate_code": candidate_code, "result": None}
    try:
        target = _load(repository, candidate_code)
    except RuntimeError as error:
        report.update(result=LOCAL_NOT_READY, detail=str(error))
        return report

    candidate, reference = target["candidate"], target["reference"]
    fahasa_images = [image for image in target["images"] if image.get("source_type") == "FAHASA"]
    own_verdicts = [
        verdict for verdict in verdicts
        if not isinstance(verdict, dict) or verdict.get("candidate_code") in (candidate_code, None, "")
    ]
    evidence, refused = merge_verdicts(candidate.get("source_evidence") or {}, own_verdicts, fahasa_images)
    rules_candidate = {**candidate, "source_evidence": evidence}

    duplicates = image_rules.find_duplicate_images(target["images"])
    records = []
    for image in fahasa_images:
        linked = reference if str(image.get("reference_id")) == str(reference["reference_id"]) else None
        records.append(
            image_rules.review_fahasa_image(image, linked, rules_candidate, duplicates.get(str(image["image_id"])))
        )
    record_by_id = {str(record["image_id"]): record for record in records}

    mains = [image for image in target["images"] if image.get("is_selected_main_image") is True]
    main = mains[0] if len(mains) == 1 else None
    main_ready = bool(
        main
        and main.get("image_status") == ImageStatus.VALIDATED
        and main.get("is_publish_eligible") is True
        and main.get("usage_rights_status") in PUBLISHABLE_RIGHTS_STATUSES
    )
    main_record = record_by_id.get(str(main["image_id"])) if main else None

    if main_record and main_record["classification"] == image_rules.FAHASA_REVIEW_REJECTED:
        result = MAIN_COVER_FAILED
    elif not main_ready:
        result = NO_APPROVED_MAIN
    else:
        result = REVIEWED

    planned = []
    if result == REVIEWED:
        for image in fahasa_images:
            if main and str(image["image_id"]) == str(main["image_id"]):
                continue
            changes = plan_image_update(image, record_by_id[str(image["image_id"])])
            if changes:
                planned.append({"image_id": image["image_id"], "changes": changes})

    counts: dict[str, int] = {}
    for record in records:
        counts[record["classification"]] = counts.get(record["classification"], 0) + 1
    report.update(
        result=DRY_RUN if dry_run else result,
        outcome=result,
        counts=counts,
        main_image_id=main.get("image_id") if main else None,
        refused_verdicts=refused,
        planned_updates=planned,
        records=records,
    )
    if dry_run:
        return report

    reviewed_at = utc_now()
    evidence["fahasa_image_review_records"] = [
        {**record, "reviewed_at": reviewed_at, "reviewer": f"{REVIEWER_NAME}/{REVIEWER_VERSION}"}
        for record in records
    ]
    response = (
        repository.client.table("product_candidates")
        .update({"source_evidence": evidence, "updated_at": reviewed_at})
        .eq("candidate_id", candidate["candidate_id"]).execute()
    )
    if len(response.data or []) != 1:
        raise RuntimeError("source_evidence update did not affect exactly one candidate.")

    for update in planned:
        rows = (
            repository.client.table("product_images")
            .update({**update["changes"], "updated_at": reviewed_at})
            .eq("candidate_id", candidate["candidate_id"]).eq("image_id", update["image_id"]).execute().data
            or []
        )
        if len(rows) != 1:
            raise RuntimeError(f"Image update did not affect exactly one image: {update['image_id']}")
    report["applied_updates"] = len(planned)
    return report


def load_verdicts(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise RuntimeError("--visual-reviews must be a JSON list of verdict objects.")
    return data


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review collected Fahasa images and add approved ones to the gallery.")
    parser.add_argument("--candidate-code", action="append", required=True, help="Exact candidate code. Repeat.")
    parser.add_argument("--max-candidates", type=int, required=True)
    parser.add_argument("--visual-reviews", type=Path, default=None, help="JSON list of persisted visual verdicts.")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--confirm-apply", action="store_true", help="Required to write; otherwise a dry run.")
    args = parser.parse_args(argv)
    if not 1 <= args.max_candidates <= MAX_CANDIDATES_HARD_CAP:
        parser.error(f"--max-candidates must be between 1 and {MAX_CANDIDATES_HARD_CAP}.")
    if len(set(args.candidate_code)) != len(args.candidate_code):
        parser.error("--candidate-code values must be unique.")
    if len(args.candidate_code) > args.max_candidates:
        parser.error("More --candidate-code values than --max-candidates.")
    return args


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_arguments()
    verdicts = load_verdicts(args.visual_reviews)

    from src.repositories.supabase_repository import SupabaseRepository

    repository = SupabaseRepository()
    for candidate_code in args.candidate_code:  # serial: one candidate at a time
        report = run_review(repository, candidate_code, verdicts, dry_run=not args.confirm_apply)
        print(f"FAHASA_GALLERY_REVIEW_RESULT {json.dumps(report, ensure_ascii=False, default=str)}")


if __name__ == "__main__":
    main()
