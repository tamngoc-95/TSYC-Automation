"""
Regression tests for scripts/register_reference_source.py's
--confirm-reauthorize correction path.

get_or_create_source_url is intentionally append-only: reusing an
existing (batch, source_type, source_url) triple never updates any
field on the existing row (see test_reference_source_idempotency_is_
unchanged_for_a_historical_batch in test_register_reference_source.py).
That is correct for protecting already-crawled/established provenance,
but it also means a source registered without --authorized by mistake
had no approved-script path back to True -- short of raw SQL, which
CLAUDE.md section 4.3 / 7 forbids.

reauthorize_existing_source_url() is the bounded, single-field fix:
upgrade is_authorized False -> True on an exact already-identified
source_urls row, only when explicitly requested, never the reverse.

Fully offline: FakeSupabaseRepository only, no live Supabase/network.
"""

from __future__ import annotations

import pytest

import register_reference_source as rrs
from support.fake_supabase import FakeSupabaseRepository


def make_source_url(
    source_url_id: str,
    is_authorized: bool,
    source_url: str = "https://www.fahasa.com/example-book.html",
) -> dict:
    return {
        "source_url_id": source_url_id,
        "batch_id": "batch-hist-autoimport",
        "source_type": "FAHASA",
        "source_url": source_url,
        "source_name": "Fahasa",
        "is_authorized": is_authorized,
        "crawl_status": "PENDING",
    }


def make_repository(is_authorized: bool) -> FakeSupabaseRepository:
    return FakeSupabaseRepository(
        tables={
            "source_urls": [
                make_source_url("src-1", is_authorized),
            ],
        }
    )


# ---------------------------------------------------------------------------
# 1. The mistaken-registration case this exists for: an unauthorized row
#    is upgraded to True, and nothing else on the row changes.
# ---------------------------------------------------------------------------


def test_upgrades_unauthorized_row_to_authorized():
    repository = make_repository(is_authorized=False)
    source_record = repository.client.tables["source_urls"][0]

    reauthorized = rrs.reauthorize_existing_source_url(
        repository=repository,
        source_record=source_record,
        requested_is_authorized=True,
    )

    assert reauthorized is True

    updated = repository.client.tables["source_urls"][0]
    assert updated["is_authorized"] is True
    assert updated["source_url_id"] == "src-1"
    assert updated["source_type"] == "FAHASA"
    assert updated["source_url"] == "https://www.fahasa.com/example-book.html"
    assert updated["crawl_status"] == "PENDING"


# ---------------------------------------------------------------------------
# 2. Already-authorized rows are a safe no-op -- no write, no error.
# ---------------------------------------------------------------------------


def test_already_authorized_row_is_a_no_op():
    repository = make_repository(is_authorized=True)
    source_record = repository.client.tables["source_urls"][0]

    reauthorized = rrs.reauthorize_existing_source_url(
        repository=repository,
        source_record=source_record,
        requested_is_authorized=True,
    )

    assert reauthorized is False
    assert repository.client.tables["source_urls"][0]["is_authorized"] is True


# ---------------------------------------------------------------------------
# 3. Never downgrades, and never silently no-ops when the caller forgot
#    --authorized on the reauthorize invocation -- it fails loudly instead.
# ---------------------------------------------------------------------------


def test_refuses_without_authorized_flag_on_this_invocation():
    repository = make_repository(is_authorized=False)
    source_record = repository.client.tables["source_urls"][0]

    with pytest.raises(RuntimeError, match="requires --authorized"):
        rrs.reauthorize_existing_source_url(
            repository=repository,
            source_record=source_record,
            requested_is_authorized=False,
        )

    # Unrelated to the raised error: confirm the row was left untouched.
    assert repository.client.tables["source_urls"][0]["is_authorized"] is False


# ---------------------------------------------------------------------------
# 4. main()'s wiring: --confirm-reauthorize is opt-in. Without it, a
#    REUSED unauthorized source_urls row is left exactly as before --
#    fully backward compatible with existing (pre-flag) behavior.
# ---------------------------------------------------------------------------


def test_parse_arguments_confirm_reauthorize_defaults_to_false(monkeypatch):
    import sys

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "register_reference_source.py",
            "--candidate-code",
            "FB-HIST-2026-AUTOIMPORT-CAN-0001",
            "--source-type",
            "FAHASA",
            "--source-url",
            "https://www.fahasa.com/example-book.html",
            "--confirm-register",
            "--non-interactive",
        ],
    )

    args = rrs.parse_arguments()

    assert args.confirm_reauthorize is False
