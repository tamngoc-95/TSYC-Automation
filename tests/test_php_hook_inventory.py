"""Offline tests of the static hook inventory, against a SYNTHETIC fixture.

They prove the parser and the report. They say nothing about the developer's
code, which is not in the repository.
"""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any

import pytest

from scripts import inventory_php_hooks as cli
from src.domain import php_hook_inventory as phi

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "php_hooks"
THEME = FIXTURE / "synthetic_theme"
MEASURED = json.loads((FIXTURE / "measured_hook_events.json").read_text(encoding="utf-8"))
FAKE_VALUES = ("fake-value-for-the-redaction-test", "hunter2hunter2", "cs_0123456789012345678901234567890123456789")


@pytest.fixture(scope="module")
def report() -> dict[str, Any]:
    sources = {path.relative_to(THEME).as_posix(): path.read_text(encoding="utf-8") for path in THEME.rglob("*.php")}
    return phi.build_inventory(sources, measured_events=MEASURED)


def rows(report: dict[str, Any], *, hook: str | None = None, callback: str | None = None) -> list[dict[str, Any]]:
    return [i for i in report["items"] if (hook is None or i["hook"] == hook) and (callback is None or i["callback"] == callback)]


def one(report: dict[str, Any], hook: str, callback: str) -> dict[str, Any]:
    found = rows(report, hook=hook, callback=callback)
    assert len(found) == 1, found
    return found[0]


# ------------------------------------------------------------------ lexing


def test_comments_strings_heredocs_and_html_are_not_registrations(report: dict[str, Any]) -> None:
    hooks = {i["hook"] for i in report["items"]}
    assert not {"in_a_docblock", "in_a_line_comment", "in_a_hash_comment", "in_a_string", "in_a_heredoc", "in_inline_html"} & hooks
    assert "after_html" in hooks, "PHP after ?> ... <?php must still be read."


def test_lexer_survives_broken_source() -> None:
    for broken in ("<?php add_action('a", "<?php /* never closed", "<?php $x = <<<EOT\nnever", "no php at all", "<?php add_action(", ""):
        assert isinstance(phi.lex(broken), list)
        assert phi.build_inventory({"x.php": broken})["status"] == "INVENTORIED"


def test_line_numbers_are_real() -> None:
    source = "<?php\n/* a\nb */\n\nadd_action('h', 'f');\n"
    assert phi.build_inventory({"a.php": source})["items"][0]["line"] == 5


def test_hash_attribute_is_not_a_comment() -> None:
    source = "<?php\n#[Attr]\nadd_action('after_attribute', 'f');\n"
    assert [i["hook"] for i in phi.build_inventory({"a.php": source})["items"]] == ["after_attribute"]


# ----------------------------------------------------- registration fields


def test_named_function_row_has_every_required_field(report: dict[str, Any]) -> None:
    row = one(report, "woocommerce_update_product", "tsyc_fixture_push_stock")
    assert row["kind"] == "action" and row["priority"] == 20 and row["accepted_args"] == 1
    assert row["file"] == "functions.php" and row["line"] > 0 and row["callback_line"] > row["line"]
    assert row["reads"].count("stock_quantity") == 1 and "backorders" in row["reads"]
    assert set(row["writes"]) == {"stock_quantity", "backorders"}
    assert "pll_get_post_translations" in row["sibling_apis"]
    assert any("pll_get_post_language" in c and "pll_default_language" in c for c in row["trigger_conditions"])
    assert {"STATIC_FLAG", "NOT_AUTOSAVE", "NOT_REVISION", "DEFAULT_LANGUAGE_CHECK"} <= set(row["guards"])
    assert row["direction_guess"] == "SIBLINGS_LANGUAGE_CONDITIONAL_CHECK_DEFAULT"


def test_defaults_and_negative_priority(report: dict[str, Any]) -> None:
    assert one(report, "save_post_product", "tsyc_fixture_resave")["priority"] == 10
    assert one(report, "after_html", "tsyc_fixture_after_html")["priority"] == -5


