"""Automated tests for scripts/watch_facebook_clipboard.py.

Fully offline: the real Windows clipboard is never read (clipboard_reader
is always injected as a stub), no live Supabase, and no
Facebook/Playwright/browser access anywhere. subprocess_runner is always
injected too, so --process never actually spawns collect_one_facebook_
post.py / clean_facebook_raw_pages.py in these tests.
"""

from __future__ import annotations

import subprocess
from typing import Any

import watch_facebook_clipboard as watcher
from src.services.source_ingestion import AUTHORIZED_GROUP_ID
from support.fake_supabase import FakeSupabaseRepository


BATCH_ID = "11111111-1111-1111-1111-111111111111"
BATCH_CODE = "FB-2026-001"


def make_repository(**tables: list[dict[str, Any]]) -> FakeSupabaseRepository:
    tables.setdefault("source_urls", [])
    tables.setdefault("batches", [{"batch_id": BATCH_ID, "batch_code": BATCH_CODE}])
    return FakeSupabaseRepository(tables=dict(tables))


def authorized_url(post_id: str = "42") -> str:
    return f"https://www.facebook.com/groups/{AUTHORIZED_GROUP_ID}/permalink/{post_id}/"


def clipboard_sequence(values: list[str | None]):
    """A clipboard_reader stub that returns each value in order, then
    repeats the last one forever (simulating a link left in the
    clipboard after the operator stops copying new ones)."""
    values = list(values)

    def reader() -> str | None:
        if values:
            return values.pop(0)
        return None

    return reader


def no_sleep(_seconds: float) -> None:
    return None


def recording_subprocess_runner(returncode: int = 0):
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, "", "")

    return runner, calls


# ---------------------------------------------------------------------
# 1. Valid TSYC permalink is registered
# ---------------------------------------------------------------------


def test_valid_tsyc_permalink_is_registered():
    repository = make_repository()

    last_seen, outcome = watcher.process_clipboard_value(
        authorized_url("1"), repository=repository, batch_id=BATCH_ID, last_seen=None
    )

    assert outcome.status == "REGISTERED"
    assert last_seen == authorized_url("1")
    assert len(repository.client.tables["source_urls"]) == 1


# ---------------------------------------------------------------------
# 2. Foreign group is ignored (SOURCE_INVALID, no write)
# ---------------------------------------------------------------------


def test_foreign_group_permalink_is_ignored():
    repository = make_repository()
    foreign_url = "https://www.facebook.com/groups/9999999999/permalink/1/"

    _last_seen, outcome = watcher.process_clipboard_value(
        foreign_url, repository=repository, batch_id=BATCH_ID, last_seen=None
    )

    assert outcome.status == "SOURCE_INVALID"
    assert repository.client.tables["source_urls"] == []


# ---------------------------------------------------------------------
# 3. Malformed URL is ignored
# ---------------------------------------------------------------------


def test_malformed_url_is_ignored():
    repository = make_repository()

    _last_seen, outcome = watcher.process_clipboard_value(
        "not-a-url-at-all", repository=repository, batch_id=BATCH_ID, last_seen=None
    )

    assert outcome.status == "SOURCE_INVALID"
    assert repository.client.tables["source_urls"] == []


# ---------------------------------------------------------------------
# 4. Arbitrary clipboard text (unrelated copy/paste content) is ignored
# ---------------------------------------------------------------------


def test_arbitrary_clipboard_text_is_ignored():
    repository = make_repository()

    _last_seen, outcome = watcher.process_clipboard_value(
        "Remember to buy milk on the way home",
        repository=repository,
        batch_id=BATCH_ID,
        last_seen=None,
    )

    assert outcome.status == "SOURCE_INVALID"
    assert repository.client.tables["source_urls"] == []


def test_empty_clipboard_produces_no_outcome():
    repository = make_repository()

    last_seen, outcome = watcher.process_clipboard_value(
        None, repository=repository, batch_id=BATCH_ID, last_seen="previous"
    )

    assert outcome is None
    assert last_seen == "previous"  # unchanged
    assert repository.client.tables["source_urls"] == []


# ---------------------------------------------------------------------
# 5. Same clipboard URL repeated -- duplicate-clipboard protection
# ---------------------------------------------------------------------


def test_same_clipboard_value_is_not_reprocessed():
    repository = make_repository()
    url = authorized_url("7")

    last_seen, outcome1 = watcher.process_clipboard_value(
        url, repository=repository, batch_id=BATCH_ID, last_seen=None
    )
    assert outcome1.status == "REGISTERED"

    # Same value still in the clipboard on the next poll -- must not call
    # the ingestion service again at all.
    last_seen2, outcome2 = watcher.process_clipboard_value(
        url, repository=repository, batch_id=BATCH_ID, last_seen=last_seen
    )

    assert outcome2 is None
    assert last_seen2 == url
    assert len(repository.client.tables["source_urls"]) == 1


