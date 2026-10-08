"""
Clear stored "ISBN" values that are not ISBNs (bounded, logged, reversible).

Background (2026-10-08): collect_reference_metadata.extract_isbn() used to
accept any 10/13-digit value, so retailer store codes (NetaBooks
"2421762043452") were stored as product_references.reference_isbn and
copied into internal_products.isbn -- invented metadata under CLAUDE.md
2.2/2.3. The parser is fixed; this script removes the existing values.

For each exact --product-code (or every product with an invalid isbn when
--all-invalid is given, bounded by --max-products):

  internal_products.isbn                      -> NULL if not a valid ISBN
  product_references.reference_isbn (same
  candidate)                                  -> NULL if not a valid ISBN

Never touches a value that looks_like_valid_isbn(), never writes a new
ISBN, never touches product_candidates.verified_isbn (verified identity
fields are reported for human review instead), product_contents, or
WooCommerce. Each change is logged to process_logs with the previous value
(process_name = isbn_correction), so it is fully reversible.

Dry run unless --confirm-clear is given.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain.rules.identity_rules import looks_like_valid_isbn  # noqa: E402
from src.repositories.supabase_repository import SupabaseRepository  # noqa: E402

PROCESS_NAME = "isbn_correction"


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--product-code", action="append", help="Exact product code. Repeatable.")
    target.add_argument("--all-invalid", action="store_true", help="Every product whose isbn is not a valid ISBN.")
    parser.add_argument("--max-products", type=int, required=True, help="Hard upper bound on products changed.")
    parser.add_argument("--confirm-clear", action="store_true", help="Write. Without it: dry run.")
    return parser.parse_args(argv)


def is_invalid(value: Any) -> bool:
    return value not in (None, "") and not looks_like_valid_isbn(str(value))


def plan_corrections(repository: SupabaseRepository, args: argparse.Namespace) -> list[dict[str, Any]]:
    query = repository.client.table("internal_products").select(
        "internal_product_id, candidate_id, product_code, isbn, woocommerce_status"
    )
    if args.product_code:
        query = query.in_("product_code", args.product_code)
    products = [p for p in (query.execute().data or []) if is_invalid(p.get("isbn"))]

    if args.product_code:
        missing = set(args.product_code) - {p["product_code"] for p in products}
        for code in sorted(missing):
            print(f"SKIP {code}: no invalid isbn stored (already valid, empty, or not found).")

    if len(products) > args.max_products:
        raise RuntimeError(
            f"{len(products)} products have an invalid isbn, above --max-products {args.max_products}."
        )

    plans = []
    for product in sorted(products, key=lambda p: p["product_code"]):
        references = (
            repository.client.table("product_references")
            .select("reference_id, reference_isbn, source_type")
            .eq("candidate_id", product["candidate_id"])
            .execute().data or []
        )
        candidate = (
            repository.client.table("product_candidates")
            .select("candidate_code, verified_isbn")
            .eq("candidate_id", product["candidate_id"])
            .execute().data or [{}]
        )[0]
        plans.append({
            "product": product,
            "references": [r for r in references if is_invalid(r.get("reference_isbn"))],
            "candidate_code": candidate.get("candidate_code"),
            "verified_isbn_invalid": is_invalid(candidate.get("verified_isbn")),
        })

    if args.all_invalid:
        # References of candidates that never got an internal product.
        planned = {r["reference_id"] for plan in plans for r in plan["references"]}
        orphans = [
            r for r in (
                repository.client.table("product_references")
                .select("reference_id, candidate_id, reference_isbn, source_type")
                .execute().data or []
            )
            if is_invalid(r.get("reference_isbn")) and r["reference_id"] not in planned
        ]
        for reference in orphans:
            plans.append({
                "product": None,
                "candidate_id": reference["candidate_id"],
                "references": [reference],
                "candidate_code": None,
                "verified_isbn_invalid": False,
            })
        if len(orphans) > args.max_products:
            raise RuntimeError(
                f"{len(orphans)} product-less references to clear, above --max-products {args.max_products}."
            )
    return plans


def apply_plan(repository: SupabaseRepository, plan: dict[str, Any]) -> None:
    product = plan["product"]
    label = product["product_code"] if product else f"candidate {plan['candidate_id']}"
    # Previous values captured before any write (the provenance record).
    previous_isbn = product["isbn"] if product else None
    previous_references = {str(r["reference_id"]): r["reference_isbn"] for r in plan["references"]}
    if product:
        updated = (
            repository.client.table("internal_products").update({"isbn": None})
            .eq("internal_product_id", product["internal_product_id"])
            .eq("isbn", previous_isbn)  # exact bounded target: unchanged since planning
            .execute().data or []
        )
        if len(updated) != 1:
            raise RuntimeError(f"{label}: internal_products update did not affect exactly one row.")

    for reference_id, previous_value in previous_references.items():
        rows = (
            repository.client.table("product_references").update({"reference_isbn": None})
            .eq("reference_id", reference_id)
            .eq("reference_isbn", previous_value)
            .execute().data or []
        )
        if len(rows) != 1:
            raise RuntimeError(f"{label}: reference {reference_id} update failed.")

    repository.write_process_log(
        message=f"Cleared non-ISBN value(s) stored as ISBN for {label}.",
        process_name=PROCESS_NAME,
        candidate_id=product["candidate_id"] if product else plan["candidate_id"],
        process_step="CLEAR_INVALID_ISBN",
        log_level="INFO",
        status="CLEARED",
        error_details={
            "product_code": product["product_code"] if product else None,
            "previous_internal_products_isbn": previous_isbn,
            "previous_reference_isbn": previous_references,
            "reason": "Not a 978/979 ISBN-13 or ISBN-10 (retailer code or barcode); CLAUDE.md 2.2/2.3.",
        },
    )


def main(argv: list[str] | None = None, repository: SupabaseRepository | None = None) -> list[dict[str, Any]]:
    args = parse_arguments(argv)
    repository = repository or SupabaseRepository()
    plans = plan_corrections(repository, args)

    for plan in plans:
        product = plan["product"] or {
            "product_code": f"(no product; candidate {plan['candidate_id']})",
            "woocommerce_status": "-", "isbn": None,
        }
        print(
            f"{'CLEAR' if args.confirm_clear else 'WOULD CLEAR'} {product['product_code']} "
            f"[{product['woocommerce_status']}] isbn={product['isbn']} "
            f"references={[r['reference_isbn'] for r in plan['references']]}"
            + ("  (candidate verified_isbn is also invalid: human review)" if plan["verified_isbn_invalid"] else "")
        )
        if args.confirm_clear:
            apply_plan(repository, plan)

    print(f"{'Cleared' if args.confirm_clear else 'Dry run'}: {len(plans)} product(s).")
    return plans


if __name__ == "__main__":
    configure_utf8_console()
    load_dotenv(PROJECT_ROOT / ".env")
    try:
        main()
    except Exception as error:
        print(f"ISBN correction failed: {type(error).__name__}: {error}")
        sys.exit(1)