def test_closure_effects_follow_called_functions(report: dict[str, Any]) -> None:
    row = one(report, "set_object_terms", "{closure}")
    assert row["accepted_args"] == 4
    assert {"regular_price", "price"} <= set(row["writes"])
    assert "tsyc_fixture_copy_price" in row["calls"]
    assert row["direction_guess"] == "SIBLINGS_ANY_LANGUAGE"


def test_wc_object_setters_count_as_writes(report: dict[str, Any]) -> None:
    row = one(report, "woocommerce_product_set_stock", "sync::on_stock")
    assert {"stock_quantity", "regular_price", "product_save"} <= set(row["writes"])
    assert {"stock_quantity", "regular_price"} <= set(row["reads"])


def test_superglobal_reads_are_recorded(report: dict[str, Any]) -> None:
    assert "$_POST" in rows(report, hook="save_post", callback="sync::static_save")[0]["reads"]


def test_raw_sql_and_literal_loop_keys(report: dict[str, Any]) -> None:
    row = one(report, "pll_copy_post_metas", "tsyc_fixture_copy_metas")
    assert row["raw_sql_write"] is True
    assert row["kind"] == "filter" and row["priority"] == 99 and row["accepted_args"] == 2
    assert row["dynamic_meta_writes"] == ["update_post_meta"], "A loop variable is not a known key."
    assert set(row["fields_mentioned"]) == {"sku", "stock_quantity"}, "The literal list still names what is copied."


@pytest.mark.parametrize(("hook", "callback"), [
    ("save_post", "sync::on_save"),
    ("save_post", "sync::static_save"),
    ("save_post", "Tsyc\\Fixture\\Other::named_class_method"),
    ("init", "sync::on_init"),
    ("admin_init", "Tsyc\\Fixture\\namespaced_function"),
])
def test_callback_forms_are_named(report: dict[str, Any], hook: str, callback: str) -> None:
    assert rows(report, hook=hook, callback=callback), [i["callback"] for i in rows(report, hook=hook)]


def test_array_callback_with_named_class_reaches_its_body(report: dict[str, Any]) -> None:
    found = [i for i in rows(report, hook="save_post") if "named_class_method" in i["callback"]]
    assert len(found) == 2 and all(i["callback_resolved"] and "polylang_language" in i["writes"] for i in found)


def test_method_on_unknown_object_is_resolved_by_name_and_says_so(report: dict[str, Any]) -> None:
    row = rows(report, hook="woocommerce_new_product")[0]
    assert row["callback"].endswith("(by name)") and row["writes"] == ["sku"]


def test_helper_method_through_this_is_followed(report: dict[str, Any]) -> None:
    # Registered twice on purpose: in the constructor, and re-added by its own helper.
    found = rows(report, hook="save_post", callback="sync::on_save")
    assert len(found) == 2 and all("post" in r["writes"] and "REMOVES_A_HOOK" in r["guards"] for r in found)
    assert any(r["registered_in"] == "sync::helper" for r in found)


def test_what_cannot_be_read_is_admitted_not_guessed(report: dict[str, Any]) -> None:
    init = rows(report, hook="init")
    assert any(i["callback"] == "tsyc_fixture_missing_function" and not i["callback_resolved"] for i in init)
    assert any(i["callback"].startswith("<unresolved") and not i["callback_resolved"] for i in init)
    assert report["unresolved_callbacks"] >= 2


def test_dynamic_hook_names_keep_their_known_parts(report: dict[str, Any]) -> None:
    assert rows(report, hook="woocommerce_{}_product")[0]["hook_is_dynamic"] is True
    assert rows(report, hook="<dynamic>"), "An interpolated name is reported as dynamic."


def test_loader_method_is_marked_indirect(report: dict[str, Any]) -> None:
    assert [i for i in report["items"] if i["indirect"]], "$loader->add_action is reported, flagged indirect."


def test_static_wrapper_is_reported_as_indirect_and_a_definition_is_not_a_registration() -> None:
    source = ("<?php class H { protected static function add_action($hook, $cb) { add_action($hook, $cb); } "
              "function boot() { self::add_action('save_post', [$this, 'x']); } function x() {} }")
    items = phi.build_inventory({"a.php": source})["items"]
    assert [i["indirect"] for i in items if i["hook"] == "save_post"] == [True]
    assert len(items) == 2, "The inner add_action is reported as dynamic; the definition is not a registration."


