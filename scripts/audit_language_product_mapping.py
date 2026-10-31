"""
Read-only audit of the per-language WooCommerce product mapping
(src/domain/language_product_mapping.py,
migrations/011_create_woocommerce_language_products.sql).

What it does (no database write, no WooCommerce write, ever):
  1. Reads internal_products and woocommerce_product_syncs and derives the
     VI mapping each sync row implies. Only TSYC's own stored create request
     counts as ownership; remote meta is never used to create a record.
  2. Reads woocommerce_language_products when the migration has been
     applied. It validates the stored rows and compares the VI rows with the
     derived ones. A missing table is reported, not an error.
  3. With --remote, reads the whole catalog (GET, status any, with
     meta_data) and verifies every mapping. Findings include copied
     ownership meta and unmapped copies sharing the SKU. A shared SKU is never
     adopted as a translation link.
  4. --plan-backfill lists the VI rows a future writer would insert (dry
     run, nothing written).
  5. Names every book without a stored VI row and why it is blocked
     (e.g. BLOCKED_COPIED_OWNERSHIP_META). Naming a block never clears or
     downgrades its finding.

Exit code: 0 no ERROR finding, 1 at least one ERROR, 2 a read failed.

Usage:
  .venv/Scripts/python.exe scripts/audit_language_product_mapping.py
  .venv/Scripts/python.exe scripts/audit_language_product_mapping.py --remote --plan-backfill
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain import language_product_mapping as lpm  # noqa: E402

configure_utf8_console()

AUDITOR_NAME = "language_product_mapping_audit"
AUDITOR_VERSION = "1.1.0"
REPORT_DIR = PROJECT_ROOT / "data" / "output" / "language_mapping"
CATALOG_FIELDS = "id,sku,status,permalink,date_modified_gmt,meta_data"
SYNC_COLUMNS = (
    "sync_id, internal_product_id, woocommerce_product_id, woocommerce_status, product_sku, "
    "request_payload, response_payload"
)
TABLE_MISSING = "NOT_APPLIED"
TABLE_PRESENT = "PRESENT"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rows(query: Any) -> list[dict[str, Any]]:
    return query.execute().data or []


def _is_missing_table(error: Exception) -> bool:
    text = str(error)
    return lpm.TABLE in text and ("PGRST205" in text or "does not exist" in text or "Could not find" in text)


def load_local(repository: Any) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    products = _rows(repository.client.table("internal_products").select("internal_product_id, product_code"))
    syncs = _rows(repository.client.table("woocommerce_product_syncs").select(SYNC_COLUMNS))
    return syncs, {str(p["internal_product_id"]): p for p in products}


def load_stored(repository: Any, products: Mapping[str, Mapping[str, Any]]) -> tuple[str, list[lpm.LanguageProduct]]:
    try:
        rows = _rows(repository.client.table(lpm.TABLE).select("*"))
    except Exception as error:  # noqa: BLE001
        if _is_missing_table(error):
            return TABLE_MISSING, []
        raise
    stored = [lpm.LanguageProduct.from_row(r, str((products.get(str(r["internal_product_id"])) or {})
                                                   .get("product_code") or "")) for r in rows]
    return TABLE_PRESENT, stored


def derive(syncs: Sequence[Mapping[str, Any]], products: Mapping[str, Mapping[str, Any]]
           ) -> tuple[list[lpm.LanguageProduct], list[lpm.Finding]]:
    derived: list[lpm.LanguageProduct] = []
    findings: list[lpm.Finding] = []
    for sync in sorted(syncs, key=lambda s: str(s.get("sync_id"))):
        row, row_findings = lpm.derive_vi_mapping(sync, products.get(str(sync.get("internal_product_id"))))
        findings.extend(row_findings)
        if row is not None:
            derived.append(row)
    return derived, findings


def read_catalog(reader: Any) -> dict[int, dict[str, Any]]:
    """Every product with status any (trash excluded by WooCommerce)."""
    catalog: dict[int, dict[str, Any]] = {}
    page, pages = 1, 1
    while page <= pages:
        products, total = reader.list_page({"status": "any", "per_page": 100, "page": page,
                                            "orderby": "id", "order": "asc", "_fields": CATALOG_FIELDS})
        pages = max(pages, total)
        for product in products:
            catalog[int(product["id"])] = product
        page += 1
    return catalog


def build_report(*, table_state: str, derived: Sequence[lpm.LanguageProduct],
                 stored: Sequence[lpm.LanguageProduct], findings: Sequence[lpm.Finding],
                 catalog_size: int | None, plan_backfill: bool) -> dict[str, Any]:
    severities = collections.Counter(f.severity for f in findings)
    report: dict[str, Any] = {
        "auditor": f"{AUDITOR_NAME}/{AUDITOR_VERSION}",
        "checked_at": utc_now(),
        "table": lpm.TABLE,
        "table_state": table_state,
        "derived_vi_mappings": len(derived),
        "derived_by_status": dict(collections.Counter(r.mapping_status for r in derived)),
        "derived_with_baseline": sum(1 for r in derived if r.baseline_source),
        "stored_rows": len(stored),
        "stored_by_language": dict(collections.Counter(r.language_code for r in stored)),
        "remote_catalog_products": catalog_size,
        "severity_counts": dict(severities),
        "finding_counts": dict(collections.Counter(f"{f.severity} {f.code}" for f in findings)),
        "findings": [f.as_dict() for f in findings],
        "blocked_books": lpm.blocked_books(derived, stored, findings),
    }
    if plan_backfill:
        stored_keys = {(r.internal_product_id, r.language_code) for r in stored}
        report["backfill_plan"] = [
            {"product_code": r.product_code, **r.as_row()}
            for r in derived if (r.internal_product_id, r.language_code) not in stored_keys
        ]
    return report


def run(repository: Any, *, reader: Any | None, plan_backfill: bool) -> dict[str, Any]:
    syncs, products = load_local(repository)
    derived, findings = derive(syncs, products)
    findings.extend(lpm.validate_mappings(derived))
    table_state, stored = load_stored(repository, products)
    if table_state == TABLE_PRESENT:
        findings.extend(lpm.validate_mappings(stored))
        findings.extend(lpm.compare_stored_vi(stored, derived))
    catalog_size = None
    if reader is not None:
        catalog = read_catalog(reader)
        catalog_size = len(catalog)
        # Stored rows are the record; a derived VI row not stored (pending or
        # isolated by the backfill) is still checked, so its conflict stays visible.
        stored_keys = {(r.internal_product_id, r.language_code) for r in stored}
        unstored = [r for r in derived if (r.internal_product_id, r.language_code) not in stored_keys]
        findings.extend(lpm.verify_remote(list(stored) + unstored, catalog))
    return build_report(table_state=table_state, derived=derived, stored=stored, findings=findings,
                        catalog_size=catalog_size, plan_backfill=plan_backfill)


def print_summary(report: Mapping[str, Any], path: Path) -> None:
    print("=" * 78)
    print("TSYC LANGUAGE PRODUCT MAPPING AUDIT (read-only)")
    print("=" * 78)
    print(f"Checked at: {report['checked_at']}")
    print(f"Table {report['table']}: {report['table_state']} ({report['stored_rows']} rows "
          f"{report['stored_by_language']})")
    print(f"VI mappings derived from sync rows: {report['derived_vi_mappings']} {report['derived_by_status']}, "
          f"{report['derived_with_baseline']} with a create baseline")
    if report["remote_catalog_products"] is not None:
        print(f"Remote catalog read: {report['remote_catalog_products']} products (GET only)")
    for key, count in sorted(report["finding_counts"].items()):
        print(f"  {key:<45} {count}")
    for finding in report["findings"]:
        if finding["severity"] == lpm.ERROR:
            print(f"- ERROR {finding['code']} {finding['product_code']} {finding['language']}: {finding['detail']}")
    if report["blocked_books"]:
        print("Books without a stored VI row (blocked; findings above stay counted):")
        for book in report["blocked_books"]:
            print(f"  {book['product_code']}: {book['disposition']} {book['finding_codes']}")
    if "backfill_plan" in report:
        print(f"Backfill plan (dry run, nothing written): {len(report['backfill_plan'])} VI row(s).")
    print(f"JSON report: {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only audit of the per-language Woo product mapping.")
    parser.add_argument("--remote", action="store_true", help="Also read the Woo catalog (GET only).")
    parser.add_argument("--plan-backfill", action="store_true", help="List VI rows a writer would insert.")
    parser.add_argument("--json-out", type=Path, help="Report path (default under data/output/language_mapping/).")
    args = parser.parse_args(argv)

    load_dotenv(PROJECT_ROOT / ".env")
    from src.repositories.supabase_repository import SupabaseRepository

    reader = None
    if args.remote:
        import audit_woo_remote_status as wrs_cli
        reader = wrs_cli.build_reader(os.environ)
    try:
        report = run(SupabaseRepository(), reader=reader, plan_backfill=args.plan_backfill)
    except Exception as error:  # noqa: BLE001 - a failed read is never a clean result
        print(f"Read failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    path = args.json_out or REPORT_DIR / f"language_mapping_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print_summary(report, path)
    return 1 if report["severity_counts"].get(lpm.ERROR) else 0


if __name__ == "__main__":
    sys.exit(main())
