"""
READ-ONLY loader for the mandatory pre-create catalog identity check
(src/domain/woo_catalog_identity.py).

Reads:
  * every WooCommerce product in status "any" AND "trash" (limited fields),
    page by page, and checks each status's X-WP-Total against the rows
    received -- any error, short read or missing total marks the snapshot
    incomplete, and the check then fails closed;
  * Supabase: other internal products, sync records (removal markers and
    legacy remote-loss records), candidates, MATCH references and
    publisher barcodes (893…, never ISBN);
  * optionally the cached storefront hreflang links
    (data/output/polylang_catalog/page_links.json) to collapse Polylang
    translation groups into one logical product. The cache only refines
    the label of a match; a missing cache can never let a duplicate pass.

Never writes anything.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

import requests

from src.domain import polylang_link_graph as plg
from src.domain import woo_catalog_identity as wci
from src.domain import woo_remote_lifecycle
from src.domain.rules import catalog_identity_index as idx
from src.domain.rules import identity_rules

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PAGE_LINKS_CACHE = PROJECT_ROOT / "data" / "output" / "polylang_catalog" / "page_links.json"
PAGE_SIZE = 100
MAX_PAGES_PER_STATUS = 100
WOO_FIELDS = "id,name,sku,status,permalink,meta_data"


def load_woo_catalog(
    *,
    products_url: str,
    auth: Any,
    headers: Mapping[str, str],
    timeout: int,
    http_get: Callable[..., Any] = requests.get,
) -> tuple[list[dict[str, Any]], bool, str | None]:
    """(products, complete, reason). Never raises: an error is an
    incomplete snapshot, which the check treats as a refusal."""
    products: list[dict[str, Any]] = []
    for status in ("any", "trash"):
        received = 0
        expected: int | None = None
        for page in range(1, MAX_PAGES_PER_STATUS + 1):
            try:
                response = http_get(
                    products_url, auth=auth, headers=dict(headers), timeout=timeout,
                    params={"status": status, "per_page": PAGE_SIZE, "page": page,
                            "orderby": "id", "order": "asc", "_fields": WOO_FIELDS},
                )
            except requests.RequestException as error:
                return products, False, f"status={status} page {page}: {type(error).__name__}"
            if response.status_code != 200:
                return products, False, f"status={status} page {page}: HTTP {response.status_code}"
            try:
                batch = response.json()
            except ValueError:
                return products, False, f"status={status} page {page}: invalid JSON"
            if not isinstance(batch, list):
                return products, False, f"status={status} page {page}: unexpected body"
            if expected is None:
                total = (getattr(response, "headers", None) or {}).get("X-WP-Total")
                if total is None or not str(total).isdigit():
                    return products, False, f"status={status}: no X-WP-Total"
                expected = int(total)
            products.extend(batch)
            received += len(batch)
            if len(batch) < PAGE_SIZE:
                break
        else:
            return products, False, f"status={status}: more than {MAX_PAGES_PER_STATUS} pages"
        if received != expected:
            return products, False, f"status={status}: read {received} of {expected} products"
    return products, True, None


def _select_all(repository: Any, table: str, columns: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    start = 0
    while True:
        batch = repository.client.table(table).select(columns).range(start, start + 999).execute().data or []
        rows.extend(batch)
        if len(batch) < 1000:
            return rows
        start += 1000


def _barcode(value: Any) -> str | None:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) == 13 and digits.startswith("893") and not identity_rules.looks_like_valid_isbn(digits):
        return digits
    return None


def load_local_snapshot(
    repository: Any, *, product_code: str, candidate_id: str
) -> tuple[list[idx.IndexEntry], list[idx.IndexEntry], dict[str, str]]:
    """(local entries, removed entries, barcodes) excluding this product's
    own records -- except its own owner-removal records, which must still
    stop a recreate."""
    products = _select_all(repository, "internal_products",
                           "internal_product_id, candidate_id, product_code, title, author, isbn, woocommerce_status")
    titles = {str(p["internal_product_id"]): p.get("title") or "" for p in products}
    by_id = {str(p["internal_product_id"]): p for p in products}
    local: list[idx.IndexEntry] = []
    removed: list[idx.IndexEntry] = []
    barcodes: dict[str, str] = {}

    for row in products:
        if row.get("product_code") == product_code:
            continue
        local.append(idx.IndexEntry(origin=idx.ORIGIN_INTERNAL_PRODUCT, ref=row["product_code"],
                                    title=row.get("title") or "", isbn=row.get("isbn"), author=row.get("author"),
                                    status=row.get("woocommerce_status")))

    syncs = _select_all(repository, "woocommerce_product_syncs",
                        "internal_product_id, woocommerce_product_id, woocommerce_status, product_sku, "
                        "product_name, product_permalink, response_payload")
    for row in syncs:
        own = row.get("product_sku") == product_code
        product = by_id.get(str(row.get("internal_product_id"))) or {}
        removal = woo_remote_lifecycle.confirmed_remote_removal(row)
        if removal is not None:
            removed.append(idx.IndexEntry(
                origin=idx.ORIGIN_WOO_SYNC, ref=f"{row.get('product_sku')} (Woo #{row.get('woocommerce_product_id')})",
                title=product.get("title") or row.get("product_name") or "", isbn=product.get("isbn"),
                author=product.get("author"), remote_removed=True, status=removal))
        elif not own and row.get("woocommerce_product_id"):
            local.append(idx.IndexEntry(
                origin=idx.ORIGIN_WOO_SYNC, ref=f"{row.get('product_sku')} (Woo #{row.get('woocommerce_product_id')})",
                title=product.get("title") or row.get("product_name") or "", isbn=product.get("isbn"),
                author=product.get("author"), status=row.get("woocommerce_status")))
    removed.extend(wci.legacy_removed_entries(syncs, titles))

    for row in _select_all(repository, "product_candidates",
                           "candidate_id, candidate_code, verified_title, extracted_title, verified_isbn, "
                           "possible_isbn, verified_author, extracted_author, identity_status"):
        if str(row.get("candidate_id")) == str(candidate_id):
            continue
        local.append(idx.IndexEntry(
            origin=idx.ORIGIN_CANDIDATE, ref=row["candidate_code"],
            title=row.get("verified_title") or row.get("extracted_title") or "",
            isbn=identity_rules.first_valid_isbn(row.get("verified_isbn"), row.get("possible_isbn")),
            author=row.get("verified_author") or row.get("extracted_author"), status=row.get("identity_status")))
        for value in (row.get("possible_isbn"), row.get("verified_isbn")):
            if _barcode(value):
                barcodes[row["candidate_code"]] = _barcode(value)

    for row in _select_all(repository, "product_references",
                           "reference_id, candidate_id, reference_title, reference_isbn, reference_author, "
                           "source_url, match_decision"):
        if str(row.get("candidate_id")) == str(candidate_id) or row.get("match_decision") != "MATCH":
            continue
        local.append(idx.IndexEntry(origin=idx.ORIGIN_REFERENCE, ref=row["reference_id"],
                                    title=row.get("reference_title") or "", isbn=row.get("reference_isbn"),
                                    author=row.get("reference_author"), url=row.get("source_url")))
        if _barcode(row.get("reference_isbn")):
            barcodes[f"reference {row['reference_id']}"] = _barcode(row.get("reference_isbn"))
    return local, removed, barcodes


def own_barcode(repository: Any, candidate_id: str) -> str | None:
    rows = (repository.client.table("product_candidates").select("possible_isbn, verified_isbn")
            .eq("candidate_id", candidate_id).limit(1).execute().data or [])
    for value in (rows[0].get("possible_isbn"), rows[0].get("verified_isbn")) if rows else ():
        if _barcode(value):
            return _barcode(value)
    return None


def load_translation_groups(products: list[dict[str, Any]], cache_file: Path = PAGE_LINKS_CACHE) -> dict[int, int]:
    """{woo id: group number} from the cached storefront hreflang links."""
    if not cache_file.exists():
        return {}
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    links = {int(p["id"]): (cache.get(str(p.get("permalink") or "")) or {}).get("links")
             for p in products if p.get("status") == "publish"}
    groups, _ = plg.build_groups(products, {pid: v for pid, v in links.items() if v is not None})
    return {pid: number for number, group in enumerate(groups) if group.status == plg.GROUP_OK
            for pid in group.members.values()}


def build_snapshot(
    repository: Any, *, product_code: str, candidate_id: str, products_url: str, auth: Any,
    headers: Mapping[str, str], timeout: int, http_get: Callable[..., Any] = requests.get,
) -> wci.CatalogSnapshot:
    products, complete, reason = load_woo_catalog(
        products_url=products_url, auth=auth, headers=headers, timeout=timeout, http_get=http_get)
    if not complete:
        return wci.CatalogSnapshot(woo_products=products, complete=False, incomplete_reason=reason)
    try:
        local, removed, barcodes = load_local_snapshot(repository, product_code=product_code, candidate_id=candidate_id)
    except Exception as error:  # noqa: BLE001 -- any local read failure fails closed
        return wci.CatalogSnapshot(woo_products=products, complete=False,
                                   incomplete_reason=f"local records: {type(error).__name__}")
    return wci.CatalogSnapshot(
        woo_products=products, complete=True, local_entries=local, removed_entries=removed,
        translation_group_of=load_translation_groups(products), barcodes=barcodes)
