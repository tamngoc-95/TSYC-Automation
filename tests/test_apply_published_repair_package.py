"""Offline tests for scripts/apply_published_repair_package.py (no live Woo)."""
from __future__ import annotations

import copy
import json

import pytest
import requests

import apply_published_repair_package as rep

RAW_BEFORE = (
    "<p>Mở đầu.</p>\r\n<h3>Tác giả</h3>\r\nTác giả của ấn phẩm là Louis Ben.\r\n"
    "<h3>Thông tin sản phẩm</h3>\r\n<ul>\r\n \t<li>Tác giả: Louis Ben</li>\r\n"
    " \t<li>Số trang: 22</li>\r\n</ul>"
)
FRAGMENTS = [
    "<h3>Tác giả</h3>\n<p>Tác giả của ấn phẩm là Louis Ben.</p>\n",
    "<li>Tác giả: Louis Ben</li>\n",
]
ATTRS_AFTER = [{"name": "Tác giả", "options": ["Louis Ben"], "visible": True}]


def render(raw: str) -> str:
    """A tiny stand-in for wpautop: LF, trimmed lines, bare text in <p>."""
    lines = [line.strip() for line in raw.replace("\r\n", "\n").split("\n")]
    return "".join((line if line.startswith("<") else f"<p>{line}</p>") + "\n" for line in lines if line)


EXPECTED_RAW = (
    "<p>Mở đầu.</p>\r\n<h3>Thông tin sản phẩm</h3>\r\n<ul>\r\n \t<li>Số trang: 22</li>\r\n</ul>"
)


def make_item() -> dict:
    return {
        "woo_id": 4769, "sku": "TSYC-X", "name": "Sách X",
        "precondition": {"status": "publish", "date_modified": "2026-10-14T21:08:38",
                         "description_sha256": rep.sha256(render(RAW_BEFORE))},
        "change": {"description": {"remove_exact_fragments": FRAGMENTS,
                                   "after_sha256": rep.sha256(render(EXPECTED_RAW))},
                   "attributes_after": ATTRS_AFTER},
        "rollback": {"attributes": []},
    }


class FakeClient:
    def __init__(self, product: dict, fail_put: Exception | None = None, apply_on_fail: bool = False):
        self.product = product
        self.puts: list[dict] = []
        self.fail_put, self.apply_on_fail = fail_put, apply_on_fail

    def get(self, product_id, context):
        data = copy.deepcopy(self.product)
        if context == "view":
            data["description"] = render(data["description"])
        return data

    def update(self, product_id, payload):
        assert set(payload) == rep.SENT_FIELDS
        self.puts.append(payload)
        if self.fail_put and not self.apply_on_fail:
            raise self.fail_put
        self.product["description"] = payload["description"]
        self.product["attributes"] = payload["attributes"]
        self.product["date_modified"] = "2026-10-19T10:00:00"
        if self.fail_put:
            raise self.fail_put
        return self.product


def make_product(**overrides) -> dict:
    product = {
        "id": 4769, "sku": "TSYC-X", "name": "Sách X", "status": "publish", "slug": "sach-x",
        "permalink": "https://x/sach-x", "regular_price": "6.9", "sale_price": "", "price": "6.9",
        "manage_stock": True, "stock_quantity": 0, "backorders": "yes", "stock_status": "onbackorder",
        "catalog_visibility": "visible", "short_description": "Ngắn.", "date_created": "2026-10-14",
        "date_modified": "2026-10-14T21:08:38", "description": RAW_BEFORE, "attributes": [],
        "images": [{"id": 1}, {"id": 2}], "categories": [{"id": 9}], "tags": [],
    }
    product.update(overrides)
    return product


@pytest.fixture
def package(tmp_path):
    path = tmp_path / "package_v2.json"
    path.write_text(json.dumps({"package_version": 2, "items": [make_item()]}, ensure_ascii=False), encoding="utf-8")
    return path


def test_raw_pattern_handles_missing_p_wrapper_crlf_and_indent():
    assert rep.repair_raw_description(RAW_BEFORE, FRAGMENTS) == EXPECTED_RAW


def test_fragment_must_match_exactly_once():
    doubled = RAW_BEFORE + "\r\n<li>Tác giả: Louis Ben</li>"
    assert rep.repair_raw_description(doubled, FRAGMENTS) is None
    assert rep.repair_raw_description("<p>khác</p>", FRAGMENTS) is None


def test_fragment_never_matches_inside_prose():
    prose = "<p>Tác giả của ấn phẩm là Louis Ben. Và hơn nữa</p>"
    assert rep.repair_raw_description(prose, [FRAGMENTS[0]]) is None


