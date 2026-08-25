"""Automated tests for scripts/preflight_pipeline.py.

Fully offline: every external dependency (Supabase, subprocess-invoked
audit, WooCommerce HTTP, Git) is injected as a stub. No live Supabase,
WooCommerce, Facebook, or Playwright access, and no production write ever
happens -- these tests only exercise read paths and pure logic.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

import preflight_pipeline as pf
from support.fake_supabase import FakeSupabaseRepository


VALID_ENV = {
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_KEY": "sb-fake-key",
    "WOOCOMMERCE_URL": "https://example-store.test",
    "WOOCOMMERCE_CONSUMER_KEY": "ck_fake",
    "WOOCOMMERCE_CONSUMER_SECRET": "cs_fake",
}


def make_repository(**tables: list[dict[str, Any]]) -> FakeSupabaseRepository:
    tables.setdefault("product_candidates", [])
    tables.setdefault("internal_products", [])
    tables.setdefault("woocommerce_product_syncs", [])
    return FakeSupabaseRepository(tables=dict(tables))


def audit_runner(returncode: int = 0, stdout: str = ""):
    def runner(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    return runner


def clean_git_runner(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(argv, 0, "", "")


def dirty_git_runner(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(argv, 0, " M scripts/run_batch.py\n", "")


class FakeHttpResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code


def ok_http_get(_url: str, **_kwargs: Any) -> FakeHttpResponse:
    return FakeHttpResponse(200)


def failing_http_get(_url: str, **_kwargs: Any) -> FakeHttpResponse:
    raise ConnectionError("simulated connection failure")


def run_all_pass(**overrides: Any) -> pf.PreflightResult:
    """Build a full run_preflight() call where every check passes unless
    overridden, so each test only has to override what it's testing."""
    kwargs = dict(
        env=VALID_ENV,
        repository=make_repository(),
        subprocess_runner=audit_runner(0, "Result: PASS\n"),
        git_runner=clean_git_runner,
        http_get=ok_http_get,
    )
    kwargs.update(overrides)
    return pf.run_preflight(**kwargs)


# ---------------------------------------------------------------------
# 1. All checks pass -> READY_FOR_BATCH / exit 0
# ---------------------------------------------------------------------


def test_all_checks_pass_is_ready_for_batch():
    result = run_all_pass()

    assert result.ready is True
    assert result.blockers == ()

    exit_code = 0 if result.ready else 1
    assert exit_code == 0


# ---------------------------------------------------------------------
# 2. Accepted audit warnings -> still ready
# ---------------------------------------------------------------------


def test_accepted_audit_warnings_still_ready():
    result = run_all_pass(
        subprocess_runner=audit_runner(
            0,
            "[1] WARNING | ISBN_MISSING\nEntity: x\nDetails: y\n"
            "[2] WARNING | WEIGHT_MISSING\nEntity: x\nDetails: y\n"
            "Result: PASS_WITH_WARNINGS\n",
        ),
    )

    assert result.ready is True
    audit_check = result.get("PIPELINE_AUDIT")
    assert audit_check.status == pf.CheckStatus.WARNING
    assert audit_check.display == "PASS_WITH_WARNINGS"
    assert "ISBN_MISSING: accepted" in result.warnings
    assert "WEIGHT_MISSING: accepted" in result.warnings


# ---------------------------------------------------------------------
# 3. Audit error -> not ready
# ---------------------------------------------------------------------


def test_audit_error_is_not_ready():
    result = run_all_pass(
        subprocess_runner=audit_runner(
            1,
            "[1] ERROR | CONTENT_STATUS_MISMATCH\nEntity: x\nDetails: y\nResult: FAIL\n",
        ),
    )

    assert result.ready is False
    audit_check = result.get("PIPELINE_AUDIT")
    assert audit_check.status == pf.CheckStatus.FAIL
    assert any(line.startswith("PIPELINE_AUDIT:") for line in result.blockers)


def test_unaccepted_audit_warning_is_not_ready():
    result = run_all_pass(
        subprocess_runner=audit_runner(
            0,
            "[1] WARNING | APPROVED_AT_MISSING\nEntity: x\nDetails: y\n"
            "Result: PASS_WITH_WARNINGS\n",
        ),
    )

    assert result.ready is False
    assert result.get("PIPELINE_AUDIT").status == pf.CheckStatus.FAIL


# ---------------------------------------------------------------------
# 4. Missing required config -> fail
# ---------------------------------------------------------------------


def test_missing_config_fails():
    result = run_all_pass(env={})

    assert result.ready is False
    config_check = result.get("CONFIGURATION")
    assert config_check.status == pf.CheckStatus.FAIL
    assert "SUPABASE_URL" in config_check.message
    assert "WOOCOMMERCE_URL" in config_check.message


def test_missing_config_never_prints_secret_values():
    env_with_secret = dict(VALID_ENV)
    env_with_secret["SUPABASE_KEY"] = "totally-secret-value-12345"
    result = run_all_pass(env=env_with_secret)

    rendered = "\n".join(
        [check.message for check in result.checks] + list(result.warnings) + list(result.blockers)
    )
    assert "totally-secret-value-12345" not in rendered


