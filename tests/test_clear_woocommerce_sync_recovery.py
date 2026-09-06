"""Automated tests for scripts/clear_woocommerce_sync_recovery.py.

Fully offline: WooCommerce HTTP is a fake http_get, Supabase is
FakeSupabaseRepository. No live network or database access, and no
retry/create/publish/price action is ever exercised here -- this tool
only clears a recovery signal on one exact sync row.
"""
from __future__ import annotations

import sys
from typing import Any

import pytest

import clear_woocommerce_sync_recovery as cwsr
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


def ok_empty_http_get(_url: str, **_kwargs: Any) -> FakeHttpResponse:
    return FakeHttpResponse(200, [])


def ok_one_match_http_get(_url: str, **_kwargs: Any) -> FakeHttpResponse:
    return FakeHttpResponse(200, [{"id": 9999, "status": "draft", "sku": "TSYC-X"}])


def ok_two_matches_http_get(_url: str, **_kwargs: Any) -> FakeHttpResponse:
    return FakeHttpResponse(
        200,
        [
            {"id": 1111, "status": "draft", "sku": "TSYC-X"},
            {"id": 2222, "status": "draft", "sku": "TSYC-X"},
        ],
    )


def failing_http_get(_url: str, **_kwargs: Any) -> FakeHttpResponse:
    raise ConnectionError("simulated outage")


def non_200_http_get(_url: str, **_kwargs: Any) -> FakeHttpResponse:
    return FakeHttpResponse(500, {"code": "server_error"})


FAILED_SYNC_PRODUCT_16 = {
    "internal_product_id": "prod-16",
    "candidate_id": "cand-16",
    "product_code": "TSYC-FB-HIST-2026-AUTOIMPORT-CAN-0016",
    "woocommerce_status": "READY_FOR_DRAFT",
}

FAILED_SYNC_16 = {
    "sync_id": "sync-16",
    "internal_product_id": "prod-16",
    "woocommerce_status": "FAILED",
    "woocommerce_product_id": None,
    "response_payload": {
        "error": {
            "first_bytes": "ffd8ffe000104a46494600010100000100010000",
            "content_type": "image/webp",
        },
        "failed_at": "2026-09-05T07:25:42.952270+00:00",
        "uploaded_media": [],
        "media_upload_completed": False,
    },
}


# ---------------------------------------------------------------------
# evaluate_recovery_clear() -- pure decision logic
# ---------------------------------------------------------------------


def test_no_sync_record_is_blocked():
    decision = cwsr.evaluate_recovery_clear(None, [], None)
    assert decision.outcome == "BLOCKED"
    assert "nothing to clear" in decision.reason


def test_sync_with_remote_id_is_blocked():
    sync = dict(FAILED_SYNC_16, woocommerce_product_id=1234)
    decision = cwsr.evaluate_recovery_clear(sync, [], None)
    assert decision.outcome == "BLOCKED"
    assert "already has a WooCommerce product ID" in decision.reason


def test_sync_not_failed_is_blocked():
    sync = dict(FAILED_SYNC_16, woocommerce_status="DRAFT_CREATED")
    decision = cwsr.evaluate_recovery_clear(sync, [], None)
    assert decision.outcome == "BLOCKED"
    assert "not FAILED" in decision.reason


def test_lookup_error_is_blocked():
    decision = cwsr.evaluate_recovery_clear(
        FAILED_SYNC_16, None, "ConnectionError: simulated outage"
    )
    assert decision.outcome == "BLOCKED"
    assert "uncertain" in decision.reason


def test_two_remote_matches_is_blocked():
    decision = cwsr.evaluate_recovery_clear(
        FAILED_SYNC_16,
        [{"id": 1}, {"id": 2}],
        None,
    )
    assert decision.outcome == "BLOCKED"
    assert "2 remote products" in decision.reason


def test_one_remote_match_is_blocked():
    decision = cwsr.evaluate_recovery_clear(
        FAILED_SYNC_16,
        [{"id": 9999}],
        None,
    )
    assert decision.outcome == "BLOCKED"
    assert "already exists" in decision.reason


def test_confirmed_absence_is_auto_pass():
    decision = cwsr.evaluate_recovery_clear(FAILED_SYNC_16, [], None)
    assert decision.outcome == "AUTO_PASS"


# ---------------------------------------------------------------------
# build_cleared_response_payload() -- history preservation
# ---------------------------------------------------------------------


