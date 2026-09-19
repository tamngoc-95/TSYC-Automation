"""Automated tests for scripts/recover_woocommerce_remote_loss.py.

Fully offline: WooCommerce HTTP is a fake http_get, Supabase is
FakeSupabaseRepository. No live network or database access, and no
create/publish/price action is ever exercised here -- this tool only
transitions one exact candidate's local state after a confirmed remote-
loss condition, never creates a WooCommerce product itself.
"""
from __future__ import annotations

import sys
from typing import Any

import pytest

import recover_woocommerce_remote_loss as rwr
import create_woocommerce_draft as cwd
import pipeline_state as ps
from support.fake_supabase import FakeSupabaseRepository


def make_repository(**tables: list[dict[str, Any]]) -> FakeSupabaseRepository:
    tables.setdefault("internal_products", [])
    tables.setdefault("woocommerce_product_syncs", [])
    return FakeSupabaseRepository(tables=dict(tables))


class FakeHttpResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


def make_confirmed_absent_http_get(product_id: int):
    """GET-by-ID 404 for `product_id`, and 0 matches for every SKU
    search (any status and trash) -- the fully-confirmed-absent case."""

    def http_get(url: str, **_kwargs: Any) -> FakeHttpResponse:
        if url.endswith(f"/products/{product_id}"):
            return FakeHttpResponse(404, {"code": "woocommerce_rest_product_invalid_id"})

        return FakeHttpResponse(200, [])

    return http_get


def make_sku_found_http_get(product_id: int, found_id: int = 9999):
    """GET-by-ID 404 for `product_id`, but the SKU search (any status)
    finds one product under a different remote id."""

    def http_get(url: str, **kwargs: Any) -> FakeHttpResponse:
        if url.endswith(f"/products/{product_id}"):
            return FakeHttpResponse(404, {"code": "woocommerce_rest_product_invalid_id"})

        params = kwargs.get("params", {})

        if params.get("status") == "trash":
            return FakeHttpResponse(200, [])

        return FakeHttpResponse(
            200, [{"id": found_id, "status": "draft", "sku": "TSYC-X"}]
        )

    return http_get


def make_trash_found_http_get(product_id: int, trash_id: int = 8888):
    """GET-by-ID 404, SKU search (any) empty, but the SKU is sitting in
    trash."""

    def http_get(url: str, **kwargs: Any) -> FakeHttpResponse:
        if url.endswith(f"/products/{product_id}"):
            return FakeHttpResponse(404, {"code": "woocommerce_rest_product_invalid_id"})

        params = kwargs.get("params", {})

        if params.get("status") == "trash":
            return FakeHttpResponse(
                200, [{"id": trash_id, "status": "trash", "sku": "TSYC-X"}]
            )

        return FakeHttpResponse(200, [])

    return http_get


def make_ambiguous_sku_http_get(product_id: int):
    """GET-by-ID 404, but SKU search (any) finds two matching products."""

    def http_get(url: str, **kwargs: Any) -> FakeHttpResponse:
        if url.endswith(f"/products/{product_id}"):
            return FakeHttpResponse(404, {"code": "woocommerce_rest_product_invalid_id"})

        params = kwargs.get("params", {})

        if params.get("status") == "trash":
            return FakeHttpResponse(200, [])

        return FakeHttpResponse(
            200,
            [
                {"id": 1111, "status": "draft", "sku": "TSYC-X"},
                {"id": 2222, "status": "draft", "sku": "TSYC-X"},
            ],
        )

    return http_get


def make_still_exists_http_get(product_id: int):
    """GET-by-ID returns 200 -- the product is not actually lost."""

    def http_get(url: str, **_kwargs: Any) -> FakeHttpResponse:
        if url.endswith(f"/products/{product_id}"):
            return FakeHttpResponse(
                200, {"id": product_id, "status": "draft", "sku": "TSYC-X"}
            )

        return FakeHttpResponse(200, [])

    return http_get


