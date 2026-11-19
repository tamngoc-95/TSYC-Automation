"""Reliability and diagnostics for the local lab pilots.

Why this exists. Two lab runs stalled without explanation: H09 on 2026-11-07
(704 s instead of ~90 s) and H11 on 2026-11-15 (2722 s instead of ~25 s). The
Windows System event log shows the machine entered Modern Standby inside
exactly those tests (17:52:05Z-18:02:37Z, and 07:25:24Z-08:10:44Z, i.e. 632 s
and 2720 s). The lab was frozen with the rest of the machine; nothing in
WordPress, the bridge or the database hung.

So a pilot run now:

  * asks Windows to stay awake while it runs (this stops the idle timer; it
    cannot stop a closed lid or a pressed power button);
  * watches for a frozen clock, and marks any test that overlapped a
    suspension INVALID_HOST_SUSPENDED instead of reporting a product failure.
    That is never a pass: the run exits non-zero and the test must be re-run;
  * keeps the PHP server's request log and the database's error log per run;
  * on any failure, captures process state, database and lock state, the
    recent log lines and the power events of that window, next to the report.

Nothing here contacts anything but the loopback lab and the local machine.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

INVALID_HOST_SUSPENDED = "INVALID_HOST_SUSPENDED"

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
TICK_SECONDS = 1.0
# A loaded machine can starve a thread for a few seconds; a suspension is far longer.
GAP_SECONDS = 20.0
TAIL_BYTES = 12000


class SuspendWatch:
    """Notices when the whole process stopped running for a while.

    A thread notes the time every second. If two notes are more than
    GAP_SECONDS apart, nothing in this process ran in between: the machine
    was suspended (or the process was frozen). time.monotonic() keeps
    counting through Windows standby, which is what makes the gap visible.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, gap_seconds: float = GAP_SECONDS) -> None:
        self.clock = clock
        self.gap_seconds = gap_seconds
        self.gaps: list[dict[str, Any]] = []
        self._last = clock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> None:
        now = self.clock()
        if now - self._last > self.gap_seconds:
            self.gaps.append({"resumed_monotonic": now, "frozen_seconds": round(now - self._last, 1),
                              "noticed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        self._last = now

    def start(self) -> "SuspendWatch":
        def loop() -> None:
            while not self._stop.wait(TICK_SECONDS):
                self.tick()
        self._thread = threading.Thread(target=loop, name="tsyc-suspend-watch", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.tick()

    def since(self, started_monotonic: float) -> list[dict[str, Any]]:
        """Suspensions that ended after this moment, i.e. overlapped the work begun then."""
        self.tick()
        return [{k: v for k, v in gap.items() if k != "resumed_monotonic"}
                for gap in self.gaps if gap["resumed_monotonic"] >= started_monotonic]

    def report(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in gap.items() if k != "resumed_monotonic"} for gap in self.gaps]


def request_stay_awake(on: bool) -> bool:
    """True when Windows accepted the request. No-op (False) elsewhere."""
    if sys.platform != "win32":
        return False
    import ctypes

    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
    return bool(ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags)))


def _powershell(command: str, timeout: int = 40) -> str:
    done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                          capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
    return done.stdout.strip()


def power_events(start_utc: datetime, end_utc: datetime) -> list[dict[str, Any]] | str:
    """Standby and wake events in a window, from the Windows System log."""
    if sys.platform != "win32":
        return "not Windows"
    command = (
        "$e = Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-Kernel-Power'; "
        f"Id=42,107,506,507; StartTime=[datetime]::Parse('{start_utc:%Y-%m-%dT%H:%M:%SZ}').ToLocalTime(); "
        f"EndTime=[datetime]::Parse('{end_utc:%Y-%m-%dT%H:%M:%SZ}').ToLocalTime()}} -ErrorAction SilentlyContinue; "
        "@($e | Sort-Object TimeCreated | ForEach-Object { [pscustomobject]@{ at_utc=$_.TimeCreated.ToUniversalTime().ToString('u'); "
        "id=$_.Id; text=($_.Message -split \"`n\")[0].Trim() } }) | ConvertTo-Json -Compress")
    try:
        raw = _powershell(command)
        if not raw:
            return []
        data = json.loads(raw)
        return data if isinstance(data, list) else [data]
    except Exception as error:  # noqa: BLE001 - diagnostics never break a run
        return f"unavailable: {type(error).__name__}: {error}"