def test_removals_are_recorded(report: dict[str, Any]) -> None:
    assert any(r["hook"] == "woocommerce_update_product" and r["kind"] == "remove_action" for r in report["removals"])


# ------------------------------------------------------ recursion, conflict


def test_unguarded_self_recursion_is_found(report: dict[str, Any]) -> None:
    found = [f for f in report["recursion"] if f["cycle"] == ["save_post_product"]]
    assert found and found[0]["code"] == "RECURSION_RISK"


def test_cycle_through_a_custom_action_is_found(report: dict[str, Any]) -> None:
    cycle = [f for f in report["recursion"] if set(f["cycle"]) == {"tsyc_fixture_custom", "woocommerce_product_set_stock"}]
    assert cycle and cycle[0]["code"] == "RECURSION_RISK"


def test_a_guard_is_reported_as_seen_never_as_safe() -> None:
    source = "<?php add_action('save_post','f'); function f($i){ static $b=false; if($b){return;} $b=true; wp_update_post(['ID'=>$i]); $b=false; }"
    finding = phi.build_inventory({"a.php": source})["recursion"][0]
    assert finding["code"] == "RECURSION_GUARD_SEEN" and "not shown to work" in finding["note"]


def test_same_field_same_hook_conflict(report: dict[str, Any]) -> None:
    conflict = [c for c in report["conflicts"] if c["code"] == "SAME_FIELD_SAME_HOOK" and c["hook"] == "woocommerce_update_product"
                and c["field"] == "stock_quantity"]
    assert conflict and len(conflict[0]["callbacks"]) == 2


def test_field_written_from_several_hooks_is_listed(report: dict[str, Any]) -> None:
    assert any(c["code"] == "FIELD_WRITTEN_FROM_SEVERAL_HOOKS" and c["field"] == "stock_quantity" for c in report["conflicts"])


def test_mutual_recursion_terminates() -> None:
    source = "<?php add_action('h','a'); function a(){ b(); } function b(){ a(); update_post_meta(1,'_sku','x'); }"
    assert phi.build_inventory({"a.php": source})["items"][0]["writes"] == ["sku"]


# ------------------------------------------------------- bridge relevance


def test_measured_events_drive_the_bridge_columns(report: dict[str, Any]) -> None:
    row = one(report, "set_object_terms", "{closure}")
    assert row["fires_on_bridge_create"] is True and row["measured_on_source_or_sibling"] is True
    assert row["risk"] == "HIGH"


def test_sibling_sync_on_a_hook_a_bridge_create_does_not_fire_is_medium(report: dict[str, Any]) -> None:
    row = one(report, "woocommerce_update_product", "tsyc_fixture_push_stock")
    assert row["fires_on_bridge_create"] is False, "The fixture's measured create fires woocommerce_new_product, not update."
    assert row["risk"] == "MEDIUM"


def test_without_measured_events_nothing_claims_to_fire() -> None:
    source = (THEME / "functions.php").read_text(encoding="utf-8")
    plain = phi.build_inventory({"f.php": source})
    assert plain["measured_events_used"] is False
    assert not any(i["fires_on_bridge_create"] for i in plain["items"])
    assert plain["risk_counts"].get("HIGH") is None


def test_the_measured_fixture_is_the_lab_not_the_store() -> None:
    assert "no theme" in MEASURED["note"]
    create = MEASURED["scenarios"]["bridge_create_en"]
    assert "set_object_terms" in create and "SOURCE" in create["set_object_terms"]["roles"]
    assert "woocommerce_update_product" not in create
    assert all("SOURCE" not in entry["roles"] for key, entry in create.items() if key != "set_object_terms")


def test_every_finding_says_it_is_a_lead(report: dict[str, Any]) -> None:
    assert "heuristic" in report["reading_note"] and "nothing here proves behaviour" in report["reading_note"]


# ----------------------------------------------------------------- secrets


