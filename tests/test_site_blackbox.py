"""The developer's black-box package (tools/site_blackbox), offline."""
from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "tools" / "site_blackbox"
sys.path.insert(0, str(PACKAGE))

import site_blackbox as bbx  # noqa: E402

from src.domain import integration_gates as ig  # noqa: E402

HOST = "copy.example.test"
TRIO = {"vi": 201, "en": 202, "de": 203}


def product(pid: int, **overrides: Any) -> dict[str, Any]:
    values = {"id": pid, "status": "publish", "type": "simple", "title": f"Book {pid}", "slug": f"book-{pid}",
              "modified_gmt": "2026-11-01 00:00:00", "content_sha256": "c", "excerpt_sha256": "e", "sku": f"SKU-{pid}",
              "regular_price": "90000", "sale_price": "", "price": "90000", "manage_stock": "yes", "stock_quantity": "4",
              "backorders": "no", "stock_status": "instock", "total_sales": "0", "image_id": 5, "gallery_image_ids": [],
              "upsell_ids": [], "cross_sell_ids": [], "category_ids": [1], "attributes_sha256": "a", "weight": "",
              "tsyc_internal_product_id": "", "tsyc_language": "", "language": "vi", "translations": {"vi": pid}}
    return {**values, **overrides}


def snapshot(products: list[dict[str, Any]], host: str = HOST, label: str = "x") -> dict[str, Any]:
    return {"tsyc_snapshot": 1, "label": label, "taken_at_gmt": "2026-11-16T00:00:00Z",
            "site": {"host": host, "environment_type": "staging"}, "product_count": len(products),
            "products": {str(p["id"]): p for p in products}}


def member(lang: str, *, bridge: bool = True, **overrides: Any) -> dict[str, Any]:
    values = dict(status="draft", sku="NEW-1" if lang == "vi" else "", regular_price="", sale_price="", price="",
                  manage_stock="yes", stock_quantity="0", backorders="yes", stock_status="onbackorder", language=lang,
                  translations=dict(TRIO), tsyc_internal_product_id="book-1" if bridge or lang == "vi" else "",
                  tsyc_language=lang if bridge and lang != "vi" else "")
    return product(TRIO[lang], **{**values, **overrides})


BYSTANDERS = [product(1), product(2, language="en", translations={"en": 2})]


def trio_products(**per_language: dict[str, Any]) -> list[dict[str, Any]]:
    bridge = per_language.pop("bridge", True)
    return [member(lang, bridge=bridge, **per_language.get(lang, {})) for lang in TRIO]


def write_run(folder: Path, steps: dict[str, list[dict[str, Any]]], host: str = HOST) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for stem, products in steps.items():
        (folder / f"{stem}.json").write_text(json.dumps(snapshot(products, host, stem)), encoding="utf-8")
    return folder


def statuses(report: dict[str, Any], key: str) -> dict[str, str]:
    return {item["id"]: item["status"] for item in report[key]}


def full_run(**changes: Any) -> dict[str, list[dict[str, Any]]]:
    """Every step, on a site that syncs nothing between copies."""
    steps = {"00_baseline": list(BYSTANDERS), "01_trio_created": BYSTANDERS + trio_products()}
    state = {lang: {} for lang in TRIO}

    def add(stem: str, lang: str, **fields: Any) -> None:
        state[lang] = {**state[lang], **fields}
        steps[stem] = copy.deepcopy(BYSTANDERS) + trio_products(**copy.deepcopy(state))

    add("02_price_on_vi", "vi", regular_price="111000", price="111000")
    add("03_price_on_en", "en", regular_price="222000", price="222000")
    add("04_stock_on_vi", "vi", stock_quantity="5", stock_status="instock")
    add("05_backorders_on_de", "de", backorders="no")
    for lang in TRIO:
        state[lang] = {**state[lang], "status": "publish", "backorders": "yes"}
    add("06_published", "vi")
    add("07_order_on_en", "en", stock_quantity="-1", total_sales="1")
    add("08_order_on_de", "de", stock_quantity="-1", total_sales="1")
    add("09_concurrent_orders", "vi", stock_quantity="4", total_sales="1")
    add("09_concurrent_orders", "en", stock_quantity="-2", total_sales="2")
    for lang in TRIO:
        state[lang] = {**state[lang], "status": "trash"}
    add("10_cleanup", "vi")
    steps.update(changes)
    return steps


