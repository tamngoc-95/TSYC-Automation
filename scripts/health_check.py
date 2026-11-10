"""
Scheduled READ-ONLY production health check (src/domain/health_monitor.py).

Runs the existing authoritative read-only checks once, turns their output
into stable findings, and alerts only on what is NEW since the previous run
and not accepted by config/health_check_baseline.json:

  1. WooCommerce remote product status   audit_woo_remote_status.run_check (GET only)
  2. Local/remote state drift             same report (drift classifications)
  3. Translation mapping conflicts        audit_language_product_mapping.run (GET only)
  4. Owner-modified products              OWNER_MODIFIED / IDENTITY_MISMATCH / copied meta
  5. Removed-product protection           removed products live again, new removals
  6. Uncertain Woo create results         UNCERTAIN_CREATE, NO_WOO_ID, recovery health
  7. API connectivity                     preflight check_supabase_read / check_woo_read
  8. Last successful audit timestamp      state file; MISSED_CHECK / NO_RECENT_SUCCESS

Never: a database write, a WooCommerce/WordPress write, the production
writer lock, a price/publish/inventory/translation-link change. The only
files written are this monitor's own report, alert and state files. The only
non-GET request is the optional notification POST to a configured webhook,
which is refused when it points at the store or the database.

Credentials (never printed, never written to a report, redacted from errors):
  default / --scheduled   only from .env.health (gitignored; least-privilege
                          read-only keys, see docs/HEALTH_MONITORING.md).
                          Missing or incomplete -> refused (exit 3).
  --allow-production-credentials
                          interactive validation only: uses .env. Refused
                          together with --scheduled.

Only one check runs at a time: a monitor-own lock file
(data/processed/health/health_check.lock, never the production writer
lock) refuses a second concurrent run (exit 3).

Exit codes: 0 healthy (no alert), 1 new alert(s), 2 degraded (a systemic
failure: the monitor could not see everything), 3 refused before running
(configuration, authorization, or another check already running),
4 crashed (redacted record in data/output/health/last_crash.txt and
alerts.jsonl; the state file is not advanced, so the next run and the
watchdog report the gap).

The watchdog stays credential-free: with --notify it reads only
TSYC_HEALTH_WEBHOOK_URL (and the two host names it must never post to)
from .env.health, never a store or database key.

Usage:
  .venv/Scripts/python.exe scripts/health_check.py --scheduled --notify
  .venv/Scripts/python.exe scripts/health_check.py --allow-production-credentials
  .venv/Scripts/python.exe scripts/health_check.py --watchdog [--notify]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence
from urllib.parse import urlparse

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain import health_monitor as hm  # noqa: E402

configure_utf8_console()

PYTHON_EXE = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
HEALTH_ENV_FILE = PROJECT_ROOT / ".env.health"
PRODUCTION_ENV_FILE = PROJECT_ROOT / ".env"
BASELINE_FILE = PROJECT_ROOT / "config" / "health_check_baseline.json"
STATE_FILE = PROJECT_ROOT / "data" / "processed" / "health" / "health_state.json"
WATCHDOG_STATE_FILE = PROJECT_ROOT / "data" / "processed" / "health" / "watchdog_state.json"
HEALTH_LOCK_FILE = PROJECT_ROOT / "data" / "processed" / "health" / "health_check.lock"
OUTPUT_DIR = PROJECT_ROOT / "data" / "output" / "health"

REQUIRED_KEYS = (
    "SUPABASE_URL", "SUPABASE_KEY",
    "WOOCOMMERCE_URL", "WOOCOMMERCE_CONSUMER_KEY", "WOOCOMMERCE_CONSUMER_SECRET",
)
SECRET_KEYS = ("SUPABASE_KEY", "WOOCOMMERCE_CONSUMER_KEY", "WOOCOMMERCE_CONSUMER_SECRET", "TSYC_HEALTH_WEBHOOK_URL")
WEBHOOK_KEY = "TSYC_HEALTH_WEBHOOK_URL"

PROFILE_HEALTH = "HEALTH_READONLY"
PROFILE_PRODUCTION = "PRODUCTION_SHARED"


class Refused(RuntimeError):
    """The check must not run (configuration/authorization)."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------
