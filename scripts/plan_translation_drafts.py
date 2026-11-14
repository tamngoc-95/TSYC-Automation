"""
Read-only dry run of the EN/DE draft flow for TSYC's RECORDED VI products
(src/domain/translation_draft_plan.py, docs/POLYLANG_BRIDGE.md).

Supabase SELECT and a WooCommerce catalog GET only. It writes nothing to either,
creates and links nothing, and generates no content.

Exit code: 0 report written, 2 a read failed (never a clean result).

Usage:
  .venv/Scripts/python.exe scripts/plan_translation_drafts.py [--json-out PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain import translation_book_input as tbi  # noqa: E402
from src.domain import translation_draft_plan as tdp  # noqa: E402

import create_woocommerce_draft as cwd  # noqa: E402  (renderer only)

configure_utf8_console()

REPORT_DIR = PROJECT_ROOT / "data" / "output" / "language_mapping" / "plan"
CATALOG_FIELDS = "id,sku,status,manage_stock,stock_quantity,backorders,regular_price,sale_price"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only dry run of the EN/DE draft flow.")
    parser.add_argument("--json-out", type=Path, help="Report path (default under data/output/language_mapping/plan/).")
    args = parser.parse_args(argv)

    load_dotenv(PROJECT_ROOT / ".env")
    from src.repositories.supabase_repository import SupabaseRepository
    import audit_woo_remote_status as wrs_cli
    import discover_translation_candidates as discovery

    try:
        repository = SupabaseRepository()
        rows = discovery.select_recorded_vi(discovery.load_rows(repository), None)
        contents: dict[str, list[dict]] = {}
        for content in repository.client.table("product_contents").select(
                ", ".join(tbi.CONTENT_COLUMNS)).execute().data or []:
            contents.setdefault(str(content["internal_product_id"]), []).append(content)
        reader = wrs_cli.build_reader(os.environ)
        catalog: dict[int, dict] = {}
        page, pages = 1, 1
        while page <= pages:
            products, total = reader.list_page({"status": "any", "per_page": 100, "page": page, "orderby": "id",
                                                "order": "asc", "_fields": CATALOG_FIELDS})
            pages = max(pages, total)
            catalog.update({int(p["id"]): p for p in products})
            page += 1
    except Exception as error:  # noqa: BLE001 - a failed read is never a clean result
        print(f"Read failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 2

    plans = [tdp.plan_book(row, catalog.get(int(row.woocommerce_product_id or 0)), contents.get(row.internal_product_id, []))
             for row in rows]
    # Would the runner get a valid input from TSYC's own records? Independent
    # of the Woo-side blocker above: both must clear before any create.
    inputs = [tbi.build_book_input(row, contents.get(row.internal_product_id, []), cwd.render_content) for row in rows]
    details = [{**plan.as_dict(), "content_input": {"ready": prepared.ready, "refusal": prepared.refusal,
                                                     "detail": prepared.detail}}
               for plan, prepared in zip(plans, inputs)]
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), **tdp.summarize(plans),
              "content_input": tbi.summarize(inputs), "books_detail": details}
    path = args.json_out or REPORT_DIR / f"plan_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("checked_at", "books", "by_blocker", "all_languages_approved",
                                             "content_input", "creates_possible_today")},
                     ensure_ascii=False, indent=1))
    print("Nothing was created, linked or modified.")
    print(f"JSON report: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
