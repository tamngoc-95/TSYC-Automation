"""
Regression test for scripts/collect_reference_metadata.py's
select_parser_or_mark_failed().

Before this change, select_parser() ran before crawl_status was ever
set to IN_PROGRESS, and its ValueError for an unsupported domain
propagated straight out of main() with no update at all -- the
source_urls row stayed at PENDING forever, indistinguishable from one
nothing had tried yet. run_batch.py's derived state (REFERENCE_
REGISTERED, keyed off is_selected_for_crawl, not crawl_status) kept
re-selecting the same dead source_url on every future pass, so a
candidate whose only registered source was on an unsupported domain
could never progress even after a second, supported-domain source was
registered for it -- select_next_reference_queue_item's own PENDING
filter is what lets it skip past a FAILED row to a later PENDING one,
and a row stuck at PENDING is never skipped.

select_parser_or_mark_failed() closes that gap: a no-parser failure is
recorded as crawl_status=FAILED (with the exact error) and discovery_
status=FAILED on the exact source_url_id/discovery_id involved, mirroring
exactly what the crawl-body except in main() already does for every
other kind of collection failure, then re-raises so main()'s own exit
code/reporting is unchanged.

Fully offline: FakeSupabaseRepository only, no live Supabase/network/
Playwright.
"""

from __future__ import annotations

import pytest

import collect_reference_metadata as crm
from support.fake_supabase import FakeSupabaseRepository


def make_repository() -> FakeSupabaseRepository:
    return FakeSupabaseRepository(
        tables={
            "source_urls": [
                {
                    "source_url_id": "src-1",
                    "batch_id": "batch-1",
                    "source_type": "BOOKSTORE",
                    "source_url": "https://example-unsupported-domain.vn/book",
                    "source_name": "Example Bookstore",
                    "is_authorized": True,
                    "crawl_status": "PENDING",
                    "last_error": None,
                },
            ],
            "candidate_reference_sources": [
                {
                    "discovery_id": "disc-1",
                    "candidate_id": "cand-1",
                    "source_url_id": "src-1",
                    "discovery_status": "SELECTED",
                    "is_selected_for_crawl": True,
                },
            ],
        }
    )


def test_unsupported_domain_marks_source_failed_instead_of_leaving_it_pending():
    repository = make_repository()

    with pytest.raises(RuntimeError, match="No metadata parser"):
        crm.select_parser_or_mark_failed(
            repository=repository,
            source_url="https://example-unsupported-domain.vn/book",
            source_url_id="src-1",
            discovery_id="disc-1",
        )

    source = repository.client.tables["source_urls"][0]
    assert source["crawl_status"] == "FAILED"
    assert "No metadata parser" in source["last_error"]

    discovery = repository.client.tables["candidate_reference_sources"][0]
    assert discovery["discovery_status"] == "FAILED"


def test_supported_domain_returns_parser_name_without_any_write():
    repository = make_repository()
    # Point at a supported domain -- no failure, so no status update at all.
    source_url = "https://www.fahasa.com/some-book.html"

    parser_name = crm.select_parser_or_mark_failed(
        repository=repository,
        source_url=source_url,
        source_url_id="src-1",
        discovery_id="disc-1",
    )

    assert parser_name == "FAHASA_METADATA_PARSER"

    # Untouched: this helper only writes on failure.
    source = repository.client.tables["source_urls"][0]
    assert source["crawl_status"] == "PENDING"
    assert source["last_error"] is None

    discovery = repository.client.tables["candidate_reference_sources"][0]
    assert discovery["discovery_status"] == "SELECTED"
