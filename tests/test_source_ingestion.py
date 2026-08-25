"""Automated tests for src/services/source_ingestion.py and
scripts/ingest_source_urls.py.

Fully offline: no live Facebook, no live Supabase, no Playwright/browser
of any kind is imported or invoked anywhere in these tests or in the
module under test. Source_ingestion never crawls/discovers URLs -- every
test supplies exact URLs, exactly as every real caller must.
"""

from __future__ import annotations

import pytest

import ingest_source_urls
from src.services import source_ingestion as si
from support.fake_supabase import FakeSupabaseRepository


AUTHORIZED_GROUP_ID = si.AUTHORIZED_GROUP_ID
BATCH_ID = "11111111-1111-1111-1111-111111111111"
BATCH_CODE = "FB-2026-001"


def make_repository(**tables) -> FakeSupabaseRepository:
    tables.setdefault("source_urls", [])
    tables.setdefault("batches", [{"batch_id": BATCH_ID, "batch_code": BATCH_CODE}])
    return FakeSupabaseRepository(tables=dict(tables))


def authorized_url(post_id: str = "123456789") -> str:
    return f"https://www.facebook.com/groups/{AUTHORIZED_GROUP_ID}/permalink/{post_id}/"


# ---------------------------------------------------------------------
# 1. Playwright must never be imported by this module
# ---------------------------------------------------------------------


def test_source_ingestion_module_never_imports_playwright():
    import ast
    from pathlib import Path

    tree = ast.parse(Path(si.__file__).read_text(encoding="utf-8"))
    imported_names = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert "playwright" not in imported_names


# ---------------------------------------------------------------------
# 2. Authorized group permalink accepted
# ---------------------------------------------------------------------


def test_authorized_group_permalink_is_normalized():
    canonical = si.normalize_facebook_permalink(authorized_url("987"))
    assert canonical == f"https://www.facebook.com/groups/{AUTHORIZED_GROUP_ID}/permalink/987/"


def test_authorized_permalink_with_trailing_query_string_normalizes_same():
    canonical = si.normalize_facebook_permalink(authorized_url("987") + "?comment_id=1")
    assert canonical == f"https://www.facebook.com/groups/{AUTHORIZED_GROUP_ID}/permalink/987/"


def test_bare_hostname_variants_all_accepted():
    for host in ("facebook.com", "www.facebook.com", "m.facebook.com"):
        url = f"https://{host}/groups/{AUTHORIZED_GROUP_ID}/permalink/42/"
        canonical = si.normalize_facebook_permalink(url)
        assert canonical == f"https://www.facebook.com/groups/{AUTHORIZED_GROUP_ID}/permalink/42/"


# ---------------------------------------------------------------------
# 3. Foreign group rejected
# ---------------------------------------------------------------------


def test_foreign_group_is_rejected():
    with pytest.raises(si.SourceValidationError, match="not the authorized group"):
        si.normalize_facebook_permalink(
            "https://www.facebook.com/groups/9999999999/permalink/1/"
        )


# ---------------------------------------------------------------------
# 4. Navigation / comment / profile / other-site URLs rejected
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://www.facebook.com/john.doe.123",  # profile
        "https://www.facebook.com/groups/2415122391976246/",  # group home, not a post
        f"https://www.facebook.com/groups/{AUTHORIZED_GROUP_ID}/permalink/123/?comment_id=456",  # comment anchor still resolves to the containing post -- accepted
        f"https://www.facebook.com/groups/{AUTHORIZED_GROUP_ID}/photo/?fbid=123",  # photo viewer, not a permalink
        f"https://www.facebook.com/groups/{AUTHORIZED_GROUP_ID}/user/999/",  # member profile within group
        "https://www.facebook.com/ads/library/?id=1",  # sponsored/ads navigation
        f"https://www.facebook.com/share/p/{AUTHORIZED_GROUP_ID}/",  # share link, not a group permalink
    ],
)
def test_non_post_navigation_urls_are_rejected_or_resolve_correctly(bad_url):
    if "comment_id" in bad_url:
        # A comment-anchor query string on an otherwise-valid permalink
        # still resolves to the containing post -- this is acceptable
        # (query strings are stripped by urlparse before path matching).
        canonical = si.normalize_facebook_permalink(bad_url)
        assert canonical.endswith("/permalink/123/")
        return

    with pytest.raises(si.SourceValidationError):
        si.normalize_facebook_permalink(bad_url)


