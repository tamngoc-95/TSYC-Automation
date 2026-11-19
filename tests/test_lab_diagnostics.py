"""Lab reliability guard (tools/wp_lab/lab_diagnostics.py), offline."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "wp_lab"))
sys.path.insert(0, str(ROOT / "scripts"))

import lab_diagnostics as ld  # noqa: E402
import run_local_pilot as pilot  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def run(self, watch: ld.SuspendWatch, seconds: int) -> None:
        for _ in range(seconds):
            self.now += 1.0
            watch.tick()

    def freeze(self, watch: ld.SuspendWatch, seconds: float) -> None:
        self.now += seconds      # nothing ticked: the machine was suspended
        watch.tick()


def test_a_normal_run_shows_no_suspension() -> None:
    clock = Clock()
    watch = ld.SuspendWatch(clock=clock)
    clock.run(watch, 300)
    clock.freeze(watch, 15.0)            # a starved thread, not a suspension
    assert watch.report() == [] and watch.since(0.0) == []


@pytest.mark.parametrize("frozen", [632.0, 2720.0])
def test_the_two_real_standbys_would_have_been_seen(frozen: float) -> None:
    """2026-11-07 H09: 632 s of Modern Standby. 2026-11-15 H11: 2720 s."""
    clock = Clock()
    watch = ld.SuspendWatch(clock=clock)
    clock.run(watch, 30)
    started = clock()
    clock.freeze(watch, frozen)
    clock.run(watch, 5)
    assert [gap["frozen_seconds"] for gap in watch.since(started)] == [frozen]
    assert watch.since(clock()) == []     # a test that began after the wake-up is not affected


def test_only_tests_that_overlap_the_suspension_are_marked() -> None:
    clock = Clock()
    watch = ld.SuspendWatch(clock=clock)
    first = clock()
    clock.run(watch, 10)
    second = clock()
    clock.freeze(watch, 600.0)
    third = clock()
    clock.run(watch, 10)
    assert len(watch.since(first)) == 1 and len(watch.since(second)) == 1 and watch.since(third + 0.5) == []


class FakeGuard:
    def __init__(self, gaps: list[dict[str, Any]]) -> None:
        self.gaps, self.captured = gaps, []

    def suspended_since(self, _mark: Any) -> list[dict[str, Any]]:
        return self.gaps

    def capture(self, label: str, _mark: Any, reason: str) -> str:
        self.captured.append((label, reason))
        return f"diagnostics_{label}.json"


GAP = [{"frozen_seconds": 632.0, "noticed_at_utc": "2026-11-07T18:02:37+00:00"}]


def test_a_failure_during_a_suspension_is_invalid_never_a_pass_or_a_product_failure() -> None:
    guard = FakeGuard(GAP)
    status, evidence = pilot.judge_with_host_state(guard, {}, "H09", pilot.FAIL, {"error": "TimeoutExpired"}, pilot.FAIL)
    assert status == pilot.INVALID_HOST_SUSPENDED and status in pilot.NOT_PASSED and status != pilot.PASS_REAL
    assert evidence["host_suspended_during_test"] == GAP and "Re-run" in evidence["rerun"]
    assert evidence["error"] == "TimeoutExpired" and guard.captured == [("H09", "host suspended")]


def test_an_ordinary_failure_stays_a_failure_and_is_captured() -> None:
    guard = FakeGuard([])
    status, evidence = pilot.judge_with_host_state(guard, {}, "T07", pilot.FAIL, {"failed": "x"}, pilot.FAIL)
    assert status == pilot.FAIL and evidence["diagnostics"] == "diagnostics_T07.json"
    assert "host_suspended_during_test" not in evidence


def test_a_pass_keeps_its_status_but_records_the_suspension() -> None:
    guard = FakeGuard(GAP)
    status, evidence = pilot.judge_with_host_state(guard, {}, "T01", pilot.PASS_REAL, {"id": 1}, pilot.FAIL)
    assert status == pilot.PASS_REAL and evidence["host_suspended_during_test"] == GAP and guard.captured == []
    blocked, _ = pilot.judge_with_host_state(FakeGuard([]), {}, "T11", pilot.BLOCKED, {"blocked_on": "x"}, pilot.FAIL)
    assert blocked == pilot.BLOCKED


def test_tail_reads_only_what_was_written_since_the_mark(tmp_path: Path) -> None:
    log = tmp_path / "php_errors.log"
    log.write_text("old line\n", encoding="utf-8")
    offset = ld.size_of(log)
    with open(log, "a", encoding="utf-8", newline="") as handle:
        handle.write("new line\n")
    assert ld.tail(log, offset) == "new line\n"
    assert ld.tail(tmp_path / "missing.log").startswith("unreadable")
    assert ld.size_of(tmp_path / "missing.log") == 0


def test_sqlite_state_reports_journal_files(tmp_path: Path) -> None:
    folder = tmp_path / "site" / "wp-content" / "database"
    folder.mkdir(parents=True)
    (folder / ".ht.sqlite").write_bytes(b"x" * 10)
    assert ld.database_state(tmp_path) == {"engine": "sqlite", "files": {".ht.sqlite": 10}, "journal_or_wal_present": False}
    (folder / ".ht.sqlite-journal").write_bytes(b"x")
    assert ld.database_state(tmp_path)["journal_or_wal_present"] is True


def test_stay_awake_asks_windows_and_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform != "win32":
        assert ld.request_stay_awake(True) is False
        return
    import ctypes

    asked: list[int] = []
    fake = type("K", (), {"SetThreadExecutionState": staticmethod(lambda flags: asked.append(flags.value) or 1)})()
    monkeypatch.setattr(ctypes, "windll", type("W", (), {"kernel32": fake})(), raising=False)
    assert ld.request_stay_awake(True) is True and ld.request_stay_awake(False) is True
    assert asked == [ld.ES_CONTINUOUS | ld.ES_SYSTEM_REQUIRED, ld.ES_CONTINUOUS]


def test_every_pilot_runs_inside_the_guard() -> None:
    for name in ("run_local_pilot.py", "run_hardening_pilot.py", "run_preorder_gate_a.py"):
        source = (ROOT / "tools" / "wp_lab" / name).read_text(encoding="utf-8")
        assert "lab_diagnostics.session(lab.dir, run_dir)" in source, name
        assert "lab.diagnostics, lab.log_dir = guard, guard.log_dir" in source, name