def test_partial_missing_config_reports_only_missing_keys():
    env = dict(VALID_ENV)
    del env["WOOCOMMERCE_CONSUMER_SECRET"]

    result = run_all_pass(env=env)

    config_check = result.get("CONFIGURATION")
    assert config_check.status == pf.CheckStatus.FAIL
    assert "WOOCOMMERCE_CONSUMER_SECRET" in config_check.message
    assert "SUPABASE_URL" not in config_check.message


def test_missing_woo_config_does_not_skip_supabase_checks():
    """Regression: check_supabase_read()/check_recovery_health() must not
    be reported as "skipped: configuration missing" just because an
    unrelated WooCommerce key is the one actually missing."""
    env = dict(VALID_ENV)
    del env["WOOCOMMERCE_CONSUMER_SECRET"]

    result = run_all_pass(env=env)

    assert result.get("SUPABASE_READ").status == pf.CheckStatus.PASS
    assert "Skipped" not in result.get("SUPABASE_READ").message
    assert result.get("RECOVERY_HEALTH").status == pf.CheckStatus.PASS
    assert result.get("WOO_READ").status == pf.CheckStatus.FAIL


def test_missing_supabase_config_does_not_skip_woo_checks():
    env = dict(VALID_ENV)
    del env["SUPABASE_URL"]

    result = run_all_pass(env=env)

    assert result.get("SUPABASE_READ").status == pf.CheckStatus.FAIL
    assert "Skipped" in result.get("SUPABASE_READ").message
    assert result.get("WOO_READ").status == pf.CheckStatus.PASS


# ---------------------------------------------------------------------
# 5. Supabase read unavailable -> fail
# ---------------------------------------------------------------------


def test_supabase_read_unavailable_fails():
    class BrokenClient:
        def table(self, _name: str):
            raise ConnectionError("simulated Supabase outage")

    class BrokenRepository:
        client = BrokenClient()

    result = run_all_pass(repository=BrokenRepository())

    assert result.ready is False
    assert result.get("SUPABASE_READ").status == pf.CheckStatus.FAIL


def test_supabase_config_missing_skips_read_without_crashing():
    env = dict(VALID_ENV)
    del env["SUPABASE_URL"]
    del env["SUPABASE_KEY"]

    result = run_all_pass(env=env)

    assert result.get("SUPABASE_READ").status == pf.CheckStatus.FAIL
    assert "Skipped" in result.get("SUPABASE_READ").message


# ---------------------------------------------------------------------
# 6. Woo read unavailable -> fail in live/required mode
# ---------------------------------------------------------------------


def test_woo_read_unavailable_fails():
    result = run_all_pass(http_get=failing_http_get)

    assert result.ready is False
    assert result.get("WOO_READ").status == pf.CheckStatus.FAIL


def test_woo_read_non_200_fails():
    result = run_all_pass(http_get=lambda *_a, **_kw: FakeHttpResponse(401))

    assert result.ready is False
    assert "401" in result.get("WOO_READ").message


def test_woo_config_missing_skips_request_without_crashing():
    calls: list[str] = []

    def tracking_http_get(url: str, **_kwargs: Any) -> FakeHttpResponse:
        calls.append(url)
        return FakeHttpResponse(200)

    env = dict(VALID_ENV)
    del env["WOOCOMMERCE_CONSUMER_KEY"]

    result = run_all_pass(env=env, http_get=tracking_http_get)

    assert result.get("WOO_READ").status == pf.CheckStatus.FAIL
    assert calls == []  # never attempted a request with missing credentials


# ---------------------------------------------------------------------
# 7. Unresolved global recovery blocker
# ---------------------------------------------------------------------


def test_candidate_specific_recovery_is_warning_not_blocker():
    repository = make_repository(
        woocommerce_product_syncs=[
            {
                "sync_id": "sync-1",
                "internal_product_id": "prod-1",
                "response_payload": {"recovery_required": True},
            }
        ],
    )

    result = run_all_pass(repository=repository)

    assert result.ready is True
    recovery_check = result.get("RECOVERY_HEALTH")
    assert recovery_check.status == pf.CheckStatus.WARNING
    assert "1 candidate" in recovery_check.message


def test_recovery_read_failure_is_a_blocker():
    class BrokenClient:
        def table(self, name: str):
            if name == "woocommerce_product_syncs":
                raise ConnectionError("simulated outage")
            raise AssertionError(f"unexpected table access: {name}")

    class BrokenRepository:
        client = BrokenClient()

    result = run_all_pass(repository=BrokenRepository())

    assert result.ready is False
    assert result.get("RECOVERY_HEALTH").status == pf.CheckStatus.FAIL


# ---------------------------------------------------------------------
# 8. Decision-engine import/rule failure
# ---------------------------------------------------------------------