# ------------------------------------------------------------------- diff


def test_diff_reports_added_removed_and_changed_fields() -> None:
    before = snapshot([product(1), product(2), product(3)])
    after = snapshot([product(1), product(2, regular_price="1", stock_quantity="3"), product(4)])
    delta = bbx.diff_snapshots(before, after)
    assert delta["added"] == [4] and delta["removed"] == [3]
    assert delta["changed"] == {"2": {"regular_price": ["90000", "1"], "stock_quantity": [4.0, 3.0]}}
    assert bbx.is_empty(bbx.diff_snapshots(before, before))
    assert bbx.outside_changes(delta, [2, 4]) == {"added": [], "removed": [3], "changed": {}}


def test_a_save_with_no_field_change_still_shows() -> None:
    delta = bbx.diff_snapshots(snapshot([product(1)]), snapshot([product(1, modified_gmt="2026-11-02 00:00:00")]))
    assert list(delta["changed"]["1"]) == ["modified_gmt"]


def test_equal_stock_written_differently_is_not_a_change() -> None:
    assert bbx.is_empty(bbx.diff_snapshots(snapshot([product(1, stock_quantity="4")]), snapshot([product(1, stock_quantity="4.0")])))


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])
def test_snapshot_loads_from_any_shell_redirect(tmp_path: Path, encoding: str) -> None:
    path = tmp_path / "s.json"
    path.write_text("Notice: something from WP-CLI\n" + json.dumps(snapshot([product(1, title="Sách")])) + "\n", encoding=encoding)
    assert bbx.load_snapshot(path)["products"]["1"]["title"] == "Sách"


@pytest.mark.parametrize("text", ["", "not json", json.dumps({"tsyc_snapshot": 2, "products": {}}),
                                  json.dumps({**snapshot([product(1)]), "product_count": 5})])
def test_an_unusable_snapshot_is_an_error_never_no_change(tmp_path: Path, text: str) -> None:
    path = tmp_path / "s.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(bbx.SnapshotError):
        bbx.load_snapshot(path)
    with pytest.raises(bbx.SnapshotError):
        bbx.load_snapshot(tmp_path / "missing.json")


# --------------------------------------------------------------- evaluate


def test_complete_bridge_run_passes_gate_a_checks_and_only_observes_gate_b(tmp_path: Path) -> None:
    report = bbx.evaluate(write_run(tmp_path, full_run()))
    assert report["status"] == "EVALUATED" and report["trio"] == TRIO and report["created_by"] == bbx.CREATED_BY_BRIDGE
    assert set(statuses(report, "gate_a_site_checks").values()) == {bbx.PASS}
    assert set(statuses(report, "gate_b_checks").values()) == {bbx.OBSERVED}
    evidence = {item["id"]: item["evidence"] for item in report["gate_b_checks"]}
    assert "EN unchanged" in evidence["B01"] and "DE unchanged" in evidence["B01"]
    assert "was kept" in evidence["B07"]
    gate_b = ig.evaluate_gate_b(ig.check_results(report["gate_b_checks"]), environment=ig.ENV_ACTUAL_SITE_COPY)
    assert gate_b.status == ig.GATE_OBSERVED_PENDING_DECISION


def test_a_site_that_syncs_prices_is_described_not_judged(tmp_path: Path) -> None:
    steps = full_run()
    for stem in list(steps)[2:]:
        for item in steps[stem]:
            if item["id"] in (TRIO["en"], TRIO["de"]) and stem == "02_price_on_vi":
                item["regular_price"] = "111000"
    report = bbx.evaluate(write_run(tmp_path, steps))
    evidence = {item["id"]: item["evidence"] for item in report["gate_b_checks"]}
    assert "EN FOLLOWED" in evidence["B01"] and "DE FOLLOWED" in evidence["B01"]
    assert statuses(report, "gate_b_checks")["B01"] == bbx.OBSERVED


def test_an_overwritten_owner_price_is_reported(tmp_path: Path) -> None:
    steps = full_run()
    for stem in list(steps)[3:-1]:
        for item in steps[stem]:
            if item["id"] == TRIO["vi"]:
                item["regular_price"] = "222000"
    evidence = {item["id"]: item["evidence"] for item in bbx.evaluate(write_run(tmp_path, steps))["gate_b_checks"]}
    assert "did NOT survive" in evidence["B07"]