def test_group_home_without_permalink_is_rejected():
    with pytest.raises(si.SourceValidationError, match="permalink form"):
        si.normalize_facebook_permalink(
            f"https://www.facebook.com/groups/{AUTHORIZED_GROUP_ID}/"
        )


def test_profile_url_is_rejected():
    with pytest.raises(si.SourceValidationError):
        si.normalize_facebook_permalink("https://www.facebook.com/some.person")


# ---------------------------------------------------------------------
# 5. Malformed / placeholder URLs rejected -- never guessed
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "",
        "   ",
        "<URL>",
        "not a url at all",
        "ftp://www.facebook.com/groups/2415122391976246/permalink/1/",
        f"https://www.facebook.com/groups/{AUTHORIZED_GROUP_ID}/permalink/abc/",  # non-numeric post id
        f"https://evil-facebook.com/groups/{AUTHORIZED_GROUP_ID}/permalink/1/",  # spoofed hostname
        f"https://www.facebook.com.evil.tld/groups/{AUTHORIZED_GROUP_ID}/permalink/1/",
    ],
)
def test_malformed_or_placeholder_urls_are_rejected(bad_url):
    with pytest.raises(si.SourceValidationError):
        si.normalize_facebook_permalink(bad_url)


def test_non_ascii_digit_post_id_is_rejected():
    """Regression: Python's \\d in a regex matches any Unicode decimal
    digit (full-width, Arabic-Indic, Devanagari, ...), not just ASCII.
    A non-ASCII "digit" post ID must never be accepted or embedded into
    the canonical URL this module writes to source_urls."""
    fullwidth_post_id = "１２３"  # full-width "123"

    with pytest.raises(si.SourceValidationError, match="permalink form"):
        si.normalize_facebook_permalink(
            f"https://www.facebook.com/groups/{AUTHORIZED_GROUP_ID}"
            f"/permalink/{fullwidth_post_id}/"
        )


def test_userinfo_hostname_confusion_is_rejected():
    """A URL like https://facebook.com@evil.example/... must resolve its
    real host (evil.example), never the userinfo component, when parsed."""
    with pytest.raises(si.SourceValidationError, match="not an authorized Facebook host"):
        si.normalize_facebook_permalink(
            f"https://facebook.com@evil.example/groups/{AUTHORIZED_GROUP_ID}"
            "/permalink/1/"
        )


def test_homograph_hostname_is_rejected():
    """A Cyrillic look-alike hostname must never string-equal the ASCII
    facebook.com literal this module checks against."""
    homograph_host = "рacebook.com"  # Cyrillic 'р' + "acebook.com"

    with pytest.raises(si.SourceValidationError, match="not an authorized Facebook host"):
        si.normalize_facebook_permalink(
            f"https://{homograph_host}/groups/{AUTHORIZED_GROUP_ID}/permalink/1/"
        )


# ---------------------------------------------------------------------
# 6. Duplicates removed / already-known source excluded
# ---------------------------------------------------------------------


def test_duplicate_url_in_same_batch_registers_once():
    repository = make_repository()

    outcomes = si.ingest_facebook_post_urls(
        repository,
        [authorized_url("1"), authorized_url("1")],
        BATCH_ID,
        max_sources=5,
    )

    assert outcomes[0].status == "REGISTERED"
    assert outcomes[1].status == "ALREADY_KNOWN"
    assert len(repository.client.tables["source_urls"]) == 1


