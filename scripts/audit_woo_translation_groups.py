"""
READ-ONLY audit of TSYC WooCommerce translation groups
(src/domain/woo_translation_group.py, docs/POLYLANG_WOOCOMMERCE_PLAN.md).

GETs every product (status=any, limited fields), keeps TSYC SKUs, groups
them by exact SKU and reports per group:
  ERROR    ambiguous group (duplicate language, no VI source, unknown
           language, broken add-on link) -- TSYC must not reconcile onto it
  WARNING  shared-field drift (price/stock/backorders differ per language),
           an EN/DE copy published while its VI source is not, or
           independent copies that the add-on does not manage

With --check-links it also reads the public product pages of multi-row
groups (bounded, unauthenticated GET, store host only) and compares the
Polylang hreflang links with the SKU group:
  ERROR    a page links a group language to a different product
  WARNING  a missing link, a link to a product without this SKU, a draft
           member whose link cannot be seen, or copies Polylang already
           links (the add-on would start syncing their price and stock)

Never writes to WooCommerce, WordPress or Supabase. No price, stock or
status is changed. Exit code 0 even with findings (it is a report); 1 only
when the catalog cannot be read completely.

Usage:
  .venv/Scripts/python.exe scripts/audit_woo_translation_groups.py
  ... --sku-prefix TSYC-FAHASA- --json-out data/output/translation_groups.json
  ... --check-links
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import time
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
from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain import woo_translation_group as wtg  # noqa: E402

configure_utf8_console()

AUDITOR_NAME = "woocommerce_translation_group_audit"
AUDITOR_VERSION = "1.1.0"
PAGE_SIZE = 100
MAX_PAGES = 200
# --check-links: public storefront pages read per run (bounded, polite).
MAX_LINK_PAGES = 60
LINK_PAGE_DELAY_SECONDS = 0.5
FIELDS = ",".join(("id", "sku", "status", "permalink", "name") + wtg.SHARED_FIELDS[1:])


def fetch_all_products(get_page: Callable[[int], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Every product, page by page; raises if the catalog never ends."""
    products: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        batch = get_page(page)
        if not batch:
            return products
        products.extend(batch)
    raise RuntimeError(f"Catalog exceeds {MAX_PAGES} pages; refusing a partial audit.")


def storefront_links(
    group: wtg.TranslationGroup, read_page: Callable[[str], str | None], budget: list[int]
) -> dict[str, dict[str, str] | None]:
    """hreflang links of each published member's public page (None = unread)."""
    links: dict[str, dict[str, str] | None] = {}
    for lang, product in group.members.items():
        if product.get("status") != "publish":
            continue
        if budget[0] <= 0:
            links[lang] = None
            continue
        budget[0] -= 1
        page_html = read_page(str(product.get("permalink") or ""))
        links[lang] = None if page_html is None else wtg.parse_hreflang_links(page_html)
    return links


def audit(
    products: list[dict[str, Any]],
    sku_prefix: str,
    read_page: Callable[[str], str | None] | None = None,
    max_link_pages: int = MAX_LINK_PAGES,
) -> dict[str, Any]:
    """Group TSYC SKUs and evaluate each group. With read_page, also check
    the Polylang links on the public product pages of multi-row groups."""
    by_sku: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for product in products:
        sku = str(product.get("sku") or "")
        if sku.startswith(sku_prefix):
            by_sku[sku].append(product)

    languages = collections.Counter(wtg.product_language(p)[0] or "unknown" for p in products)
    budget = [max_link_pages]
    groups = []
    for sku in sorted(by_sku):
        group = wtg.evaluate_translation_group(sku, by_sku[sku])
        if read_page is not None and group.ok and len(group.members) > 1:
            group.issues.extend(wtg.evaluate_storefront_links(group, storefront_links(group, read_page, budget)))
        if len(by_sku[sku]) == 1 and not group.issues:
            continue
        groups.append({
            "sku": sku,
            "mode": group.mode,
            "members": {lang: {"id": p["id"], "status": p.get("status")} for lang, p in group.members.items()},
            "issues": [{"severity": i.severity, "code": i.code, "detail": i.detail} for i in group.issues],
        })

    severities = collections.Counter(i["severity"] for g in groups for i in g["issues"])
    return {
        "auditor": f"{AUDITOR_NAME}/{AUDITOR_VERSION}",
        "products_read": len(products),
        "products_by_language": dict(languages),
        "sku_prefix": sku_prefix,
        "storefront_links_checked": read_page is not None,
        "storefront_pages_read": max_link_pages - budget[0] if read_page is not None else 0,
        "tsyc_skus": len(by_sku),
        "multi_row_or_flagged_groups": len(groups),
        "errors": severities.get(wtg.ERROR, 0),
        "warnings": severities.get(wtg.WARNING, 0),
        "groups": groups,
    }


def print_report(report: dict[str, Any]) -> None:
    print("=" * 72)
    print("WOOCOMMERCE TRANSLATION GROUP AUDIT (read-only)")
    print("=" * 72)
    print(f"Products read: {report['products_read']}  by language: {report['products_by_language']}")
    if report["storefront_links_checked"]:
        print(f"Storefront pages read for Polylang links: {report['storefront_pages_read']}")
    print(f"TSYC SKUs ({report['sku_prefix']}*): {report['tsyc_skus']}  "
          f"flagged groups: {report['multi_row_or_flagged_groups']}  "
          f"errors: {report['errors']}  warnings: {report['warnings']}")
    for group in report["groups"]:
        members = ", ".join(f"{lang}#{m['id']}({m['status']})" for lang, m in group["members"].items())
        print()
        print(f"{group['sku']}  [{group['mode']}]  {members}")
        for issue in group["issues"]:
            print(f"   {issue['severity']:<7} {issue['code']}: {issue['detail']}")
    print()
    print("No product was created or modified by this audit.")


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only audit of TSYC WooCommerce translation groups.")
    parser.add_argument("--sku-prefix", default="TSYC-", help="Only SKUs with this prefix (default TSYC-).")
    parser.add_argument("--json-out", type=Path, help="Also write the report as JSON (keep it out of Git).")
    parser.add_argument(
        "--check-links", action="store_true",
        help=f"Also read the public pages of multi-row groups (at most {MAX_LINK_PAGES}, unauthenticated GET) "
             "and compare their Polylang hreflang links with the SKU group.")
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

    store_host = urlparse(store_url).netloc.lower()

    def read_page(permalink: str) -> str | None:
        # Public page only: never send the API credentials, never leave the store host.
        if urlparse(permalink).scheme != "https" or urlparse(permalink).netloc.lower() != store_host:
            return None
        time.sleep(LINK_PAGE_DELAY_SECONDS)
        try:
            response = requests.get(permalink, headers=headers, timeout=timeout, allow_redirects=False)
        except requests.RequestException:
            return None
        return response.text if response.status_code == 200 else None

    report = audit(fetch_all_products(get_page), args.sku_prefix, read_page if args.check_links else None)
    print_report(report)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"JSON report: {args.json_out}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, requests.RequestException) as error:
        print(f"Translation group audit failed: {type(error).__name__}: {error}")
        sys.exit(1)
