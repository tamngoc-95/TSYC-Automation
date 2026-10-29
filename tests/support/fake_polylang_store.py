"""In-memory model of the store's existing multilingual architecture, for
offline tests of src/domain/multilingual_adapter.py.

Modelled on the read-only findings of 2026-10-22 and the developer's
description; the real Flatsome Child hooks have NOT been inspected, so
every hook here is a switch a test turns on or off. Passing tests prove
TSYC's rules under each assumed behaviour, never the live hooks.

  * per-language products with their own id, price and stock;
  * free Polylang links (no REST access; links are visible only as
    hreflang on published pages);
  * bulk-link v1 (slug root) and v2 (featured image, then slug);
  * optional "theme" hooks: stock sync and metadata copy to siblings;
  * orders that decrement the ordered product's stock.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from src.domain import woo_translation_group as wtg

HOST = "https://tiemsachyeucon.com"
_LANG_SUFFIX = re.compile(r"-(en|de)$")


class InterruptedWrite(Exception):
    """The write reached the store, but the caller never saw the answer."""


class FakePolylangStore:
    def __init__(self, *, stock_sync: bool = False, meta_copy: bool = False) -> None:
        self.products: dict[int, dict[str, Any]] = {}
        self.group_of: dict[int, int] = {}
        self.lang_of: dict[int, str] = {}
        self.stock_sync = stock_sync
        self.meta_copy = meta_copy
        self.writes: list[tuple[int, dict[str, Any]]] = []
        self._next_group = 1

    # --- catalog ---------------------------------------------------------

    def add(self, pid: int, lang: str, slug: str, *, status: str = "publish", sku: str = "",
            price: str = "9.99", stock: int | None = 0, backorders: str = "yes", image: int | None = None,
            name: str = "", meta: dict[str, str] | None = None) -> dict[str, Any]:
        prefix = "" if lang == "vi" else f"/{lang}"
        product = {
            "id": pid, "sku": sku, "status": status, "slug": slug, "name": name or slug,
            "permalink": f"{HOST}{prefix}/product/{slug}/" if status == "publish" else f"{HOST}/?post_type=product&p={pid}",
            "regular_price": price, "sale_price": "", "manage_stock": stock is not None,
            "stock_quantity": stock, "backorders": backorders,
            "stock_status": "instock" if (stock or 0) > 0 else "onbackorder",
            "images": [{"id": image}] if image else [],
            "description": "", "short_description": "",
            "meta_data": [{"key": k, "value": v} for k, v in (meta or {}).items()],
            "date_modified_gmt": "2026-10-01T10:00:00",
        }
        self.products[pid] = product
        self.lang_of[pid] = lang
        return product

    def link(self, *pids: int) -> None:
        group = self._next_group
        self._next_group += 1
        for pid in pids:
            self.group_of[pid] = group

    def siblings(self, pid: int) -> list[int]:
        group = self.group_of.get(pid)
        if group is None:
            return []
        return [other for other, g in self.group_of.items() if g == group and other != pid]

    # --- what TSYC can read ---------------------------------------------

    def page_links(self, pid: int) -> dict[str, str] | None:
        """hreflang of a published page; None for a product without a page."""
        product = self.products[pid]
        if product["status"] != "publish":
            return None
        members = [pid] + self.siblings(pid)
        published = [m for m in members if self.products[m]["status"] == "publish"]
        # Polylang prints alternates only when at least one other published
        # translation exists; a draft sibling never appears.
        if len(published) < 2:
            return {}
        return {
            wtg.product_language(self.products[m])[0]: wtg.normalise_url(self.products[m]["permalink"])
            for m in published
        }

    def all_page_links(self) -> dict[int, dict[str, str] | None]:
        return {pid: self.page_links(pid) for pid, p in self.products.items() if p["status"] == "publish"}

    def polylang_translations(self, pid: int) -> dict[str, int]:
        """Polylang's own map of a product, as pll_get_post_translations
        returns it (drafts included, one product per language, the last
        write wins). Live, nothing can read this yet: it models the
        staging-proven mechanism TSYC is waiting for."""
        return {self.lang_of[m]: m for m in sorted([pid] + self.siblings(pid))}

    def sku_matches(self, sku: str) -> list[dict[str, Any]]:
        return [copy.deepcopy(p) for p in self.products.values() if p["sku"] == sku]

    def snapshot(self) -> dict[int, dict[str, Any]]:
        return copy.deepcopy(self.products)

    # --- bulk-link plugins (one-time tools) -----------------------------

    def bulk_link_v1(self) -> None:
        """Link products whose slug root matches (slug minus -en/-de)."""
        roots: dict[str, list[int]] = {}
        for pid, product in self.products.items():
            roots.setdefault(_LANG_SUFFIX.sub("", product["slug"]), []).append(pid)
        for pids in roots.values():
            if len(pids) > 1:
                self.link(*pids)

    def bulk_link_v2(self) -> None:
        """Link products sharing a featured image (primary), else slug root."""
        by_image: dict[int, list[int]] = {}
        for pid, product in self.products.items():
            if product["images"]:
                by_image.setdefault(product["images"][0]["id"], []).append(pid)
        for pids in by_image.values():
            if len(pids) > 1:
                self.link(*pids)

    # --- writes and hooks -----------------------------------------------

    def save(self, pid: int, fields: dict[str, Any], *, interrupt: bool = False) -> dict[str, Any]:
        product = self.products[pid]
        product.update(copy.deepcopy(fields))
        product["date_modified_gmt"] = "2026-10-22T12:00:00"
        self.writes.append((pid, dict(fields)))
        if self.meta_copy:
            for sibling in self.siblings(pid):
                for key in ("regular_price", "sale_price", "manage_stock", "stock_quantity", "backorders", "sku"):
                    self.products[sibling][key] = product[key]
                self.products[sibling]["date_modified_gmt"] = product["date_modified_gmt"]
        if interrupt:
            raise InterruptedWrite(f"connection lost after saving #{pid}")
        return copy.deepcopy(product)

    def place_order(self, pid: int, quantity: int = 1) -> None:
        """An order in any language decrements the ordered product."""
        product = self.products[pid]
        if product["manage_stock"]:
            product["stock_quantity"] = (product["stock_quantity"] or 0) - quantity
            if self.stock_sync:
                for sibling in self.siblings(pid):
                    self.products[sibling]["stock_quantity"] = product["stock_quantity"]