def test_decision_engine_check_passes_normally():
    result = run_all_pass()
    assert result.get("DECISION_ENGINE").status == pf.CheckStatus.PASS


def test_decision_engine_detects_duplicate_rule_codes(monkeypatch):
    """check_decision_engine() only counts *self-named* module constants
    (NAME = "NAME") as rule codes -- that is what makes an accidental
    collision between two modules' rule codes (rather than an incidental
    string match) detectable. Simulate that collision directly: give
    image_rules a second, self-named constant with the exact name
    identity_rules already registers."""
    import src.domain.rules.identity_rules as identity_rules
    import src.domain.rules.image_rules as image_rules

    assert identity_rules.IDENTITY_EXACT_ISBN == "IDENTITY_EXACT_ISBN"
    monkeypatch.setattr(
        image_rules, "IDENTITY_EXACT_ISBN", "IDENTITY_EXACT_ISBN", raising=False
    )

    check = pf.check_decision_engine()
    assert check.status == pf.CheckStatus.FAIL
    assert "Duplicate rule code" in check.message


# ---------------------------------------------------------------------
# 9. No secrets exposed anywhere in a full report
# ---------------------------------------------------------------------


def test_print_report_never_prints_secret_values(capsys):
    env = dict(VALID_ENV)
    env["SUPABASE_KEY"] = "another-secret-abcdef"
    env["WOOCOMMERCE_CONSUMER_SECRET"] = "cs_supersecret999"

    result = run_all_pass(env=env)
    pf.print_report(result)

    captured = capsys.readouterr()
    assert "another-secret-abcdef" not in captured.out
    assert "cs_supersecret999" not in captured.out


# ---------------------------------------------------------------------
# 10. Dirty Git state -> warning, not a blocker
# ---------------------------------------------------------------------


def test_dirty_git_state_is_warning_not_blocker():
    result = run_all_pass(git_runner=dirty_git_runner)

    assert result.ready is True
    git_check = result.get("GIT_STATE")
    assert git_check.status == pf.CheckStatus.WARNING
    assert git_check.display == "DIRTY"
    assert any(line.startswith("GIT_STATE:") for line in result.warnings)


def test_clean_git_state_displays_clean():
    result = run_all_pass()

    git_check = result.get("GIT_STATE")
    assert git_check.status == pf.CheckStatus.PASS
    assert git_check.display == "CLEAN"


def test_git_unavailable_is_warning_not_blocker():
    def broken_git_runner(_argv: list[str]) -> subprocess.CompletedProcess:
        raise FileNotFoundError("git not found")

    result = run_all_pass(git_runner=broken_git_runner)

    assert result.ready is True
    assert result.get("GIT_STATE").display == "UNAVAILABLE"


# ---------------------------------------------------------------------
# 11. No production writes during preflight
#
# The fake repository/query builder only supports the read-only surface
# used here (select/eq/limit/execute) -- any accidental insert/update/
# delete call would raise, and no test above ever calls one. This test
# additionally asserts the in-memory store is untouched after a full run.
# ---------------------------------------------------------------------


def test_preflight_makes_no_production_writes():
    repository = make_repository(
        product_candidates=[{"candidate_id": "c1", "candidate_code": "CAN-0001"}],
        internal_products=[{"internal_product_id": "p1", "woocommerce_status": "NOT_CREATED"}],
    )
    before = {
        table: [dict(row) for row in rows]
        for table, rows in repository.client.tables.items()
    }

    run_all_pass(repository=repository)

    after = repository.client.tables
    assert after == before


# ---------------------------------------------------------------------
# 12. Orchestrator safety check
# ---------------------------------------------------------------------


def test_orchestrator_safety_passes_against_the_real_run_batch_module():
    check = pf.check_orchestrator_safety()
    assert check.status == pf.CheckStatus.PASS


# ---------------------------------------------------------------------
# 13. Domain/schema check
# ---------------------------------------------------------------------


def test_domain_schema_passes_against_real_constants():
    check = pf.check_domain_schema()
    assert check.status == pf.CheckStatus.PASS


def test_domain_schema_detects_collapsed_enum(monkeypatch):
    """Regression: if two distinct-by-design enums (different tables,
    same column name) were ever accidentally defined with identical
    values, check_domain_schema() must catch the collapse without
    needing a second hardcoded copy of the correct values."""
    monkeypatch.setattr(
        pf, "ALL_INTERNAL_PRODUCT_IMAGE_STATUSES", pf.ALL_IMAGE_STATUSES
    )

    check = pf.check_domain_schema()

    assert check.status == pf.CheckStatus.FAIL
    assert "collapsed" in check.message


# ---------------------------------------------------------------------
# 14. main()'s exit-code mapping
# ---------------------------------------------------------------------


def test_result_ready_maps_to_zero_exit_code():
    assert (0 if run_all_pass().ready else 1) == 0


def test_result_not_ready_maps_to_nonzero_exit_code():
    result = run_all_pass(env={})
    assert (0 if result.ready else 1) == 1