def test_no_secret_value_reaches_any_output(report: dict[str, Any]) -> None:
    blob = json.dumps(report) + phi.to_markdown(report, relevant_only=False)
    for value in FAKE_VALUES:
        assert value not in blob


def test_suspected_secrets_are_located_without_values(report: dict[str, Any]) -> None:
    found = report["suspected_secrets"]
    assert {f["kind"] for f in found} >= {"NAMED_CREDENTIAL", "WOOCOMMERCE_KEY"}
    assert all(set(f) == {"file", "line", "kind"} for f in found)
    assert len(found) == 3


def test_secret_in_a_condition_is_redacted() -> None:
    source = "<?php add_action('save_post','f'); function f(){ if ($x === 'sk_live_abcdefghijkl' || $y === 'plain words here') { update_post_meta(1,'_sku','x'); } }"
    condition = phi.build_inventory({"a.php": source})["items"][0]["trigger_conditions"][0]
    assert "sk_live" not in condition and "plain words" not in condition and "'...'" in condition


def test_secret_shaped_hook_name_is_redacted() -> None:
    source = "<?php add_action('ck_0123456789012345678901234567890123456789', 'f');"
    assert phi.build_inventory({"a.php": source})["items"][0]["hook"] == "<redacted>"


# --------------------------------------------------------------------- CLI


def make_zip(path: Path, entries: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as bundle:
        for name, text in entries.items():
            bundle.writestr(name, text)
    return path


def run_cli(tmp_path: Path, *args: str) -> tuple[int, dict[str, Any] | None]:
    out = tmp_path / "out"
    code = cli.main([*args, "--out", str(out), "--measured-events", str(FIXTURE / "measured_hook_events.json")])
    reports = sorted(out.glob("*/hook_inventory.json"))
    return code, json.loads(reports[-1].read_text(encoding="utf-8")) if reports else None


def test_no_source_is_blocked_never_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "DEFAULT_SOURCE", tmp_path / "absent")
    code, report = run_cli(tmp_path)
    assert code == cli.EXIT_BLOCKED and report["status"] == "BLOCKED_MISSING_SOURCE"
    assert "not a clean result" in report["reading_note"] and "items" not in report


def test_missing_path_is_blocked(tmp_path: Path) -> None:
    code, report = run_cli(tmp_path, "--source", str(tmp_path / "nope"))
    assert code == cli.EXIT_BLOCKED and report["status"] == "BLOCKED_MISSING_SOURCE"


def test_folder_without_php_is_blocked(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "style.css").write_text("x", encoding="utf-8")
    code, report = run_cli(tmp_path, "--source", str(source))
    assert code == cli.EXIT_BLOCKED and report["status"] == "BLOCKED_MISSING_SOURCE"


def test_zip_is_read_in_memory_and_nothing_is_extracted(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "flatsome-child.zip", {"flatsome-child/functions.php": "<?php add_action('save_post','f'); function f(){}"})
    before = {p for p in tmp_path.rglob("*")}
    code, report = run_cli(tmp_path, "--source", str(archive))
    assert code == cli.EXIT_OK and report["items"][0]["file"] == "flatsome-child/flatsome-child/functions.php"
    created = {p for p in tmp_path.rglob("*")} - before
    assert all(p == tmp_path / "out" or (tmp_path / "out") in p.parents for p in created)


def test_unsafe_and_sensitive_zip_members_are_not_read(tmp_path: Path) -> None:
    secret = "<?php define('DB_PASSWORD', 'must-never-be-read');"
    archive = make_zip(tmp_path / "s.zip", {
        "../escape.php": "<?php add_action('escaped','x');", "/abs.php": "<?php add_action('abs','x');",
        "C:/win.php": "<?php add_action('win','x');", "wp-config.php": secret, "site/.env": "KEY=value", "db.sql": "DROP",
        "inner.zip": "x", "tool.phar": "x", "ok.php": "<?php add_action('ok','x');"})
    _code, report = run_cli(tmp_path, "--source", str(archive))
    assert [i["hook"] for i in report["items"]] == ["ok"]
    reasons = sorted(s["reason"] for s in report["skipped"])
    assert reasons.count("UNSAFE_PATH_NOT_READ") == 3 and reasons.count("SENSITIVE_FILE_NOT_READ") == 3
    assert reasons.count("NESTED_ARCHIVE_NOT_OPENED") == 2
    assert "must-never-be-read" not in json.dumps(report)