# Credentials (values never leave this process except as auth headers)
# ---------------------------------------------------------------------


def load_credentials(*, scheduled: bool, allow_production: bool, env_file: Path | None = None
                     ) -> tuple[dict[str, str], str]:
    if scheduled and allow_production:
        raise Refused("--allow-production-credentials is refused for --scheduled runs.")
    if allow_production:
        path, profile = PRODUCTION_ENV_FILE, PROFILE_PRODUCTION
    else:
        path, profile = env_file or HEALTH_ENV_FILE, PROFILE_HEALTH
    if not path.exists():
        raise Refused(f"Credential file {path.name} not found. See docs/HEALTH_MONITORING.md (read-only keys).")
    values = {k: (v or "").strip() for k, v in dotenv_values(path).items() if v is not None}
    missing = [k for k in REQUIRED_KEYS if not values.get(k)]
    if missing:
        raise Refused(f"{path.name} is missing: {', '.join(missing)}.")
    return values, profile


def secret_values(creds: Mapping[str, str]) -> list[str]:
    return [creds.get(k, "") for k in SECRET_KEYS if creds.get(k)]


def load_notification_target(env_file: Path | None = None) -> dict[str, str]:
    """For the credential-free watchdog: only the webhook URL and the two
    protected host URLs, never a store or database key. Empty when absent."""
    path = env_file or HEALTH_ENV_FILE
    if not path.exists():
        return {}
    values = dotenv_values(path)
    return {k: (values.get(k) or "").strip() for k in (WEBHOOK_KEY, "WOOCOMMERCE_URL", "SUPABASE_URL")
            if (values.get(k) or "").strip()}


@contextmanager
def single_instance(path: Path) -> Iterator[None]:
    """Monitor-own lock (never the production writer lock): a second
    concurrent check is refused instead of double-alerting."""
    from src.services.writer_lock import ProductionWriterLock, WriterLockBusy

    lock = ProductionWriterLock("health_check", path)
    try:
        lock.acquire()
    except WriterLockBusy as error:
        raise Refused(f"Another health check run is active ({path.name}); this run is skipped.") from error
    try:
        yield
    finally:
        lock.release()


class ReadRepository:
    """Only `.client` -- every reused check reads through it. Built from the
    chosen credential file, never from the ambient .env."""

    def __init__(self, url: str, key: str) -> None:
        from supabase import create_client

        self.client = create_client(url.rstrip("/"), key)


# ---------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------


def _default_audit_runner(env: Mapping[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(PYTHON_EXE), str(PROJECT_ROOT / "scripts" / "audit_pipeline_state.py")],
        cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=dict(env), timeout=900,
    )


def _default_remote_check(repository: Any, reader: Any) -> dict[str, Any]:
    import audit_woo_remote_status as remote_status

    return remote_status.run_check(remote_status.load_local_records(repository), reader).as_dict()


def _default_mapping_check(repository: Any, reader: Any) -> dict[str, Any]:
    import audit_language_product_mapping as mapping_audit

    return mapping_audit.run(repository, reader=reader, plan_backfill=False)


def _default_reader_factory(creds: Mapping[str, str]) -> Any:
    import audit_woo_remote_status as remote_status

    return remote_status.build_reader(creds)


def _default_http_get(url: str, **kwargs: Any) -> Any:
    import requests

    return requests.get(url, **kwargs)


@dataclass
class Collectors:
    repository_factory: Callable[[Mapping[str, str]], Any] = lambda c: ReadRepository(c["SUPABASE_URL"], c["SUPABASE_KEY"])
    reader_factory: Callable[[Mapping[str, str]], Any] = _default_reader_factory
    remote_check: Callable[[Any, Any], Mapping[str, Any]] = _default_remote_check
    mapping_check: Callable[[Any, Any], Mapping[str, Any]] = _default_mapping_check
    audit_runner: Callable[[Mapping[str, str]], subprocess.CompletedProcess] = _default_audit_runner
    http_get: Callable[..., Any] = _default_http_get
    lock_inspector: Callable[[], Any] | None = None