def lab_processes(lab_dir: Path) -> list[dict[str, Any]] | str:
    """Every process whose program lives in the lab folder: PHP servers, WP-CLI
    runs and the database. The pilot itself runs from the repository, so it is not one."""
    if sys.platform != "win32":
        return "not Windows"
    needle = str(lab_dir).replace("'", "''")
    command = (
        f"@(Get-CimInstance Win32_Process | Where-Object {{ $_.ExecutablePath -and $_.ExecutablePath.StartsWith('{needle}', [System.StringComparison]::OrdinalIgnoreCase) }} | "
        "ForEach-Object { [pscustomobject]@{ pid=$_.ProcessId; name=$_.Name; started_utc=$_.CreationDate.ToUniversalTime().ToString('u'); "
        "cpu_seconds=[math]::Round(($_.KernelModeTime + $_.UserModeTime) / 1e7, 1); memory_mb=[math]::Round($_.WorkingSetSize / 1MB, 1); "
        "threads=$_.ThreadCount; command=([string]$_.CommandLine).Substring(0, [math]::Min(220, ([string]$_.CommandLine).Length)) } }) | ConvertTo-Json -Compress")
    try:
        raw = _powershell(command)
        if not raw:
            return []
        data = json.loads(raw)
        return data if isinstance(data, list) else [data]
    except Exception as error:  # noqa: BLE001
        return f"unavailable: {type(error).__name__}: {error}"


def machine_resources() -> dict[str, Any] | str:
    if sys.platform != "win32":
        return "not Windows"
    command = ("$o = Get-CimInstance Win32_OperatingSystem; $c = Get-CimInstance Win32_Processor | Select-Object -First 1; "
               "[pscustomobject]@{ free_memory_mb=[math]::Round($o.FreePhysicalMemory / 1KB); total_memory_mb=[math]::Round($o.TotalVisibleMemorySize / 1KB); "
               "cpu_load_percent=$c.LoadPercentage } | ConvertTo-Json -Compress")
    try:
        return json.loads(_powershell(command))
    except Exception as error:  # noqa: BLE001
        return f"unavailable: {type(error).__name__}: {error}"


