"""Automated tests for src/cli_bootstrap.py.

These tests are pure/offline: no Supabase, no WooCommerce, no network, no
filesystem writes outside an in-memory buffer. Safe to run in CI or on a
developer machine with no credentials configured.
"""

import ast
import io
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src import cli_bootstrap


VIETNAMESE_TEXT = "Giáo Dục Giới Tính – Vệ Sinh Cá Nhân"


def _make_cp1252_stream() -> io.TextIOWrapper:
    """Build a text stream that mimics a default Windows console encoding.

    cp1252 cannot represent several Vietnamese diacritics used in
    VIETNAMESE_TEXT, so printing to this stream before reconfiguration
    reproduces the real UnicodeEncodeError this bootstrap is meant to
    prevent.
    """
    buffer = io.BytesIO()
    return io.TextIOWrapper(buffer, encoding="cp1252", errors="strict")


def test_vietnamese_text_raises_before_configuration_baseline():
    """Sanity check: prove the failure mode this module fixes is real."""
    stream = _make_cp1252_stream()

    with pytest.raises(UnicodeEncodeError):
        print(VIETNAMESE_TEXT, file=stream)
        stream.flush()


def test_configure_utf8_console_is_idempotent(monkeypatch):
    monkeypatch.setattr(cli_bootstrap.sys, "platform", "win32")
    stdout_stream = _make_cp1252_stream()
    stderr_stream = _make_cp1252_stream()
    monkeypatch.setattr(cli_bootstrap.sys, "stdout", stdout_stream)
    monkeypatch.setattr(cli_bootstrap.sys, "stderr", stderr_stream)

    cli_bootstrap.configure_utf8_console()
    cli_bootstrap.configure_utf8_console()  # second call must not raise

    assert stdout_stream.encoding.lower() == "utf-8"
    assert stderr_stream.encoding.lower() == "utf-8"


def test_vietnamese_text_does_not_raise_after_configure(monkeypatch):
    monkeypatch.setattr(cli_bootstrap.sys, "platform", "win32")
    stream = _make_cp1252_stream()
    monkeypatch.setattr(cli_bootstrap.sys, "stdout", stream)
    monkeypatch.setattr(cli_bootstrap.sys, "stderr", _make_cp1252_stream())

    cli_bootstrap.configure_utf8_console()

    # Before reconfiguration this exact call raises UnicodeEncodeError
    # under cp1252 (proven by the baseline test above); after
    # reconfiguration it must not.
    print(VIETNAMESE_TEXT, file=stream)
    stream.flush()


def test_stream_without_reconfigure_is_handled_gracefully(monkeypatch):
    class NoReconfigureStream:
        pass

    monkeypatch.setattr(cli_bootstrap.sys, "platform", "win32")
    monkeypatch.setattr(cli_bootstrap.sys, "stdout", NoReconfigureStream())
    monkeypatch.setattr(cli_bootstrap.sys, "stderr", NoReconfigureStream())

    # Must not raise even though neither stream supports reconfigure().
    cli_bootstrap.configure_utf8_console()


def test_reconfigure_that_raises_is_handled_gracefully(monkeypatch):
    class ExplodingStream:
        def reconfigure(self, *, encoding=None, errors=None):
            raise ValueError("stream is already in use")

    monkeypatch.setattr(cli_bootstrap.sys, "platform", "win32")
    monkeypatch.setattr(cli_bootstrap.sys, "stdout", ExplodingStream())
    monkeypatch.setattr(cli_bootstrap.sys, "stderr", ExplodingStream())

    # Must not crash the caller even if reconfigure() itself raises.
    cli_bootstrap.configure_utf8_console()


def test_reconfigure_uses_fail_safe_errors_mode(monkeypatch):
    calls = []

    class RecordingStream:
        def reconfigure(self, *, encoding=None, errors=None):
            calls.append({"encoding": encoding, "errors": errors})

    monkeypatch.setattr(cli_bootstrap.sys, "platform", "win32")
    monkeypatch.setattr(cli_bootstrap.sys, "stdout", RecordingStream())
    monkeypatch.setattr(cli_bootstrap.sys, "stderr", RecordingStream())

    cli_bootstrap.configure_utf8_console()

    assert calls == [
        {"encoding": "utf-8", "errors": "replace"},
        {"encoding": "utf-8", "errors": "replace"},
    ]


def test_non_windows_platform_does_not_mutate_streams(monkeypatch):
    class ExplodingIfCalledStream:
        def reconfigure(self, *, encoding=None, errors=None):
            raise AssertionError(
                "reconfigure() must not be called on non-Windows platforms"
            )

    monkeypatch.setattr(cli_bootstrap.sys, "platform", "linux")
    monkeypatch.setattr(cli_bootstrap.sys, "stdout", ExplodingIfCalledStream())
    monkeypatch.setattr(cli_bootstrap.sys, "stderr", ExplodingIfCalledStream())

    cli_bootstrap.configure_utf8_console()  # must be a no-op


def test_stdin_is_never_touched(monkeypatch):
    class ExplodingIfCalledStream:
        def reconfigure(self, *, encoding=None, errors=None):
            raise AssertionError("stdin must never be reconfigured")

    monkeypatch.setattr(cli_bootstrap.sys, "platform", "win32")
    monkeypatch.setattr(cli_bootstrap.sys, "stdin", ExplodingIfCalledStream())
    monkeypatch.setattr(cli_bootstrap.sys, "stdout", _make_cp1252_stream())
    monkeypatch.setattr(cli_bootstrap.sys, "stderr", _make_cp1252_stream())

    cli_bootstrap.configure_utf8_console()


def test_no_network_or_database_imports():
    """cli_bootstrap must stay a zero-dependency, side-effect-free module."""
    source = Path(cli_bootstrap.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module.split(".")[0])

    forbidden = {
        "supabase",
        "postgrest",
        "storage3",
        "requests",
        "httpx",
        "playwright",
        "dotenv",
        "urllib3",
        "socket",
        "os",  # no env/secret access either
    }

    disallowed_hits = imported_names & forbidden
    assert not disallowed_hits, (
        f"cli_bootstrap.py must not import: {sorted(disallowed_hits)}"
    )