@dataclass
class Collected:
    findings: list[hm.Finding] = field(default_factory=list)
    accepted_audit_counts: dict[str, int] = field(default_factory=dict)
    checks: dict[str, str] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    writer_active: bool = False


def _error(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def collect(creds: Mapping[str, str], collectors: Collectors) -> Collected:
    """Run every read-only check; a failing check becomes a systemic finding
    and never stops the others."""
    import preflight_pipeline as preflight
    from pipeline_state import ACCEPTED_WARNING_CODES

    out = Collected()

    inspector = collectors.lock_inspector
    if inspector is None:
        from src.services.writer_lock import inspect_lock as inspector  # noqa: N813
    try:
        lock = inspector()
        out.checks["WRITER_LOCK"] = lock.state
        if lock.state == "HELD_BY_OTHER":
            # Mid-write reads show transient drift; skip instead of alerting on it.
            out.writer_active = True
            return out
    except Exception as error:  # noqa: BLE001
        out.checks["WRITER_LOCK"] = "UNREADABLE"
        out.summary["writer_lock_error"] = _error(error)

    repository = None
    try:
        repository = collectors.repository_factory(creds)
    except Exception as error:  # noqa: BLE001
        out.findings.append(hm.systemic("SUPABASE_READ", f"Client could not be built: {_error(error)}"))

    supabase_ok = False
    if repository is not None:
        check = preflight.check_supabase_read(configuration_ok=True, repository=repository,
                                              repository_factory=lambda: repository)
        out.checks["SUPABASE_READ"] = check.status
        supabase_ok = check.status == preflight.CheckStatus.PASS
        if not supabase_ok:
            out.findings.append(hm.systemic("SUPABASE_READ", check.message))

    woo = preflight.check_woo_read(configuration_ok=True, env=creds, http_get=collectors.http_get)
    out.checks["WOO_READ"] = woo.status
    woo_ok = woo.status == preflight.CheckStatus.PASS
    if not woo_ok:
        out.findings.append(hm.systemic("WOO_READ", woo.message))

    if supabase_ok and woo_ok:
        try:
            report = collectors.remote_check(repository, collectors.reader_factory(creds))
            out.findings.extend(hm.remote_findings(report))
            out.checks["WOO_REMOTE_STATE"] = "UNRELIABLE" if report.get("systemic_failure") else "READ"
            out.summary["remote"] = {"records": report.get("records"), "counts": report.get("counts"),
                                     "systemic_message": report.get("systemic_message")}
        except Exception as error:  # noqa: BLE001
            out.checks["WOO_REMOTE_STATE"] = "FAILED"
            out.findings.append(hm.systemic("WOO_REMOTE_STATE", _error(error)))
        try:
            report = collectors.mapping_check(repository, collectors.reader_factory(creds))
            out.findings.extend(hm.mapping_findings(report))
            out.checks["LANGUAGE_MAPPING"] = "READ"
            out.summary["mapping"] = {"stored_rows": report.get("stored_rows"),
                                      "severity_counts": report.get("severity_counts"),
                                      "remote_catalog_products": report.get("remote_catalog_products")}
        except Exception as error:  # noqa: BLE001
            out.checks["LANGUAGE_MAPPING"] = "FAILED"
            out.findings.append(hm.systemic("LANGUAGE_MAPPING", _error(error)))
    else:
        for name in ("WOO_REMOTE_STATE", "LANGUAGE_MAPPING"):
            out.checks[name] = "SKIPPED"
            out.findings.append(hm.systemic(name, "Skipped: Supabase or WooCommerce read connectivity failed."))

    if supabase_ok:
        env = {**os.environ, **{k: v for k, v in creds.items() if k in REQUIRED_KEYS}}
        try:
            completed = collectors.audit_runner(env)
            stdout = completed.stdout or ""
            if "Result:" not in stdout:
                raise RuntimeError(f"audit_pipeline_state.py exited {completed.returncode} without a result.")
            findings, counts = hm.audit_findings(stdout, ACCEPTED_WARNING_CODES)
            out.findings.extend(findings)
            out.accepted_audit_counts = counts
            out.checks["PIPELINE_AUDIT"] = "READ"
            out.summary["audit"] = {"errors": sum(1 for f in findings if f.category == hm.AUDIT_ERROR),
                                    "unaccepted_warnings": sum(1 for f in findings
                                                               if f.category == hm.AUDIT_UNACCEPTED_WARNING),
                                    "accepted_warnings": counts}
        except Exception as error:  # noqa: BLE001
            out.checks["PIPELINE_AUDIT"] = "FAILED"
            out.findings.append(hm.systemic("PIPELINE_AUDIT", _error(error)))

        recovery = preflight.check_recovery_health(configuration_ok=True, repository=repository,
                                                   repository_factory=lambda: repository)
        out.checks["RECOVERY_HEALTH"] = recovery.status
        if recovery.status == preflight.CheckStatus.FAIL:
            out.findings.append(hm.systemic("RECOVERY_HEALTH", recovery.message))
        elif recovery.status == preflight.CheckStatus.WARNING:
            first = recovery.message.split(" ", 1)[0]
            finding = hm.recovery_finding(int(first) if first.isdigit() else -1)
            if finding:
                out.findings.append(finding)
    else:
        out.checks["PIPELINE_AUDIT"] = out.checks["RECOVERY_HEALTH"] = "SKIPPED"
    return out


# ---------------------------------------------------------------------
# Files and notification
# ---------------------------------------------------------------------


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as error:
        raise Refused(f"{path.name} is unreadable ({type(error).__name__}); fix or move it aside.") from error


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    os.replace(temporary, path)


def notify(message: str, creds: Mapping[str, str], *, http_post: Callable[..., Any] | None = None) -> str:
    """One POST of {"text": message} to the configured webhook. Not retried
    (a lost notification is visible through the watchdog and the files)."""
    url = (creds.get(WEBHOOK_KEY) or "").strip()
    if not url:
        return "NOT_CONFIGURED"
    target = urlparse(url)
    if target.scheme != "https":
        return "REFUSED_NOT_HTTPS"
    protected = {urlparse(creds.get(k, "")).hostname for k in ("WOOCOMMERCE_URL", "SUPABASE_URL")}
    if target.hostname in protected:
        return "REFUSED_PROTECTED_HOST"
    if http_post is None:
        import requests

        http_post = requests.post
    try:
        response = http_post(url, json={"text": message}, timeout=15)
    except Exception as error:  # noqa: BLE001
        return f"FAILED ({type(error).__name__})"
    status = getattr(response, "status_code", None)
    return "SENT" if isinstance(status, int) and 200 <= status < 300 else f"FAILED (HTTP {status})"


# ---------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------


def run_health_check(creds: Mapping[str, str], profile: str, *, collectors: Collectors, baseline: Mapping[str, Any],
                     state_file: Path, output_dir: Path, now: datetime, send: bool, update_state: bool,
                     max_gap_hours: float, success_window_hours: float,
                     http_post: Callable[..., Any] | None = None) -> tuple[int, dict[str, Any]]:
    previous = read_json(state_file, {})
    collected = collect(creds, collectors)
    if collected.writer_active:
        run_status = hm.RUN_SKIPPED_WRITER
    elif any(f.systemic for f in collected.findings):
        run_status = hm.RUN_DEGRADED
    else:
        run_status = hm.RUN_OK
    evaluation = hm.evaluate(collected.findings, baseline=baseline, previous=previous, now=now,
                             run_status=run_status, max_gap_hours=max_gap_hours,
                             success_window_hours=success_window_hours,
                             accepted_audit_counts=collected.accepted_audit_counts)
    secrets = secret_values(creds)
    message = hm.redact(hm.format_message(evaluation.alerts, run_status=run_status, now=now), secrets)
    delivery = "NOT_REQUESTED"
    if message and send:
        delivery = notify(message, creds, http_post=http_post)

    report = {
        "monitor": f"tsyc_health_check/{hm.HEALTH_MONITOR_VERSION}",
        "checked_at": now.isoformat(),
        "credential_profile": profile,
        "run_status": run_status,
        "checks": collected.checks,
        "summary": collected.summary,
        "alerts": [a.as_dict() for a in evaluation.alerts],
        "new_keys": evaluation.new_keys,
        "persisting_keys": evaluation.persisting_keys,
        "resolved_keys": evaluation.resolved_keys,
        "accepted_present": evaluation.accepted_keys,
        "baseline_no_longer_present": evaluation.stale_baseline_keys,
        "tracked": evaluation.tracked,
        "last_success_at": evaluation.next_state["last_success_at"],
        "last_audit_pass_at": evaluation.next_state["last_audit_pass_at"],
        "findings": [f.as_dict() for f in evaluation.findings.values()],
        "notification": delivery,
        "state_updated": update_state,
    }
    text = hm.redact(json.dumps(report, ensure_ascii=False, indent=1, default=str), secrets)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"health_{now:%Y%m%dT%H%M%SZ}.json").write_text(text, encoding="utf-8")
    (output_dir / "latest_report.json").write_text(text, encoding="utf-8")
    (output_dir / "latest_alerts.txt").write_text(message or "No alerts.\n", encoding="utf-8")
    if evaluation.alerts:
        with (output_dir / "alerts.jsonl").open("a", encoding="utf-8") as log:
            for alert in evaluation.alerts:
                line = json.dumps({"at": now.isoformat(), **alert.as_dict()}, ensure_ascii=False)
                log.write(hm.redact(line, secrets) + "\n")
    if update_state:
        write_json(state_file, evaluation.next_state)

    if run_status == hm.RUN_DEGRADED:
        code = 2
    else:
        code = 1 if evaluation.alerts else 0
    return code, report


