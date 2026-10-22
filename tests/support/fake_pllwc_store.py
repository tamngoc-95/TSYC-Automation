"""In-memory WooCommerce store modelling Polylang for WooCommerce.

Models the vendor-documented contract that docs/POLYLANG_WOOCOMMERCE_PLAN.md
stage 4 must prove on a real staging site: translations are linked through
`lang` / `translations`, and SKU, price, stock and backorders are
synchronised across the group. This proves TSYC's logic against that
contract only -- never the add-on itself.

`addon=False` models production today: independent per-language copies,
no `lang`/`translations` fields, no synchronisation.
"""
from __future__ import annotations

import copy
from typing import Any

from src.domain.woo_translation_group import SHARED_FIELDS

_GROUP_SHARED = SHARED_FIELDS + ("images", "categories")


class FakePllwcStore:
    def __init__(self, addon: bool = True, base_url: str = "https://staging.example.test") -> None:
        self.addon = addon
        self.base_url = base_url
        self.products: dict[int, dict[str, Any]] = {}
        self._next_id = 5000
        self.writes: list[tuple[str, int, dict[str, Any]]] = []

    # --- helpers -------------------------------------------------------
    def _permalink(self, lang: str, slug: str) -> str:
        prefix = "" if lang == "vi" else f"/{lang}"
        return f"{self.base_url}{prefix}/product/{slug}/"

    def _group_ids(self, product_id: int) -> list[int]:
        product = self.products[product_id]
        if not self.addon:
            return [product_id]
        return [pid for pid in product["_translations"].values() if pid in self.products]

    def _public(self, product: dict[str, Any]) -> dict[str, Any]:
        view = {k: copy.deepcopy(v) for k, v in product.items() if not k.startswith("_")}
        if self.addon:
            view["lang"] = product["_lang"]
            view["translations"] = dict(product["_translations"])
        return view

    # --- REST-like API -------------------------------------------------
    def seed_source(self, **fields: Any) -> dict[str, Any]:
        """A VI product as create_woocommerce_draft.py leaves it."""
        product = {
            "status": "draft", "type": "simple", "regular_price": "", "sale_price": "",
            "manage_stock": True, "stock_quantity": 0, "backorders": "yes", "stock_status": "onbackorder",
            "images": [{"id": 901}, {"id": 902}], "categories": [{"id": 57}],
            "name": "Từ Bé Trai, Tôi Trở Thành Người Lớn", "slug": "tu-be-trai",
            "description": "<p>VI</p>", "short_description": "<p>VI ngắn</p>",
        }
        product.update(fields)
        return self.create({**product, "lang": "vi"}, _seed=True)

    def create(self, payload: dict[str, Any], _seed: bool = False) -> dict[str, Any]:
        pid = self._next_id
        self._next_id += 1
        lang = payload.get("lang", "vi")
        product = {k: copy.deepcopy(v) for k, v in payload.items() if k not in ("lang", "translations")}
        product["id"] = pid
        product.setdefault("slug", f"product-{pid}")
        product["_lang"] = lang
        product["_translations"] = {lang: pid}
        links = payload.get("translations") or {}
        if self.addon and links:
            source = self.products[int(links["vi"])]
            for key in _GROUP_SHARED:
                product[key] = copy.deepcopy(source.get(key))
            group = dict(source["_translations"])
            group[lang] = pid
            for member_id in group.values():
                if member_id in self.products:
                    self.products[member_id]["_translations"] = dict(group)
            product["_translations"] = dict(group)
        product["permalink"] = self._permalink(lang, product["slug"])
        self.products[pid] = product
        if not _seed:
            self.writes.append(("POST", pid, copy.deepcopy(payload)))
        return self._public(product)

    def update(self, product_id: int, fields: dict[str, Any]) -> dict[str, Any]:
        self.writes.append(("PUT", product_id, copy.deepcopy(fields)))
        for member_id in self._group_ids(product_id):
            member = self.products[member_id]
            for key, value in fields.items():
                if member_id == product_id or key in _GROUP_SHARED:
                    member[key] = copy.deepcopy(value)
            if "stock_quantity" in fields or "backorders" in fields:
                qty = member.get("stock_quantity") or 0
                member["stock_status"] = "instock" if qty > 0 else (
                    "onbackorder" if member.get("backorders") in ("yes", "notify") else "outofstock")
        return self.get(product_id)

    def corrupt(self, product_id: int, fields: dict[str, Any]) -> None:
        """Change one member only, bypassing sync (a broken add-on)."""
        self.products[product_id].update(copy.deepcopy(fields))

    def delete(self, product_id: int) -> None:
        self.writes.append(("DELETE", product_id, {}))
        product = self.products.pop(product_id)
        if self.addon:
            group = {k: v for k, v in product["_translations"].items() if v != product_id}
            for member_id in group.values():
                self.products[member_id]["_translations"] = dict(group)

    def get(self, product_id: int) -> dict[str, Any]:
        return self._public(self.products[product_id])

    def search_sku(self, sku: str) -> list[dict[str, Any]]:
        return [self._public(p) for p in self.products.values() if p.get("sku") == sku]