def test_missing_steps_are_blocked_never_passed(tmp_path: Path) -> None:
    steps = {k: v for k, v in full_run().items() if k in ("00_baseline", "01_trio_created", "02_price_on_vi")}
    report = bbx.evaluate(write_run(tmp_path, steps))
    gate_b = statuses(report, "gate_b_checks")
    assert gate_b["B02"] == gate_b["B03"] == gate_b["B04"] == gate_b["B05"] == gate_b["B01"] == bbx.BLOCKED
    assert bbx.PASS not in gate_b.values() and bbx.OBSERVED not in gate_b.values()
    assert ig.evaluate_gate_b(ig.check_results(report["gate_b_checks"]), environment=ig.ENV_ACTUAL_SITE_COPY).status == ig.GATE_BLOCKED

    empty = bbx.evaluate(write_run(tmp_path / "empty", {}))
    assert empty["status"] == "BLOCKED_NO_SNAPSHOTS"
    assert set(statuses(empty, "gate_a_site_checks").values()) | set(statuses(empty, "gate_b_checks").values()) == {bbx.BLOCKED}


def test_a_change_outside_the_trio_fails_isolation(tmp_path: Path) -> None:
    steps = full_run()
    steps["04_stock_on_vi"][0]["stock_quantity"] = "3"   # a bystander lost stock
    for stem in list(steps)[5:]:
        steps[stem][0]["stock_quantity"] = "3"
    report = bbx.evaluate(write_run(tmp_path, steps))
    assert statuses(report, "gate_a_site_checks")["A11"] == bbx.FAIL
    assert sorted(report["isolation"]["04_stock_on_vi"]["changed"]) == ["1"]
    gate_a = ig.evaluate_gate_a(ig.check_results(report["gate_a_site_checks"]), environment=ig.ENV_ACTUAL_SITE_COPY)
    assert gate_a.status == ig.GATE_FAIL


def test_a_price_or_wrong_inventory_on_the_new_drafts_fails(tmp_path: Path) -> None:
    steps = {"00_baseline": BYSTANDERS, "01_trio_created": BYSTANDERS + trio_products(
        en={"regular_price": "5", "price": "5"}, de={"backorders": "no"})}
    site = statuses(bbx.evaluate(write_run(tmp_path, steps)), "gate_a_site_checks")
    assert site["A08"] == bbx.FAIL and site["A07"] == bbx.FAIL and site["A05"] == bbx.PASS


def test_a_hand_made_trio_is_an_observation_not_a_gate_a_result(tmp_path: Path) -> None:
    steps = {"00_baseline": BYSTANDERS, "01_trio_created": BYSTANDERS + trio_products(bridge=False)}
    report = bbx.evaluate(write_run(tmp_path, steps))
    assert report["created_by"] == bbx.CREATED_BY_HAND
    assert set(statuses(report, "gate_a_site_checks").values()) == {bbx.BLOCKED}
    assert "not a Gate A result" in report["gate_a_site_checks"][0]["evidence"]


@pytest.mark.parametrize("products, reason", [
    (BYSTANDERS + trio_products()[:2], "added 2"),
    (BYSTANDERS + trio_products(de={"language": "en"}), "languages"),
    (BYSTANDERS + trio_products(de={"translations": {"de": 203}}), "linked"),
])
def test_an_unclear_trio_stops_the_evaluation(tmp_path: Path, products: list[dict[str, Any]], reason: str) -> None:
    report = bbx.evaluate(write_run(tmp_path, {"00_baseline": BYSTANDERS, "01_trio_created": products}))
    assert report["status"] == "TRIO_NOT_DETECTED" and reason in report["gate_a_site_checks"][0]["evidence"]
    assert bbx.PASS not in statuses(report, "gate_a_site_checks").values()
    assert set(statuses(report, "gate_b_checks").values()) == {bbx.BLOCKED}


@pytest.mark.parametrize("host", sorted(bbx.PRODUCTION_HOSTS))
def test_snapshots_from_production_are_refused(tmp_path: Path, host: str) -> None:
    report = bbx.evaluate(write_run(tmp_path, full_run(), host=host))
    assert report["status"] == "INVALID_PRODUCTION_HOST"
    assert set(statuses(report, "gate_a_site_checks").values()) | set(statuses(report, "gate_b_checks").values()) == {bbx.BLOCKED}
    assert bbx.main(["evaluate", str(tmp_path)]) == 4