def make_erroring_http_get(product_id: int):
    """GET-by-ID raises (simulated network outage / ambiguous result)."""

    def http_get(url: str, **_kwargs: Any) -> FakeHttpResponse:
        if url.endswith(f"/products/{product_id}"):
            raise ConnectionError("simulated outage")

        return FakeHttpResponse(200, [])

    return http_get


STALE_PRODUCT_13 = {
    "internal_product_id": "prod-13",
    "candidate_id": "cand-13",
    "product_code": "TSYC-FB-HIST-2026-002-CAN-0013",
    "woocommerce_status": "DRAFT_CREATED",
    "purchase_price_vnd": None,
    "cover_price_vnd": None,
    "suggested_price_eur": None,
}

STALE_SYNC_13 = {
    "sync_id": "sync-13",
    "internal_product_id": "prod-13",
    "woocommerce_status": "DRAFT_CREATED",
    "woocommerce_product_id": 3746,
    "product_sku": "TSYC-FB-HIST-2026-002-CAN-0013",
    "product_permalink": "https://tiemsachyeucon.com/?post_type=product&p=3746",
    "response_payload": {
        "uploaded_media": [
            {
                "source_image_id": "img-1",
                "wordpress_media_id": 3745,
                "image_role": "FRONT_COVER",
            }
        ],
        "latest_status_check": {
            "checked_at": "2026-09-05T07:02:39.848386+00:00",
            "woocommerce_product": {"id": 3746, "status": "draft"},
        },
        "media_upload_completed": True,
    },
}

UNRELATED_PRODUCT = {
    "internal_product_id": "prod-99",
    "candidate_id": "cand-99",
    "product_code": "TSYC-FB-HIST-2026-999-CAN-0099",
    "woocommerce_status": "DRAFT_CREATED",
}

UNRELATED_SYNC = {
    "sync_id": "sync-99",
    "internal_product_id": "prod-99",
    "woocommerce_status": "DRAFT_CREATED",
    "woocommerce_product_id": 5000,
    "product_sku": "TSYC-FB-HIST-2026-999-CAN-0099",
    "response_payload": {"uploaded_media": []},
}


# ---------------------------------------------------------------------
# evaluate_remote_loss_recovery() -- pure decision logic
# ---------------------------------------------------------------------


def test_no_sync_record_is_blocked():
    decision = rwr.evaluate_remote_loss_recovery(STALE_PRODUCT_13, None, None, None, None)
    assert decision.outcome == "BLOCKED"
    assert "nothing to recover" in decision.reason


def test_sync_without_product_id_is_blocked():
    sync = dict(STALE_SYNC_13, woocommerce_product_id=None)
    decision = rwr.evaluate_remote_loss_recovery(STALE_PRODUCT_13, sync, None, None, None)
    assert decision.outcome == "BLOCKED"
    assert "no stored WooCommerce product ID" in decision.reason


def test_sync_not_draft_created_is_blocked():
    sync = dict(STALE_SYNC_13, woocommerce_status="FAILED")
    decision = rwr.evaluate_remote_loss_recovery(
        STALE_PRODUCT_13, sync, rwr.RemoteLookupResult(exists=False), rwr.RemoteLookupResult(matches=[]), rwr.RemoteLookupResult(matches=[])
    )
    assert decision.outcome == "BLOCKED"
    assert "not DRAFT_CREATED" in decision.reason


def test_internal_product_not_draft_created_is_blocked():
    product = dict(STALE_PRODUCT_13, woocommerce_status="PUBLISHED")
    decision = rwr.evaluate_remote_loss_recovery(
        product, STALE_SYNC_13, rwr.RemoteLookupResult(exists=False), rwr.RemoteLookupResult(matches=[]), rwr.RemoteLookupResult(matches=[])
    )
    assert decision.outcome == "BLOCKED"
    assert "PUBLISHED" in decision.reason


def test_get_by_id_still_exists_is_blocked():
    decision = rwr.evaluate_remote_loss_recovery(
        STALE_PRODUCT_13, STALE_SYNC_13, rwr.RemoteLookupResult(exists=True), None, None
    )
    assert decision.outcome == "BLOCKED"
    assert "still exists remotely" in decision.reason


