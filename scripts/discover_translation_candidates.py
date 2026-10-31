"""
Read-only EN/DE translation discovery for TSYC's RECORDED VI products
(src/domain/translation_discovery.py, docs/POLYLANG_EXISTING_ARCHITECTURE.md).

What it does (no database write, no WooCommerce/WordPress write, ever):
  1. Reads the RECORDED vi rows of woocommerce_language_products (SELECT).
  2. Reads the whole catalog over wc/v3 (GET, status any, with meta_data).
  3. Reads the public storefront page of each published VI product, each
     SKU copy, and every product those pages link (unauthenticated GET, store
     host only, no redirects, polite delay), and parses the hreflang links.
  4. Classifies every EN/DE candidate as VERIFIED_LINK, POTENTIAL_LINK,
     OWNER_MAINTAINED, UNVERIFIABLE or IDENTITY_CONFLICT.

No mapping row is planned unless a staging-proven Polylang map confirms the
link (VERIFIED_LINK). There is none today, so the plan is always empty.

Exit code: 0 report written, 2 a read failed (never a clean result).

Usage:
  .venv/Scripts/python.exe scripts/discover_translation_candidates.py
  ... --product-code TSYC-... --json-out data/output/language_mapping/discovery/report.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain import language_product_mapping as lpm  # noqa: E402
from src.domain import polylang_link_graph as plg  # noqa: E402
from src.domain import translation_discovery as td  # noqa: E402
from src.domain import woo_translation_group as wtg  # noqa: E402

configure_utf8_console()

DISCOVERER_NAME = "translation_discovery"
DISCOVERER_VERSION = "1.0.0"
REPORT_DIR = PROJECT_ROOT / "data" / "output" / "language_mapping" / "discovery"
CATALOG_FIELDS = "id,sku,status,name,slug,permalink,date_created_gmt,date_modified_gmt,images,meta_data"
PAGE_DELAY_SECONDS = 0.3
DEFAULT_MAX_PAGES = 600
LINK_ROUNDS = 3


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_recorded_vi(repository: Any, product_codes: Sequence[str] | None) -> list[lpm.LanguageProduct]:
    products = repository.client.table("internal_products").select("internal_product_id, product_code").execute().data or []
    codes = {str(p["internal_product_id"]): str(p.get("product_code") or "") for p in products}
    rows = repository.client.table(lpm.TABLE).select("*").execute().data or []
    recorded = [lpm.LanguageProduct.from_row(r, codes.get(str(r["internal_product_id"]), "")) for r in rows]
    recorded = [r for r in recorded if r.language_code == wtg.SOURCE_LANGUAGE and r.mapping_status == lpm.RECORDED
                and r.origin == lpm.ORIGIN_TSYC_CREATED and r.woocommerce_product_id is not None]
    if product_codes:
        wanted = set(product_codes)
        recorded = [r for r in recorded if r.product_code in wanted]
    return sorted(recorded, key=lambda r: r.product_code)


def read_catalog(reader: Any) -> dict[int, dict[str, Any]]:
    """Every product (status any; WooCommerce leaves trash out)."""
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


def read_page_links(
    catalog: Mapping[int, Mapping[str, Any]],
    start_ids: Sequence[int],
    read_page: Callable[[str], str | None],
    max_pages: int,
) -> dict[int, dict[str, str] | None]:
    """hreflang links of the start pages and of every product they link,
    a few rounds deep, so each group is read whole. None = unread."""
    by_url = {wtg.normalise_url(str(p.get("permalink") or "")): pid for pid, p in catalog.items()
              if p.get("status") == "publish" and p.get("permalink")}
    links: dict[int, dict[str, str] | None] = {}
    todo = sorted({pid for pid in start_ids if catalog.get(pid, {}).get("status") == "publish"})
    fetched = 0
    for _round in range(LINK_ROUNDS):
        following: set[int] = set()
        for pid in todo:
            if pid in links:
                continue
            if fetched >= max_pages:
                links[pid] = None
                continue
            fetched += 1
            page_html = read_page(str(catalog[pid].get("permalink") or ""))
            parsed = None if page_html is None else wtg.parse_hreflang_links(page_html)
            links[pid] = parsed
            for url in (parsed or {}).values():
                target = by_url.get(url)
                if target is not None and target not in links:
                    following.add(target)
        todo = sorted(following)
        if not todo:
            break
    return links


def run(rows: Sequence[lpm.LanguageProduct], catalog: Mapping[int, Mapping[str, Any]],
        read_page: Callable[[str], str | None], max_pages: int) -> dict[str, Any]:
    codes = {row.product_code for row in rows}
    start = {int(row.woocommerce_product_id or 0) for row in rows} | {
        pid for pid, p in catalog.items() if str(p.get("sku") or "") in codes and p.get("status") != "trash"}
    page_links = read_page_links(catalog, sorted(start), read_page, max_pages)
    groups, _unlinked = plg.build_groups(list(catalog.values()), page_links)
    books = [td.discover_book(row, catalog, groups, page_links) for row in rows]
    return {
        "discoverer": f"{DISCOVERER_NAME}/{DISCOVERER_VERSION}",
        "checked_at": utc_now(),
        "recorded_vi_rows": len(rows),
        "remote_catalog_products": len(catalog),
        "pages_read": sum(1 for v in page_links.values() if v is not None),
        "pages_unread": sum(1 for v in page_links.values() if v is None),
        "link_authority": "none: no staging-proven Polylang map; hreflang and SKU are supplementary",
        **td.summarize(books),
        "books_detail": [b.as_dict() for b in books],
    }


def print_summary(report: Mapping[str, Any], path: Path) -> None:
    print("=" * 78)
    print("TSYC EN/DE TRANSLATION DISCOVERY (read-only)")
    print("=" * 78)
    for key in ("checked_at", "recorded_vi_rows", "remote_catalog_products", "pages_read", "pages_unread",
                "link_authority", "books_by_outcome", "adapter_status", "candidates", "candidates_by_class",
                "candidates_by_language_class", "mapping_inserts_allowed"):
        print(f"{key}: {report[key]}")
    for book in report["books_detail"]:
        if not book["candidates"]:
            continue
        print(f"- {book['product_code']} VI #{book['vi_woo_id']} ({book['vi_status']}) -> {book['outcome']}")
        for c in book["candidates"]:
            print(f"    {c['language']} #{c['woo_id']} {c['status']} sku={c['sku'] or '-'} "
                  f"[{'+'.join(c['bases'])}] {c['classification']}")
    print("No product, link, mapping row, price or stock was created or modified.")
    print(f"JSON report: {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only EN/DE translation discovery for recorded VI products.")
    parser.add_argument("--product-code", action="append", help="Limit to exact product codes (repeatable).")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES,
                        help=f"Storefront pages fetched (default {DEFAULT_MAX_PAGES}).")
    parser.add_argument("--json-out", type=Path, help="Report path (default under data/output/language_mapping/discovery/).")
    args = parser.parse_args(argv)

    load_dotenv(PROJECT_ROOT / ".env")
    from src.repositories.supabase_repository import SupabaseRepository
    import audit_woo_remote_status as wrs_cli

    store_url = (os.environ.get("WOOCOMMERCE_URL") or "").strip()
    store_host = urlparse(store_url).netloc.lower()
    headers = {"Accept": "text/html", "User-Agent": f"{DISCOVERER_NAME}/{DISCOVERER_VERSION}"}

    def read_page(permalink: str) -> str | None:
        # Public page only: never send the API credentials, never leave the store host.
        parsed = urlparse(permalink)
        if parsed.scheme != "https" or parsed.netloc.lower() != store_host:
            return None
        time.sleep(PAGE_DELAY_SECONDS)
        try:
            response = requests.get(permalink, headers=headers, timeout=30, allow_redirects=False)
        except requests.RequestException:
            return None
        return response.text if response.status_code == 200 else None

    try:
        rows = load_recorded_vi(SupabaseRepository(), args.product_code)
        catalog = read_catalog(wrs_cli.build_reader(os.environ))
        report = run(rows, catalog, read_page, args.max_pages)
    except Exception as error:  # noqa: BLE001 - a failed read is never a clean result
        print(f"Read failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    path = args.json_out or REPORT_DIR / f"discovery_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print_summary(report, path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