def test_mixed_hosts_are_refused(tmp_path: Path) -> None:
    folder = write_run(tmp_path, full_run())
    (folder / "02_price_on_vi.json").write_text(json.dumps(snapshot(BYSTANDERS + trio_products(), "other.example.test")), encoding="utf-8")
    assert bbx.evaluate(folder)["status"] == "INVALID_MIXED_HOSTS"


def test_cli_exit_codes(tmp_path: Path) -> None:
    assert bbx.main(["evaluate", str(write_run(tmp_path / "ok", full_run()))]) == 0
    assert (tmp_path / "ok" / "blackbox_report.json").exists()
    bad = full_run()
    bad["01_trio_created"] = BYSTANDERS + trio_products(en={"regular_price": "5"})
    assert bbx.main(["evaluate", str(write_run(tmp_path / "bad", {k: bad[k] for k in ("00_baseline", "01_trio_created")}))]) == 1


# ------------------------------------------------------------- the PHP files


def php(name: str) -> str:
    text = (PACKAGE / name).read_text(encoding="utf-8")
    return re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)


def test_the_snapshot_script_cannot_write() -> None:
    code = php("tsyc-site-snapshot.php")
    forbidden = (r"\b(update|add|delete)_(option|post_meta|post|term|user_meta)\b", r"\bwp_(insert|update|delete|trash|set)_\w+",
                 r"->save\s*\(", r"->(insert|update|delete|replace)\s*\(", r"\b(INSERT|UPDATE|DELETE|REPLACE|ALTER|DROP|TRUNCATE)\b",
                 r"\bwp_remote_\w+", r"\bcurl_\w+", r"\bfile_put_contents\b", r"\bfopen\b", r"\bpll_(set|save)_\w+",
                 r"\bset_transient\b", r"\bdo_action\b", r"\beval\s*\(")
    for pattern in forbidden:
        assert not re.search(pattern, code), pattern
    assert len(re.findall(r"\$wpdb->", code)) == 2 and "SELECT ID" in code     # one query, one table name
    for field in ("status", "regular_price", "sale_price", "stock_quantity", "backorders", "sku", "image_id",
                  "gallery_image_ids", "translations", "upsell_ids", "cross_sell_ids"):
        assert f"'{field}'" in code, field
    # Text is hashed, and nothing about customers, orders or keys is read.
    assert "'content_sha256'" in code and "post_content'" not in code.replace("row->post_content", "")
    for private in ("shop_order", "wc_orders", "user_email", "woocommerce_api_keys", "consumer_secret", "wp_users"):
        assert private not in code


def test_the_driver_refuses_production_and_never_prices_or_publishes() -> None:
    code = php("tsyc-bridge-trio-driver.php")
    assert "'tiemsachyeucon.com'" in code and "PRODUCTION_HOST_REFUSED" in code
    assert "array('local', 'staging')" in code and "tsyc_bridge_writes_enabled()" in code
    guard, first_write = code.index("tsyc_bbx_guard();"), code.index("new WC_Product_Simple")
    assert guard < first_write
    for forbidden in ("set_regular_price", "set_sale_price", "set_price", "_regular_price", "_sale_price", "'publish'",
                      "wp_delete_post", "wp_publish_post", "wp_remote_", "curl_"):
        assert forbidden not in code, forbidden
    assert re.findall(r"set_status\('(\w+)'\)", code) == ["draft"]
    # Cleanup only trashes products carrying this driver's own SKU prefix and book ID.
    assert "NOT_A_DRIVER_PRODUCT" in code and "TSYC_BBX_SKU_PREFIX" in code


def test_the_comparison_tool_needs_nothing_but_the_standard_library() -> None:
    source = (PACKAGE / "site_blackbox.py").read_text(encoding="utf-8")
    imports = set(re.findall(r"^(?:from|import)\s+([\w.]+)", source, flags=re.M))
    assert imports <= {"__future__", "argparse", "json", "sys", "pathlib", "typing"}, imports
    assert "requests" not in source and "urllib" not in source and "socket" not in source