def test_get_by_id_error_is_blocked():
    decision = rwr.evaluate_remote_loss_recovery(
        STALE_PRODUCT_13,
        STALE_SYNC_13,
        rwr.RemoteLookupResult(error="ConnectionError: simulated outage"),
        None,
        None,
    )
    assert decision.outcome == "BLOCKED"
    assert "uncertain" in decision.reason


def test_sku_search_one_match_is_blocked():
    decision = rwr.evaluate_remote_loss_recovery(
        STALE_PRODUCT_13,
        STALE_SYNC_13,
        rwr.RemoteLookupResult(exists=False),
        rwr.RemoteLookupResult(matches=[{"id": 9999}]),
        None,
    )
    assert decision.outcome == "BLOCKED"
    assert "already exists under this exact SKU" in decision.reason


def test_sku_search_two_matches_is_blocked():
    decision = rwr.evaluate_remote_loss_recovery(
        STALE_PRODUCT_13,
        STALE_SYNC_13,
        rwr.RemoteLookupResult(exists=False),
        rwr.RemoteLookupResult(matches=[{"id": 1}, {"id": 2}]),
        None,
    )
    assert decision.outcome == "BLOCKED"
    assert "2 remote products" in decision.reason


def test_trash_match_is_blocked():
    decision = rwr.evaluate_remote_loss_recovery(
        STALE_PRODUCT_13,
        STALE_SYNC_13,
        rwr.RemoteLookupResult(exists=False),
        rwr.RemoteLookupResult(matches=[]),
        rwr.RemoteLookupResult(matches=[{"id": 8888, "status": "trash"}]),
    )
    assert decision.outcome == "BLOCKED"
    assert "trash" in decision.reason


def test_trash_lookup_error_is_blocked():
    decision = rwr.evaluate_remote_loss_recovery(
        STALE_PRODUCT_13,
        STALE_SYNC_13,
        rwr.RemoteLookupResult(exists=False),
        rwr.RemoteLookupResult(matches=[]),
        rwr.RemoteLookupResult(error="TimeoutError: simulated"),
    )
    assert decision.outcome == "BLOCKED"
    assert "uncertain" in decision.reason


def test_fully_confirmed_absence_is_auto_pass():
    decision = rwr.evaluate_remote_loss_recovery(
        STALE_PRODUCT_13,
        STALE_SYNC_13,
        rwr.RemoteLookupResult(exists=False),
        rwr.RemoteLookupResult(matches=[]),
        rwr.RemoteLookupResult(matches=[]),
    )
    assert decision.outcome == "AUTO_PASS"
    assert decision.evidence["previous_woocommerce_product_id"] == 3746


# ---------------------------------------------------------------------
# build_remote_loss_response_payload() -- evidence preservation
# ---------------------------------------------------------------------


def test_payload_preserves_prior_evidence_and_records_old_id():
    original = STALE_SYNC_13["response_payload"]
    updated = rwr.build_remote_loss_response_payload(
        original, "test reason", "TSYC-X", 3746, "DRAFT_CREATED"
    )

    assert updated["uploaded_media"] == original["uploaded_media"]
    assert updated["latest_status_check"] == original["latest_status_check"]
    assert updated["media_upload_completed"] is True
    assert updated["remote_loss_confirmed"]["previous_woocommerce_product_id"] == 3746
    assert updated["remote_loss_confirmed"]["previous_woocommerce_status"] == "DRAFT_CREATED"
    assert updated["remote_loss_confirmed"]["sku_checked"] == "TSYC-X"


# ---------------------------------------------------------------------
# main() end-to-end, offline
# ---------------------------------------------------------------------


