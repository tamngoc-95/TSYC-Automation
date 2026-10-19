"""
Record the verified contributor roles of one internal product
(src.domain.rules.contributor_roles).

For exactly one --product-code:
  1. the evidence URL must be a registered product_reference of this
     candidate (source_url_id present) -- provenance first (CLAUDE.md 10)
  2. every contributor name must be in the verified author field (an
     ORIGINAL_AUTHOR excepted) and appear in the recorded credit text, and
     no author-field name may be left without a role -- never a guess
  3. writes ONLY internal_products.product_metadata.contributor_roles;
     internal_products.author is never rewritten (CLAUDE.md 2.7)
  4. an existing, different record is refused (no silent overwrite); an
     identical one is a no-op
  5. writes one process_logs row

Usage:
  .venv/Scripts/python.exe scripts/record_contributor_roles.py --product-code X \
      --contributor "RETELLER=Hiếu Minh" --contributor "ILLUSTRATOR=Cloud Pillow Studio" \
      --evidence-url URL --evidence-image-url URL \
      --credit-text "Hiếu Minh kể lại | Cloud Pillow Studio minh họa"           (dry run)
  ... --confirm-record                                                          (write)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain.rules import author_rules, contributor_roles  # noqa: E402
from src.repositories.supabase_repository import SupabaseRepository  # noqa: E402

configure_utf8_console()

TOOL_NAME = "record_contributor_roles"
TOOL_VERSION = "1.0.0"

RECORDED = "CONTRIBUTOR_ROLES_RECORDED"
UNCHANGED = "CONTRIBUTOR_ROLES_UNCHANGED"
REFUSED = "CONTRIBUTOR_ROLES_REFUSED"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_contributor_args(values: list[str]) -> list[dict[str, str]]:
    contributors = []
    for value in values:
        role, separator, name = value.partition("=")
        if not separator:
            raise ValueError(f"--contributor must be ROLE=Name, got {value!r}.")
        contributors.append({"role": role.strip().upper(), "name": name.strip()})
    return contributors


def plan_record(
    product: dict[str, Any],
    references: list[dict[str, Any]],
    contributors: list[dict[str, str]],
    evidence: dict[str, Any],
    now: str,
) -> dict[str, Any]:
    """Pure: {"outcome", "reasons", "record", "update"}."""
    try:
        pairs = contributor_roles.normalize_contributors(contributors)
    except ValueError as error:
        return {"outcome": REFUSED, "reasons": [str(error)], "record": None, "update": None}

    reasons: list[str] = []
    reference = next(
        (ref for ref in references if ref.get("source_url") == evidence.get("source_url")),
        None,
    )
    if reference is None:
        reasons.append("Evidence URL is not a registered product_reference of this candidate.")
    elif not reference.get("source_url_id"):
        reasons.append("Evidence reference has no source_url_id (unregistered provenance).")
    reasons += contributor_roles.check_against_evidence(product.get("author"), pairs, evidence.get("credit_text"))
    # A translator must be the one the registered reference itself recorded
    # (Fahasa's "Người Dịch" field), never only an operator-typed credit.
    recorded_translator = author_rules.person_name_key(((reference or {}).get("raw_metadata") or {}).get("translator"))
    for name, role in pairs:
        if role == contributor_roles.TRANSLATOR and author_rules.person_name_key(name) != recorded_translator:
            reasons.append(f"Translator {name!r} does not match the reference's recorded translator.")

    record = {
        "contributors": [{"name": name, "role": role} for name, role in pairs],
        "evidence": {
            **evidence,
            "reference_id": (reference or {}).get("reference_id"),
            "source_url_id": (reference or {}).get("source_url_id"),
            "source_type": (reference or {}).get("source_type"),
            "author_field": product.get("author"),
        },
        "recorded_by": f"{TOOL_NAME}/{TOOL_VERSION}",
        "recorded_at": now,
    }
    if reasons:
        return {"outcome": REFUSED, "reasons": reasons, "record": record, "update": None}

    metadata = dict(product.get("product_metadata") or {})
    existing = contributor_roles.verified_contributors(metadata)
    if existing is not None:
        if existing == pairs:
            return {"outcome": UNCHANGED, "reasons": [], "record": metadata[contributor_roles.METADATA_KEY], "update": None}
        return {
            "outcome": REFUSED,
            "reasons": [f"A different contributor_roles record already exists: {existing}."],
            "record": record,
            "update": None,
        }

    metadata[contributor_roles.METADATA_KEY] = record
    return {"outcome": RECORDED, "reasons": [], "record": record, "update": {"product_metadata": metadata, "updated_at": now}}


def run(
    repository: SupabaseRepository,
    product_code: str,
    contributors: list[dict[str, str]],
    evidence: dict[str, Any],
    confirm: bool,
) -> dict[str, Any]:
    products = (
        repository.client.table("internal_products")
        .select("internal_product_id, candidate_id, product_code, author, product_metadata")
        .eq("product_code", product_code)
        .limit(2)
        .execute()
        .data
        or []
    )
    if len(products) != 1:
        raise RuntimeError(f"product_code did not resolve to exactly one product: {product_code}")
    product = products[0]
    references = (
        repository.client.table("product_references")
        .select("reference_id, source_url_id, source_type, source_url, match_decision, raw_metadata")
        .eq("candidate_id", product["candidate_id"])
        .execute()
        .data
        or []
    )

    plan = plan_record(product, references, contributors, evidence, utc_now())
    report = {
        "product_code": product_code,
        "outcome": plan["outcome"],
        "reasons": plan["reasons"],
        "contributors": (plan["record"] or {}).get("contributors"),
        "written": False,
    }
    if plan["update"] is None or not confirm:
        return report

    written = (
        repository.client.table("internal_products")
        .update(plan["update"])
        .eq("internal_product_id", product["internal_product_id"])
        .execute()
        .data
        or []
    )
    if len(written) != 1:
        raise RuntimeError("Contributor-role record did not update exactly one internal product.")
    report["written"] = True

    try:
        repository.write_process_log(
            message=f"Contributor roles recorded for {product_code}: {report['contributors']}.",
            process_name=TOOL_NAME,
            candidate_id=product.get("candidate_id"),
            process_step="RECORD_CONTRIBUTOR_ROLES",
            log_level="INFO",
            status=RECORDED,
            error_details={"evidence": plan["record"]["evidence"]},
        )
    except Exception as error:  # audit failure must not mask the write
        print(f"Warning: process_logs entry failed: {type(error).__name__}: {error}")

    return report


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(description="Record one product's verified contributor roles.")
    parser.add_argument("--product-code", required=True)
    parser.add_argument("--contributor", action="append", required=True, help="ROLE=Name; repeatable.")
    parser.add_argument("--evidence-url", required=True, help="Registered reference URL the credit was read from.")
    parser.add_argument("--evidence-image-url", help="Cover/credit image the roles were read from.")
    parser.add_argument("--credit-text", required=True, help="Verbatim credit line, e.g. 'X kể lại | Y minh họa'.")
    parser.add_argument("--confirm-record", action="store_true", help="Write; otherwise dry run.")
    args = parser.parse_args()

    evidence = {
        "source_url": args.evidence_url,
        "image_url": args.evidence_image_url,
        "credit_text": args.credit_text,
    }
    report = run(SupabaseRepository(), args.product_code, parse_contributor_args(args.contributor), evidence, args.confirm_record)
    print(f"CONTRIBUTOR_ROLES_RESULT {json.dumps(report, ensure_ascii=False)}")
    if report["outcome"] == REFUSED:
        sys.exit(2)


if __name__ == "__main__":
    main()
