"""
READ-ONLY audit of the whole catalog's Polylang translation groups
(src/domain/polylang_link_graph.py, docs/POLYLANG_EXISTING_ARCHITECTURE.md).

1. GETs every product (status=any, limited fields) from wc/v3.
2. Reads the public page of every published product (unauthenticated GET,
   store host only, polite delay) and parses its hreflang links. Pages
   are cached in data/output/polylang_catalog/page_links.json so a rerun
   only fetches what is missing (--refresh refetches everything).
3. Builds translation groups from links that agree with each other and
   reports conflicts, missing languages, per-field drift (price, stock,
   backorders, SKU, featured image) and sibling-save coincidences.

Never writes to WooCommerce, WordPress or Supabase. No price, stock,
status or link is changed. Reads no orders and no customer data.

Usage:
  .venv/Scripts/python.exe scripts/audit_polylang_catalog.py
  ... --max-pages 50 --json-out data/output/polylang_catalog/report.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import create_woocommerce_draft as cwd  # noqa: E402
from audit_woo_translation_groups import fetch_all_products  # noqa: E402
from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain import polylang_link_graph as plg  # noqa: E402
from src.domain import woo_translation_group as wtg  # noqa: E402

configure_utf8_console()

AUDITOR_NAME = "polylang_catalog_audit"
AUDITOR_VERSION = "1.0.0"
PAGE_SIZE = 100
FIELDS = ",".join((
    "id", "sku", "status", "permalink", "name", "slug", "date_created_gmt", "date_modified_gmt",
    "regular_price", "sale_price", "manage_stock", "stock_quantity", "backorders", "stock_status",
    "total_sales", "images",
))
CACHE_FILE = PROJECT_ROOT / "data" / "output" / "polylang_catalog" / "page_links.json"
DEFAULT_MAX_PAGES = 2500
PAGE_DELAY_SECONDS = 0.3


def read_page_links(
    products: list[dict[str, Any]],
    read_page: Callable[[str], str | None],
    cache: dict[str, Any],
    max_pages: int,
    checkpoint: Callable[[], None] = lambda: None,
) -> tuple[dict[int, dict[str, str] | None], int]:
    """{product id: hreflang links or None} for every published product,
    from the cache where possible. Returns (links, pages fetched now).
    checkpoint() runs every 100 fetches so an interrupted crawl resumes."""
    links: dict[int, dict[str, str] | None] = {}
    fetched = 0
    for product in products:
        if product.get("status") != "publish":
            continue
        permalink = str(product.get("permalink") or "")
        cached = cache.get(permalink)
        if cached is not None:
            links[int(product["id"])] = cached["links"]
            continue
        if fetched >= max_pages:
            links[int(product["id"])] = None
            continue
        fetched += 1
        page_html = read_page(permalink)
        parsed = None if page_html is None else wtg.parse_hreflang_links(page_html)
        if parsed is not None:
            cache[permalink] = {"links": parsed, "fetched_at": datetime.now(timezone.utc).isoformat()}
        links[int(product["id"])] = parsed
        if fetched % 100 == 0:
            checkpoint()
    return links, fetched


def build_report(products: list[dict[str, Any]], links: dict[int, dict[str, str] | None]) -> dict[str, Any]:
    groups, unlinked = plg.build_groups(products, links)
    by_id = {int(p["id"]): p for p in products}
    report = plg.summarize(groups, unlinked, products)
    report.update({
        "auditor": f"{AUDITOR_NAME}/{AUDITOR_VERSION}",
        "products_read": len(products),
        "published_pages": sum(1 for p in products if p.get("status") == "publish"),
        "pages_unread": sum(1 for v in links.values() if v is None),
        "groups_detail": [
            {
                "status": g.status,
                "members": {lang: {"id": pid, "sku": by_id[pid].get("sku"), "name": by_id[pid].get("name")}
                            for lang, pid in sorted(g.members.items())},
                "findings": [{"code": f.code, "detail": f.detail} for f in g.findings],
            }
            for g in groups
        ],
        "unlinked_published": [
            {"id": pid, "lang": wtg.product_language(by_id[pid])[0], "sku": by_id[pid].get("sku"),
             "name": by_id[pid].get("name")}
            for pid in unlinked
        ],
        "sibling_save_coincidences": [list(ids) for ids in plg.sibling_save_coincidences(
            [g for g in groups if g.status == plg.GROUP_OK], by_id)],
    })
    return report


def print_report(report: dict[str, Any]) -> None:
    print("=" * 72)
    print("POLYLANG CATALOG AUDIT (read-only)")
    print("=" * 72)
    for key in ("products_read", "published_pages", "pages_unread", "groups", "groups_ok", "groups_conflicted",
                "group_shapes", "unlinked_published_by_language", "field_in_sync_groups", "field_drift_groups",
                "sibling_save_coincidence_groups"):
        print(f"{key}: {report[key]}")
    conflicted = [g for g in report["groups_detail"] if g["status"] != plg.GROUP_OK]
    if conflicted:
        print()
        print("Conflicted groups:")
        for group in conflicted[:40]:
            members = ", ".join(f"{lang}#{m['id']}" for lang, m in group["members"].items())
            print(f"  [{members}]")
            for finding in group["findings"]:
                print(f"     {finding['code']}: {finding['detail']}")
    print()
    print("No product, link, price or stock was created or modified by this audit.")


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only audit of the catalog's Polylang translation groups.")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES,
                        help=f"Storefront pages fetched in this run (default {DEFAULT_MAX_PAGES}; cached pages are free).")
    parser.add_argument("--refresh", action="store_true", help="Ignore the page cache and refetch.")
    parser.add_argument("--json-out", type=Path, help="Also write the report as JSON (keep it out of Git).")
    return parser.parse_args(argv)


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_arguments()
    store_url = cwd.get_required_environment_variable("WOOCOMMERCE_URL")
    if not store_url.startswith("https://"):
        raise RuntimeError("WOOCOMMERCE_URL must use HTTPS.")
    api_version = cwd.os.getenv("WOOCOMMERCE_API_VERSION", "wc/v3").strip()
    auth = HTTPBasicAuth(
        cwd.get_required_environment_variable("WOOCOMMERCE_CONSUMER_KEY"),
        cwd.get_required_environment_variable("WOOCOMMERCE_CONSUMER_SECRET"),
    )
    timeout = cwd.get_timeout_seconds()
    headers = {"Accept": "application/json", "User-Agent": f"{AUDITOR_NAME}/{AUDITOR_VERSION}"}
    store_host = urlparse(store_url).netloc.lower()

    def get_page(page: int) -> list[dict[str, Any]]:
        response = requests.get(
            cwd.build_api_url(store_url, api_version, "products"),
            auth=auth, headers=headers, timeout=timeout,
            params={"per_page": PAGE_SIZE, "page": page, "status": "any",
                    "orderby": "id", "order": "asc", "_fields": FIELDS},
        )
        if response.status_code != 200 or not isinstance(response.json(), list):
            raise RuntimeError(f"Product read failed on page {page} (HTTP {response.status_code}).")
        return response.json()

    def read_page(permalink: str) -> str | None:
        # Public page only: never send the API credentials, never leave the store host.
        parsed = urlparse(permalink)
        if parsed.scheme != "https" or parsed.netloc.lower() != store_host:
            return None
        time.sleep(PAGE_DELAY_SECONDS)
        try:
            response = requests.get(permalink, headers=headers, timeout=timeout, allow_redirects=False)
        except requests.RequestException:
            return None
        return response.text if response.status_code == 200 else None

    products = fetch_all_products(get_page)
    cache: dict[str, Any] = {}
    if CACHE_FILE.exists() and not args.refresh:
        cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

    def save_cache() -> None:
        CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    links, fetched = read_page_links(products, read_page, cache, args.max_pages, save_cache)
    save_cache()
    print(f"Storefront pages fetched now: {fetched} (cache: {CACHE_FILE})")

    report = build_report(products, links)
    print_report(report)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"JSON report: {args.json_out}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, requests.RequestException) as error:
        print(f"Polylang catalog audit failed: {type(error).__name__}: {error}")
        sys.exit(1)