def test_oversized_zip_member_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "MAX_FILE_BYTES", 10)
    archive = make_zip(tmp_path / "big.zip", {"big.php": "<?php " + "x" * 100, "ok.php": "<?php"})
    skipped: list[dict[str, Any]] = []
    sources, skipped = cli.load_sources([archive])
    assert list(sources) == ["big/ok.php"] and skipped[0]["reason"] == "TOO_LARGE"


def test_folder_never_reads_config_or_follows_links(tmp_path: Path) -> None:
    source = tmp_path / "theme"
    source.mkdir()
    (source / "wp-config.php").write_text("<?php add_action('from_config','x');", encoding="utf-8")
    (source / "ok.php").write_text("<?php add_action('ok','x');", encoding="utf-8")
    sources, skipped = cli.load_sources([source])
    assert list(sources) == ["theme/ok.php"] and skipped[0]["reason"] == "SENSITIVE_FILE_NOT_READ"


def test_bad_zip_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    code, _ = run_cli(tmp_path, "--source", str(bad))
    assert code == cli.EXIT_USAGE


@pytest.mark.parametrize("inside", ["wordpress/x", "src", "docs/x.zip"])
def test_a_source_in_a_tracked_folder_is_refused(tmp_path: Path, inside: str) -> None:
    code, _ = run_cli(tmp_path, "--source", str(cli.PROJECT_ROOT / inside))
    assert code == cli.EXIT_USAGE


def test_a_report_in_a_tracked_folder_is_refused() -> None:
    assert cli.main(["--source", str(THEME), "--out", str(cli.PROJECT_ROOT / "docs" / "x")]) == cli.EXIT_USAGE


def test_the_gitignored_data_folder_is_allowed() -> None:
    cli.require_private(cli.PROJECT_ROOT / "data" / "raw" / "developer_source", "--source")


def test_data_folder_is_really_gitignored() -> None:
    ignored = (cli.PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/raw/*" in ignored and "data/output/*" in ignored


def test_high_risk_exits_as_a_lead(tmp_path: Path) -> None:
    code, report = run_cli(tmp_path, "--source", str(THEME), "--allow-repo-path")
    assert code == cli.EXIT_LEADS and report["risk_counts"]["HIGH"] >= 1


def test_the_tool_cannot_run_php() -> None:
    for path in (cli.PROJECT_ROOT / "scripts" / "inventory_php_hooks.py", cli.PROJECT_ROOT / "src" / "domain" / "php_hook_inventory.py"):
        text = path.read_text(encoding="utf-8")
        assert "subprocess" not in text and "os.system" not in text and "extractall" not in text and ".extract(" not in text


def test_markdown_lists_high_first(report: dict[str, Any]) -> None:
    markdown = phi.to_markdown(report)
    assert markdown.index("| HIGH |") < markdown.index("| MEDIUM |") < markdown.index("| LOW |")
    assert "## HIGH: read these first" in markdown and "## Recursion" in markdown


def test_blocked_markdown_is_explicit() -> None:
    markdown = phi.to_markdown(cli.blocked_report("Nothing supplied."))
    assert "BLOCKED_MISSING_SOURCE" in markdown and "Nothing supplied." in markdown


def test_the_domain_module_does_no_io() -> None:
    text = (cli.PROJECT_ROOT / "src" / "domain" / "php_hook_inventory.py").read_text(encoding="utf-8")
    assert not re.search(r"\bopen\(|\bimport os\b|\bpathlib\b", text)


def test_a_long_opaque_string_is_redacted_but_a_long_hook_name_is_not() -> None:
    assert phi.safe_name("a1b2c3d4e5f6a7b8c9d0e1f2a3b4") == "<redacted>"
    assert phi.safe_name("woocommerce_product_options_general_product_data") == "woocommerce_product_options_general_product_data"
    assert phi.safe_name("db_password") == "<redacted>"
