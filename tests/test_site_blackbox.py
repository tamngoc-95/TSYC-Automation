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


def vi_alone(**overrides: Any) -> dict[str, Any]:
    """The VI draft as it is before any translation exists."""
    return member("vi", **{"translations": {"vi": TRIO["vi"]}, **overrides})


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
    steps = {"00_baseline": list(BYSTANDERS), "00b_vi_created": BYSTANDERS + [vi_alone()],
             "01_trio_created": BYSTANDERS + trio_products()}
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
    for stem in list(steps)[3:]:
        for item in steps[stem]:
            if item["id"] in (TRIO["en"], TRIO["de"]) and stem == "02_price_on_vi":
                item["regular_price"] = "111000"
    report = bbx.evaluate(write_run(tmp_path, steps))
    evidence = {item["id"]: item["evidence"] for item in report["gate_b_checks"]}
    assert "EN FOLLOWED" in evidence["B01"] and "DE FOLLOWED" in evidence["B01"]
    assert statuses(report, "gate_b_checks")["B01"] == bbx.OBSERVED


def test_an_overwritten_owner_price_is_reported(tmp_path: Path) -> None:
    steps = full_run()
    for stem in list(steps)[4:-1]:
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
    for stem in list(steps)[6:]:
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
    assert site["A15"] == bbx.BLOCKED       # no VI-only snapshot was taken


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


def test_an_empty_catalog_is_a_valid_snapshot(tmp_path: Path) -> None:
    # Found running the shipped package on an emptied lab: PHP encodes an empty array as [].
    for name, products in (("list.json", []), ("object.json", {})):
        (tmp_path / name).write_text(json.dumps({"tsyc_snapshot": 1, "label": "x", "site": {"host": HOST},
                                                 "product_count": 0, "products": products}), encoding="utf-8")
        assert bbx.load_snapshot(tmp_path / name)["products"] == {}
    (tmp_path / "bad.json").write_text(json.dumps({"tsyc_snapshot": 1, "product_count": 2, "products": []}), encoding="utf-8")
    with pytest.raises(bbx.SnapshotError):
        bbx.load_snapshot(tmp_path / "bad.json")                 # a list that claims products is still refused
    assert "$products ? $products : new stdClass()" in php("tsyc-site-snapshot.php")


def test_the_harness_judges_a_site_that_starts_with_no_products(tmp_path: Path) -> None:
    site = FakeSite()
    site.products = []
    report = harness(tmp_path, site)
    assert report["status"] == "EVALUATED" and report["real_site_compatibility"] == bbx.PASS


def test_the_isolation_check_is_read_only_and_fails_closed() -> None:
    code = php("tsyc-isolation.php")
    for pattern in (r"\b(update|add|delete)_(option|post_meta|post|term|user_meta)\b", r"\bwp_(insert|update|delete|trash|set)_\w+",
                    r"->save\s*\(", r"->(insert|update|delete|replace|query)\s*\(", r"\b(INSERT|UPDATE|DELETE|REPLACE|ALTER|DROP)\b",
                    r"\bwp_remote_\w+", r"\bcurl_\w+", r"\bfile_put_contents\b", r"\bfopen\b", r"\bwp_mail\b"):
        assert not re.search(pattern, code), pattern
    for check in ("HOST_NOT_PRODUCTION", "ENVIRONMENT_TYPE", "OUTBOUND_HTTP_BLOCKED", "OPERATOR_ATTESTATION"):
        assert f"'{check}'" in code
    # Isolated only when every check is ok; the attestation must equal this copy's own host.
    assert "$isolated = $isolated && $check['ok']" in code and "$attested === $hosts['home_url']" in code
    assert "WP_HTTP_BLOCK_EXTERNAL === true" in code and "'tiemsachyeucon.com'" in code
    assert "stored_" in code and "option_name IN ('home', 'siteurl')" in code         # the stored address, not only filters


