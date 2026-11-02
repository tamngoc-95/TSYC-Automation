"""In-memory model of the TSYC bridge's HTTP contract, for offline tests of the
writer and runner.

It models the contract the real bridge passed on the local lab
(tools/wp_lab/run_local_pilot.py). Passing against this fake proves TSYC's client
rules only. It is not evidence about WordPress, Polylang or the store's hooks.
"""
from __future__ import annotations

from typing import Any, Mapping


class Response:
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeBridge:
    def __init__(self, *, source_id: int, internal_product_id: str, product_code: str, modified_gmt: str) -> None:
        self.source_id = source_id
        self.internal_product_id = internal_product_id
        self.product_code = product_code
        self.products: dict[int, dict[str, Any]] = {
            source_id: {"id": source_id, "_lang": "vi", "sku": product_code, "status": "draft", "name": "VI",
                        "description": "", "short_description": "", "date_modified_gmt": modified_gmt,
                        "permalink": f"https://lab.example.test/?post_type=product&p={source_id}",
                        "meta_data": [{"key": "_tsyc_internal_product_id", "value": internal_product_id}]}}
        self.group: dict[str, int] = {"vi": source_id}
        self.by_request: dict[str, int] = {}
        self.next_id = source_id + 1
        self.posts: list[dict[str, Any]] = []
        self.fail_post_with: Any = None
        self.land_then_fail = False
        self.fail_get_with: Exception | None = None

    # ------------------------------------------------------------------- GET

    def get(self, url: str, **_kwargs: Any) -> Response:
        if self.fail_get_with is not None:
            raise self.fail_get_with
        if "/translations/by-request/" in url:
            request_id = url.rsplit("/", 1)[1]
            pid = self.by_request.get(request_id)
            if pid is None:
                return Response(200, {"request_id": request_id, "found": False})
            product = self.products[pid]
            return Response(200, {"request_id": request_id, "found": True, "product_id": pid, "status": product["status"],
                                  "lang": product["_lang"], "state": "LINKED", "source_id": self.source_id,
                                  "group": {"translations": dict(self.group), "consistent": True, "issues": []}})
        pid = int(url.rsplit("/", 1)[1])
        product = self.products.get(pid)
        if product is None:
            return Response(404, {"code": "tsyc_not_a_product", "message": "No such product."})
        group = self.group if pid in self.group.values() else {product["_lang"]: pid}
        return Response(200, {"id": pid, "lang": product["_lang"], "sku": product["sku"], "status": product["status"],
                              "translations": dict(group), "members": {}, "consistent": True, "issues": []})

    # ------------------------------------------------------------------ POST

    def post(self, url: str, *, json: Mapping[str, Any], **_kwargs: Any) -> Response:
        self.posts.append(dict(json))
        if isinstance(self.fail_post_with, Exception) and not self.land_then_fail:
            raise self.fail_post_with
        if isinstance(self.fail_post_with, Response) and not self.land_then_fail:
            return self.fail_post_with
        request_id = json["request_id"]
        lang = json["lang"]
        replayed = request_id in self.by_request
        if not replayed:
            if lang in self.group:
                return Response(409, {"code": "tsyc_translation_exists", "message": f"{lang} exists."})
            pid = self.next_id
            self.next_id += 1
            content = json["content"]
            self.products[pid] = {"id": pid, "_lang": lang, "sku": "", "status": "draft", "name": content["name"],
                                  "description": content["description"], "short_description": content["short_description"],
                                  "date_modified_gmt": "2026-11-02T00:00:01",
                                  "permalink": f"https://lab.example.test/?post_type=product&p={pid}",
                                  "meta_data": [{"key": "_tsyc_internal_product_id", "value": self.internal_product_id},
                                                {"key": "_tsyc_language", "value": lang}]}
            self.group[lang] = pid
            self.by_request[request_id] = pid
        pid = self.by_request[request_id]
        if self.land_then_fail:
            failure, self.land_then_fail, self.fail_post_with = self.fail_post_with, False, None
            if isinstance(failure, Exception):
                raise failure
            return failure
        product = self.products[pid]
        return Response(200 if replayed else 201, {
            "operation_id": request_id, "state": "LINKED", "replayed": replayed, "product_id": pid,
            "source_id": self.source_id, "lang": lang, "status": "draft", "modified_gmt": product["date_modified_gmt"],
            "text": {"name": product["name"], "description": product["description"],
                     "short_description": product["short_description"]},
            "translations": dict(self.group), "warnings": [], "read_back": {"verified": True, "bridge_version": "1.0.0"}})

    # ----------------------------------------------------------------- reads

    def read_catalog(self, ids: Any, code: str) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
        products = {int(pid): dict(self.products[int(pid)]) for pid in ids}
        return products, [dict(p) for p in self.products.values() if p["sku"] == code]