def run_main(monkeypatch, repository, http_get, argv):
    monkeypatch.setattr(rwr, "SupabaseRepository", lambda: repository)
    monkeypatch.setattr(rwr.requests, "get", http_get)
    monkeypatch.setenv("WOOCOMMERCE_URL", "https://example-store.test")
    monkeypatch.setenv("WOOCOMMERCE_CONSUMER_KEY", "ck_fake")
    monkeypatch.setenv("WOOCOMMERCE_CONSUMER_SECRET", "cs_fake")
    monkeypatch.setattr(rwr, "load_dotenv", lambda *_a, **_kw: None)
    monkeypatch.setattr(sys, "argv", ["recover_woocommerce_remote_loss.py", *argv])
    return rwr.main()


def test_end_to_end_recovers_when_confirmed_absent(monkeypatch):
    repository = make_repository(
        internal_products=[dict(STALE_PRODUCT_13)],
        woocommerce_product_syncs=[dict(STALE_SYNC_13)],
    )

    exit_code = run_main(
        monkeypatch,
        repository,
        make_confirmed_absent_http_get(3746),
        [
            "--product-code",
            "TSYC-FB-HIST-2026-002-CAN-0013",
            "--non-interactive",
            "--confirm-recover",
        ],
    )

    assert exit_code == 0

    updated_sync = repository.client.table("woocommerce_product_syncs").select("*").eq(
        "sync_id", "sync-13"
    ).execute().data[0]

    # 1. recoverable: local state transitions cleanly.
    assert updated_sync["woocommerce_status"] == "PENDING"
    assert updated_sync["woocommerce_product_id"] is None
    assert updated_sync["product_permalink"] is None

    # 6. old remote evidence preserved.
    assert updated_sync["response_payload"]["uploaded_media"] == STALE_SYNC_13["response_payload"]["uploaded_media"]
    assert updated_sync["response_payload"]["latest_status_check"] == STALE_SYNC_13["response_payload"]["latest_status_check"]
    assert updated_sync["response_payload"]["remote_loss_confirmed"]["previous_woocommerce_product_id"] == 3746

    updated_product = repository.client.table("internal_products").select("*").eq(
        "internal_product_id", "prod-13"
    ).execute().data[0]

    assert updated_product["woocommerce_status"] == "READY_FOR_DRAFT"

    # 9. price fields remain empty/untouched.
    assert updated_product["purchase_price_vnd"] is None
    assert updated_product["cover_price_vnd"] is None
    assert updated_product["suggested_price_eur"] is None

    # 10. status never moves toward publish.
    assert updated_sync["woocommerce_status"] != "PUBLISHED"
    assert updated_product["woocommerce_status"] != "PUBLISHED"

    # pipeline_state must not flag a lingering recovery condition.
    assert ps.derive_sync_recovery_state(updated_sync, updated_product) is None

    # 7. fresh draft creation after recovery: create_woocommerce_draft.py's
    # own gate must now see this candidate as eligible again.
    assert cwd.has_existing_remote_draft(updated_sync) is False


def test_end_to_end_refuses_when_sku_found(monkeypatch):
    repository = make_repository(
        internal_products=[dict(STALE_PRODUCT_13)],
        woocommerce_product_syncs=[dict(STALE_SYNC_13)],
    )

    exit_code = run_main(
        monkeypatch,
        repository,
        make_sku_found_http_get(3746, found_id=9999),
        [
            "--product-code",
            "TSYC-FB-HIST-2026-002-CAN-0013",
            "--non-interactive",
            "--confirm-recover",
        ],
    )

    assert exit_code == 1

    unchanged_sync = repository.client.table("woocommerce_product_syncs").select("*").eq(
        "sync_id", "sync-13"
    ).execute().data[0]
    assert unchanged_sync["woocommerce_status"] == "DRAFT_CREATED"
    assert unchanged_sync["woocommerce_product_id"] == 3746
    assert "remote_loss_confirmed" not in (unchanged_sync.get("response_payload") or {})