def test_the_driver_establishes_isolation_before_any_write() -> None:
    code = php("tsyc-bridge-trio-driver.php")
    guard_body = code[code.index("function tsyc_bbx_guard()"):code.index("function tsyc_bbx_bridge_post")]
    assert "tsyc-isolation.php" in guard_body and "tsyc_bbx_isolation()" in guard_body
    assert "$isolation['isolated'] !== true" in guard_body and "NOT_ISOLATED" in guard_body
    assert guard_body.index("NOT_ISOLATED") < guard_body.index("PRODUCTION_HOST_REFUSED")
    # A missing library is a refusal, not a skipped check.
    assert "!is_readable($library)" in guard_body


def test_the_comparison_tool_needs_nothing_but_the_standard_library() -> None:
    source = (PACKAGE / "site_blackbox.py").read_text(encoding="utf-8")
    imports = set(re.findall(r"^(?:from|import)\s+([\w.]+)", source, flags=re.M))
    assert imports <= {"__future__", "argparse", "hashlib", "json", "os", "shlex", "subprocess", "sys", "time", "zipfile",
                       "pathlib", "typing"}, imports
    assert "requests" not in source and "urllib" not in source and "socket" not in source and "http" not in source.lower()


# ------------------------------------ did creating EN/DE change anything?


def test_an_untouched_vi_draft_and_catalog_read_as_unchanged_in_every_group(tmp_path: Path) -> None:
    report = bbx.evaluate(write_run(tmp_path, {k: v for k, v in full_run().items() if k < "02"}))
    assert statuses(report, "gate_a_site_checks") == {cid: bbx.PASS for cid in ig.GATE_A_SITE_CHECKS}
    assert report["real_site_compatibility"] == bbx.PASS
    vi, others = report["compatibility"]["vi_draft"], report["compatibility"]["every_other_product"]
    assert set(vi) == set(others) == set(bbx.FIELD_GROUPS)
    assert vi["translation_links"] == "UNCHANGED apart from the two new links"
    assert {value for group, value in vi.items() if group != "translation_links"} == {"UNCHANGED"}
    assert set(others.values()) == {"UNCHANGED"}
    gate = ig.evaluate_gate_a(ig.check_results(report["gate_a_site_checks"]), environment=ig.ENV_ACTUAL_SITE_COPY)
    assert gate.status == ig.GATE_PASS


@pytest.mark.parametrize("change, group", [
    ({"regular_price": "50000", "price": "50000"}, "price"),
    ({"stock_quantity": "3"}, "stock"),
    ({"backorders": "no"}, "backorders"),
    ({"sku": "CHANGED"}, "sku"),
    ({"image_id": 99}, "images"),
    ({"status": "publish"}, "status"),
    ({"title": "Renamed"}, "owner_maintained_fields"),
    ({"content_sha256": "other"}, "owner_maintained_fields"),
    ({"modified_gmt": "2026-11-16 09:00:00"}, "saved_again"),
])
def test_a_change_to_the_vi_draft_during_the_create_fails_and_names_the_group(tmp_path: Path, change: dict[str, Any], group: str) -> None:
    steps = {"00_baseline": BYSTANDERS, "00b_vi_created": BYSTANDERS + [vi_alone()],
             "01_trio_created": BYSTANDERS + trio_products(vi=change)}
    report = bbx.evaluate(write_run(tmp_path, steps))
    assert statuses(report, "gate_a_site_checks")["A15"] == bbx.FAIL
    assert report["compatibility"]["vi_draft"][group].startswith("CHANGED")
    assert report["real_site_compatibility"] == bbx.FAIL


def test_a_vi_draft_that_lost_or_gained_other_links_fails(tmp_path: Path) -> None:
    steps = {"00_baseline": BYSTANDERS, "00b_vi_created": BYSTANDERS + [vi_alone(translations={"vi": TRIO["vi"], "en": 2})],
             "01_trio_created": BYSTANDERS + trio_products()}
    report = bbx.evaluate(write_run(tmp_path, steps))
    assert statuses(report, "gate_a_site_checks")["A15"] == bbx.PASS     # it ended exactly as the trio
    broken = {**steps, "01_trio_created": BYSTANDERS + trio_products(vi={"language": "en"})}
    assert bbx.evaluate(write_run(tmp_path / "b", broken))["status"] == "TRIO_NOT_DETECTED"