def tail(path: Path, offset: int = 0) -> str:
    """The file from `offset`, at most the last TAIL_BYTES of it."""
    try:
        with open(path, "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(offset, size - TAIL_BYTES))
            return handle.read().decode("utf-8", errors="replace")
    except OSError as error:
        return f"unreadable: {error}"


def size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def database_state(lab_dir: Path) -> dict[str, Any]:
    """Connections, open transactions and lock waits (MariaDB), or the database files (SQLite)."""
    client = lab_dir / "mariadb" / "bin" / "mariadb.exe"
    if not client.exists():
        folder = lab_dir / "site" / "wp-content" / "database"
        files = {p.name: p.stat().st_size for p in folder.glob("*")} if folder.exists() else {}
        return {"engine": "sqlite", "files": files,
                "journal_or_wal_present": any(n.endswith(("-journal", "-wal", "-shm")) for n in files)}
    state: dict[str, Any] = {"engine": "mariadb"}
    queries = {
        "processlist": "SELECT id, user, db, command, time, state, LEFT(info, 200) AS info FROM information_schema.PROCESSLIST",
        "open_transactions": "SELECT trx_id, trx_state, trx_started, trx_wait_started, trx_mysql_thread_id, "
                             "LEFT(trx_query, 200) AS trx_query FROM information_schema.INNODB_TRX",
        "lock_waits": "SELECT * FROM information_schema.INNODB_LOCK_WAITS",
        "bridge_lock_rows": "SELECT option_name, option_value FROM lab.wp_options WHERE option_name LIKE 'tsyc\\_bridge\\_lock\\_%'",
    }
    for name, sql in queries.items():
        try:
            import provision_lab  # the lab's own port constant

            done = subprocess.run([str(client), "-h127.0.0.1", f"-P{provision_lab.MARIADB_PORT}", "-uroot", "--batch", "-e", sql],
                                  capture_output=True, encoding="utf-8", errors="replace", timeout=20)
            state[name] = done.stdout.strip() if done.returncode == 0 else f"query failed: {done.stderr.strip()[:300]}"
        except Exception as error:  # noqa: BLE001
            state[name] = f"unavailable: {type(error).__name__}: {error}"
    return state


class Session:
    """One pilot run's reliability guard. Use through `session(...)`."""

    def __init__(self, lab_dir: Path, run_dir: Path) -> None:
        self.lab_dir = lab_dir
        self.run_dir = run_dir
        self.log_dir = run_dir / "lab_logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.watch = SuspendWatch()
        self.started_utc = datetime.now(timezone.utc)
        self.stay_awake_accepted = False
        self.php_errors = lab_dir / "php_errors.log"
        self.captures: list[str] = []

    # ---- per test ---------------------------------------------------------

    def mark(self) -> dict[str, Any]:
        return {"monotonic": time.monotonic(), "utc": datetime.now(timezone.utc), "php_errors_offset": size_of(self.php_errors),
                "server_offsets": {p.name: size_of(p) for p in self.log_dir.glob("*.log")}}

    def suspended_since(self, mark: dict[str, Any]) -> list[dict[str, Any]]:
        return self.watch.since(mark["monotonic"])

    def capture(self, label: str, mark: dict[str, Any], reason: str) -> str:
        """Everything needed to explain a failed or stalled test, written next to the report."""
        now = datetime.now(timezone.utc)
        data = {
            "label": label, "reason": reason, "test_started_utc": mark["utc"].isoformat(timespec="seconds"),
            "captured_utc": now.isoformat(timespec="seconds"),
            "seconds_since_test_start": round(time.monotonic() - mark["monotonic"], 1),
            "host_suspended_during_test": self.suspended_since(mark),
            "power_events": power_events(mark["utc"] - timedelta(seconds=30), now + timedelta(seconds=5)),
            "machine": machine_resources(),
            "lab_processes": lab_processes(self.lab_dir),
            "database": database_state(self.lab_dir),
            "php_errors_since_test_start": tail(self.php_errors, mark["php_errors_offset"]),
            "server_logs_since_test_start": {name: tail(self.log_dir / name, offset)
                                             for name, offset in mark["server_offsets"].items()},
        }
        path = self.log_dir / f"diagnostics_{label}_{now:%H%M%S}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        self.captures.append(str(path))
        return str(path)

    # ---- whole run --------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        """Call after the lab's servers and database were stopped."""
        left = lab_processes(self.lab_dir)
        return {
            "stay_awake_requested": True, "stay_awake_accepted": self.stay_awake_accepted,
            "host_suspensions": self.watch.report(),
            "power_events": power_events(self.started_utc, datetime.now(timezone.utc) + timedelta(seconds=5)),
            "diagnostic_captures": list(self.captures),
            "logs": str(self.log_dir),
            "cleanup": {"lab_processes_left_running": left,
                        "clean": isinstance(left, list) and not left},
        }


@contextlib.contextmanager
def session(lab_dir: Path, run_dir: Path) -> Iterator[Session]:
    current = Session(lab_dir, run_dir)
    current.stay_awake_accepted = request_stay_awake(True)
    current.watch.start()
    try:
        yield current
    finally:
        current.watch.stop()
        request_stay_awake(False)
