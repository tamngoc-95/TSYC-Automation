"""Offline tests for the RECORDED -> REMOTE_REMOVED mapping transition
(src.domain.language_product_mapping.plan_removal_transition and
scripts/reconcile_language_mapping_status.py).

Incident (2026-11-07): after the local Woo reconciliation recorded the
owner's trash of #4928/#4930 on the sync rows, the mapping rows still said
RECORDED and the mapping audit raised STORED_DIFFERS_FROM_SYNC.
"""
from __future__ import annotations

import json

import pytest

import reconcile_language_mapping_status as rms
from src.domain import language_product_mapping as lpm
from support.fake_supabase import FakeSupabaseRepository

CODE = "TSYC-FAHASA-2026-002-CAN-0013"
IPID = "22222222-2222-4222-8222-222222222222"
WOO = 4928
LPID = "lp-4928"


def meta(values):
    return [{"key": k, "value": v} for k, v in values.items()]


def sync_row(*, removed=True, marker_id=WOO, recovery=False):
    response = {"woocommerce_product": {"id": WOO, "sku": CODE, "status": "draft",
                                        "date_modified_gmt": "2026-10-19T08:00:00", "meta_data": []}}
    if removed:
        response["remote_removal"] = {"state": "REMOTE_TRASHED", "woocommerce_product_id": marker_id,
                                      "sku": CODE, "recreate": False}
    if recovery:
        response["recovery_required"] = True
    return {"sync_id": "sync-4928", "internal_product_id": IPID, "woocommerce_product_id": WOO,
            "woocommerce_status": "FAILED" if removed else "DRAFT_CREATED", "product_sku": CODE,
            "request_payload": {"sku": CODE, "status": "draft", "meta_data": meta({"_tsyc_internal_product_id": IPID})},
            "response_payload": response}


def mapping_row(status=lpm.RECORDED, *, language="vi", lpid=LPID, woo=WOO, sync_id="sync-4928", updated="t0"):
    return {"language_product_id": lpid, "internal_product_id": IPID, "language_code": language,
            "woocommerce_product_id": woo, "origin": lpm.ORIGIN_TSYC_CREATED, "mapping_status": status,
            "sync_id": sync_id, "product_content_id": None, "request_id": None if language == "vi" else "req-1",
            "baseline_source": None, "baseline_modified_gmt": None, "baseline_text": None,
            "link_mechanism": None, "link_verified_at": None, "link_evidence": {}, "updated_at": updated}


def repository(*, sync=None, rows=None):
    return FakeSupabaseRepository({
        "internal_products": [{"internal_product_id": IPID, "product_code": CODE}],
        "woocommerce_product_syncs": [sync or sync_row()],
        lpm.TABLE: rows if rows is not None else [mapping_row()],
    })


def ctx(repo, tmp_path, *, catalog=None, confirm=True, name="run"):
    return rms.Context(repository=repo, catalog=catalog or {}, run_dir=tmp_path / name, confirm=confirm,
                       log=repo.write_process_log)


def decision(summary):
    return summary["decisions"][0]


def stored(repo):
    return repo.client.tables[lpm.TABLE]


# --- write path ------------------------------------------------------------------