def test_cleared_payload_preserves_original_failure_evidence():
    original = FAILED_SYNC_16["response_payload"]
    updated = cwsr.build_cleared_response_payload(original, "test reason", "TSYC-X")

    assert updated["error"] == original["error"]
    assert updated["failed_at"] == original["failed_at"]
    assert updated["uploaded_media"] == []
    assert updated["media_upload_completed"] is False
    assert updated["recovery_cleared"]["reason"] == "test reason"
    assert updated["recovery_cleared"]["sku_checked"] == "TSYC-X"


# ---------------------------------------------------------------------
# main() end-to-end, offline
# ---------------------------------------------------------------------


def run_main(monkeypatch, repository, http_get, argv):
    monkeypatch.setattr(cwsr, "SupabaseRepository", lambda: repository)
    monkeypatch.setattr(cwsr.requests, "get", http_get)
    monkeypatch.setenv("WOOCOMMERCE_URL", "https://example-store.test")
    monkeypatch.setenv("WOOCOMMERCE_CONSUMER_KEY", "ck_fake")
    monkeypatch.setenv("WOOCOMMERCE_CONSUMER_SECRET", "cs_fake")
    monkeypatch.setattr(cwsr, "load_dotenv", lambda *_a, **_kw: None)
    monkeypatch.setattr(sys, "argv", ["clear_woocommerce_sync_recovery.py", *argv])
    return cwsr.main()


def test_end_to_end_clears_can_0016_when_remote_absent(monkeypatch):
    repository = make_repository(
        internal_products=[dict(FAILED_SYNC_PRODUCT_16)],
        woocommerce_product_syncs=[dict(FAILED_SYNC_16)],
    )

    exit_code = run_main(
        monkeypatch,
        repository,
        ok_empty_http_get,
        [
            "--product-code",
            "TSYC-FB-HIST-2026-AUTOIMPORT-CAN-0016",
            "--non-interactive",
            "--confirm-clear",
        ],
    )

    assert exit_code == 0

    updated_sync = repository.client.table("woocommerce_product_syncs").select("*").eq(
        "sync_id", "sync-16"
    ).execute().data[0]

    assert updated_sync["woocommerce_status"] == "PENDING"
    assert updated_sync["error_code"] is None
    assert updated_sync["error_message"] is None
    assert updated_sync["response_payload"]["error"] == FAILED_SYNC_16["response_payload"]["error"]
    assert "recovery_cleared" in updated_sync["response_payload"]

    # internal_products row must be completely untouched.
    product = repository.client.table("internal_products").select("*").eq(
        "internal_product_id", "prod-16"
    ).execute().data[0]
    assert product["woocommerce_status"] == "READY_FOR_DRAFT"

    # pipeline_state must no longer report a recovery condition.
    assert ps.derive_sync_recovery_state(updated_sync, product) is None


def test_end_to_end_refuses_when_remote_product_exists(monkeypatch):
    repository = make_repository(
        internal_products=[dict(FAILED_SYNC_PRODUCT_16)],
        woocommerce_product_syncs=[dict(FAILED_SYNC_16)],
    )

    exit_code = run_main(
        monkeypatch,
        repository,
        ok_one_match_http_get,
        [
            "--product-code",
            "TSYC-FB-HIST-2026-AUTOIMPORT-CAN-0016",
            "--non-interactive",
            "--confirm-clear",
        ],
    )

    assert exit_code == 1

    unchanged_sync = repository.client.table("woocommerce_product_syncs").select("*").eq(
        "sync_id", "sync-16"
    ).execute().data[0]
    assert unchanged_sync["woocommerce_status"] == "FAILED"
    assert "recovery_cleared" not in (unchanged_sync.get("response_payload") or {})


def test_end_to_end_refuses_on_duplicate_remote_matches(monkeypatch):
    repository = make_repository(
        internal_products=[dict(FAILED_SYNC_PRODUCT_16)],
        woocommerce_product_syncs=[dict(FAILED_SYNC_16)],
    )

    exit_code = run_main(
        monkeypatch,
        repository,
        ok_two_matches_http_get,
        [
            "--product-code",
            "TSYC-FB-HIST-2026-AUTOIMPORT-CAN-0016",
            "--non-interactive",
            "--confirm-clear",
        ],
    )

    assert exit_code == 1

    unchanged_sync = repository.client.table("woocommerce_product_syncs").select("*").eq(
        "sync_id", "sync-16"
    ).execute().data[0]
    assert unchanged_sync["woocommerce_status"] == "FAILED"


