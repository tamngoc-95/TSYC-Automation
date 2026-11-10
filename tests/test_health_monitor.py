"""Offline tests for the scheduled read-only health monitor
(src/domain/health_monitor.py, scripts/health_check.py). No live Supabase,
WooCommerce, network or credentials."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import health_check as hc
from src.domain import health_monitor as hm

NOW = datetime(2026, 11, 8, 6, 0, tzinfo=timezone.utc)
SECRET = "cs_secretvalue_0123456789"
CREDS = {
    "SUPABASE_URL": "https://db.example.supabase.co", "SUPABASE_KEY": "sb_secretvalue_0123456789",
    "WOOCOMMERCE_URL": "https://shop.example.com", "WOOCOMMERCE_CONSUMER_KEY": "ck_secretvalue_0123456789",
    "WOOCOMMERCE_CONSUMER_SECRET": SECRET,
}


def remote_row(code: str, classification: str, woo_id: int | None = 100, **extra: Any) -> dict[str, Any]:
    row = {"product_code": code, "classification": classification, "woocommerce_product_id": woo_id,
           "reason": f"{classification} reason", "remote_status": extra.pop("remote_status", "draft"),
           "evidence": extra.pop("evidence", {})}
    row.update(extra)
    return row


def remote_report(*rows: dict[str, Any], systemic: bool = False) -> dict[str, Any]:
    return {"results": list(rows), "systemic_failure": systemic, "bulk_read_ok": not systemic,
            "systemic_message": "x", "records": len(rows), "counts": {}}


# ---------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------


def test_remote_findings_categories_and_consistent_ignored():
    findings = hm.remote_findings(remote_report(
        remote_row("A", "CONSISTENT"),
        remote_row("B", "REMOTE_PUBLISHED", 2),
        remote_row("C", "REMOTE_TRASHED", 3),
        remote_row("D", "UNCERTAIN_CREATE", None),
        remote_row("E", "OWNER_MODIFIED", 5),
    ))
    by_code = {f.product_code: f for f in findings}
    assert "A" not in by_code
    assert by_code["B"].category == hm.NEW_REMOTE_DRIFT
    assert by_code["C"].category == hm.REMOTE_PRODUCT_REMOVED
    assert by_code["D"].category == hm.UNCERTAIN_CREATE and by_code["D"].key.endswith(":-")
    assert by_code["E"].category == hm.OWNERSHIP_CHANGE
    assert by_code["B"].key == "remote:REMOTE_PUBLISHED:B:2"


def test_removed_product_live_again_is_removed_product_restored():
    [finding] = hm.remote_findings(remote_report(remote_row(
        "CAN-0010", "OWNER_MODIFIED", 3772, remote_status="publish",
        evidence={"local_removal_marker": "REMOTE_TRASHED"})))
    assert finding.category == hm.REMOVED_PRODUCT_RESTORED
    assert finding.severity == hm.CRITICAL


def test_unknown_remote_classification_is_loud_not_ignored():
    [finding] = hm.remote_findings(remote_report(remote_row("X", "SOMETHING_NEW")))
    assert finding.category == hm.NEW_REMOTE_DRIFT


def test_unreliable_remote_read_is_systemic():
    findings = hm.remote_findings(remote_report(systemic=True))
    assert [f.key for f in findings] == ["systemic:WOO_REMOTE_STATE"]
    assert findings[0].systemic


def test_mapping_findings_skip_info_and_extract_woo_id():
    findings = hm.mapping_findings({"findings": [
        {"severity": "INFO", "code": "REMOTE_PUBLISHED", "product_code": "A", "language": "vi", "detail": "#1"},
        {"severity": "ERROR", "code": "COPIED_OWNERSHIP_META", "product_code": "B", "language": None,
         "detail": "#4858 carries TSYC's meta"},
        {"severity": "WARNING", "code": "UNMAPPED_SKU_COPY", "product_code": "C", "language": "en", "detail": "#4004"},
        {"severity": "WARNING", "code": "REMOTE_PRESENT_AFTER_REMOVAL", "product_code": "D", "language": "vi",
         "detail": "#3772 is recorded removed"},
    ]})
    by_code = {f.product_code: f for f in findings}
    assert "A" not in by_code
    assert by_code["B"].category == hm.OWNERSHIP_CHANGE and by_code["B"].woo_id == 4858
    assert by_code["B"].key == "mapping:COPIED_OWNERSHIP_META:B:-"
    assert by_code["C"].category == hm.NEW_MAPPING_CONFLICT
    assert by_code["D"].category == hm.REMOVED_PRODUCT_RESTORED


AUDIT_STDOUT = """ISSUES