def test_confirmed_removal_moves_the_row_verifies_checkpoints_and_logs(tmp_path):
    repo = repository()
    summary = rms.run(ctx(repo, tmp_path), [CODE], max_products=2)
    assert decision(summary)["outcome"] == lpm.TRANSITION_TO_REMOVED
    [row] = stored(repo)
    assert row["mapping_status"] == lpm.REMOTE_REMOVED
    assert (row["woocommerce_product_id"], row["sync_id"], row["origin"]) == (WOO, "sync-4928", lpm.ORIGIN_TSYC_CREATED)
    checkpoint = json.loads((tmp_path / "run" / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["status"] == "COMPLETED"
    assert checkpoint["products"][CODE]["result"] == rms.UPDATED
    assert checkpoint["products"][CODE]["before"]["mapping_status"] == lpm.RECORDED
    [log] = repo.client.tables["process_logs"]
    assert log["process_name"] == rms.PROCESS_NAME and log["status"] == "UPDATED"


def test_rerun_is_idempotent(tmp_path):
    repo = repository()
    rms.run(ctx(repo, tmp_path, name="a"), [CODE], max_products=2)
    snapshot = [dict(r) for r in stored(repo)]
    summary = rms.run(ctx(repo, tmp_path, name="b"), [CODE], max_products=2)
    assert decision(summary)["outcome"] == lpm.ALREADY_REMOVED
    assert "checkpoint" not in summary
    assert stored(repo) == snapshot


def test_dry_run_writes_nothing(tmp_path):
    repo = repository()
    summary = rms.run(ctx(repo, tmp_path, confirm=False), [CODE], max_products=2)
    assert decision(summary)["outcome"] == lpm.TRANSITION_TO_REMOVED
    assert stored(repo)[0]["mapping_status"] == lpm.RECORDED
    assert repo.client.tables.get("process_logs", []) == []


def test_max_products_blocks_the_write(tmp_path):
    repo = repository()
    summary = rms.run(ctx(repo, tmp_path), [CODE], max_products=0)
    assert summary["blocked"] and stored(repo)[0]["mapping_status"] == lpm.RECORDED


# --- refusals -----------------------------------------------------------------------


@pytest.mark.parametrize("sync", [
    sync_row(removed=False),                 # owner did not remove it
    sync_row(marker_id=9999),                # marker for another Woo ID
    sync_row(recovery=True),                 # recovery voids the marker
], ids=["not-removed", "marker-other-id", "recovery-required"])
def test_unconfirmed_removal_is_refused(tmp_path, sync):
    repo = repository(sync=sync)
    summary = rms.run(ctx(repo, tmp_path), [CODE], max_products=2)
    assert decision(summary)["outcome"] == lpm.TRANSITION_REFUSED
    assert stored(repo)[0]["mapping_status"] == lpm.RECORDED


def test_owner_restored_product_is_never_overwritten(tmp_path):
    """#3772 pattern: the product reads 'publish' again. Owner decision."""
    repo = repository()
    summary = rms.run(ctx(repo, tmp_path, catalog={WOO: {"id": WOO, "status": "publish"}}), [CODE], max_products=2)
    assert decision(summary)["outcome"] == lpm.TRANSITION_REFUSED
    assert "live again" in decision(summary)["reason"]
    assert stored(repo)[0]["mapping_status"] == lpm.RECORDED


def test_dependent_translation_row_refuses(tmp_path):
    repo = repository(rows=[mapping_row(), mapping_row(language="en", lpid="lp-en", woo=5001, sync_id=None)])
    summary = rms.run(ctx(repo, tmp_path), [CODE], max_products=2)
    assert decision(summary)["outcome"] == lpm.TRANSITION_REFUSED
    assert all(r["mapping_status"] == lpm.RECORDED for r in stored(repo))


def test_stored_row_pointing_elsewhere_is_refused(tmp_path):
    repo = repository(rows=[mapping_row(woo=4000)])
    summary = rms.run(ctx(repo, tmp_path), [CODE], max_products=2)
    assert decision(summary)["outcome"] == lpm.TRANSITION_REFUSED


def test_unknown_product_code_is_refused(tmp_path):
    repo = repository()
    summary = rms.run(ctx(repo, tmp_path), ["TSYC-NOPE"], max_products=2)
    assert decision(summary)["outcome"] == lpm.TRANSITION_REFUSED


# --- interruption / concurrency ---------------------------------------------------


def test_failed_read_back_stops_without_retry(tmp_path, monkeypatch):
    repo = repository()
    calls = {"n": 0}
    real = rms._read_row

    def flaky(repository, lpid):
        calls["n"] += 1
        row = real(repository, lpid)
        return {**row, "woocommerce_product_id": 1} if row else row

    monkeypatch.setattr(rms, "_read_row", flaky)
    summary = rms.run(ctx(repo, tmp_path), [CODE], max_products=2)
    checkpoint = summary["checkpoint"]
    assert checkpoint["status"] == rms.STOPPED
    assert checkpoint["products"][CODE]["result"] == rms.STOPPED
    assert calls["n"] == 1  # read once, never retried


def test_row_changed_after_planning_is_not_written(tmp_path, monkeypatch):
    """Compare-and-set on updated_at: a concurrent change makes the update
    match zero rows, and the run stops."""
    repo = repository()
    real_plan = rms.plan
    touches = {"n": 0}

    def plan_then_touch(context, codes):
        result = real_plan(context, codes)
        touches["n"] += 1  # another writer changes the row after every read
        stored(repo)[0]["updated_at"] = f"t-concurrent-{touches['n']}"
        return result

    monkeypatch.setattr(rms, "plan", plan_then_touch)
    summary = rms.run(ctx(repo, tmp_path), [CODE], max_products=2)
    assert summary["checkpoint"]["status"] == rms.STOPPED
    assert stored(repo)[0]["mapping_status"] == lpm.RECORDED


# --- rollback ------------------------------------------------------------------------


def test_rollback_restores_only_unchanged_rows(tmp_path):
    repo = repository()
    rms.run(ctx(repo, tmp_path), [CODE], max_products=2)
    dry = rms.rollback(repo, tmp_path / "run", confirm=False)
    assert dry == {"restored": [CODE], "kept": [], "missing": []}
    assert stored(repo)[0]["mapping_status"] == lpm.REMOTE_REMOVED
    done = rms.rollback(repo, tmp_path / "run", confirm=True, log=repo.write_process_log)
    assert done["restored"] == [CODE] and stored(repo)[0]["mapping_status"] == lpm.RECORDED


def test_rollback_keeps_a_row_changed_after_the_run(tmp_path):
    repo = repository()
    rms.run(ctx(repo, tmp_path), [CODE], max_products=2)
    stored(repo)[0]["updated_at"] = "t-later"
    result = rms.rollback(repo, tmp_path / "run", confirm=True)
    assert result["kept"] == [CODE] and stored(repo)[0]["mapping_status"] == lpm.REMOTE_REMOVED


# --- audit agreement ------------------------------------------------------------------


def test_after_the_transition_the_stored_row_equals_the_derived_row(tmp_path):
    import audit_language_product_mapping as audit_cli

    repo = repository()
    rms.run(ctx(repo, tmp_path), [CODE], max_products=2)
    syncs, products = audit_cli.load_local(repo)
    derived, _ = audit_cli.derive(syncs, products)
    _, rows = audit_cli.load_stored(repo, products)
    assert lpm.compare_stored_vi(rows, derived) == []
