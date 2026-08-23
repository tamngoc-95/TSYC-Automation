"""Shared CLI startup bootstrap for TSYC production scripts.

Every `scripts/*.py` entrypoint prints Supabase-sourced text -- book
titles, authors, descriptions, source URLs, status messages -- that is
frequently Vietnamese and contains diacritics. On a default (non-UTF-8)
Windows console, `print()`-ing that text can raise `UnicodeEncodeError`
and crash a script mid-batch.

Several scripts (for example manual_create_product_reference.py,
register_reference_source.py, correct_candidate_extraction.py,
prepare_product_content.py) also read free-text Vietnamese input back via
`input()` -- reviewer-entered titles, source names, corrected content.
When that stdin is redirected (piped from a file, a heredoc, or another
process -- as opposed to a live interactive console, which on modern
Windows reads native UTF-16 through the console API and mostly bypasses
this problem) it falls back to decoding with the process's legacy
codepage (frequently cp1252 or cp850), which cannot represent many
Vietnamese diacritics and silently mangles that input into mojibake
unless a caller remembers to set `PYTHONIOENCODING=utf-8` before starting
the interpreter.

`configure_utf8_console()` fixes both directions once, in one place,
instead of every script (or an external environment variable) having to
be remembered on every invocation.

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
    """Configure sys.stdin/sys.stdout/sys.stderr for safe UTF-8 on Windows.

    Idempotent: safe to call more than once (including from more than one
    imported module in the same process) without side effects beyond
    re-applying the same encoding.

    On non-Windows platforms this is a no-op -- those consoles already
    default to UTF-8, so the streams are left untouched.

    stdout/stderr use `errors="replace"`: an unmappable output character
    is replaced with U+FFFD instead of crashing a script mid-batch, which
    is the right trade-off for a stream nothing reads back programmatically.

    sys.stdin deliberately uses `errors="strict"` instead, not "replace".
    Bytes read from stdin can become reviewer-entered reference/identity
    evidence that gets persisted (see manual_create_product_reference.py,
    register_reference_source.py). Silently substituting U+FFFD for
    malformed input would let corrupted text flow into saved records
    unnoticed; raising `UnicodeDecodeError` instead fails loudly so the
    operator sees and can correct the problem, rather than the pipeline
    quietly writing mangled data under CLAUDE.md's "do not silently
    corrupt or overwrite finalized identity evidence" rule. Reconfiguring
    the encoding to UTF-8 at all is still what fixes the common case
    (mojibake from a legacy-codepage/UTF-8 mismatch, which typically
    decodes without raising anything, just wrong) -- "strict" only changes
    behavior for genuinely invalid byte sequences.
    """
    if not sys.platform.startswith("win"):
        return

    _reconfigure_stream(sys.stdin, errors="strict")
    _reconfigure_stream(sys.stdout, errors="replace")
    _reconfigure_stream(sys.stderr, errors="replace")


def _reconfigure_stream(stream: Any, *, errors: str) -> None:
    """Best-effort UTF-8 reconfiguration of a single text stream.

    Fails safe: if the stream has no `reconfigure()` method (for example
    because it was replaced by a test harness or an unusual redirect), or
    `reconfigure()` itself raises, the stream is left exactly as found
    rather than crashing the caller. This applies to the reconfiguration
    call itself only -- the caller-supplied `errors` mode still governs
    ordinary decode/encode behavior on the stream afterward.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return

    try:
        reconfigure(encoding="utf-8", errors=errors)
    except Exception:
        return