@pytest.mark.parametrize("field, group", [("regular_price", "price"), ("stock_quantity", "stock"), ("backorders", "backorders"),
                                         ("sku", "sku"), ("image_id", "images"), ("status", "status"),
                                         ("translations", "translation_links"), ("slug", "owner_maintained_fields")])
def test_a_change_to_any_other_product_during_the_create_is_named_by_group(tmp_path: Path, field: str, group: str) -> None:
    touched = copy.deepcopy(BYSTANDERS)
    touched[0][field] = {"vi": 1, "en": 2} if field == "translations" else "x9"
    steps = {"00_baseline": BYSTANDERS, "00b_vi_created": BYSTANDERS + [vi_alone()],
             "01_trio_created": touched + trio_products()}
    report = bbx.evaluate(write_run(tmp_path, steps))
    assert report["compatibility"]["every_other_product"][group] == "CHANGED on #1"
    assert statuses(report, "gate_a_site_checks")["A11"] == bbx.FAIL and report["real_site_compatibility"] == bbx.FAIL


def test_copies_that_are_another_book_or_lost_the_image_fail(tmp_path: Path) -> None:
    def run(folder: str, **per_language: Any) -> dict[str, str]:
        steps = {"00_baseline": BYSTANDERS, "00b_vi_created": BYSTANDERS + [vi_alone()],
                 "01_trio_created": BYSTANDERS + trio_products(**per_language)}
        return statuses(bbx.evaluate(write_run(tmp_path / folder, steps)), "gate_a_site_checks")

    assert run("sku", en={"sku": "OTHER-BOOK"})["A06"] == bbx.FAIL
    assert run("copied", en={"sku": "NEW-1"})["A06"] == bbx.PASS            # the VI SKU copied by the site: one book
    assert run("image", de={"image_id": 0})["A10"] == bbx.FAIL
    assert run("gallery", de={"gallery_image_ids": [3]})["A10"] == bbx.FAIL


@pytest.mark.parametrize("steps", [{}, {"00_baseline": BYSTANDERS},
                                   {"00_baseline": BYSTANDERS, "01_trio_created": BYSTANDERS + trio_products()},
                                   {"00_baseline": BYSTANDERS, "00b_vi_created": BYSTANDERS + [vi_alone()],
                                    "01_trio_created": BYSTANDERS + trio_products(bridge=False)}])
def test_without_a_complete_bridge_run_the_real_site_test_is_blocked_never_passed(tmp_path: Path, steps: dict[str, Any]) -> None:
    report = bbx.evaluate(write_run(tmp_path, steps))
    assert report["real_site_compatibility"] == bbx.BLOCKED_REAL_SITE_TEST
    # The exit code must say the same: a blocked run (missing step, trio made by hand)
    # is never 0, the code a script would read as PASS.
    assert bbx.main(["evaluate", str(tmp_path)]) == 4


# ------------------------------------------------- the one-command harness


ISOLATION_IDS = (("I1", "HOST_NOT_PRODUCTION"), ("I2", "ENVIRONMENT_TYPE"), ("I3", "OUTBOUND_HTTP_BLOCKED"),
                 ("I4", "OPERATOR_ATTESTATION"))


def isolation_answer(failing: tuple[str, ...] = (), bridge_sha256: str | None = None) -> dict[str, Any]:
    checks = [{"id": cid, "name": name, "ok": cid not in failing, "detail": f"{name} detail"} for cid, name in ISOLATION_IDS]
    return {"tsyc_isolation": 1, "isolated": not failing, "checks": checks, "bridge_version": "1.2.0",
            "bridge_sha256": bbx.expected_bridge_sha256(PACKAGE) if bridge_sha256 is None else bridge_sha256}