# ---------------------------------------------------------------------
# 6. Already-known URL -- reported ALREADY_KNOWN, crawl_status untouched
# ---------------------------------------------------------------------


def test_already_known_url_is_reported_without_resetting_status():
    repository = make_repository(
        source_urls=[
            {
                "source_url_id": "existing-1",
                "batch_id": BATCH_ID,
                "source_type": "FACEBOOK_POST",
                "source_url": authorized_url("55"),
                "crawl_status": "COLLECTED",
                "is_authorized": True,
            }
        ]
    )

    _last_seen, outcome = watcher.process_clipboard_value(
        authorized_url("55"), repository=repository, batch_id=BATCH_ID, last_seen=None
    )

    assert outcome.status == "ALREADY_KNOWN"
    assert repository.client.tables["source_urls"][0]["crawl_status"] == "COLLECTED"
    assert len(repository.client.tables["source_urls"]) == 1


# ---------------------------------------------------------------------
# 7. Newly ingested URL -- exactly one row created
# ---------------------------------------------------------------------


def test_newly_ingested_url_creates_exactly_one_row():
    repository = make_repository()

    watcher.process_clipboard_value(
        authorized_url("100"), repository=repository, batch_id=BATCH_ID, last_seen=None
    )

    assert len(repository.client.tables["source_urls"]) == 1
    row = repository.client.tables["source_urls"][0]
    assert row["crawl_status"] == "PENDING"
    assert row["is_authorized"] is True


# ---------------------------------------------------------------------
# 8. No duplicate write across two different valid clipboard readings of
# the same underlying post (e.g. copied again after being replaced)
# ---------------------------------------------------------------------


def test_no_duplicate_write_when_same_url_reappears_after_a_change():
    repository = make_repository()
    url_a = authorized_url("1")
    url_b = authorized_url("2")

    last_seen, _ = watcher.process_clipboard_value(
        url_a, repository=repository, batch_id=BATCH_ID, last_seen=None
    )
    last_seen, _ = watcher.process_clipboard_value(
        url_b, repository=repository, batch_id=BATCH_ID, last_seen=last_seen
    )
    # url_a copied again later (clipboard changed away and back)
    last_seen, outcome = watcher.process_clipboard_value(
        url_a, repository=repository, batch_id=BATCH_ID, last_seen=last_seen
    )

    assert outcome.status == "ALREADY_KNOWN"
    assert len(repository.client.tables["source_urls"]) == 2


# ---------------------------------------------------------------------
# 9. --once behavior: reads exactly once, ingests if valid, exits
# ---------------------------------------------------------------------


def test_cli_once_ingests_a_valid_clipboard_url(capsys):
    repository = make_repository()
    reader = clipboard_sequence([authorized_url("9")])

    exit_code = watcher.main(
        ["--once", "--batch-code", BATCH_CODE],
        repository=repository,
        clipboard_reader=reader,
    )

    assert exit_code == 0
    assert len(repository.client.tables["source_urls"]) == 1
    output = capsys.readouterr().out
    assert "REGISTERED" in output


def test_cli_once_ignores_invalid_clipboard_without_writing(capsys):
    repository = make_repository()
    reader = clipboard_sequence(["just some random text"])

    exit_code = watcher.main(
        ["--once", "--batch-code", BATCH_CODE],
        repository=repository,
        clipboard_reader=reader,
    )

    assert exit_code == 0
    assert repository.client.tables["source_urls"] == []
    output = capsys.readouterr().out
    assert "ignored" in output.lower()
    # Privacy: raw clipboard content must never be echoed.
    assert "just some random text" not in output


def test_cli_once_reads_clipboard_exactly_once():
    repository = make_repository()
    call_count = {"n": 0}

    def counting_reader() -> str | None:
        call_count["n"] += 1
        return authorized_url("1")

    watcher.main(
        ["--once", "--batch-code", BATCH_CODE],
        repository=repository,
        clipboard_reader=counting_reader,
    )

    assert call_count["n"] == 1


# ---------------------------------------------------------------------
# 10. --watch bounding: requires --max-sources, stops at the bound
# ---------------------------------------------------------------------


def test_cli_watch_without_max_sources_fails_before_any_io():
    exit_code = watcher.main(["--watch", "--batch-code", BATCH_CODE])
    assert exit_code == 2


def test_cli_watch_stops_after_max_sources_reached():
    repository = make_repository()
    reader = clipboard_sequence(
        [authorized_url("1"), authorized_url("2"), authorized_url("3")]
    )

    exit_code = watcher.main(
        ["--watch", "--batch-code", BATCH_CODE, "--max-sources", "2"],
        repository=repository,
        clipboard_reader=reader,
        sleep=no_sleep,
    )

    assert exit_code == 0
    assert len(repository.client.tables["source_urls"]) == 2