def test_end_to_end_refuses_on_lookup_error(monkeypatch):
    repository = make_repository(
        internal_products=[dict(FAILED_SYNC_PRODUCT_16)],
        woocommerce_product_syncs=[dict(FAILED_SYNC_16)],
    )

    exit_code = run_main(
        monkeypatch,
        repository,
        failing_http_get,
        [
            "--product-code",
            "TSYC-FB-HIST-2026-AUTOIMPORT-CAN-0016",
            "--non-interactive",
            "--confirm-clear",
        ],
    )

    assert exit_code == 1

    unchanged_sync = repository.client.table("woocommerce_product_syncs").select("*").eq(
        "sync_id", "sync-16"
    ).execute().data[0]
    assert unchanged_sync["woocommerce_status"] == "FAILED"


def test_end_to_end_refuses_on_non_200_lookup(monkeypatch):
    repository = make_repository(
        internal_products=[dict(FAILED_SYNC_PRODUCT_16)],
        woocommerce_product_syncs=[dict(FAILED_SYNC_16)],
    )

    exit_code = run_main(
        monkeypatch,
        repository,
        non_200_http_get,
        [
            "--product-code",
            "TSYC-FB-HIST-2026-AUTOIMPORT-CAN-0016",
            "--non-interactive",
            "--confirm-clear",
        ],
    )

    assert exit_code == 1


def test_end_to_end_refuses_when_sync_already_has_remote_id(monkeypatch):
    repository = make_repository(
        internal_products=[dict(FAILED_SYNC_PRODUCT_16)],
        woocommerce_product_syncs=[dict(FAILED_SYNC_16, woocommerce_product_id=5555)],
    )

    exit_code = run_main(
        monkeypatch,
        repository,
        ok_empty_http_get,
        [
            "--product-code",
            "TSYC-FB-HIST-2026-AUTOIMPORT-CAN-0016",
            "--non-interactive",
            "--confirm-clear",
        ],
    )

    assert exit_code == 1

    unchanged_sync = repository.client.table("woocommerce_product_syncs").select("*").eq(
        "sync_id", "sync-16"
    ).execute().data[0]
    assert unchanged_sync["woocommerce_status"] == "FAILED"


def test_end_to_end_refuses_without_confirmation(monkeypatch):
    repository = make_repository(
        internal_products=[dict(FAILED_SYNC_PRODUCT_16)],
        woocommerce_product_syncs=[dict(FAILED_SYNC_16)],
    )

    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "")

    exit_code = run_main(
        monkeypatch,
        repository,
        ok_empty_http_get,
        ["--product-code", "TSYC-FB-HIST-2026-AUTOIMPORT-CAN-0016"],
    )

    assert exit_code == 1

    unchanged_sync = repository.client.table("woocommerce_product_syncs").select("*").eq(
        "sync_id", "sync-16"
    ).execute().data[0]
    assert unchanged_sync["woocommerce_status"] == "FAILED"


def test_end_to_end_no_such_product_code(monkeypatch):
    repository = make_repository()

    exit_code = run_main(
        monkeypatch,
        repository,
        ok_empty_http_get,
        [
            "--product-code",
            "TSYC-DOES-NOT-EXIST",
            "--non-interactive",
            "--confirm-clear",
        ],
    )

    assert exit_code == 1


def test_end_to_end_no_sync_record_at_all(monkeypatch):
    repository = make_repository(internal_products=[dict(FAILED_SYNC_PRODUCT_16)])

    exit_code = run_main(
        monkeypatch,
        repository,
        ok_empty_http_get,
        [
            "--product-code",
            "TSYC-FB-HIST-2026-AUTOIMPORT-CAN-0016",
            "--non-interactive",
            "--confirm-clear",
        ],
    )

    assert exit_code == 1


def test_non_interactive_requires_confirm_clear():
    import sys

    with pytest.raises(RuntimeError, match="--non-interactive requires --confirm-clear"):
        sys.argv = [
            "clear_woocommerce_sync_recovery.py",
            "--product-code",
            "X",
            "--non-interactive",
        ]
        args = cwsr.parse_arguments()

        if args.non_interactive and not args.confirm_clear:
            raise RuntimeError("--non-interactive requires --confirm-clear.")
