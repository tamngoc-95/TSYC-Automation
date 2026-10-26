"""Single-production-writer lock (CLAUDE_AUTOMATION.md "Single-Writer Rule").

A lock file created atomically (O_CREAT | O_EXCL) holds the owner's PID.
Another writer that finds a live holder refuses to start; a holder whose
process no longer exists is stale and is taken over (the takeover is
reported, never silent). Read-only tools never take the lock; preflight
only inspects it.

Process liveness on Windows uses OpenProcess/GetExitCodeProcess: on
Windows `os.kill(pid, 0)` would TERMINATE the process, so it is never used
there.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOCK_PATH = PROJECT_ROOT / "data" / "processed" / "orchestration" / "production_writer.lock"

HELD_BY_OTHER = "HELD_BY_OTHER"
HELD_BY_SELF = "HELD_BY_SELF"
STALE = "STALE"
FREE = "FREE"
UNREADABLE = "UNREADABLE"


class WriterLockBusy(RuntimeError):
    """Another live production writer holds the lock."""


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # Access denied still means the process exists.
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass(frozen=True)
class LockState:
    state: str
    holder: dict[str, Any] | None
    path: Path


def inspect_lock(
    path: Path = DEFAULT_LOCK_PATH,
    *,
    alive: Callable[[int], bool] = process_alive,
    own_pid: int | None = None,
) -> LockState:
    """Read-only view of the lock."""
    own_pid = os.getpid() if own_pid is None else own_pid
    if not path.exists():
        return LockState(FREE, None, path)
    try:
        holder = json.loads(path.read_text(encoding="utf-8"))
        pid = int(holder["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return LockState(UNREADABLE, None, path)
    if pid == own_pid:
        return LockState(HELD_BY_SELF, holder, path)
    if alive(pid):
        return LockState(HELD_BY_OTHER, holder, path)
    return LockState(STALE, holder, path)


class ProductionWriterLock:
    """Context manager; refuses when another live writer holds the lock."""

    def __init__(
        self,
        owner: str,
        path: Path = DEFAULT_LOCK_PATH,
        *,
        alive: Callable[[int], bool] = process_alive,
    ) -> None:
        self.owner = owner
        self.path = path
        self.alive = alive
        self.took_over: dict[str, Any] | None = None
        self._held = False

    def acquire(self) -> "ProductionWriterLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "pid": os.getpid(),
            "owner": self.owner,
            "acquired_at": datetime.now(timezone.utc).isoformat(),
        }
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                state = inspect_lock(self.path, alive=self.alive)
                if state.state == HELD_BY_OTHER:
                    raise WriterLockBusy(
                        f"Production writer lock is held by PID {state.holder.get('pid')} "
                        f"({state.holder.get('owner')}, since {state.holder.get('acquired_at')})."
                    )
                if state.state == HELD_BY_SELF:
                    raise WriterLockBusy("This process already holds the production writer lock.")
                if state.state == UNREADABLE:
                    raise WriterLockBusy(f"Production writer lock {self.path} is unreadable; inspect it by hand.")
                # STALE: the holder process is gone.
                self.took_over = state.holder
                self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle)
            self._held = True
            return self
        raise WriterLockBusy("Could not acquire the production writer lock (raced with another writer).")

    def release(self) -> None:
        if not self._held:
            return
        state = inspect_lock(self.path, alive=self.alive)
        if state.state == HELD_BY_SELF:
            self.path.unlink(missing_ok=True)
        self._held = False

    def __enter__(self) -> "ProductionWriterLock":
        return self.acquire()

    def __exit__(self, *exc: Any) -> None:
        self.release()