def test_cli_interval_below_minimum_fails_before_any_io():
    repository = make_repository()

    exit_code = watcher.main(
        ["--watch", "--batch-code", BATCH_CODE, "--max-sources", "1", "--interval", "0.1"],
        repository=repository,
    )

    assert exit_code == 2
    assert repository.client.tables["source_urls"] == []


def test_cli_once_and_watch_are_mutually_exclusive():
    import pytest

    with pytest.raises(SystemExit):
        watcher.main(["--once", "--watch", "--batch-code", BATCH_CODE])


def test_cli_unknown_batch_code_fails():
    repository = make_repository()
    reader = clipboard_sequence([authorized_url("1")])

    exit_code = watcher.main(
        ["--once", "--batch-code", "NO-SUCH-BATCH"],
        repository=repository,
        clipboard_reader=reader,
    )

    assert exit_code == 2
    assert repository.client.tables["source_urls"] == []


# ---------------------------------------------------------------------
# 11. --process auto-continue invokes the existing collector/cleaner,
# never creates a candidate, never touches WooCommerce
# ---------------------------------------------------------------------


def test_process_flag_invokes_collect_and_clean_for_new_registration():
    repository = make_repository()
    reader = clipboard_sequence([authorized_url("1")])
    runner, calls = recording_subprocess_runner()

    watcher.main(
        ["--once", "--batch-code", BATCH_CODE, "--process"],
        repository=repository,
        clipboard_reader=reader,
        subprocess_runner=runner,
    )

    assert len(calls) == 2
    assert calls[0][1].endswith("collect_one_facebook_post.py")
    assert calls[1][1].endswith("clean_facebook_raw_pages.py")
    assert "--source-url-id" in calls[0]
    assert "--confirm-save" in calls[0]
    assert "--non-interactive" in calls[0]


def test_process_flag_never_invokes_candidate_creation_or_woo():
    repository = make_repository()
    reader = clipboard_sequence([authorized_url("1")])
    runner, calls = recording_subprocess_runner()

    watcher.main(
        ["--once", "--batch-code", BATCH_CODE, "--process"],
        repository=repository,
        clipboard_reader=reader,
        subprocess_runner=runner,
    )

    joined = " ".join(" ".join(argv) for argv in calls)
    assert "create_candidates_from_cleaned_posts.py" not in joined
    assert "create_woocommerce_draft.py" not in joined
    assert "woocommerce" not in joined.lower()


def test_process_flag_not_invoked_for_already_known_url():
    repository = make_repository(
        source_urls=[
            {
                "source_url_id": "existing-1",
                "batch_id": BATCH_ID,
                "source_type": "FACEBOOK_POST",
                "source_url": authorized_url("55"),
                "crawl_status": "COLLECTED",
                "is_authorized": True,
            }
        ]
    )
    reader = clipboard_sequence([authorized_url("55")])
    runner, calls = recording_subprocess_runner()

    watcher.main(
        ["--once", "--batch-code", BATCH_CODE, "--process"],
        repository=repository,
        clipboard_reader=reader,
        subprocess_runner=runner,
    )

    assert calls == []


def test_default_behavior_never_invokes_process_chain():
    repository = make_repository()
    reader = clipboard_sequence([authorized_url("1")])
    runner, calls = recording_subprocess_runner()

    watcher.main(
        ["--once", "--batch-code", BATCH_CODE],
        repository=repository,
        clipboard_reader=reader,
        subprocess_runner=runner,
    )

    assert calls == []


def test_process_chain_failure_does_not_crash_watcher():
    repository = make_repository()
    reader = clipboard_sequence([authorized_url("1")])
    runner, _calls = recording_subprocess_runner(returncode=1)

    exit_code = watcher.main(
        ["--once", "--batch-code", BATCH_CODE, "--process"],
        repository=repository,
        clipboard_reader=reader,
        subprocess_runner=runner,
    )

    assert exit_code == 0  # ingestion itself still succeeded


# ---------------------------------------------------------------------
# 12. Privacy: no arbitrary clipboard content ever reaches process_logs
# or stdout for an invalid value
# ---------------------------------------------------------------------


def test_invalid_clipboard_content_never_persisted_anywhere(capsys):
    repository = make_repository()
    secret_like_text = "hunter2-my-password-do-not-log-me"
    reader = clipboard_sequence([secret_like_text])

    watcher.main(
        ["--once", "--batch-code", BATCH_CODE],
        repository=repository,
        clipboard_reader=reader,
    )

    output = capsys.readouterr().out
    assert secret_like_text not in output
    assert repository.client.tables.get("process_logs", []) == []
    assert repository.client.tables["source_urls"] == []


# ---------------------------------------------------------------------
# 13. Clipboard reading itself never imports/uses Playwright
# ---------------------------------------------------------------------


def test_watcher_module_never_imports_playwright():
    import ast
    from pathlib import Path

    tree = ast.parse(Path(watcher.__file__).read_text(encoding="utf-8"))
    imported_names = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert "playwright" not in imported_names