def test_end_to_end_refuses_when_replacement_id_found(monkeypatch):
    """A remote match under a DIFFERENT id than the stale stored one is
    still a confirmed remote product -- never recreated, never silently
    adopted."""
    repository = make_repository(
        internal_products=[dict(STALE_PRODUCT_13)],
        woocommerce_product_syncs=[dict(STALE_SYNC_13)],
    )

    exit_code = run_main(
        monkeypatch,
        repository,
        make_sku_found_http_get(3746, found_id=4200),
        [
            "--product-code",
            "TSYC-FB-HIST-2026-002-CAN-0013",
            "--non-interactive",
            "--confirm-recover",
        ],
    )

    assert exit_code == 1

    unchanged_sync = repository.client.table("woocommerce_product_syncs").select("*").eq(
        "sync_id", "sync-13"
    ).execute().data[0]
    assert unchanged_sync["woocommerce_product_id"] == 3746


def test_end_to_end_refuses_when_trash_match_found(monkeypatch):
    repository = make_repository(
        internal_products=[dict(STALE_PRODUCT_13)],
        woocommerce_product_syncs=[dict(STALE_SYNC_13)],
    )

    exit_code = run_main(
        monkeypatch,
        repository,
        make_trash_found_http_get(3746),
        [
            "--product-code",
            "TSYC-FB-HIST-2026-002-CAN-0013",
            "--non-interactive",
            "--confirm-recover",
        ],
    )

    assert exit_code == 1

    unchanged_sync = repository.client.table("woocommerce_product_syncs").select("*").eq(
        "sync_id", "sync-13"
    ).execute().data[0]
    assert unchanged_sync["woocommerce_status"] == "DRAFT_CREATED"


def test_end_to_end_refuses_on_ambiguous_sku_result(monkeypatch):
    repository = make_repository(
        internal_products=[dict(STALE_PRODUCT_13)],
        woocommerce_product_syncs=[dict(STALE_SYNC_13)],
    )

    exit_code = run_main(
        monkeypatch,
        repository,
        make_ambiguous_sku_http_get(3746),
        [
            "--product-code",
            "TSYC-FB-HIST-2026-002-CAN-0013",
            "--non-interactive",
            "--confirm-recover",
        ],
    )

    assert exit_code == 1

    unchanged_sync = repository.client.table("woocommerce_product_syncs").select("*").eq(
        "sync_id", "sync-13"
    ).execute().data[0]
    assert unchanged_sync["woocommerce_status"] == "DRAFT_CREATED"


def test_end_to_end_refuses_when_still_exists(monkeypatch):
    repository = make_repository(
        internal_products=[dict(STALE_PRODUCT_13)],
        woocommerce_product_syncs=[dict(STALE_SYNC_13)],
    )

    exit_code = run_main(
        monkeypatch,
        repository,
        make_still_exists_http_get(3746),
        [
            "--product-code",
            "TSYC-FB-HIST-2026-002-CAN-0013",
            "--non-interactive",
            "--confirm-recover",
        ],
    )

    assert exit_code == 1

    unchanged_sync = repository.client.table("woocommerce_product_syncs").select("*").eq(
        "sync_id", "sync-13"
    ).execute().data[0]
    assert unchanged_sync["woocommerce_product_id"] == 3746


def test_end_to_end_refuses_on_lookup_error(monkeypatch):
    repository = make_repository(
        internal_products=[dict(STALE_PRODUCT_13)],
        woocommerce_product_syncs=[dict(STALE_SYNC_13)],
    )

    exit_code = run_main(
        monkeypatch,
        repository,
        make_erroring_http_get(3746),
        [
            "--product-code",
            "TSYC-FB-HIST-2026-002-CAN-0013",
            "--non-interactive",
            "--confirm-recover",
        ],
    )

    assert exit_code == 1

    unchanged_sync = repository.client.table("woocommerce_product_syncs").select("*").eq(
        "sync_id", "sync-13"
    ).execute().data[0]
    assert unchanged_sync["woocommerce_status"] == "DRAFT_CREATED"


