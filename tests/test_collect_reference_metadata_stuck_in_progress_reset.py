"""
Regression tests for scripts/collect_reference_metadata.py's
reset_stuck_in_progress_source().

crawl_status transitions PENDING -> IN_PROGRESS -> COLLECTED/FAILED, and
the COLLECTED/FAILED transition only ever happens inside main()'s own
try/except (see the crawl-body except and select_parser_or_mark_failed).
If the process itself is killed mid-fetch -- e.g. an operator's shell
timeout -- crawl_status is stranded at IN_PROGRESS forever: select_next_
reference_queue_item only matches crawl_status == "PENDING", so the row
is simply never selected again, and there is no approved-script path
back except raw SQL (forbidden by CLAUDE.md section 4.3 / 7).

reset_stuck_in_progress_source() is the bounded, "reconcile before
retry" fix (mirroring CLAUDE.md's WooCommerce recovery rule, section
2.6/18): it only resets a row that is (a) actually IN_PROGRESS and (b)
has no product_references row yet from that source for its candidate --
i.e. verifiably nothing was collected before the interruption -- back to
PENDING for a normal retry. It refuses in every other case.

Fully offline: FakeSupabaseRepository only, no live Supabase/network.
"""

from __future__ import annotations

import pytest

import collect_reference_metadata as crm
from support.fake_supabase import FakeSupabaseRepository


def make_repository(
    crawl_status: str = "IN_PROGRESS",
    with_reference: bool = False,
) -> FakeSupabaseRepository:
    tables = {
        "source_urls": [
            {
                "source_url_id": "src-1",
                "batch_id": "batch-1",
                "source_type": "FAHASA",
                "source_url": "https://www.fahasa.com/some-book.html",
                "source_name": "Fahasa",
                "is_authorized": True,
                "crawl_status": crawl_status,
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
        "product_references": [],
    }

    if with_reference:
        tables["product_references"].append(
            {
                "reference_id": "ref-1",
                "candidate_id": "cand-1",
                "source_url_id": "src-1",
            }
        )

    return FakeSupabaseRepository(tables=tables)


# ---------------------------------------------------------------------------
# 1. The case this exists for: IN_PROGRESS with nothing collected resets
#    cleanly to PENDING.
# ---------------------------------------------------------------------------


def test_resets_stuck_in_progress_with_no_collected_reference():
    repository = make_repository(crawl_status="IN_PROGRESS", with_reference=False)

    crm.reset_stuck_in_progress_source(
        repository=repository,
        source_url_id="src-1",
    )

    source = repository.client.tables["source_urls"][0]
    assert source["crawl_status"] == "PENDING"


# ---------------------------------------------------------------------------
# 2. Never resets a row that isn't actually IN_PROGRESS -- PENDING,
#    COLLECTED, and FAILED are all left alone.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("crawl_status", ["PENDING", "COLLECTED", "FAILED"])
def test_refuses_when_not_actually_in_progress(crawl_status):
    repository = make_repository(crawl_status=crawl_status, with_reference=False)

    with pytest.raises(RuntimeError, match="not IN_PROGRESS"):
        crm.reset_stuck_in_progress_source(
            repository=repository,
            source_url_id="src-1",
        )

    assert (
        repository.client.tables["source_urls"][0]["crawl_status"]
        == crawl_status
    )


# ---------------------------------------------------------------------------
# 3. Never resets when a product_references row already exists from this
#    source -- something WAS in fact collected, so this is a genuine
#    recovery/reconciliation call, not a bounded technical reset.
# ---------------------------------------------------------------------------


def test_refuses_when_a_reference_was_already_collected():
    repository = make_repository(crawl_status="IN_PROGRESS", with_reference=True)

    with pytest.raises(RuntimeError, match="product_references row already exists"):
        crm.reset_stuck_in_progress_source(
            repository=repository,
            source_url_id="src-1",
        )

    assert (
        repository.client.tables["source_urls"][0]["crawl_status"]
        == "IN_PROGRESS"
    )


# ---------------------------------------------------------------------------
# 4. Unknown source_url_id is rejected clearly.
# ---------------------------------------------------------------------------


def test_refuses_unknown_source_url_id():
    repository = make_repository()

    with pytest.raises(RuntimeError, match="No source_urls row found"):
        crm.reset_stuck_in_progress_source(
            repository=repository,
            source_url_id="does-not-exist",
        )


# ---------------------------------------------------------------------------
# 5. main()'s CLI wiring requires both --source-url-id and --confirm-reset.
# ---------------------------------------------------------------------------


def test_parse_arguments_reset_flags_default_to_false(monkeypatch):
    import sys

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "collect_reference_metadata.py",
            "--candidate-code",
            "FB-HIST-2026-AUTOIMPORT-CAN-0001",
            "--non-interactive",
        ],
    )

    args = crm.parse_arguments()

    assert args.reset_stuck_in_progress is False
    assert args.confirm_reset is False