class FakeSite:
    """Stands in for WP-CLI on a site copy: answers the isolation, snapshot and driver calls."""

    def __init__(self, host: str = HOST, translate_ok: bool = True, change_bystander: bool = False,
                 failing: tuple[str, ...] = (), bridge_sha256: str | None = None, snapshot_isolated: bool = True,
                 hang_on: str | None = None) -> None:
        self.host, self.translate_ok, self.change_bystander = host, translate_ok, change_bystander
        self.isolation = isolation_answer(failing, bridge_sha256)
        self.snapshot_isolated, self.hang_on = snapshot_isolated, hang_on
        self.products = copy.deepcopy(BYSTANDERS)
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> Any:
        self.calls.append(list(command))
        at = command.index("eval-file")      # after whatever starts WP-CLI on this machine
        script, arguments = Path(command[at + 1]).name, command[at + 2:]
        if self.hang_on and self.hang_on in (script, arguments[0] if arguments else ""):
            raise bbx.subprocess.TimeoutExpired(command, kwargs.get("timeout"), stderr=b"still working")
        out, code = "", 0
        if script == "tsyc-isolation.php":
            out, code = json.dumps(self.isolation), (0 if self.isolation["isolated"] else 5)
        elif script == "tsyc-site-snapshot.php":
            data = snapshot(self.products, self.host, arguments[0])
            data["site"]["isolation"] = self.isolation if self.snapshot_isolated else None
            out = json.dumps(data)
        elif arguments[0] == "create-vi":
            self.products.append(vi_alone())
            out = json.dumps({"tsyc_driver": 1, "ok": True, "trio": {"vi": TRIO["vi"]}, "answers": {}})
        elif arguments[0] == "translate":
            if self.translate_ok:
                self.products = [p for p in self.products if p["id"] not in TRIO.values()] + trio_products()
                if self.change_bystander:
                    self.products[0]["stock_quantity"] = "1"
                out = json.dumps({"tsyc_driver": 1, "ok": True, "trio": TRIO, "answers": {}})
            else:
                out, code = json.dumps({"tsyc_driver": 1, "ok": False, "trio": {"vi": TRIO["vi"]},
                                        "answers": {"en": {"http_status": 409, "code": "tsyc_sibling_side_effect"}}}), 4
        elif arguments[0] == "cleanup":
            for item in self.products:
                if item["id"] in TRIO.values():
                    item["status"] = "trash"
            out = json.dumps({"tsyc_driver": 1, "ok": True, "trashed": TRIO})
        return type("Done", (), {"stdout": ("Notice: x\n" + out + "\n").encode("utf-8"), "stderr": b"", "returncode": code})()

    def writes(self) -> list[str]:
        return [call[call.index("eval-file") + 2] for call in self.calls
                if Path(call[call.index("eval-file") + 1]).name == "tsyc-bridge-trio-driver.php"]


def harness(tmp_path: Path, site: FakeSite, **options: Any) -> dict[str, Any]:
    waits: list[float] = []
    report = bbx.run_gate_a(tmp_path / "run", wp=["wp", "--path=/copy"], user="admin", run=site, sleep=waits.append,
                            say=lambda _line: None, **options)
    report["_waits"] = waits
    return report


