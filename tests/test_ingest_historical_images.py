"""
Tests for scripts/ingest_historical_images.py -- the thin extract+upload
chain scripts/run_batch.py dispatches for the IMAGE_INGEST_PENDING_
HISTORICAL state.

Fully offline: subprocess_runner is injected, so no real Python
subprocess (and therefore no Supabase/filesystem access) is ever
started.
"""

from __future__ import annotations

import subprocess
from typing import Callable

import ingest_historical_images as ingest


def _completed(argv: list[str], returncode: int, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=argv, returncode=returncode, stdout=stdout, stderr="")


def test_build_extract_argv_uses_non_interactive_confirm_extract():
    argv = ingest.build_extract_argv("FB-HIST-2026-001-CAN-0001", "FB-2026-001")

    assert "extract_historical_facebook_images.py" in argv[1]
    assert "--candidate-code" in argv and "FB-HIST-2026-001-CAN-0001" in argv
    assert "--non-interactive" in argv
    assert "--confirm-extract" in argv


def test_build_upload_argv_uses_images_all():
    argv = ingest.build_upload_argv("FB-HIST-2026-001-CAN-0001", "FB-2026-001")

    assert "upload_facebook_images_to_supabase.py" in argv[1]
    assert "--images" in argv and "ALL" in argv
    assert "--non-interactive" in argv
    assert "--confirm-upload" in argv


def test_non_interactive_requires_confirm_ingest():
    exit_code = ingest.main(
        ["--candidate-code", "FB-HIST-2026-001-CAN-0001", "--non-interactive"]
    )

    assert exit_code == 2


def test_confirm_ingest_required_even_interactively():
    exit_code = ingest.main(["--candidate-code", "FB-HIST-2026-001-CAN-0001"])

    assert exit_code == 2


def test_chains_extract_then_upload_on_success():
    calls: list[list[str]] = []

    def fake_runner(argv: list[str]) -> subprocess.CompletedProcess:
        calls.append(argv)
        return _completed(argv, 0)

    exit_code = ingest.main(
        [
            "--candidate-code",
            "FB-HIST-2026-001-CAN-0001",
            "--non-interactive",
            "--confirm-ingest",
        ],
        subprocess_runner=fake_runner,
    )

    assert exit_code == 0
    assert len(calls) == 2
    assert "extract_historical_facebook_images.py" in calls[0][1]
    assert "upload_facebook_images_to_supabase.py" in calls[1][1]


def test_upload_is_never_invoked_when_extraction_fails():
    calls: list[list[str]] = []

    def fake_runner(argv: list[str]) -> subprocess.CompletedProcess:
        calls.append(argv)
        return _completed(argv, 1, "boom")

    exit_code = ingest.main(
        [
            "--candidate-code",
            "FB-HIST-2026-001-CAN-0001",
            "--non-interactive",
            "--confirm-ingest",
        ],
        subprocess_runner=fake_runner,
    )

    assert exit_code == 1
    assert len(calls) == 1


def test_upload_failure_is_propagated():
    def fake_runner(argv: list[str]) -> subprocess.CompletedProcess:
        if "extract_historical_facebook_images.py" in argv[1]:
            return _completed(argv, 0)
        return _completed(argv, 1, "upload boom")

    exit_code = ingest.main(
        [
            "--candidate-code",
            "FB-HIST-2026-001-CAN-0001",
            "--non-interactive",
            "--confirm-ingest",
        ],
        subprocess_runner=fake_runner,
    )

    assert exit_code == 1