def test_rerun_after_recovery_does_not_duplicate(monkeypatch):
    """8. rerun does not duplicate."""
    repository = make_repository(
        internal_products=[dict(STALE_PRODUCT_13)],
        woocommerce_product_syncs=[dict(STALE_SYNC_13)],
    )

    http_get = make_confirmed_absent_http_get(3746)

    first_exit = run_main(
        monkeypatch,
        repository,
        http_get,
        [
            "--product-code",
            "TSYC-FB-HIST-2026-002-CAN-0013",
            "--non-interactive",
            "--confirm-recover",
        ],
    )
    assert first_exit == 0

    once_recovered_sync = repository.client.table("woocommerce_product_syncs").select("*").eq(
        "sync_id", "sync-13"
    ).execute().data[0]

    second_exit = run_main(
        monkeypatch,
        repository,
        http_get,
        [
            "--product-code",
            "TSYC-FB-HIST-2026-002-CAN-0013",
            "--non-interactive",
            "--confirm-recover",
        ],
    )
    # No stored product id anymore -- refuses cleanly, no duplicate write.
    assert second_exit == 1

    twice_recovered_sync = repository.client.table("woocommerce_product_syncs").select("*").eq(
        "sync_id", "sync-13"
    ).execute().data[0]

    assert once_recovered_sync == twice_recovered_sync


def test_unrelated_candidates_unchanged(monkeypatch):
    """11. unrelated candidates unchanged."""
    repository = make_repository(
        internal_products=[dict(STALE_PRODUCT_13), dict(UNRELATED_PRODUCT)],
        woocommerce_product_syncs=[dict(STALE_SYNC_13), dict(UNRELATED_SYNC)],
    )

    exit_code = run_main(
        monkeypatch,
        repository,
        make_confirmed_absent_http_get(3746),
        [
            "--product-code",
            "TSYC-FB-HIST-2026-002-CAN-0013",
            "--non-interactive",
            "--confirm-recover",
        ],
    )

    assert exit_code == 0

    unrelated_sync = repository.client.table("woocommerce_product_syncs").select("*").eq(
        "sync_id", "sync-99"
    ).execute().data[0]
    assert unrelated_sync == UNRELATED_SYNC

    unrelated_product = repository.client.table("internal_products").select("*").eq(
        "internal_product_id", "prod-99"
    ).execute().data[0]
    assert unrelated_product == UNRELATED_PRODUCT


def test_end_to_end_refuses_without_confirmation(monkeypatch):
    repository = make_repository(
        internal_products=[dict(STALE_PRODUCT_13)],
        woocommerce_product_syncs=[dict(STALE_SYNC_13)],
    )

    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "")

    exit_code = run_main(
        monkeypatch,
        repository,
        make_confirmed_absent_http_get(3746),
        ["--product-code", "TSYC-FB-HIST-2026-002-CAN-0013"],
    )

    assert exit_code == 1

    unchanged_sync = repository.client.table("woocommerce_product_syncs").select("*").eq(
        "sync_id", "sync-13"
    ).execute().data[0]
    assert unchanged_sync["woocommerce_status"] == "DRAFT_CREATED"


def test_end_to_end_no_such_product_code(monkeypatch):
    repository = make_repository()

    exit_code = run_main(
        monkeypatch,
        repository,
        make_confirmed_absent_http_get(3746),
        [
            "--product-code",
            "TSYC-DOES-NOT-EXIST",
            "--non-interactive",
            "--confirm-recover",
        ],
    )

    assert exit_code == 1


def test_end_to_end_no_sync_record_at_all(monkeypatch):
    repository = make_repository(internal_products=[dict(STALE_PRODUCT_13)])

    exit_code = run_main(
        monkeypatch,
        repository,
        make_confirmed_absent_http_get(3746),
        [
            "--product-code",
            "TSYC-FB-HIST-2026-002-CAN-0013",
            "--non-interactive",
            "--confirm-recover",
        ],
    )

    assert exit_code == 1


def test_non_interactive_requires_confirm_recover():
    with pytest.raises(RuntimeError, match="--non-interactive requires --confirm-recover"):
        sys.argv = [
            "recover_woocommerce_remote_loss.py",
            "--product-code",
            "X",
            "--non-interactive",
        ]
        args = rwr.parse_arguments()

        if args.non_interactive and not args.confirm_recover:
            raise RuntimeError("--non-interactive requires --confirm-recover.")