def test_harness_runs_the_whole_gate_a_site_test_in_order(tmp_path: Path) -> None:
    site = FakeSite()
    report = harness(tmp_path, site)
    assert report["real_site_compatibility"] == bbx.PASS and report["_waits"] == [120.0]
    assert site.writes() == ["create-vi", "translate", "cleanup"]
    assert sorted(p.stem for p in (tmp_path / "run").glob("*.json")) == [
        "00_baseline", "00_isolation", "00b_vi_created", "01_trio_created", "10_cleanup", "blackbox_report"]
    assert all(call[:3] == ["wp", "--path=/copy", "eval-file"] for call in site.calls)
    assert all("--user=admin" in call for call in site.calls if "driver" in call[3])
    # The isolation check is the very first call; the summary and the call log are written.
    assert Path(site.calls[0][3]).name == "tsyc-isolation.php"
    summary = (tmp_path / "run" / "summary.txt").read_text(encoding="utf-8")
    assert summary.startswith("TSYC black-box Gate A on a site copy: PASS") and "all UNCHANGED" in summary
    log = [json.loads(line) for line in (tmp_path / "run" / "run_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(log) == len(site.calls) and all("--user=admin" not in entry["arguments"] for entry in log)
    assert not (tmp_path / "run" / "failure.json").exists()


@pytest.mark.parametrize("failing", [("I1",), ("I2",), ("I3",), ("I4",), ("I1", "I2", "I3", "I4")])
def test_harness_writes_nothing_unless_every_isolation_check_passes(tmp_path: Path, failing: tuple[str, ...]) -> None:
    site = FakeSite(failing=failing)
    with pytest.raises(bbx.HarnessError, match="Not an established isolated copy"):
        harness(tmp_path, site)
    assert site.writes() == [] and len(site.calls) == 1          # only the read-only isolation check ran
    failure = json.loads((tmp_path / "run" / "failure.json").read_text(encoding="utf-8"))
    assert failure["nothing_written"] is True and failure["cleanup"] is None and failure["retried"] is False


@pytest.mark.parametrize("answer", [None, {}, {"isolated": True}, {"isolated": True, "checks": []},
                                    {"isolated": True, "checks": [{"id": "I1", "ok": True}]},
                                    {"isolated": False, "checks": [{"id": c, "ok": True} for c in ("I1", "I2", "I3", "I4")]}])
def test_an_incomplete_or_missing_isolation_answer_is_never_isolated(answer: Any) -> None:
    assert bbx.isolation_problems(answer)


def test_harness_refuses_a_bridge_that_is_not_the_shipped_file(tmp_path: Path) -> None:
    for folder, sha in (("other", "0" * 64), ("none", "")):
        site = FakeSite(bridge_sha256=sha)
        with pytest.raises(bbx.HarnessError, match="not the file shipped"):
            harness(tmp_path / folder, site)
        assert site.writes() == []


def test_harness_refuses_when_the_baseline_does_not_confirm_isolation(tmp_path: Path) -> None:
    site = FakeSite(snapshot_isolated=False)
    with pytest.raises(bbx.HarnessError, match="baseline snapshot does not confirm isolation"):
        harness(tmp_path, site)
    assert site.writes() == []


def test_harness_refuses_without_a_reference_bridge_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "pkg" / "a" / "b"
    empty.mkdir(parents=True)
    with pytest.raises(bbx.HarnessError, match="cannot be checked"):
        bbx.expected_bridge_sha256(empty)
    site = FakeSite()
    real = bbx.expected_bridge_sha256
    monkeypatch.setattr(bbx, "expected_bridge_sha256", lambda _here: real(empty))
    with pytest.raises(bbx.HarnessError, match="cannot be checked"):
        harness(tmp_path, site)
    assert site.calls == []


def test_bridge_identity_ignores_line_endings_only(tmp_path: Path) -> None:
    (tmp_path / "lf.php").write_bytes(b"<?php\necho 1;\n")
    (tmp_path / "crlf.php").write_bytes(b"<?php\r\necho 1;\r\n")
    (tmp_path / "other.php").write_bytes(b"<?php\necho 2;\n")
    assert bbx.normalized_sha256(tmp_path / "lf.php") == bbx.normalized_sha256(tmp_path / "crlf.php")
    assert bbx.normalized_sha256(tmp_path / "lf.php") != bbx.normalized_sha256(tmp_path / "other.php")


def test_a_timeout_stops_the_run_records_where_and_retries_nothing(tmp_path: Path) -> None:
    site = FakeSite(hang_on="translate")
    with pytest.raises(bbx.HarnessError, match="did not answer within 7 s"):
        harness(tmp_path, site, timeout=7)
    assert site.writes() == ["create-vi", "translate"]           # translate was not sent again
    failure = json.loads((tmp_path / "run" / "failure.json").read_text(encoding="utf-8"))
    assert failure["stopped_at_step"] == "driver translate" and failure["vi_test_product_id"] == TRIO["vi"]
    assert failure["cleanup"] == f"wp eval-file tsyc-bridge-trio-driver.php cleanup {TRIO['vi']} --user=admin"
    last = json.loads((tmp_path / "run" / "run_log.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert last["outcome"] == "TIMEOUT after 7 s" and last["stderr_tail"] == "still working"


def test_the_default_call_timeout_is_bounded() -> None:
    assert 0 < bbx.DEFAULT_CALL_TIMEOUT <= 600


def test_pack_hashes_other_products_titles_and_still_evaluates_the_same(tmp_path: Path) -> None:
    harness(tmp_path, FakeSite())
    archive = bbx.pack_results(tmp_path / "run")
    import zipfile
    with zipfile.ZipFile(archive) as packed:
        names = set(packed.namelist())
        assert {"run/blackbox_report.json", "run/summary.txt", "run/run_log.jsonl", "run/00_isolation.json",
                "run/00_baseline.json", "run/01_trio_created.json"} <= names
        unpacked = tmp_path / "unpacked"
        packed.extractall(unpacked)
    baseline = json.loads((unpacked / "run" / "00_baseline.json").read_text(encoding="utf-8"))
    assert baseline["sanitized"] is True
    assert all(p["title"].startswith("sha256:") and p["slug"].startswith("sha256:") for p in baseline["products"].values())
    after = json.loads((unpacked / "run" / "01_trio_created.json").read_text(encoding="utf-8"))
    assert after["products"][str(TRIO["en"])]["title"] == f"Book {TRIO['en']}"            # the test trio stays readable
    (unpacked / "run" / "blackbox_report.json").unlink()
    again = bbx.evaluate(unpacked / "run")
    original = json.loads((tmp_path / "run" / "blackbox_report.json").read_text(encoding="utf-8"))
    assert again["real_site_compatibility"] == original["real_site_compatibility"] == bbx.PASS
    assert again["gate_a_site_checks"] == original["gate_a_site_checks"]


def test_harness_writes_nothing_on_the_production_host(tmp_path: Path) -> None:
    site = FakeSite(host="tiemsachyeucon.com")
    with pytest.raises(bbx.HarnessError, match="production host"):
        harness(tmp_path, site)
    assert site.writes() == []


def test_harness_reports_a_bridge_stop_as_the_finding_and_retries_nothing(tmp_path: Path) -> None:
    site = FakeSite(translate_ok=False)
    report = harness(tmp_path, site)
    assert site.writes().count("translate") == 1
    assert report["real_site_compatibility"] != bbx.PASS and report["status"] == "TRIO_NOT_DETECTED"
    assert report["harness"]["translate"]["answers"]["en"]["code"] == "tsyc_sibling_side_effect"


def test_harness_fails_when_the_site_changed_another_product(tmp_path: Path) -> None:
    report = harness(tmp_path, FakeSite(change_bystander=True))
    assert report["real_site_compatibility"] == bbx.FAIL
    assert report["compatibility"]["every_other_product"]["stock"] == "CHANGED on #1"


def test_harness_never_mixes_two_runs_in_one_folder(tmp_path: Path) -> None:
    harness(tmp_path, FakeSite())
    again = FakeSite()
    with pytest.raises(bbx.HarnessError, match="already holds snapshots"):
        harness(tmp_path, again)
    assert again.calls == []


def test_harness_cli_reports_a_refusal_as_blocked_real_site_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                 capsys: pytest.CaptureFixture[str]) -> None:
    site = FakeSite(host="www.tiemsachyeucon.com")
    monkeypatch.setattr(bbx.subprocess, "run", site)
    assert bbx.main(["run-gate-a", str(tmp_path / "r"), "--user", "admin", "--wait", "0"]) == 4
    assert bbx.BLOCKED_REAL_SITE_TEST in capsys.readouterr().err
    assert site.writes() == [] and site.calls[0][0] == "wp"     # the default command, and nothing written


def test_the_driver_splits_into_two_steps_without_a_second_write_path() -> None:
    code = php("tsyc-bridge-trio-driver.php")
    assert code.count("function tsyc_bbx_translate(") == 1 and code.count("tsyc_bbx_bridge_post('translations'") == 1
    assert code.count("new WC_Product_Simple") == 1
    assert code.index("tsyc_bbx_guard();") < code.index("=== 'translate'") < code.index("=== 'create-vi'")
