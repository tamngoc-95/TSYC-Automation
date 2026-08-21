"""Shared CLI startup bootstrap for TSYC production scripts.

Every `scripts/*.py` entrypoint prints Supabase-sourced text -- book
titles, authors, descriptions, source URLs, status messages -- that is
frequently Vietnamese and contains diacritics. On a default (non-UTF-8)
Windows console, `print()`-ing that text can raise `UnicodeEncodeError`
and crash a script mid-batch.

`configure_utf8_console()` fixes this once, in one place, instead of every
script (or an environment variable such as PYTHONIOENCODING) having to be
remembered on every invocation.

This module is intentionally minimal:
- no Supabase dependency
- no WooCommerce dependency
- no environment-secret dependency
- no database/network side effects
- safe to import and call at process startup, before any other pipeline
  code runs
"""

from __future__ import annotations

import sys
from typing import Any


def configure_utf8_console() -> None:
    """Configure sys.stdout/sys.stderr for safe UTF-8 output on Windows.

    Idempotent: safe to call more than once (including from more than one
    imported module in the same process) without side effects beyond
    re-applying the same encoding.

    On non-Windows platforms this is a no-op -- those consoles already
    default to UTF-8, so the streams are left untouched.

    sys.stdin is deliberately left alone: no script in this pipeline reads
    interactive input that requires re-encoding, so there is no
    demonstrated need to mutate it.
    """
    if not sys.platform.startswith("win"):
        return

    _reconfigure_stream(sys.stdout)
    _reconfigure_stream(sys.stderr)


def _reconfigure_stream(stream: Any) -> None:
    """Best-effort UTF-8 reconfiguration of a single text stream.

    Fails safe: if the stream has no `reconfigure()` method (for example
    because it was replaced by a test harness or an unusual redirect), or
    `reconfigure()` itself raises, the stream is left exactly as found
    rather than crashing the caller.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return

    try:
        reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        return