def test_previously_registered_source_is_excluded_not_duplicated():
    repository = make_repository(
        source_urls=[
            {
                "source_url_id": "existing-1",
                "batch_id": BATCH_ID,
                "source_type": "FACEBOOK_POST",
                "source_url": authorized_url("777"),
                "crawl_status": "COLLECTED",
                "is_authorized": True,
            }
        ]
    )

    outcome = si.ingest_facebook_post_url(repository, authorized_url("777"), BATCH_ID)

    assert outcome.status == "ALREADY_KNOWN"
    assert outcome.source_url_id == "existing-1"
    # Crawl status of the existing row must never be reset back to
    # PENDING just because the same URL was ingested again.
    assert repository.client.tables["source_urls"][0]["crawl_status"] == "COLLECTED"
    assert len(repository.client.tables["source_urls"]) == 1


# ---------------------------------------------------------------------
# 7. Post-limit / bounded batch enforced -- no unlimited processing
# ---------------------------------------------------------------------


def test_max_sources_enforced_before_any_write():
    repository = make_repository()

    with pytest.raises(ValueError, match="exceeds --max-sources"):
        si.ingest_facebook_post_urls(
            repository,
            [authorized_url("1"), authorized_url("2"), authorized_url("3")],
            BATCH_ID,
            max_sources=2,
        )

    assert repository.client.tables["source_urls"] == []


# ---------------------------------------------------------------------
# 8. No DB write for an invalid URL
# ---------------------------------------------------------------------


def test_invalid_url_never_writes_to_source_urls():
    repository = make_repository()

    outcome = si.ingest_facebook_post_url(repository, "<URL>", BATCH_ID)

    assert outcome.status == "SOURCE_INVALID"
    assert repository.client.tables["source_urls"] == []


# ---------------------------------------------------------------------
# 9. Exact source registration only -- registered row matches the
# canonical URL, not the raw input, and carries expected provenance
# ---------------------------------------------------------------------


def test_registered_row_uses_canonical_url_and_authorized_defaults():
    repository = make_repository()

    outcome = si.ingest_facebook_post_url(
        repository, authorized_url("42") + "?ref=abc", BATCH_ID
    )

    assert outcome.status == "REGISTERED"
    row = repository.client.tables["source_urls"][0]
    assert row["source_url"] == authorized_url("42")
    assert row["source_type"] == "FACEBOOK_POST"
    assert row["is_authorized"] is True
    assert row["crawl_status"] == "PENDING"


# ---------------------------------------------------------------------
# 10. scripts/ingest_source_urls.py -- the CLI adapter invokes the same
# shared service and enforces the same bounds
# ---------------------------------------------------------------------


def test_cli_no_urls_fails_before_any_io():
    exit_code = ingest_source_urls.main(
        ["--batch-code", BATCH_CODE, "--max-sources", "5", "--non-interactive", "--confirm-register"]
    )
    assert exit_code == 2


def test_cli_url_count_exceeding_max_sources_fails_before_any_io():
    repository = make_repository()

    exit_code = ingest_source_urls.main(
        [
            "--url", authorized_url("1"),
            "--url", authorized_url("2"),
            "--batch-code", BATCH_CODE,
            "--max-sources", "1",
            "--non-interactive", "--confirm-register",
        ],
        repository=repository,
    )

    assert exit_code == 2
    assert repository.client.tables["source_urls"] == []


def test_cli_non_interactive_requires_confirm_register():
    exit_code = ingest_source_urls.main(
        [
            "--url", authorized_url("1"),
            "--batch-code", BATCH_CODE,
            "--max-sources", "5",
            "--non-interactive",
        ]
    )
    assert exit_code == 2


def test_cli_unknown_batch_code_fails():
    repository = make_repository()

    exit_code = ingest_source_urls.main(
        [
            "--url", authorized_url("1"),
            "--batch-code", "NO-SUCH-BATCH",
            "--max-sources", "5",
            "--non-interactive", "--confirm-register",
        ],
        repository=repository,
    )

    assert exit_code == 2