def run_watchdog(*, state_file: Path, watchdog_file: Path, now: datetime, max_gap_hours: float,
                 success_window_hours: float, output_dir: Path) -> tuple[int, list[hm.Alert]]:
    """Credential-free: only reads the state file. Alerts once per stale run
    (re-alerts only after a newer run went stale again)."""
    state = read_json(state_file, {})
    if not state:
        alerts = [hm.Alert(hm.MISSED_CHECK, hm.CRITICAL, "schedule:NEVER_RAN", "No health check has run yet.")]
    else:
        alerts = hm.schedule_alerts(state, now, max_gap_hours=max_gap_hours, success_window_hours=success_window_hours)
    seen = read_json(watchdog_file, {})
    marker = str(state.get("last_run_at"))
    fresh = [a for a in alerts if seen.get(a.key) != marker]
    if fresh:
        output_dir.mkdir(parents=True, exist_ok=True)
        with (output_dir / "alerts.jsonl").open("a", encoding="utf-8") as log:
            for alert in fresh:
                log.write(json.dumps({"at": now.isoformat(), "source": "watchdog", **alert.as_dict()},
                                     ensure_ascii=False) + "\n")
        write_json(watchdog_file, {**seen, **{a.key: marker for a in fresh}})
    return (1 if fresh else 0), fresh