def test_apply_sends_only_description_and_attributes_and_preserves_store_fields(package):
    client = FakeClient(make_product())
    reports = rep.run(package, [4769], 12, client, dry_run=False)
    assert reports[0]["result"] == rep.APPLIED
    assert set(client.puts[0]) == {"description", "attributes"}
    assert client.product["description"] == EXPECTED_RAW
    for field in ("status", "regular_price", "stock_quantity", "backorders", "images", "categories", "slug"):
        assert client.product[field] == make_product()[field]


def test_rollback_record_written_before_put(package):
    client = FakeClient(make_product())
    seen = {}
    original_update = client.update

    def update(product_id, payload):
        seen["records"] = list((package.parent / "rollback").glob("4769_*.json"))
        return original_update(product_id, payload)

    client.update = update
    rep.run(package, [4769], 12, client, dry_run=False)
    assert len(seen["records"]) == 1
    record = json.loads(seen["records"][0].read_text(encoding="utf-8"))
    assert record["restore_payload"]["description"] == RAW_BEFORE
    assert record["preserved_fields_before"]["regular_price"] == "6.9"


def test_owner_edit_after_baseline_is_isolated_without_write(package):
    client = FakeClient(make_product(description=RAW_BEFORE + "\r\nChủ shop thêm."))
    reports = rep.run(package, [4769], 12, client, dry_run=False)
    assert reports[0]["result"] == rep.OWNER_EDITED
    assert client.puts == []


def test_date_modified_change_is_isolated(package):
    client = FakeClient(make_product(date_modified="2026-10-18T00:00:00"))
    assert rep.run(package, [4769], 12, client, dry_run=False)[0]["result"] == rep.OWNER_EDITED
    assert client.puts == []


@pytest.mark.parametrize("override", [{"sku": "OTHER"}, {"status": "draft"}, {"name": "Khác"}])
def test_identity_or_status_mismatch_is_isolated(package, override):
    client = FakeClient(make_product(**override))
    assert rep.run(package, [4769], 12, client, dry_run=False)[0]["result"] == rep.IDENTITY_MISMATCH
    assert client.puts == []


def test_dry_run_writes_nothing(package):
    client = FakeClient(make_product())
    assert rep.run(package, [4769], 12, client, dry_run=True)[0]["result"] == rep.DRY_RUN
    assert client.puts == []
    assert not (package.parent / "rollback").exists()


def test_rerun_resumes_from_checkpoint_without_second_write(package):
    client = FakeClient(make_product())
    rep.run(package, [4769], 12, client, dry_run=False)
    again = rep.run(package, [4769], 12, client, dry_run=False)
    assert again[0]["result"] == rep.APPLIED and len(client.puts) == 1


def test_already_repaired_product_is_reconciled_not_rewritten(package):
    client = FakeClient(make_product(description=EXPECTED_RAW, attributes=ATTRS_AFTER))
    assert rep.run(package, [4769], 12, client, dry_run=False)[0]["result"] == rep.ALREADY_APPLIED
    assert client.puts == []


def test_uncertain_put_is_not_retried_and_stops_batch(package):
    client = FakeClient(make_product(), fail_put=requests.Timeout("t"))
    reports = rep.run(package, [4769, 4771], 12, client, dry_run=False)
    assert reports[0]["result"] == rep.UNCERTAIN
    assert len(client.puts) == 1 and len(reports) == 1


def test_timeout_with_remote_success_is_reconciled_as_applied(package):
    client = FakeClient(make_product(), fail_put=requests.Timeout("t"), apply_on_fail=True)
    assert rep.run(package, [4769], 12, client, dry_run=False)[0]["result"] == rep.APPLIED
    assert len(client.puts) == 1


def test_ids_outside_package_and_max_products(package):
    client = FakeClient(make_product())
    assert rep.run(package, [9999], 12, client, dry_run=False)[0]["result"] == rep.NOT_IN_PACKAGE
    with pytest.raises(RuntimeError):
        rep.run(package, [4769, 4771], 1, client, dry_run=False)


def test_client_refuses_other_fields():
    client = rep.WooClient("https://x", "wc/v3", "k", "s", 5)
    with pytest.raises(RuntimeError):
        client.update(1, {"description": "", "attributes": [], "regular_price": "1"})


def test_rollback_restores_only_while_repair_is_untouched(package):
    client = FakeClient(make_product())
    rep.run(package, [4769], 12, client, dry_run=False)
    result = rep.rollback_one(4769, client, package.parent, dry_run=False)
    assert result["result"] == rep.ROLLED_BACK and client.product["description"] == RAW_BEFORE

    client2 = FakeClient(make_product())
    rep.run(package, [4769], 12, client2, dry_run=False)  # checkpoint says APPLIED: no write
    client2.product["description"] = EXPECTED_RAW + "\r\nSửa tay."
    assert rep.rollback_one(4769, client2, package.parent, dry_run=False)["result"] == rep.ROLLBACK_REFUSED