def test_cli_registers_valid_urls_and_reports_invalid_ones(capsys):
    repository = make_repository()

    exit_code = ingest_source_urls.main(
        [
            "--url", authorized_url("1"),
            "--url", "<URL>",
            "--batch-code", BATCH_CODE,
            "--max-sources", "5",
            "--non-interactive", "--confirm-register",
        ],
        repository=repository,
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "REGISTERED: 1" in output
    assert "SOURCE_INVALID: 1" in output
    assert len(repository.client.tables["source_urls"]) == 1

    process_logs = repository.client.tables["process_logs"]
    assert process_logs[-1]["log_level"] == "INFO"
    assert process_logs[-1]["status"] == "SUCCESS"


def test_cli_all_invalid_batch_logs_warning_not_silent_success():
    """Regression: a batch where every URL fails validation must not be
    logged identically to a normal successful run -- an operator
    skimming process_logs should be able to tell nothing was registered."""
    repository = make_repository()

    exit_code = ingest_source_urls.main(
        [
            "--url", "<URL>",
            "--batch-code", BATCH_CODE,
            "--max-sources", "5",
            "--non-interactive", "--confirm-register",
        ],
        repository=repository,
    )

    assert exit_code == 0
    assert repository.client.tables["source_urls"] == []

    process_logs = repository.client.tables["process_logs"]
    assert process_logs[-1]["log_level"] == "WARNING"
    assert process_logs[-1]["status"] == "ALL_INVALID"


def test_cli_reads_urls_from_input_file(tmp_path):
    repository = make_repository()
    input_file = tmp_path / "urls.txt"
    input_file.write_text(
        "# comment line, ignored\n"
        f"{authorized_url('10')}\n"
        "\n"
        f"{authorized_url('11')}\n",
        encoding="utf-8",
    )

    exit_code = ingest_source_urls.main(
        [
            "--input", str(input_file),
            "--batch-code", BATCH_CODE,
            "--max-sources", "5",
            "--non-interactive", "--confirm-register",
        ],
        repository=repository,
    )

    assert exit_code == 0
    assert len(repository.client.tables["source_urls"]) == 2


def test_cli_missing_input_file_fails_before_any_io():
    repository = make_repository()

    exit_code = ingest_source_urls.main(
        [
            "--input", "does-not-exist.txt",
            "--batch-code", BATCH_CODE,
            "--max-sources", "5",
            "--non-interactive", "--confirm-register",
        ],
        repository=repository,
    )

    assert exit_code == 2
    assert repository.client.tables["source_urls"] == []


# ---------------------------------------------------------------------
# 11. No arbitrary "newest pending" fallback -- resolve_url_list only
# ever returns exactly the URLs explicitly supplied
# ---------------------------------------------------------------------


def test_resolve_url_list_never_falls_back_to_pending_sources():
    import argparse

    args = argparse.Namespace(urls=[authorized_url("1")], input=None, max_sources=5)
    resolved = ingest_source_urls.resolve_url_list(args)
    assert resolved == [authorized_url("1")]


def test_resolve_url_list_dedupes_repeated_input_before_bounding():
    import argparse

    args = argparse.Namespace(
        urls=[authorized_url("1"), authorized_url("1")], input=None, max_sources=1
    )
    resolved = ingest_source_urls.resolve_url_list(args)
    assert resolved == [authorized_url("1")]


# ---------------------------------------------------------------------
# 12. No Woo write / publish / price mutation anywhere in this module
# ---------------------------------------------------------------------


def test_no_woocommerce_publish_or_price_code_paths_exist():
    import inspect

    source = inspect.getsource(si) + inspect.getsource(ingest_source_urls)
    lowered = source.lower()

    for forbidden in ("woocommerce", "publish", "regular_price", "sale_price"):
        assert forbidden not in lowered
