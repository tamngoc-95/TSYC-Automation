"""
Repair one internal product whose author is a captured UI label (e.g. the
publisher page's "Đọc thử" button) -- never a person.

For exactly one --product-code:
  1. refuses unless internal_products.author is invalid
     (author_rules.is_invalid_author_value) -- a real author is never
     touched (CLAUDE.md 2.7)
  2. resolves the author ONLY from deterministic registered-reference
     evidence (author_rules.resolve_author_from_references: same base
     title, same publisher, valid author, all qualifying references agree)
  3. resolved  -> writes internal_products.author; the previous value and
                  the evidence are kept in product_metadata.author_repair
     unresolved -> clears the invalid value (a UI label is never an
                  author), flags review_required with the reason
  4. writes one process_logs row

product_references rows are never modified (source provenance is kept
verbatim, including the defective extracted value).

Usage:
  .venv/Scripts/python.exe scripts/repair_product_author.py --product-code X            (dry run)
  .venv/Scripts/python.exe scripts/repair_product_author.py --product-code X --confirm-repair
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
from src.domain.rules import author_rules  # noqa: E402
from src.repositories.supabase_repository import SupabaseRepository  # noqa: E402

configure_utf8_console()

TOOL_NAME = "repair_product_author"
TOOL_VERSION = "1.0.0"

RESOLVED = "AUTHOR_RESOLVED"
UNRESOLVED = "AUTHOR_UNRESOLVED_REVIEW"
NOT_DEFECTIVE = "AUTHOR_NOT_DEFECTIVE"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def plan_author_repair(
    product: dict[str, Any],
    references: list[dict[str, Any]],
    now: str,
) -> dict[str, Any]:
    """Pure: {"outcome", "author", "evidence", "update"}; update is the
    exact internal_products payload (None when nothing may be written)."""
    if not author_rules.is_invalid_author_value(product.get("author")):
        return {"outcome": NOT_DEFECTIVE, "author": product.get("author"), "evidence": {}, "update": None}

    author, evidence = author_rules.resolve_author_from_references(product, references)
    metadata = dict(product.get("product_metadata") or {})
    metadata["author_repair"] = {
        "tool": f"{TOOL_NAME}/{TOOL_VERSION}",
        "repaired_at": now,
        "previous_author": product.get("author"),
        "new_author": author,
        "evidence": evidence,
    }
    update: dict[str, Any] = {"author": author, "product_metadata": metadata, "updated_at": now}

    if author is None:
        update["review_required"] = True
        update["review_reason"] = (
            f"Author value {product.get('author')!r} was a captured page label and was "
            f"cleared; the real author could not be established deterministically "
            f"({evidence.get('reason')})."
        )
        return {"outcome": UNRESOLVED, "author": None, "evidence": evidence, "update": update}

    return {"outcome": RESOLVED, "author": author, "evidence": evidence, "update": update}


def run(repository: SupabaseRepository, product_code: str, confirm: bool) -> dict[str, Any]:
    products = (
        repository.client.table("internal_products")
        .select("internal_product_id, candidate_id, product_code, title, author, publisher, product_metadata")
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
        .select(
            "reference_id, source_url_id, source_type, match_decision, "
            "reference_title, reference_author, reference_publisher"
        )
        .eq("candidate_id", product["candidate_id"])
        .execute()
        .data
        or []
    )

    plan = plan_author_repair(product, references, utc_now())
    report = {
        "product_code": product_code,
        "outcome": plan["outcome"],
        "previous_author": product.get("author"),
        "author": plan["author"],
        "evidence": plan["evidence"],
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
        raise RuntimeError("Author repair did not update exactly one internal product.")
    report["written"] = True

    try:
        repository.write_process_log(
            message=f"Author repair for {product_code}: {plan['outcome']} ({product.get('author')!r} -> {plan['author']!r}).",
            process_name=TOOL_NAME,
            candidate_id=product.get("candidate_id"),
            process_step="REPAIR_AUTHOR",
            log_level="INFO" if plan["outcome"] == RESOLVED else "WARNING",
            status=plan["outcome"],
            error_details={"previous_author": product.get("author"), "evidence": plan["evidence"]},
        )
    except Exception as error:  # audit failure must not mask the write
        print(f"Warning: process_logs entry failed: {type(error).__name__}: {error}")

    return report


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(description="Repair one product's invalid (UI-label) author.")
    parser.add_argument("--product-code", required=True)
    parser.add_argument("--confirm-repair", action="store_true", help="Write; otherwise dry run.")
    args = parser.parse_args()
    report = run(SupabaseRepository(), args.product_code, args.confirm_repair)
    print(f"AUTHOR_REPAIR_RESULT {json.dumps(report, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
