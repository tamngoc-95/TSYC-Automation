"""
READ-ONLY Fahasa new-book discovery with unified duplicate classification.

Pipeline position: before any candidate exists. It answers "which newly
listed Fahasa books are genuinely new to TSYC?" and writes a report; it
never writes Supabase, WooCommerce, images or prices.

  1. builds the unified identity index (src.domain.rules.
     catalog_identity_index) from:
       - every WooCommerce product, status any + trash (read-only GET)
       - woocommerce_product_syncs (incl. owner-removed products, which
         no longer exist remotely -- the memory of REMOTE_REMOVED)
       - internal_products, product_candidates, product_references
       - the shop's Facebook history export
         (data/processed/facebook_history_classification.csv)
  2. reads ONE bounded Fahasa listing (newest first) -- robots.txt allows
     category and product pages; requests are serial and paced;
  3. parses each product page with collect_reference_metadata.py's own
     Fahasa parser (one parser implementation, not two);
  4. classifies each listing (NEW_CONFIRMED / EXISTING_PRODUCT /
     POSSIBLE_DUPLICATE / DISTINCT_EDITION / FACEBOOK_COVERAGE_UNKNOWN /
     REMOTE_REMOVED) and records identity strength, sellable unit and
     image provenance;
  5. writes data/output/fahasa_discovery/discovery_<UTC>.json and keeps an
     append-only first-seen ledger
     (data/processed/fahasa_discovery/first_seen.json).

No Fahasa price is recorded at all: Fahasa is never a purchase-price
source (CLAUDE.md 8.2/8.3) and availability on Fahasa is never evidence
of supplier commitment or a delivery date.

Image rights: a Fahasa image is recorded as RIGHTS_UNKNOWN. The
FAHASA -> SUPPLIER_APPROVED auto-authorization (CLAUDE.md 14.7) is
scoped to FB-HIST candidates only and is not extended here.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from dotenv import load_dotenv  # noqa: E402

from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain import woo_remote_lifecycle  # noqa: E402
from src.domain.rules import catalog_identity_index as idx  # noqa: E402
from src.domain.rules import identity_rules  # noqa: E402

DISCOVERER_NAME = "fahasa_new_book_discoverer"
DISCOVERER_VERSION = "1.0.0"

DEFAULT_LISTING_URL = "https://www.fahasa.com/sach-trong-nuoc/thieu-nhi.html?order=created_at&limit=48"
MAX_LISTINGS_HARD_CAP = 60
PAGE_DELAY_SECONDS = 2.0
FACEBOOK_EXPORT_CSV = PROJECT_ROOT / "data" / "processed" / "facebook_history_classification.csv"
OUTPUT_DIR = PROJECT_ROOT / "data" / "output" / "fahasa_discovery"
FIRST_SEEN_LEDGER = PROJECT_ROOT / "data" / "processed" / "fahasa_discovery" / "first_seen.json"

_PRODUCT_URL_RE = re.compile(r"^https://www\.fahasa\.com/[a-z0-9-]+\.html$")
# Site navigation links present on every Fahasa listing page.
_NAVIGATION_SLUGS = frozenset(
    {
        "sach-trong-nuoc",
        "foreigncategory",
        "do-choi-luu-niem",
        "lam-dep-suc-khoe",
        "lam-d-p-s-c-kh-e",
        "bach-hoa-t-ng-h-p",
        "all-category",
    }
)
_FB_DATE_RE = re.compile(r"Tháng (\d{1,2}) (\d{1,2}), (\d{4})")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Fahasa new-book discovery and duplicate classification.")
    parser.add_argument("--listing-url", default=DEFAULT_LISTING_URL, help="Fahasa category listing URL (newest first).")
    parser.add_argument("--max-listings", type=int, required=True, help=f"Exact upper bound (<= {MAX_LISTINGS_HARD_CAP}).")
    parser.add_argument("--max-listing-pages", type=int, default=3, help="Listing pages to read at most.")
    parser.add_argument(
        "--reclassify",
        type=Path,
        help="Re-classify the records of an earlier discovery report against a fresh index; Fahasa is not contacted.",
    )
    args = parser.parse_args()
    if not 1 <= args.max_listings <= MAX_LISTINGS_HARD_CAP:
        parser.error(f"--max-listings must be between 1 and {MAX_LISTINGS_HARD_CAP}.")
    if not args.listing_url.startswith("https://www.fahasa.com/"):
        parser.error("--listing-url must be a https://www.fahasa.com/ URL.")
    return args


# --- index sources ------------------------------------------------------------


def product_links_from_listing(links: Iterable[tuple[str, str]]) -> list[str]:
    """Unique product-page URLs in listing order, navigation removed."""
    seen: list[str] = []
    for href, text in links:
        href = href.split("?")[0].split("#")[0]
        if not _PRODUCT_URL_RE.match(href) or not text.strip():
            continue
        slug = href.rsplit("/", 1)[-1][: -len(".html")]
        if slug in _NAVIGATION_SLUGS or href in seen:
            continue
        seen.append(href)
    return seen


def load_woo_entries() -> list[idx.IndexEntry]:
    import requests
    from requests.auth import HTTPBasicAuth

    import sync_woocommerce_product_status as woo

    store_url = woo.get_required_environment_variable("WOOCOMMERCE_URL")
    auth = HTTPBasicAuth(
        woo.get_required_environment_variable("WOOCOMMERCE_CONSUMER_KEY"),
        woo.get_required_environment_variable("WOOCOMMERCE_CONSUMER_SECRET"),
    )
    api_version = os.getenv("WOOCOMMERCE_API_VERSION", "wc/v3").strip()
    url = woo.build_api_url(store_url, api_version, "products")
    entries: list[idx.IndexEntry] = []
    for status in ("any", "trash"):
        page = 1
        while True:
            response = requests.get(
                url,
                auth=auth,
                params={"status": status, "per_page": 100, "page": page},
                headers={"Accept": "application/json", "User-Agent": f"{DISCOVERER_NAME}/{DISCOVERER_VERSION}"},
                timeout=woo.get_timeout_seconds(),
            )
            if response.status_code != 200:
                # An incomplete Woo index could misclassify a duplicate as new.
                raise RuntimeError(f"WooCommerce product listing failed (status={status}, HTTP {response.status_code}).")
            products = response.json()
            if not isinstance(products, list):
                raise RuntimeError("WooCommerce product listing returned an unexpected body.")
            for product in products:
                entries.append(
                    idx.IndexEntry(
                        origin=idx.ORIGIN_WOO,
                        ref=str(product.get("id")),
                        title=str(product.get("name") or ""),
                        remote_removed=product.get("status") == "trash",
                        status=product.get("status"),
                        url=product.get("permalink"),
                    )
                )
            if len(products) < 100:
                break
            page += 1
    return entries


def _select_all(repository: Any, table: str, columns: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    start = 0
    while True:
        batch = repository.client.table(table).select(columns).range(start, start + 999).execute().data or []
        rows.extend(batch)
        if len(batch) < 1000:
            return rows
        start += 1000


def load_database_entries(repository: Any) -> list[idx.IndexEntry]:
    entries: list[idx.IndexEntry] = []
    products = _select_all(
        repository, "internal_products", "internal_product_id, product_code, title, author, isbn, woocommerce_status"
    )
    product_by_id = {row["internal_product_id"]: row for row in products}
    for row in products:
        entries.append(
            idx.IndexEntry(
                origin=idx.ORIGIN_INTERNAL_PRODUCT,
                ref=row["product_code"],
                title=row.get("title") or "",
                isbn=row.get("isbn"),
                author=row.get("author"),
                status=row.get("woocommerce_status"),
            )
        )
    for row in _select_all(
        repository,
        "woocommerce_product_syncs",
        "internal_product_id, woocommerce_product_id, woocommerce_status, product_sku, product_name, product_permalink, response_payload",
    ):
        product = product_by_id.get(row.get("internal_product_id")) or {}
        removal = woo_remote_lifecycle.confirmed_remote_removal(row)
        entries.append(
            idx.IndexEntry(
                origin=idx.ORIGIN_WOO_SYNC,
                ref=f"{row.get('product_sku')} (Woo #{row.get('woocommerce_product_id')})",
                title=row.get("product_name") or product.get("title") or "",
                isbn=product.get("isbn"),
                author=product.get("author"),
                remote_removed=removal is not None,
                status=removal or row.get("woocommerce_status"),
                url=row.get("product_permalink"),
            )
        )
    for row in _select_all(
        repository,
        "product_candidates",
        "candidate_code, verified_title, extracted_title, verified_isbn, possible_isbn, verified_author, extracted_author, identity_status",
    ):
        entries.append(
            idx.IndexEntry(
                origin=idx.ORIGIN_CANDIDATE,
                ref=row["candidate_code"],
                title=row.get("verified_title") or row.get("extracted_title") or "",
                isbn=identity_rules.first_valid_isbn(row.get("verified_isbn"), row.get("possible_isbn")),
                author=row.get("verified_author") or row.get("extracted_author"),
                status=row.get("identity_status"),
            )
        )
    for row in _select_all(
        repository,
        "product_references",
        "reference_id, reference_title, reference_isbn, reference_author, source_url, match_decision",
    ):
        # Only a reference that was MATCHed to a shop candidate represents
        # a TSYC product; an unmatched reference is just a looked-at page.
        if row.get("match_decision") != "MATCH":
            continue
        entries.append(
            idx.IndexEntry(
                origin=idx.ORIGIN_REFERENCE,
                ref=row["reference_id"],
                title=row.get("reference_title") or "",
                isbn=row.get("reference_isbn"),
                author=row.get("reference_author"),
                url=row.get("source_url"),
            )
        )
    return entries


def load_facebook_posts(path: Path) -> tuple[list[idx.IndexEntry] | None, str | None]:
    """(posts, coverage note). None posts = Facebook data unavailable."""
    if not path.exists():
        return None, None
    posts: list[idx.IndexEntry] = []
    latest: tuple[int, int, int] | None = None
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            text = row.get("full_text") or ""
            if not text.strip():
                continue
            posts.append(
                idx.IndexEntry(
                    origin=idx.ORIGIN_FACEBOOK_POST,
                    ref=f"fb-export-{row.get('record_index')}",
                    title=text,
                    relevance=(row.get("tsyc_relevance") or "").upper(),
                    status=row.get("date_text"),
                )
            )
            match = _FB_DATE_RE.search(row.get("date_text") or "")
            if match:
                stamp = (int(match[3]), int(match[1]), int(match[2]))
                latest = stamp if latest is None or stamp > latest else latest
    note = None
    if latest:
        note = (
            f"Facebook checked against the shop export through {latest[0]:04d}-{latest[1]:02d}-{latest[2]:02d} "
            "plus every collected live Facebook candidate; posts after that date that were never collected are not covered."
        )
    return posts, note


# --- discovery ----------------------------------------------------------------


def load_first_seen() -> dict[str, str]:
    if FIRST_SEEN_LEDGER.exists():
        return json.loads(FIRST_SEEN_LEDGER.read_text(encoding="utf-8"))
    return {}


def save_first_seen(ledger: dict[str, str]) -> None:
    FIRST_SEEN_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    temporary = FIRST_SEEN_LEDGER.with_suffix(".tmp")
    temporary.write_text(json.dumps(ledger, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(FIRST_SEEN_LEDGER)


def is_book_product_page(metadata: dict[str, Any] | None) -> bool:
    """A Fahasa book page: product JSON-LD plus a title and a publisher
    (category and gift pages also carry JSON-LD but no publisher)."""
    if not metadata or not (metadata.get("raw_metadata") or {}).get("json_ld_found"):
        return False
    return bool((metadata.get("reference_title") or "").strip() and (metadata.get("reference_publisher") or "").strip())


def build_record(url: str, metadata: dict[str, Any], first_seen_at: str) -> dict[str, Any]:
    raw = metadata.get("raw_metadata") or {}
    isbn_raw = metadata.get("reference_isbn")
    return {
        "source_type": "FAHASA",
        "source_url": url,
        "first_seen_at": first_seen_at,
        "title": metadata.get("reference_title"),
        "author": metadata.get("reference_author"),
        "translator": raw.get("translator"),
        "publisher": metadata.get("reference_publisher"),
        "displayed_supplier": raw.get("displayed_supplier"),
        "isbn": identity_rules.first_valid_isbn(isbn_raw),
        "isbn_raw_value": isbn_raw,
        "book_format": raw.get("book_format"),
        "publication_year": raw.get("publication_year"),
        "page_count": metadata.get("reference_page_count"),
        "weight_grams": metadata.get("reference_weight_grams"),
        "dimensions_cm": [
            metadata.get("reference_length_cm"),
            metadata.get("reference_width_cm"),
            metadata.get("reference_height_cm"),
        ],
        "reference_description": metadata.get("reference_description"),
        "image": {
            "url": metadata.get("reference_image_url"),
            "source_type": "FAHASA",
            "source_page": url,
            # CLAUDE.md 14.7 auto-authorization is FB-HIST only.
            "rights_status": "RIGHTS_UNKNOWN",
            "rights_note": "Fahasa image; no established live-pipeline usage permission.",
        },
        "json_ld_found": raw.get("json_ld_found"),
    }


def classify_record(
    record: dict[str, Any],
    entries: list[idx.IndexEntry],
    posts: list[idx.IndexEntry] | None,
    coverage_note: str | None,
) -> dict[str, Any]:
    listing = idx.Listing(
        title=record.get("title") or "",
        isbn=record.get("isbn"),
        author=record.get("author"),
        publisher=record.get("publisher"),
        url=record.get("source_url"),
    )
    result = idx.classify_listing(listing, entries, posts, coverage_note)
    linked = result.linked
    return {
        "classification": result.classification,
        "reason": result.reason,
        "linked": None
        if linked is None
        else {"origin": linked.origin, "ref": linked.ref, "title": linked.title, "status": linked.status, "url": linked.url},
        "evidence": result.evidence()[:10],
        "warnings": list(result.warnings),
        "identity_strength": idx.listing_identity_strength(listing),
        "sellable_unit": idx.listing_sellable_unit(listing),
    }


def main() -> None:
    configure_utf8_console()
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_arguments()

    from discover_reference_sources import PlaywrightPageSource
    from src.repositories.supabase_repository import SupabaseRepository

    started_at = utc_now_iso()
    print("=" * 78)
    print(f"FAHASA NEW-BOOK DISCOVERY (READ-ONLY) v{DISCOVERER_VERSION}")
    print("=" * 78)

    woo_entries = load_woo_entries()
    database_entries = load_database_entries(SupabaseRepository())
    posts, coverage_note = load_facebook_posts(FACEBOOK_EXPORT_CSV)
    entries = woo_entries + database_entries
    print(f"Index: {len(woo_entries)} Woo products, {len(database_entries)} DB records, "
          f"{'unavailable' if posts is None else len(posts)} Facebook posts.")

    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    if args.reclassify:
        previous = json.loads(args.reclassify.read_text(encoding="utf-8"))
        for record in previous["records"][: args.max_listings]:
            if not (record.get("title") and record.get("publisher")):
                continue  # same book-page rule as a live run
            record.update(classify_record(record, entries, posts, coverage_note))
            records.append(record)
            print(f"[{len(records):>2}] {record['classification']:<26} {record['identity_strength']:<8} {record['title']}")
        write_report(
            previous.get("listing_url") or args.listing_url,
            started_at, woo_entries, database_entries, posts, coverage_note, records, errors,
            reclassified_from=str(args.reclassify),
        )
        return

    source = PlaywrightPageSource()
    ledger = load_first_seen()
    try:
        product_urls: list[str] = []
        for page_number in range(1, args.max_listing_pages + 1):
            listing_url = args.listing_url if page_number == 1 else f"{args.listing_url}&p={page_number}"
            for url in product_links_from_listing(source.search(listing_url)):
                if url not in product_urls:
                    product_urls.append(url)
            if len(product_urls) >= args.max_listings * 2:
                break
            time.sleep(PAGE_DELAY_SECONDS)

        for url in product_urls:
            if len(records) >= args.max_listings:
                break
            time.sleep(PAGE_DELAY_SECONDS)
            try:
                metadata = source.fetch_metadata(url)
            except Exception as error:  # one page failing never stops the batch
                errors.append({"url": url, "error": f"{type(error).__name__}: {error}"[:300]})
                continue
            if not is_book_product_page(metadata):
                continue  # category/landing/gift page, not a book
            ledger.setdefault(url, utc_now_iso())
            record = build_record(url, metadata, ledger[url])
            record.update(classify_record(record, entries, posts, coverage_note))
            records.append(record)
            print(f"[{len(records):>2}] {record['classification']:<26} {record['identity_strength']:<8} {record['title']}")
    finally:
        source.close()
        save_first_seen(ledger)

    write_report(args.listing_url, started_at, woo_entries, database_entries, posts, coverage_note, records, errors)


def write_report(
    listing_url: str,
    started_at: str,
    woo_entries: list[idx.IndexEntry],
    database_entries: list[idx.IndexEntry],
    posts: list[idx.IndexEntry] | None,
    coverage_note: str | None,
    records: list[dict[str, Any]],
    errors: list[dict[str, str]],
    reclassified_from: str | None = None,
) -> Path:
    summary: dict[str, int] = {name: 0 for name in idx.CLASSIFICATIONS}
    for record in records:
        summary[record["classification"]] += 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / f"discovery_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    output_path.write_text(
        json.dumps(
            {
                "discoverer": DISCOVERER_NAME,
                "version": DISCOVERER_VERSION,
                "index_rules_version": idx.INDEX_RULES_VERSION,
                "started_at": started_at,
                "finished_at": utc_now_iso(),
                "listing_url": listing_url,
                "reclassified_from": reclassified_from,
                "index_counts": {
                    "woo_products": len(woo_entries),
                    "database_records": len(database_entries),
                    "facebook_posts": None if posts is None else len(posts),
                },
                "facebook_coverage_note": coverage_note,
                "summary": summary,
                "errors": errors,
                "records": records,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print("-" * 78)
    for name, count in summary.items():
        print(f"{name:<26} {count}")
    print(f"Fetch errors: {len(errors)}")
    print(f"Report: {output_path}")
    return output_path


if __name__ == "__main__":
    main()
