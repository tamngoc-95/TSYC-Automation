"""
Read-only production health check for the TSYC automation pipeline.

This script never mutates Supabase business rows, WooCommerce products,
WordPress media, Facebook state, prices, or publish status. It only reads
what already exists (configuration presence, database connectivity, the
existing deterministic audit, the existing decision-engine rule modules,
and the existing WooCommerce read API) and reports whether the pipeline is
safe to run as an unattended batch.

Usage:
    .venv/Scripts/python.exe scripts/preflight_pipeline.py

Exit code 0 and a final "READY_FOR_BATCH" line mean every blocking check
passed (accepted warnings such as a dirty Git tree or ISBN/WEIGHT-only
audit warnings do not block). Exit code 1 and a final
"NOT_READY_FOR_BATCH" line mean at least one check is a hard blocker.

scripts/run_batch.py imports `run_preflight()` from this module and calls
it in-process before any production (non-dry-run) write -- see that
script's `main()`. This file intentionally contains no stage business
logic of its own: it re-reads state the pipeline already writes and
re-invokes scripts/audit_pipeline_state.py exactly as
scripts/run_batch.py already does, rather than duplicating either.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain.content_status import (  # noqa: E402
    ALL_CONTENT_STATUSES,
    ALL_INTERNAL_PRODUCT_CONTENT_STATUSES,
)
from src.domain.identity_status import (  # noqa: E402
    ALL_IDENTITY_STATUSES,
    ALL_MATCH_DECISIONS,
)
from src.domain.image_status import (  # noqa: E402
    ALL_IMAGE_STATUSES,
    ALL_INTERNAL_PRODUCT_IMAGE_STATUSES,
)
from src.domain.reference_sources import (  # noqa: E402
    ALL_REFERENCE_SOURCE_TYPES,
    REFERENCE_SOURCE_PRIORITY,
)
from src.domain.rights_status import (  # noqa: E402
    ALL_RIGHTS_STATUSES,
    PUBLISHABLE_RIGHTS_STATUSES,
)
from src.domain.woocommerce_status import (  # noqa: E402
    ALL_WOOCOMMERCE_STATUSES,
    ALL_WOOCOMMERCE_SYNC_STATUSES,
)

configure_utf8_console()


SCRIPT_VERSION = "1.0.0"

PYTHON_EXE = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"

REQUIRED_CONFIG_KEYS = (
    "SUPABASE_URL",
    "SUPABASE_KEY",
    "WOOCOMMERCE_URL",
    "WOOCOMMERCE_CONSUMER_KEY",
    "WOOCOMMERCE_CONSUMER_SECRET",
)

# Core tables every candidate/product touches. A minimal, bounded
# (limit=1) read against each is enough to prove read connectivity and
# table accessibility without loading real data volume.
CORE_READ_TABLES = (
    "product_candidates",
    "internal_products",
    "woocommerce_product_syncs",
)

# Every domain-constant collection this check inspects, purely for the
# "loads, is non-empty, is immutable" structural sanity pass below. The
# actual canonical *values* are deliberately not re-literalized here --
# tests/test_domain_constants.py already holds the one hand-verified
# fixture of each migration CHECK constraint's exact values; duplicating
# that fixture a second time here would just be a second copy to keep in
# lockstep, catching nothing pytest doesn't already catch, and risking a
# false pass/fail if the two copies ever drifted from each other instead
# of from the schema.
DOMAIN_CONSTANT_COLLECTIONS: tuple[tuple[str, frozenset[str]], ...] = (
    ("ALL_RIGHTS_STATUSES", ALL_RIGHTS_STATUSES),
    ("PUBLISHABLE_RIGHTS_STATUSES", PUBLISHABLE_RIGHTS_STATUSES),
    ("ALL_IMAGE_STATUSES", ALL_IMAGE_STATUSES),
    ("ALL_INTERNAL_PRODUCT_IMAGE_STATUSES", ALL_INTERNAL_PRODUCT_IMAGE_STATUSES),
    ("ALL_CONTENT_STATUSES", ALL_CONTENT_STATUSES),
    (
        "ALL_INTERNAL_PRODUCT_CONTENT_STATUSES",
        ALL_INTERNAL_PRODUCT_CONTENT_STATUSES,
    ),
    ("ALL_IDENTITY_STATUSES", ALL_IDENTITY_STATUSES),
    ("ALL_MATCH_DECISIONS", ALL_MATCH_DECISIONS),
    ("ALL_WOOCOMMERCE_STATUSES", ALL_WOOCOMMERCE_STATUSES),
    ("ALL_WOOCOMMERCE_SYNC_STATUSES", ALL_WOOCOMMERCE_SYNC_STATUSES),
    ("ALL_REFERENCE_SOURCE_TYPES", ALL_REFERENCE_SOURCE_TYPES),
)


class CheckStatus:
    """Canonical per-check severity. FAIL is the only blocking status."""

    PASS = "PASS"
    WARNING = "WARNING"
    FAIL = "FAIL"


@dataclass(frozen=True)
class PreflightCheck:
    """One preflight check's result.

    `display` overrides the printed status column for checks whose
    natural vocabulary is richer than PASS/WARNING/FAIL (pipeline audit's
    PASS_WITH_WARNINGS, Git state's CLEAN/DIRTY) -- `status` remains the
    plain severity every caller (including tests) can rely on.
    """

    code: str
    status: str
    message: str
    display: str | None = None
    warning_lines: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in (CheckStatus.PASS, CheckStatus.WARNING, CheckStatus.FAIL):
            raise ValueError(f"Unknown check status: {self.status!r}")

    @property
    def label(self) -> str:
        return self.display or self.status


@dataclass(frozen=True)
class PreflightResult:
    """The full preflight outcome: every check plus the aggregate verdict."""

    checks: tuple[PreflightCheck, ...] = field(default_factory=tuple)

    @property
    def blockers(self) -> tuple[str, ...]:
        return tuple(
            f"{check.code}: {check.message}"
            for check in self.checks
            if check.status == CheckStatus.FAIL
        )

    @property
    def warnings(self) -> tuple[str, ...]:
        lines: list[str] = []

        for check in self.checks:
            if check.warning_lines:
                lines.extend(check.warning_lines)
            elif check.status == CheckStatus.WARNING:
                lines.append(f"{check.code}: {check.message}")

        return tuple(lines)

    @property
    def ready(self) -> bool:
        return len(self.blockers) == 0

    def get(self, code: str) -> PreflightCheck | None:
        for check in self.checks:
            if check.code == code:
                return check
        return None


# ---------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------


def check_python_environment() -> PreflightCheck:
    """Verify the repository virtual environment exists and required
    imports load. Never requires system Python; never falls back to it."""
    if not PYTHON_EXE.exists():
        return PreflightCheck(
            code="PYTHON_ENVIRONMENT",
            status=CheckStatus.FAIL,
            message=(
                f"Repository virtual environment not found at {PYTHON_EXE}. "
                "Create it before running production workflows."
            ),
        )

    required_modules = (
        "dotenv",
        "requests",
        "supabase",
        "src.domain.decisions",
        "src.repositories.supabase_repository",
    )
    missing: list[str] = []

    for module_name in required_modules:
        try:
            __import__(module_name)
        except Exception:
            missing.append(module_name)

    if missing:
        return PreflightCheck(
            code="PYTHON_ENVIRONMENT",
            status=CheckStatus.FAIL,
            message=f"Required import(s) failed to load: {', '.join(missing)}.",
        )

    try:
        running_under_venv = Path(sys.executable).resolve() == PYTHON_EXE.resolve()
    except OSError:
        running_under_venv = False

    if not running_under_venv:
        return PreflightCheck(
            code="PYTHON_ENVIRONMENT",
            status=CheckStatus.WARNING,
            message=(
                "Required imports load, but this process is not running "
                f"under {PYTHON_EXE}. Production commands must use the "
                "repository virtual environment explicitly."
            ),
        )

    return PreflightCheck(
        code="PYTHON_ENVIRONMENT",
        status=CheckStatus.PASS,
        message="Repository virtual environment present; required imports load.",
    )


def check_configuration(env: Mapping[str, str]) -> PreflightCheck:
    """Verify required configuration keys are present. Never prints values."""
    missing = [key for key in REQUIRED_CONFIG_KEYS if not (env.get(key) or "").strip()]

    if missing:
        return PreflightCheck(
            code="CONFIGURATION",
            status=CheckStatus.FAIL,
            message=f"Missing required configuration key(s): {', '.join(missing)}.",
        )

    return PreflightCheck(
        code="CONFIGURATION",
        status=CheckStatus.PASS,
        message=f"All {len(REQUIRED_CONFIG_KEYS)} required configuration keys are present.",
    )


def check_supabase_read(
    *,
    configuration_ok: bool,
    repository: Any | None,
    repository_factory: Callable[[], Any],
) -> PreflightCheck:
    """Perform a minimal read-only query against core tables.

    No INSERT/UPDATE/DELETE. Never loads more than one row per table.
    """
    if not configuration_ok:
        return PreflightCheck(
            code="SUPABASE_READ",
            status=CheckStatus.FAIL,
            message="Skipped: required Supabase configuration is missing.",
        )

    try:
        active_repository = repository if repository is not None else repository_factory()

        for table_name in CORE_READ_TABLES:
            (
                active_repository.client.table(table_name)
                .select("*")
                .limit(1)
                .execute()
            )

    except Exception as error:
        return PreflightCheck(
            code="SUPABASE_READ",
            status=CheckStatus.FAIL,
            message=f"Supabase read connectivity failed: {type(error).__name__}: {error}",
        )

    return PreflightCheck(
        code="SUPABASE_READ",
        status=CheckStatus.PASS,
        message=f"Read access confirmed for: {', '.join(CORE_READ_TABLES)}.",
    )


def check_domain_schema() -> PreflightCheck:
    """Verify the canonical Python domain constants are internally
    consistent -- structural sanity, not a second copy of their exact
    values. tests/test_domain_constants.py already holds the one
    hand-verified fixture of each migration CHECK constraint's exact
    values; re-literalizing that fixture here would only add a second
    copy to keep in lockstep, so this check instead re-derives the same
    cross-table invariants that fixture protects (distinct-but-related
    enums, no accidental collapse/overlap) directly from the constants
    themselves."""
    problems: list[str] = []

    for name, collection in DOMAIN_CONSTANT_COLLECTIONS:
        if not isinstance(collection, frozenset) or not collection:
            problems.append(f"{name} is empty or not an immutable frozenset")

    if not (PUBLISHABLE_RIGHTS_STATUSES <= ALL_RIGHTS_STATUSES):
        problems.append("PUBLISHABLE_RIGHTS_STATUSES is not a subset of ALL_RIGHTS_STATUSES")

    if PUBLISHABLE_RIGHTS_STATUSES == ALL_RIGHTS_STATUSES:
        problems.append("every rights status is publishable -- expected some non-publishable values")

    if ALL_IMAGE_STATUSES == ALL_INTERNAL_PRODUCT_IMAGE_STATUSES:
        problems.append(
            "ALL_IMAGE_STATUSES and ALL_INTERNAL_PRODUCT_IMAGE_STATUSES have collapsed "
            "into the same set -- these are two distinct columns/tables"
        )

    if ALL_CONTENT_STATUSES == ALL_INTERNAL_PRODUCT_CONTENT_STATUSES:
        problems.append(
            "ALL_CONTENT_STATUSES and ALL_INTERNAL_PRODUCT_CONTENT_STATUSES have "
            "collapsed into the same set -- these are two distinct columns/tables"
        )
    elif ALL_CONTENT_STATUSES.isdisjoint(ALL_INTERNAL_PRODUCT_CONTENT_STATUSES):
        problems.append(
            "ALL_CONTENT_STATUSES and ALL_INTERNAL_PRODUCT_CONTENT_STATUSES share no "
            "values -- expected a shared DRAFTED/REVIEW_REQUIRED/APPROVED lifecycle"
        )

    if not ALL_IDENTITY_STATUSES.isdisjoint(ALL_MATCH_DECISIONS):
        problems.append(
            "ALL_IDENTITY_STATUSES and ALL_MATCH_DECISIONS overlap -- these are two "
            "distinct vocabularies and must never share a value"
        )

    if ALL_WOOCOMMERCE_STATUSES == ALL_WOOCOMMERCE_SYNC_STATUSES:
        problems.append(
            "ALL_WOOCOMMERCE_STATUSES and ALL_WOOCOMMERCE_SYNC_STATUSES have collapsed "
            "into the same set -- these are two distinct columns/tables"
        )
    elif ALL_WOOCOMMERCE_STATUSES.isdisjoint(ALL_WOOCOMMERCE_SYNC_STATUSES):
        problems.append(
            "ALL_WOOCOMMERCE_STATUSES and ALL_WOOCOMMERCE_SYNC_STATUSES share no "
            "values -- expected a shared DRAFT_CREATED/FAILED overlap"
        )

    if set(REFERENCE_SOURCE_PRIORITY.keys()) != ALL_REFERENCE_SOURCE_TYPES:
        problems.append(
            "REFERENCE_SOURCE_PRIORITY does not cover exactly ALL_REFERENCE_SOURCE_TYPES"
        )

    priority_values = list(REFERENCE_SOURCE_PRIORITY.values())
    if len(priority_values) != len(set(priority_values)):
        problems.append("REFERENCE_SOURCE_PRIORITY has duplicate priority values")

    if problems:
        return PreflightCheck(
            code="DOMAIN_SCHEMA",
            status=CheckStatus.FAIL,
            message="; ".join(problems),
        )

    return PreflightCheck(
        code="DOMAIN_SCHEMA",
        status=CheckStatus.PASS,
        message=f"{len(DOMAIN_CONSTANT_COLLECTIONS)} domain constant set(s) internally consistent.",
    )


def check_decision_engine() -> PreflightCheck:
    """Import the decision engine and verify rule-code uniqueness."""
    try:
        from src.domain.decisions import ALL_OUTCOMES, DecisionResult, Outcome
        from src.domain.rules import (
            content_rules,
            identity_rules,
            image_rules,
            readiness_rules,
        )
    except Exception as error:
        return PreflightCheck(
            code="DECISION_ENGINE",
            status=CheckStatus.FAIL,
            message=f"Decision engine failed to import: {type(error).__name__}: {error}",
        )

    if len(ALL_OUTCOMES) != 4:
        return PreflightCheck(
            code="DECISION_ENGINE",
            status=CheckStatus.FAIL,
            message=f"Expected 4 canonical outcomes, found {len(ALL_OUTCOMES)}.",
        )

    rule_modules = (identity_rules, image_rules, content_rules, readiness_rules)
    all_codes: list[str] = []

    for module in rule_modules:
        for attr_name, value in vars(module).items():
            if attr_name.isupper() and isinstance(value, str) and value == attr_name:
                all_codes.append(value)

    duplicates = {code for code in all_codes if all_codes.count(code) > 1}

    if duplicates:
        return PreflightCheck(
            code="DECISION_ENGINE",
            status=CheckStatus.FAIL,
            message=f"Duplicate rule code(s) across rule modules: {', '.join(sorted(duplicates))}.",
        )

    if not all_codes:
        return PreflightCheck(
            code="DECISION_ENGINE",
            status=CheckStatus.FAIL,
            message="No rule codes were discovered in the rule modules.",
        )

    return PreflightCheck(
        code="DECISION_ENGINE",
        status=CheckStatus.PASS,
        message=(
            f"{len(rule_modules)} rule module(s) loaded; "
            f"{len(all_codes)} unique rule code(s) registered."
        ),
    )


def check_pipeline_audit(
    subprocess_runner: Callable[[list[str]], subprocess.CompletedProcess],
) -> PreflightCheck:
    """Reuse scripts/audit_pipeline_state.py's own deterministic audit by
    invoking it exactly as scripts/run_batch.py already does, then
    interpreting its result with the same accepted-warning policy.

    Imports run_batch deliberately deferred to call time: run_batch.py
    itself imports this module at its own module level (to call
    run_preflight() before production writes), so a module-level import
    here would be circular. Both sides only need each other's names once
    every top-level module in the process has already finished loading,
    which a deferred, function-body import guarantees."""
    from pipeline_state import ACCEPTED_WARNING_CODES  # noqa: E402
    from run_batch import parse_audit_output  # noqa: E402

    argv = [str(PYTHON_EXE), str(SCRIPTS_DIR / "audit_pipeline_state.py")]

    try:
        completed = subprocess_runner(argv)
    except Exception as error:
        return PreflightCheck(
            code="PIPELINE_AUDIT",
            status=CheckStatus.FAIL,
            message=f"Could not run audit_pipeline_state.py: {type(error).__name__}: {error}",
        )

    error_count, warning_codes = parse_audit_output(completed.stdout)
    unaccepted = [code for code in warning_codes if code not in ACCEPTED_WARNING_CODES]

    if completed.returncode not in (0,) or error_count or unaccepted:
        return PreflightCheck(
            code="PIPELINE_AUDIT",
            status=CheckStatus.FAIL,
            display="FAIL",
            message=(
                f"Audit reported {error_count} error(s)"
                + (f"; unaccepted warning(s): {', '.join(unaccepted)}" if unaccepted else "")
                + "."
            ),
        )

    if warning_codes:
        from collections import Counter

        counts = Counter(warning_codes)

        return PreflightCheck(
            code="PIPELINE_AUDIT",
            status=CheckStatus.WARNING,
            display="PASS_WITH_WARNINGS",
            message=f"{len(warning_codes)} accepted warning(s).",
            warning_lines=tuple(
                f"{code}: accepted ({count})" if count > 1 else f"{code}: accepted"
                for code, count in sorted(counts.items())
            ),
        )

    return PreflightCheck(
        code="PIPELINE_AUDIT",
        status=CheckStatus.PASS,
        message="No integrity issues detected.",
    )


def check_recovery_health(
    *,
    configuration_ok: bool,
    repository: Any | None,
    repository_factory: Callable[[], Any],
) -> PreflightCheck:
    """Report unresolved recovery states without mutating anything.

    Candidate-specific recovery (a handful of products stuck pending
    reconciliation) is reported as a non-blocking warning -- each
    recovery candidate already stops individually in run_batch.py. Being
    unable to even read the sync table is treated as a systemic problem
    and blocks.
    """
    if not configuration_ok:
        return PreflightCheck(
            code="RECOVERY_HEALTH",
            status=CheckStatus.FAIL,
            message="Skipped: required Supabase configuration is missing.",
        )

    try:
        active_repository = repository if repository is not None else repository_factory()

        syncs = (
            active_repository.client.table("woocommerce_product_syncs")
            .select("sync_id, internal_product_id, woocommerce_status, response_payload")
            .execute()
            .data
            or []
        )

        failed_products = (
            active_repository.client.table("internal_products")
            .select("internal_product_id, product_code, woocommerce_status")
            .eq("woocommerce_status", "FAILED")
            .execute()
            .data
            or []
        )

    except Exception as error:
        return PreflightCheck(
            code="RECOVERY_HEALTH",
            status=CheckStatus.FAIL,
            message=f"Could not read recovery state: {type(error).__name__}: {error}",
        )

    recovery_sync_count = sum(
        1
        for sync in syncs
        if isinstance(sync.get("response_payload"), dict)
        and sync["response_payload"].get("recovery_required") is True
    )
    total_candidate_specific = recovery_sync_count + len(failed_products)

    if total_candidate_specific:
        return PreflightCheck(
            code="RECOVERY_HEALTH",
            status=CheckStatus.WARNING,
            message=(
                f"{total_candidate_specific} candidate(s) require recovery "
                "review. Each stops individually in run_batch.py; this "
                "does not block other candidates or this preflight."
            ),
        )

    return PreflightCheck(
        code="RECOVERY_HEALTH",
        status=CheckStatus.PASS,
        message="No unresolved recovery states found.",
    )


def check_woo_read(
    *,
    configuration_ok: bool,
    env: Mapping[str, str],
    http_get: Callable[..., Any],
) -> PreflightCheck:
    """Perform one safe, non-mutating GET against the WooCommerce REST API.
    No POST/PUT/DELETE; no media upload; no product creation."""
    if not configuration_ok:
        return PreflightCheck(
            code="WOO_READ",
            status=CheckStatus.FAIL,
            message="Skipped: required WooCommerce configuration is missing.",
        )

    store_url = (env.get("WOOCOMMERCE_URL") or "").strip().rstrip("/")
    consumer_key = (env.get("WOOCOMMERCE_CONSUMER_KEY") or "").strip()
    consumer_secret = (env.get("WOOCOMMERCE_CONSUMER_SECRET") or "").strip()
    api_version = (env.get("WOOCOMMERCE_API_VERSION") or "wc/v3").strip().strip("/")
    timeout_seconds = 15

    url = f"{store_url}/wp-json/{api_version}/system_status"

    try:
        response = http_get(
            url,
            auth=(consumer_key, consumer_secret),
            headers={"Accept": "application/json"},
            timeout=timeout_seconds,
        )
    except Exception as error:
        return PreflightCheck(
            code="WOO_READ",
            status=CheckStatus.FAIL,
            message=f"WooCommerce read connectivity failed: {type(error).__name__}: {error}",
        )

    status_code = getattr(response, "status_code", None)

    if status_code != 200:
        return PreflightCheck(
            code="WOO_READ",
            status=CheckStatus.FAIL,
            message=f"WooCommerce read connectivity failed: HTTP {status_code}.",
        )

    return PreflightCheck(
        code="WOO_READ",
        status=CheckStatus.PASS,
        message="Authenticated read-only WooCommerce connectivity confirmed.",
    )


_FORBIDDEN_DISPATCH_SUBSTRINGS = (
    "publish",
    "PUBLISH",
    "price",
    "PRICE",
    "regular_price",
    "sale_price",
)


def check_orchestrator_safety() -> PreflightCheck:
    """Statically verify scripts/run_batch.py still enforces its bounded,
    no-implicit-allowlist, gated-Woo-draft, no-price/publish safety
    contract, by exercising the real module (not a text search)."""
    try:
        import run_batch  # noqa: E402
        from pipeline_state import CandidateState  # noqa: E402
    except Exception as error:
        return PreflightCheck(
            code="ORCHESTRATOR_SAFETY",
            status=CheckStatus.FAIL,
            message=f"run_batch.py failed to import: {type(error).__name__}: {error}",
        )

    problems: list[str] = []

    # No implicit "all" / "newest" candidate selection.
    try:
        empty_args = argparse.Namespace(
            candidate_code=None, candidate_codes=None, max_candidates=5
        )
        run_batch.resolve_allowlist(empty_args)
        problems.append(
            "resolve_allowlist() did not reject a missing candidate allowlist"
        )
    except run_batch.OrchestratorArgumentError:
        pass
    except Exception as error:
        problems.append(f"resolve_allowlist() raised unexpectedly: {error}")

    # --max-candidates remains a required, explicit bound.
    try:
        # parse_arguments() builds a fresh parser each call; inspect it
        # directly rather than re-implementing argument definitions here.
        import inspect

        source = inspect.getsource(run_batch.parse_arguments)
        if '"--max-candidates"' not in source or "required=True" not in source:
            problems.append("--max-candidates is no longer a required argument")
    except Exception as error:
        problems.append(f"Could not inspect parse_arguments(): {error}")

    # WOO_DRAFT_DISPATCH is only reachable when explicitly authorized.
    ready_state = CandidateState(
        candidate_code="CAN-PREFLIGHT-CHECK",
        candidate_id="preflight-check",
        product_code="TSYC-CAN-PREFLIGHT-CHECK",
        derived_state="READY_FOR_DRAFT",
        human_gate=True,
        human_gate_reason="preflight self-check",
    )

    gated_kind, gated_dispatch, _ = run_batch.decide_action(ready_state, False)
    if gated_kind != "human_gate" or gated_dispatch is not None:
        problems.append(
            "READY_FOR_DRAFT is dispatched without explicit --allow-woo-draft"
        )

    authorized_kind, authorized_dispatch, _ = run_batch.decide_action(ready_state, True)
    if authorized_kind != "invoke" or authorized_dispatch is not run_batch.WOO_DRAFT_DISPATCH:
        problems.append(
            "--allow-woo-draft does not delegate to create_woocommerce_draft.py"
        )

    # No publish/price argument anywhere in the dispatch table.
    sample_state = CandidateState(
        candidate_code="CAN-PREFLIGHT-CHECK",
        candidate_id="preflight-check",
        product_code="TSYC-CAN-PREFLIGHT-CHECK",
        derived_state="PREFLIGHT_CHECK",
    )
    all_entries = list(run_batch.AUTOMATABLE_DISPATCH.values()) + [
        run_batch.WOO_DRAFT_DISPATCH
    ]

    for entry in all_entries:
        for built_arg in entry.build_args(sample_state):
            for forbidden in _FORBIDDEN_DISPATCH_SUBSTRINGS:
                if forbidden in built_arg:
                    problems.append(
                        f"{entry.script} dispatch args contain forbidden "
                        f"substring {forbidden!r}"
                    )

    if run_batch.MAX_STAGES_PER_CANDIDATE < 1 or run_batch.MAX_STAGES_PER_CANDIDATE > 50:
        problems.append(
            "MAX_STAGES_PER_CANDIDATE is not a small bounded positive integer"
        )

    if problems:
        return PreflightCheck(
            code="ORCHESTRATOR_SAFETY",
            status=CheckStatus.FAIL,
            message="; ".join(problems),
        )

    return PreflightCheck(
        code="ORCHESTRATOR_SAFETY",
        status=CheckStatus.PASS,
        message=(
            "Bounded allowlist, gated Woo draft creation, and no-price/"
            "publish dispatch contract all verified."
        ),
    )


def check_git_state(
    git_runner: Callable[[list[str]], subprocess.CompletedProcess],
) -> PreflightCheck:
    """Report Git working-tree cleanliness. Never commits/resets/stashes."""
    try:
        completed = git_runner(["git", "status", "--porcelain"])
    except Exception:
        return PreflightCheck(
            code="GIT_STATE",
            status=CheckStatus.WARNING,
            display="UNAVAILABLE",
            message="Git state could not be determined.",
        )

    if completed.returncode != 0:
        return PreflightCheck(
            code="GIT_STATE",
            status=CheckStatus.WARNING,
            display="UNAVAILABLE",
            message="Git state could not be determined.",
        )

    if (completed.stdout or "").strip():
        return PreflightCheck(
            code="GIT_STATE",
            status=CheckStatus.WARNING,
            display="DIRTY",
            message=(
                "Working tree has uncommitted changes. Non-blocking unless "
                "the changes affect production execution files."
            ),
        )

    return PreflightCheck(
        code="GIT_STATE",
        status=CheckStatus.PASS,
        display="CLEAN",
        message="Working tree is clean.",
    )


# ---------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------


def _default_repository_factory() -> Any:
    from src.repositories.supabase_repository import SupabaseRepository

    return SupabaseRepository()


def _default_subprocess_runner(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _default_git_runner(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _default_http_get(url: str, **kwargs: Any) -> Any:
    import requests

    return requests.get(url, **kwargs)


def run_preflight(
    *,
    env: Mapping[str, str] | None = None,
    repository: Any | None = None,
    repository_factory: Callable[[], Any] | None = None,
    subprocess_runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
    git_runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
    http_get: Callable[..., Any] | None = None,
) -> PreflightResult:
    """Run every deterministic preflight check and return the aggregate result.

    Every argument is optional and independently injectable so this can
    run fully offline in tests: pass a FakeSupabaseRepository as
    `repository`, a stub `subprocess_runner` that returns canned audit
    output, a stub `http_get`, and/or an explicit `env` mapping.
    Production callers (this module's `main()`, and
    scripts/run_batch.py) omit them and get the real repository/HTTP/Git
    behavior.
    """
    active_env: Mapping[str, str] = env if env is not None else __import__("os").environ
    active_repository_factory = repository_factory or _default_repository_factory
    active_subprocess_runner = subprocess_runner or _default_subprocess_runner
    active_git_runner = git_runner or _default_git_runner
    active_http_get = http_get or _default_http_get

    python_check = check_python_environment()
    config_check = check_configuration(active_env)

    # Each service's own "is its configuration OK" flag depends only on
    # whether *that service's* keys are among the missing ones -- not on
    # config_check.status as a whole, so a Woo-only credential gap never
    # also marks Supabase's checks as "skipped: configuration missing"
    # (and vice versa).
    missing_keys = _missing_keys(config_check)
    supabase_config_ok = not any(
        key in ("SUPABASE_URL", "SUPABASE_KEY") for key in missing_keys
    )
    woo_config_ok = not any(
        key in ("WOOCOMMERCE_URL", "WOOCOMMERCE_CONSUMER_KEY", "WOOCOMMERCE_CONSUMER_SECRET")
        for key in missing_keys
    )

    supabase_read_check = check_supabase_read(
        configuration_ok=supabase_config_ok,
        repository=repository,
        repository_factory=active_repository_factory,
    )
    domain_schema_check = check_domain_schema()
    decision_engine_check = check_decision_engine()
    pipeline_audit_check = check_pipeline_audit(active_subprocess_runner)
    recovery_health_check = check_recovery_health(
        configuration_ok=supabase_config_ok,
        repository=repository,
        repository_factory=active_repository_factory,
    )
    woo_read_check = check_woo_read(
        configuration_ok=woo_config_ok,
        env=active_env,
        http_get=active_http_get,
    )
    orchestrator_safety_check = check_orchestrator_safety()
    git_state_check = check_git_state(active_git_runner)

    return PreflightResult(
        checks=(
            python_check,
            config_check,
            supabase_read_check,
            domain_schema_check,
            decision_engine_check,
            pipeline_audit_check,
            recovery_health_check,
            woo_read_check,
            orchestrator_safety_check,
            git_state_check,
        )
    )


def _missing_keys(config_check: PreflightCheck) -> tuple[str, ...]:
    """Parse the missing-key list back out of check_configuration()'s own
    message so configuration-dependency branching has one source of truth
    (the message text) rather than a second parallel key list."""
    prefix = "Missing required configuration key(s): "

    if not config_check.message.startswith(prefix):
        return ()

    raw = config_check.message[len(prefix) :].rstrip(".")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


# ---------------------------------------------------------------------
# Console reporting
# ---------------------------------------------------------------------

_LABELS: dict[str, str] = {
    "PYTHON_ENVIRONMENT": "Python environment",
    "CONFIGURATION": "Configuration",
    "SUPABASE_READ": "Supabase read",
    "DOMAIN_SCHEMA": "Domain/schema",
    "DECISION_ENGINE": "Decision engine",
    "PIPELINE_AUDIT": "Pipeline audit",
    "RECOVERY_HEALTH": "Recovery health",
    "WOO_READ": "Woo read connectivity",
    "ORCHESTRATOR_SAFETY": "Orchestrator safety",
    "GIT_STATE": "Git state",
}

_LABEL_WIDTH = 26


def print_report(result: PreflightResult) -> None:
    print("=" * 78)
    print("TSYC PRODUCTION PREFLIGHT")
    print("=" * 78)
    print(f"Version: {SCRIPT_VERSION}")
    print()

    for check in result.checks:
        label = _LABELS.get(check.code, check.code)
        dots = "." * max(3, _LABEL_WIDTH - len(label))
        print(f"{label} {dots} {check.label}")

    print()
    print("Warnings:")

    if result.warnings:
        for line in result.warnings:
            print(f"- {line}")
    else:
        print("- none")

    print()
    print("Blockers:")

    if result.blockers:
        for line in result.blockers:
            print(f"- {line}")
    else:
        print("- none")

    print()
    print("READY_FOR_BATCH" if result.ready else "NOT_READY_FOR_BATCH")


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")

    result = run_preflight()
    print_report(result)

    return 0 if result.ready else 1


if __name__ == "__main__":
    sys.exit(main())