def record_crash(error: BaseException, *, output_dir: Path, now: datetime, secrets: Sequence[str]) -> hm.Alert:
    """A scheduled run has no console: keep a redacted trace of an
    unexpected failure next to the reports."""
    alert = hm.Alert(hm.CHECK_CRASHED, hm.CRITICAL, "run:CHECK_CRASHED",
                     hm.redact(f"Health check crashed: {type(error).__name__}: {error}", secrets)[:300])
    trace = hm.redact("".join(traceback.format_exception(type(error), error, error.__traceback__)), secrets)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "last_crash.txt").write_text(f"{now.isoformat()}\n{trace}", encoding="utf-8")
    with (output_dir / "alerts.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps({"at": now.isoformat(), "source": "health_check", **alert.as_dict()},
                             ensure_ascii=False) + "\n")
    return alert


def print_summary(report: Mapping[str, Any]) -> None:
    print("=" * 78)
    print("TSYC SCHEDULED HEALTH CHECK (read-only)")
    print("=" * 78)
    print(f"Checked at: {report['checked_at']}   credentials: {report['credential_profile']}")
    print(f"Run: {report['run_status']}")
    for name, status in report["checks"].items():
        print(f"  {name:<20} {status}")
    print(f"Last success: {report['last_success_at']}   last audit pass: {report['last_audit_pass_at']}")
    print(f"Findings: {len(report['findings'])} (new {len(report['new_keys'])}, persisting "
          f"{len(report['persisting_keys'])}, accepted {len(report['accepted_present'])}, resolved "
          f"{len(report['resolved_keys'])})")
    for item_id, item in report["tracked"].items():
        print(f"  tracked {item_id:<10} {'CHANGED' if item.get('changed') else 'unchanged'}  {item.get('keys')}")
    if report["baseline_no_longer_present"]:
        print(f"Baseline entries no longer present (prune after review): {report['baseline_no_longer_present']}")
    print(f"Alerts: {len(report['alerts'])}   notification: {report['notification']}")
    for alert in report["alerts"]:
        print(f"- [{alert['severity']}] {alert['category']}: {alert['message']}")


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scheduled read-only TSYC production health check.")
    parser.add_argument("--scheduled", action="store_true",
                        help="Unattended run: requires .env.health; refuses production credentials.")
    parser.add_argument("--allow-production-credentials", action="store_true",
                        help="Interactive validation only: read with the production .env.")
    parser.add_argument("--env-file", type=Path, help="Alternative read-only credential file.")
    parser.add_argument("--notify", action="store_true",
                        help="POST new alerts to TSYC_HEALTH_WEBHOOK_URL (also with --watchdog).")
    parser.add_argument("--no-state-update", action="store_true", help="Do not persist the run state.")
    parser.add_argument("--watchdog", action="store_true", help="Credential-free missed-check detection only.")
    parser.add_argument("--max-gap-hours", type=float, default=26.0)
    parser.add_argument("--success-window-hours", type=float, default=50.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_arguments(argv)
    now = utc_now()
    creds: Mapping[str, str] = {}
    try:
        if args.watchdog:
            code, alerts = run_watchdog(state_file=STATE_FILE, watchdog_file=WATCHDOG_STATE_FILE, now=now,
                                        max_gap_hours=args.max_gap_hours,
                                        success_window_hours=args.success_window_hours, output_dir=OUTPUT_DIR)
            print("Watchdog:", "; ".join(f"{a.category}: {a.message}" for a in alerts) or "OK")
            if alerts and args.notify:
                target = load_notification_target(args.env_file)
                message = hm.redact(hm.format_message(alerts, run_status="WATCHDOG", now=now), secret_values(target))
                print("Notification:", notify(message, target))
            return code
        creds, profile = load_credentials(scheduled=args.scheduled, allow_production=args.allow_production_credentials,
                                          env_file=args.env_file)
        baseline = read_json(BASELINE_FILE, {})
        with single_instance(HEALTH_LOCK_FILE):
            code, report = run_health_check(
                creds, profile, collectors=Collectors(), baseline=baseline, state_file=STATE_FILE,
                output_dir=OUTPUT_DIR, now=now, send=args.notify, update_state=not args.no_state_update,
                max_gap_hours=args.max_gap_hours, success_window_hours=args.success_window_hours)
    except Refused as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 3
    except Exception as error:  # noqa: BLE001 - scheduled runs have no console; keep a trace
        secrets = secret_values(creds)
        alert = record_crash(error, output_dir=OUTPUT_DIR, now=now, secrets=secrets)
        print(f"CRASHED: {alert.message} (see data/output/health/last_crash.txt)", file=sys.stderr)
        if args.notify and creds:
            notify(hm.redact(hm.format_message([alert], run_status="CRASHED", now=now), secrets), creds)
        return 4
    print_summary(report)
    return code


if __name__ == "__main__":
    sys.exit(main())