[1] WARNING | ISBN_MISSING
Entity: P1
Details: x

[2] ERROR | SOMETHING_BROKEN
Entity: P2
Details: y

[3] WARNING | NEW_UNACCEPTED
Entity: P3

Result: FAIL
"""


def test_audit_findings_errors_and_unaccepted_only():
    findings, counts = hm.audit_findings(AUDIT_STDOUT, {"ISBN_MISSING"})
    assert counts == {"ISBN_MISSING": 1}
    assert {(f.category, f.product_code) for f in findings} == {
        (hm.AUDIT_ERROR, "P2"), (hm.AUDIT_UNACCEPTED_WARNING, "P3")}


def test_recovery_finding_keyed_by_count():
    assert hm.recovery_finding(0) is None
    assert hm.recovery_finding(2).key == "recovery:RECOVERY_REQUIRED:2"
    assert hm.recovery_finding(-1).key == "recovery:RECOVERY_REQUIRED:unknown"


# ---------------------------------------------------------------------
# Evaluation: dedupe, baseline, systemic, tracked, schedule
# ---------------------------------------------------------------------


def finding(key_suffix: str, category: str = hm.NEW_REMOTE_DRIFT, product: str = "P", woo: int | None = None):
    return hm.Finding(key=f"remote:X:{key_suffix}", category=category, source="remote", code="X",
                      product_code=product, woo_id=woo)


def test_new_finding_alerts_once_then_persists_silently():
    first = hm.evaluate([finding("a")], baseline={}, previous={}, now=NOW, run_status=hm.RUN_OK)
    assert [a.key for a in first.alerts] == ["remote:X:a"]
    second = hm.evaluate([finding("a")], baseline={}, previous=first.next_state,
                         now=NOW + timedelta(days=1), run_status=hm.RUN_OK)
    assert second.alerts == [] and second.persisting_keys == ["remote:X:a"]
    assert second.next_state["open"]["remote:X:a"]["first_seen"] == NOW.isoformat()


def test_resolved_then_reappearing_alerts_again():
    first = hm.evaluate([finding("a")], baseline={}, previous={}, now=NOW, run_status=hm.RUN_OK)
    cleared = hm.evaluate([], baseline={}, previous=first.next_state, now=NOW + timedelta(days=1),
                          run_status=hm.RUN_OK)
    assert cleared.resolved_keys == ["remote:X:a"] and cleared.alerts == []
    again = hm.evaluate([finding("a")], baseline={}, previous=cleared.next_state, now=NOW + timedelta(days=2),
                        run_status=hm.RUN_OK)
    assert [a.key for a in again.alerts] == ["remote:X:a"]


def test_baseline_accepted_never_alerts_and_stale_entries_reported():
    baseline = {"accepted": [{"key": "remote:X:a"}, {"key": "remote:X:gone"}]}
    result = hm.evaluate([finding("a")], baseline=baseline, previous={}, now=NOW, run_status=hm.RUN_OK)
    # The present accepted finding is silent; only the gone entry raises its one-time prune warning.
    assert [(a.category, a.key) for a in result.alerts] == [(hm.BASELINE_ENTRY_RESOLVED, "baseline:remote:X:gone")]
    assert result.accepted_keys == ["remote:X:a"]
    assert result.stale_baseline_keys == ["remote:X:gone"]


def test_resolved_baseline_entry_alerts_once_and_only_on_complete_runs():
    baseline = {"accepted": [{"key": "remote:X:gone"}]}
    degraded = hm.evaluate([hm.systemic("WOO_READ", "down")], baseline=baseline, previous={}, now=NOW,
                           run_status=hm.RUN_DEGRADED)
    assert hm.BASELINE_ENTRY_RESOLVED not in {a.category for a in degraded.alerts}
    first = hm.evaluate([], baseline=baseline, previous=degraded.next_state, now=NOW, run_status=hm.RUN_OK)
    [alert] = [a for a in first.alerts if a.category == hm.BASELINE_ENTRY_RESOLVED]
    assert alert.key == "baseline:remote:X:gone" and alert.severity == hm.WARNING
    second = hm.evaluate([], baseline=baseline, previous=first.next_state, now=NOW + timedelta(days=1),
                         run_status=hm.RUN_OK)
    assert second.alerts == [] and second.stale_baseline_keys == ["remote:X:gone"]
    # Reappears (still accepted, silent), then resolves again: alerted again.
    back = hm.evaluate([finding("gone")], baseline=baseline, previous=second.next_state,
                       now=NOW + timedelta(days=2), run_status=hm.RUN_OK)
    assert back.alerts == [] and back.next_state["stale_baseline"] == []
    again = hm.evaluate([], baseline=baseline, previous=back.next_state, now=NOW + timedelta(days=3),
                        run_status=hm.RUN_OK)
    assert [a.category for a in again.alerts] == [hm.BASELINE_ENTRY_RESOLVED]


def test_systemic_failure_alerts_every_run_and_keeps_last_success():
    previous = {"last_run_at": (NOW - timedelta(hours=24)).isoformat(),
                "last_success_at": (NOW - timedelta(hours=24)).isoformat(),
                "open": {"remote:X:a": {"first_seen": "t0"}}}
    failing = [hm.systemic("WOO_READ", "HTTP 500")]
    one = hm.evaluate(failing, baseline={}, previous=previous, now=NOW, run_status=hm.RUN_DEGRADED)
    two = hm.evaluate(failing, baseline={}, previous=one.next_state, now=NOW + timedelta(hours=1),
                      run_status=hm.RUN_DEGRADED)
    assert [a.category for a in one.alerts] == [hm.SYSTEMIC_API_FAILURE]
    assert [a.category for a in two.alerts] == [hm.SYSTEMIC_API_FAILURE]
    # A blind run neither resolves nor forgets what it could not see.
    assert one.resolved_keys == [] and "remote:X:a" in one.next_state["open"]
    assert one.next_state["last_success_at"] == previous["last_success_at"]


def test_tracked_item_alerts_on_change_in_either_direction():
    baseline = {"accepted": [{"key": "remote:X:a"}],
                "tracked": [{"id": "WOO-3772", "woo_ids": [3772], "product_codes": ["CAN-0010"],
                             "expected_keys": ["remote:X:a"]}]}
    known = finding("a", product="CAN-0010", woo=3772)
    first = hm.evaluate([known], baseline=baseline, previous={}, now=NOW, run_status=hm.RUN_OK)
    assert first.alerts == [] and first.tracked["WOO-3772"]["changed"] is False

    worse = hm.evaluate([known, finding("b", hm.OWNERSHIP_CHANGE, product="other", woo=3772)],
                        baseline=baseline, previous=first.next_state, now=NOW, run_status=hm.RUN_OK)
    assert hm.TRACKED_ITEM_CHANGED in {a.category for a in worse.alerts}

    resolved = hm.evaluate([], baseline=baseline, previous=first.next_state, now=NOW, run_status=hm.RUN_OK)
    [alert] = [a for a in resolved.alerts if a.category == hm.TRACKED_ITEM_CHANGED]
    assert "cleared: remote:X:a" in alert.message


def test_missed_check_and_no_recent_success():
    assert hm.schedule_alerts({}, NOW, max_gap_hours=26, success_window_hours=50) == []
    stale = {"last_run_at": (NOW - timedelta(hours=30)).isoformat(),
             "last_success_at": (NOW - timedelta(hours=30)).isoformat()}
    assert [a.category for a in hm.schedule_alerts(stale, NOW, max_gap_hours=26, success_window_hours=50)] == [
        hm.MISSED_CHECK]
    failing = {"last_run_at": (NOW - timedelta(hours=1)).isoformat(),
               "last_success_at": (NOW - timedelta(hours=60)).isoformat()}
    assert [a.category for a in hm.schedule_alerts(failing, NOW, max_gap_hours=26, success_window_hours=50)] == [
        hm.NO_RECENT_SUCCESS]


def test_audit_pass_timestamp_only_without_audit_error():
    clean = hm.evaluate([], baseline={}, previous={}, now=NOW, run_status=hm.RUN_OK)
    assert clean.next_state["last_audit_pass_at"] == NOW.isoformat()
    error = hm.Finding("audit:ERROR:E:-", hm.AUDIT_ERROR, "audit", "E")
    broken = hm.evaluate([error], baseline={}, previous={"last_audit_pass_at": "before"}, now=NOW,
                         run_status=hm.RUN_OK)
    assert broken.next_state["last_audit_pass_at"] == "before"


def test_message_is_short_and_redacted():
    alerts = [hm.Alert(hm.NEW_REMOTE_DRIFT, hm.WARNING, f"k{i}", f"m{i} {SECRET}") for i in range(30)]
    message = hm.redact(hm.format_message(alerts, run_status=hm.RUN_OK, now=NOW, max_lines=5), [SECRET])
    assert SECRET not in message and "[REDACTED]" in message
    assert len(message.splitlines()) == 7 and "25 more" in message
    assert hm.format_message([], run_status=hm.RUN_OK, now=NOW) == ""


# ---------------------------------------------------------------------
# Committed baseline
# ---------------------------------------------------------------------


def test_committed_baseline_tracks_required_items():
    baseline = json.loads((Path(hc.BASELINE_FILE)).read_text(encoding="utf-8"))
    tracked = {t["id"]: t for t in baseline["tracked"]}
    woo_ids = {w for t in baseline["tracked"] for w in t["woo_ids"]}
    assert {"CAN-0033", "WOO-4829", "WOO-3772"} <= set(tracked)
    assert {4858, 4829, 3772} <= woo_ids
    accepted = hm.accepted_keys(baseline)
    for item in baseline["tracked"]:
        assert set(item["expected_keys"]) <= accepted
    assert all(item.get("reason") for item in baseline["accepted"])


# ---------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------


def write_env(path: Path, values: dict[str, str]) -> Path:
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    return path


def test_scheduled_refuses_production_credentials(tmp_path):
    with pytest.raises(hc.Refused):
        hc.load_credentials(scheduled=True, allow_production=True)


def test_missing_or_incomplete_health_env_refused(tmp_path):
    with pytest.raises(hc.Refused, match="not found"):
        hc.load_credentials(scheduled=True, allow_production=False, env_file=tmp_path / "missing")
    partial = write_env(tmp_path / ".env.health", {"SUPABASE_URL": "https://x"})
    with pytest.raises(hc.Refused, match="missing"):
        hc.load_credentials(scheduled=True, allow_production=False, env_file=partial)


def test_health_env_loaded_without_touching_process_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("WOOCOMMERCE_CONSUMER_SECRET", raising=False)
    path = write_env(tmp_path / ".env.health", CREDS)
    creds, profile = hc.load_credentials(scheduled=True, allow_production=False, env_file=path)
    assert profile == hc.PROFILE_HEALTH and creds["WOOCOMMERCE_CONSUMER_SECRET"] == SECRET
    import os
    assert "WOOCOMMERCE_CONSUMER_SECRET" not in os.environ


# ---------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------


def test_notify_guards_and_single_attempt():
    calls: list[str] = []

    def post(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=500)

    assert hc.notify("m", CREDS, http_post=post) == "NOT_CONFIGURED"
    assert hc.notify("m", {**CREDS, hc.WEBHOOK_KEY: "http://hook.example.net/x"}, http_post=post) == "REFUSED_NOT_HTTPS"
    assert hc.notify("m", {**CREDS, hc.WEBHOOK_KEY: "https://shop.example.com/hook"},
                     http_post=post) == "REFUSED_PROTECTED_HOST"
    assert calls == []
    assert hc.notify("m", {**CREDS, hc.WEBHOOK_KEY: "https://hook.example.net/x"},
                     http_post=post) == "FAILED (HTTP 500)"
    assert len(calls) == 1  # never retried


# ---------------------------------------------------------------------
# End-to-end with fakes (read-only guarantee)
# ---------------------------------------------------------------------


class ReadOnlyQuery:
    def __init__(self, table: str, log: list[str]):
        self.table, self.log = table, log

    def select(self, *_a, **_k):
        return self

    def limit(self, *_a):
        return self

    def in_(self, *_a):
        return self

    def execute(self):
        self.log.append(f"read:{self.table}")
        return SimpleNamespace(data=[])

    def __getattr__(self, name):
        if name in ("insert", "update", "upsert", "delete", "rpc"):
            raise AssertionError(f"write attempted: {name} on {self.table}")
        raise AttributeError(name)


class ReadOnlyRepo:
    def __init__(self):
        self.log: list[str] = []
        self.client = SimpleNamespace(table=lambda name: ReadOnlyQuery(name, self.log))


def collectors(repo: ReadOnlyRepo, *, remote=None, mapping=None, lock_state="FREE", woo_status=200,
               audit_stdout="Result: PASS\n", calls: list[str] | None = None) -> hc.Collectors:
    calls = calls if calls is not None else []

    def remote_check(repository, reader):
        calls.append("remote")
        return remote or remote_report()

    def mapping_check(repository, reader):
        calls.append("mapping")
        return mapping or {"findings": []}

    return hc.Collectors(
        repository_factory=lambda c: repo, reader_factory=lambda c: object(),
        remote_check=remote_check, mapping_check=mapping_check,
        audit_runner=lambda env: subprocess.CompletedProcess([], 0, audit_stdout, ""),
        http_get=lambda url, **kw: SimpleNamespace(status_code=woo_status),
        lock_inspector=lambda: SimpleNamespace(state=lock_state),
    )


def run(tmp_path, cols, **kwargs):
    defaults = dict(baseline={}, state_file=tmp_path / "state.json", output_dir=tmp_path / "out", now=NOW,
                    send=False, update_state=True, max_gap_hours=26, success_window_hours=50)
    defaults.update(kwargs)
    return hc.run_health_check(CREDS, hc.PROFILE_HEALTH, collectors=cols, **defaults)


def test_healthy_run_reads_only_and_writes_no_secret(tmp_path):
    repo = ReadOnlyRepo()
    code, report = run(tmp_path, collectors(repo))
    assert code == 0 and report["run_status"] == hm.RUN_OK
    assert repo.log and all(entry.startswith("read:") for entry in repo.log)
    for path in (tmp_path / "out").iterdir():
        text = path.read_text(encoding="utf-8")
        for value in CREDS.values():
            assert value not in text or value.startswith("https://")
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["last_success_at"] == NOW.isoformat()


def test_new_drift_alerts_then_quiet(tmp_path):
    remote = remote_report(remote_row("P", "REMOTE_PUBLISHED", 9))
    code, report = run(tmp_path, collectors(ReadOnlyRepo(), remote=remote))
    assert code == 1 and [a["category"] for a in report["alerts"]] == [hm.NEW_REMOTE_DRIFT]
    code, report = run(tmp_path, collectors(ReadOnlyRepo(), remote=remote), now=NOW + timedelta(days=1))
    assert code == 0 and report["alerts"] == []


def test_woo_outage_is_degraded_and_skips_remote_checks(tmp_path):
    calls: list[str] = []
    code, report = run(tmp_path, collectors(ReadOnlyRepo(), woo_status=503, calls=calls))
    assert code == 2 and report["run_status"] == hm.RUN_DEGRADED
    assert calls == []
    assert {a["key"] for a in report["alerts"]} >= {"systemic:WOO_READ", "systemic:WOO_REMOTE_STATE"}


def test_active_production_writer_skips_every_check(tmp_path):
    calls: list[str] = []
    repo = ReadOnlyRepo()
    code, report = run(tmp_path, collectors(repo, lock_state="HELD_BY_OTHER", calls=calls))
    assert report["run_status"] == hm.RUN_SKIPPED_WRITER
    assert calls == [] and repo.log == []
    assert report["last_success_at"] is None


def test_notification_only_when_requested(tmp_path):
    posts: list[dict[str, Any]] = []
    creds = {**CREDS, hc.WEBHOOK_KEY: "https://hook.example.net/x"}
    remote = remote_report(remote_row("P", "UNCERTAIN_CREATE", None))
    _, report = hc.run_health_check(
        creds, hc.PROFILE_HEALTH, collectors=collectors(ReadOnlyRepo(), remote=remote), baseline={},
        state_file=tmp_path / "s.json", output_dir=tmp_path / "o", now=NOW, send=True, update_state=False,
        max_gap_hours=26, success_window_hours=50,
        http_post=lambda url, **kw: posts.append(kw["json"]) or SimpleNamespace(status_code=200))
    assert report["notification"] == "SENT" and "UNCERTAIN_CREATE" in posts[0]["text"]
    assert not (tmp_path / "s.json").exists()


def test_watchdog_alerts_once_per_stale_run(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"last_run_at": (NOW - timedelta(hours=40)).isoformat(),
                                 "last_success_at": (NOW - timedelta(hours=40)).isoformat()}), encoding="utf-8")
    kwargs = dict(state_file=state, watchdog_file=tmp_path / "wd.json", max_gap_hours=26, success_window_hours=50,
                  output_dir=tmp_path / "out")
    code, alerts = hc.run_watchdog(now=NOW, **kwargs)
    assert code == 1 and [a.category for a in alerts] == [hm.MISSED_CHECK]
    code, alerts = hc.run_watchdog(now=NOW + timedelta(hours=1), **kwargs)
    assert code == 0 and alerts == []


def test_watchdog_never_ran(tmp_path):
    code, alerts = hc.run_watchdog(state_file=tmp_path / "none.json", watchdog_file=tmp_path / "wd.json", now=NOW,
                                   max_gap_hours=26, success_window_hours=50, output_dir=tmp_path / "out")
    assert code == 1 and alerts[0].key == "schedule:NEVER_RAN"


def test_watchdog_notification_target_never_loads_store_or_database_keys(tmp_path):
    path = write_env(tmp_path / ".env.health", {**CREDS, hc.WEBHOOK_KEY: "https://hook.example.net/x"})
    target = hc.load_notification_target(path)
    assert set(target) == {hc.WEBHOOK_KEY, "WOOCOMMERCE_URL", "SUPABASE_URL"}
    for key in ("SUPABASE_KEY", "WOOCOMMERCE_CONSUMER_KEY", "WOOCOMMERCE_CONSUMER_SECRET"):
        assert CREDS[key] not in target.values()
    assert hc.load_notification_target(tmp_path / "missing") == {}


# ---------------------------------------------------------------------
# Single instance and crash record
# ---------------------------------------------------------------------


def test_second_concurrent_check_is_refused_and_lock_released(tmp_path):
    lock = tmp_path / "health_check.lock"
    with hc.single_instance(lock):
        assert lock.exists()
        with pytest.raises(hc.Refused, match="Another health check"):
            with hc.single_instance(lock):
                pass
    assert not lock.exists()


def test_stale_monitor_lock_is_taken_over(tmp_path):
    lock = tmp_path / "health_check.lock"
    lock.write_text(json.dumps({"pid": 999_999_99, "owner": "health_check"}), encoding="utf-8")
    with hc.single_instance(lock):
        assert json.loads(lock.read_text(encoding="utf-8"))["owner"] == "health_check"
    assert not lock.exists()


def test_monitor_lock_is_not_the_production_writer_lock():
    from src.services.writer_lock import DEFAULT_LOCK_PATH

    assert hc.HEALTH_LOCK_FILE != DEFAULT_LOCK_PATH


def test_crash_record_is_redacted(tmp_path):
    try:
        raise RuntimeError(f"boom with {SECRET}")
    except RuntimeError as error:
        alert = hc.record_crash(error, output_dir=tmp_path, now=NOW, secrets=[SECRET])
    assert alert.category == hm.CHECK_CRASHED and alert.severity == hm.CRITICAL
    for name in ("last_crash.txt", "alerts.jsonl"):
        text = (tmp_path / name).read_text(encoding="utf-8")
        assert SECRET not in text and "[REDACTED]" in text


def test_main_crash_returns_4_without_advancing_state(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(hc, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(hc, "HEALTH_LOCK_FILE", tmp_path / "lock")
    monkeypatch.setattr(hc, "load_credentials", lambda **kw: (CREDS, hc.PROFILE_HEALTH))

    def explode(*_a, **_k):
        raise ValueError("unexpected")

    monkeypatch.setattr(hc, "run_health_check", explode)
    assert hc.main(["--scheduled"]) == 4
    assert (tmp_path / "out" / "last_crash.txt").exists()
    assert not (tmp_path / "state.json").exists() and not (tmp_path / "lock").exists()
