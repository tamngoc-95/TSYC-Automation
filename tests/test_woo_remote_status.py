"""Offline tests: local-versus-remote Woo status classification, the
read-only checker, local-only reconciliation, the writer lock and the
preflight gate (2026-10-26: 67 local DRAFT_CREATED rows were published,
deleted or trashed remotely without anything noticing). No network."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import audit_woo_remote_status as aws
import preflight_pipeline as pf
from src.domain import woo_remote_lifecycle as lifecycle
from src.domain import woo_remote_status as wrs
from src.services import writer_lock as wl
from support.fake_supabase import FakeSupabaseRepository

SKU = "TSYC-FAHASA-2026-003-CAN-0002"
STORE = "https://example-store.test"


def record(**overrides: Any) -> wrs.LocalRecord:
    base = dict(
        sync_id="s1", internal_product_id="ip1", product_code=SKU, candidate_id="c1",
        sync_status="DRAFT_CREATED", product_status="DRAFT_CREATED", woocommerce_product_id=4998,
        sku=SKU, last_local_write_at="2026-10-24T08:00:00+00:00",
    )
    base.update(overrides)
    return wrs.LocalRecord(**base)


def remote(**overrides: Any) -> dict[str, Any]:
    base = {"id": 4998, "sku": SKU, "status": "draft", "name": "Tớ Tự Tin",
            "permalink": f"{STORE}/?post_type=product&p=4998", "date_modified_gmt": "2026-10-24T08:00:30"}
    base.update(overrides)
    return base


def copy(pid: int, lang: str) -> dict[str, Any]:
    return {"id": pid, "sku": SKU, "status": "publish", "permalink": f"{STORE}/{lang}/product/x-{pid}/"}


def obs(status: int | None, product: dict | None = None, sku_any=None, sku_trash=None, error=None):
    return wrs.RemoteObservation(http_status=status, product=product, sku_any=sku_any, sku_trash=sku_trash, error=error)


# ---------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------


def test_local_draft_remote_draft_is_consistent():
    result = wrs.classify(record(), obs(200, remote()))
    assert result.classification == wrs.CONSISTENT and result.action == wrs.ACTION_NONE
    assert wrs.write_eligibility(result) == (True, wrs.GATE_OK)


def test_local_draft_remote_published_is_reconcilable_locally():
    result = wrs.classify(record(), obs(200, remote(status="publish")))
    assert result.classification == wrs.REMOTE_PUBLISHED
    assert result.action == wrs.ACTION_LOCAL_RECONCILE
    assert wrs.write_eligibility(result)[0] is False


def test_local_draft_remote_trash_confirmed_by_sku_search():
    result = wrs.classify(record(), obs(200, remote(status="trash"), sku_any=[], sku_trash=[]))
    assert result.classification == wrs.REMOTE_TRASHED
    assert result.action == wrs.ACTION_LOCAL_RECONCILE


def test_permanently_removed_needs_both_empty_sku_searches():
    result = wrs.classify(record(), obs(404, sku_any=[], sku_trash=[]))
    assert result.classification == wrs.REMOTE_REMOVED
    assert result.action == wrs.ACTION_LOCAL_RECONCILE


@pytest.mark.parametrize("sku_any, sku_trash", [(None, None), ([], None)])
def test_404_alone_is_never_a_deletion(sku_any, sku_trash):
    result = wrs.classify(record(), obs(404, sku_any=sku_any, sku_trash=sku_trash))
    assert result.classification == wrs.API_ACCESS_ERROR
    assert result.action == wrs.ACTION_REREAD


def test_404_with_sku_in_trash_under_another_id_is_unresolved():
    other = {"id": 7777, "sku": SKU, "status": "trash", "permalink": ""}
    result = wrs.classify(record(), obs(404, sku_any=[], sku_trash=[other]))
    assert result.classification == wrs.UNRESOLVED and result.action == wrs.ACTION_PROPOSE


@pytest.mark.parametrize("status", [401, 403, 500, 502, None])
def test_unauthorized_or_failed_reads_are_api_errors_never_removals(status):
    result = wrs.classify(record(), obs(status, error="denied"))
    assert result.classification == wrs.API_ACCESS_ERROR
    assert result.action == wrs.ACTION_REREAD


def test_missing_woo_id_is_an_uncertain_create_unless_resolved():
    unresolved = wrs.classify(record(woocommerce_product_id=None, sync_status="FAILED"), None)
    assert unresolved.classification == wrs.UNCERTAIN_CREATE
    assert "recover_historical_woo_create" in unresolved.proposed_correction
    resolved = wrs.classify(record(woocommerce_product_id=None, sync_status="FAILED",
                                   create_outcome_resolved=True), None)
    assert resolved.classification == wrs.CONSISTENT


def test_lost_create_response_stays_uncertain_whatever_the_remote_says():
    result = wrs.classify(record(recovery_required=True), obs(200, remote()))
    assert result.classification == wrs.UNCERTAIN_CREATE
    assert result.action == wrs.ACTION_PROPOSE


def test_product_without_stored_sku_is_never_confirmed_removed():
    result = wrs.classify(record(sku=""), obs(404, sku_any=[], sku_trash=[]))
    assert result.classification == wrs.UNRESOLVED


def test_remote_without_sku_is_an_identity_mismatch():
    result = wrs.classify(record(), obs(200, remote(sku="")))
    assert result.classification == wrs.IDENTITY_MISMATCH


def test_different_edition_with_the_same_title_is_never_matched_by_title():
    result = wrs.classify(record(), obs(200, remote(sku="TSYC-OTHER-EDITION", name="Tớ Tự Tin")))
    assert result.classification == wrs.IDENTITY_MISMATCH
    other_id = wrs.classify(record(), obs(200, remote(id=5000)))
    assert other_id.classification == wrs.IDENTITY_MISMATCH


def test_removed_vi_with_live_translations_is_translation_linked_not_removed():
    result = wrs.classify(record(), obs(404, sku_any=[copy(5001, "en"), copy(5002, "de")], sku_trash=[]))
    assert result.classification == wrs.TRANSLATION_LINKED_RECORD
    assert result.action == wrs.ACTION_PROPOSE
    assert result.evidence["sku_any_languages"] == {"5001": "en", "5002": "de"}


def test_trashed_vi_with_live_translations_is_translation_linked():
    result = wrs.classify(record(), obs(200, remote(status="trash"), sku_any=[copy(5001, "en")], sku_trash=[]))
    assert result.classification == wrs.TRANSLATION_LINKED_RECORD


def test_live_vi_copy_with_same_sku_is_not_called_a_translation():
    vi_copy = {"id": 5003, "sku": SKU, "status": "publish", "permalink": f"{STORE}/product/x/"}
    result = wrs.classify(record(), obs(404, sku_any=[vi_copy], sku_trash=[]))
    assert result.classification == wrs.UNRESOLVED


def test_consistent_published_vi_with_translations_stays_consistent():
    result = wrs.classify(record(product_status="PUBLISHED"), obs(200, remote(status="publish")))
    assert result.classification == wrs.CONSISTENT
    assert wrs.write_eligibility(result) == (False, wrs.GATE_PUBLISHED_NOT_AUTHORIZED)
    assert wrs.write_eligibility(result, allow_published=True) == (True, wrs.GATE_OK)


def test_owner_restored_a_recorded_removal():
    result = wrs.classify(record(sync_status="FAILED", product_status="FAILED",
                                 removal_state=lifecycle.REMOTE_DELETED), obs(200, remote(status="publish")))
    assert result.classification == wrs.OWNER_MODIFIED
    assert result.action == wrs.ACTION_PROPOSE


def test_recorded_removal_still_removed_is_consistent():
    gone = record(sync_status="FAILED", product_status="FAILED", removal_state=lifecycle.REMOTE_DELETED)
    assert wrs.classify(gone, obs(404)).classification == wrs.CONSISTENT
    assert wrs.classify(gone, obs(200, remote(status="trash"))).classification == wrs.CONSISTENT


@pytest.mark.parametrize("status", ["pending", "private"])
def test_owner_workflow_states_are_never_mapped(status):
    result = wrs.classify(record(), obs(200, remote(status=status)))
    assert result.classification == wrs.OWNER_MODIFIED
    assert result.action == wrs.ACTION_PROPOSE


def test_published_moved_back_to_draft_is_owner_modified_not_automation_owned():
    result = wrs.classify(record(product_status="PUBLISHED"), obs(200, remote(status="draft")))
    assert result.classification == wrs.OWNER_MODIFIED


def test_internal_status_lagging_its_sync_row_is_local_stale():
    result = wrs.classify(record(product_status="READY_FOR_DRAFT"), obs(200, remote()))
    assert result.classification == wrs.LOCAL_STATUS_STALE
    assert result.action == wrs.ACTION_LOCAL_RECONCILE


def test_owner_edit_after_our_last_write_is_evidence():
    edited = wrs.classify(record(), obs(200, remote(date_modified_gmt="2026-10-25T10:00:00")))
    assert edited.classification == wrs.CONSISTENT
    assert edited.owner_modified_after_sync is True
    ours = wrs.classify(record(), obs(200, remote(date_modified_gmt="2026-10-24T08:00:30")))
    assert ours.owner_modified_after_sync is False
    unknown = wrs.classify(record(last_local_write_at=None), obs(200, remote()))
    assert unknown.owner_modified_after_sync is None


def test_systemic_assessment():
    ok = [wrs.classify(record(sync_id=str(i)), obs(200, remote())) for i in range(40)]
    errors = [wrs.classify(record(sync_id=f"e{i}"), obs(503)) for i in range(5)]
    # Threshold is max(3, ceil(10 % of checked)): 2 of 42 isolated, 5 of 45 systemic.
    assert wrs.assess_systemic(ok + errors[:2], True)[0] is False
    assert wrs.assess_systemic(ok + errors, True)[0] is True
    assert wrs.assess_systemic(ok, False)[0] is True


# ---------------------------------------------------------------------
# Local write gate
# ---------------------------------------------------------------------


def sync_row(**overrides: Any) -> dict[str, Any]:
    base = {"woocommerce_product_id": 4998, "product_sku": SKU, "woocommerce_status": "DRAFT_CREATED",
            "response_payload": {}}
    base.update(overrides)
    return base


@pytest.mark.parametrize("product, sync, expected", [
    ({"woocommerce_status": "DRAFT_CREATED"}, sync_row(), None),
    ({"woocommerce_status": "DRAFT_CREATED"}, sync_row(woocommerce_product_id=None), wrs.GATE_NO_REMOTE_IDENTITY),
    ({"woocommerce_status": "DRAFT_CREATED"}, sync_row(product_sku=""), wrs.GATE_NO_REMOTE_IDENTITY),
    ({"woocommerce_status": "FAILED"}, sync_row(woocommerce_status="FAILED", response_payload={
        "remote_removal": {"state": "REMOTE_DELETED", "woocommerce_product_id": 4998}}), wrs.GATE_OWNER_REMOVED),
    ({"woocommerce_status": "DRAFT_CREATED"}, sync_row(response_payload={
        "remote_removal": {"state": "REMOTE_DELETED", "woocommerce_product_id": 1}}), wrs.GATE_OWNER_REMOVED),
    ({"woocommerce_status": "DRAFT_CREATED"}, sync_row(response_payload={"recovery_required": True}),
     wrs.GATE_UNCERTAIN_CREATE),
    ({"woocommerce_status": "PUBLISHED"}, sync_row(), wrs.GATE_PUBLISHED_NOT_AUTHORIZED),
    ({"woocommerce_status": "READY_FOR_DRAFT"}, sync_row(), wrs.GATE_LOCAL_STATUS),
    ({"woocommerce_status": "DRAFT_CREATED"}, sync_row(woocommerce_status="FAILED"), wrs.GATE_LOCAL_STATUS),
])
def test_local_write_gate(product, sync, expected):
    assert wrs.local_write_block_reason(product, sync) == expected


def test_local_write_gate_allows_published_only_when_authorized():
    assert wrs.local_write_block_reason({"woocommerce_status": "PUBLISHED"}, sync_row(), allow_published=True) is None


def test_preorder_and_gallery_writers_refuse_owner_removed_products():
    import apply_fahasa_gallery_to_woo_drafts as gallery
    import apply_preorder_inventory as preorder

    from src.domain.rules import preorder_inventory_rules as rules

    def repo_with(payload: dict) -> FakeSupabaseRepository:
        return FakeSupabaseRepository(tables={
            "internal_products": [{"internal_product_id": "ip1", "product_code": SKU, "candidate_id": "c1",
                                   "woocommerce_status": "DRAFT_CREATED"}],
            "woocommerce_product_syncs": [{"sync_id": "s1", "internal_product_id": "ip1",
                                           **sync_row(response_payload=payload)}],
            "product_candidates": [{"candidate_id": "c1", "candidate_code": "FAHASA-2026-003-CAN-0002",
                                    "source_evidence": {"origin": rules.FAHASA_DISCOVERY_ORIGIN,
                                                        "supply_status": rules.PREORDER_SUPPLY_STATUS}}],
            "product_images": [],
        })

    # A stale marker whose state no longer matches the row still blocks.
    removed = {"remote_removal": {"state": "REMOTE_TRASHED", "woocommerce_product_id": 4998}}
    for loader in (preorder.load_local_target, gallery.load_local_target):
        with pytest.raises(RuntimeError, match="Local write gate: OWNER_REMOVED"):
            loader(repo_with(removed), SKU)
    # Control: without the marker the gate passes (the gallery loader then
    # stops at its own, later image requirement).
    assert preorder.load_local_target(repo_with({}), SKU)["sync"]["sync_id"] == "s1"
    with pytest.raises(RuntimeError) as error:
        gallery.load_local_target(repo_with({}), SKU)
    assert "write gate" not in str(error.value)


# ---------------------------------------------------------------------
# Checker over a fake store
# ---------------------------------------------------------------------


class FakeReader:
    def __init__(self, products: dict[int, dict[str, Any]], *, fail_bulk: bool = False,
                 errors: dict[int, int] | None = None, sku_index: dict[str, list[dict]] | None = None) -> None:
        self.products = products
        self.fail_bulk = fail_bulk
        self.errors = errors or {}
        self.sku_index = sku_index or {}
        self.calls: list[tuple] = []

    def get_many(self, ids):
        self.calls.append(("many", tuple(ids)))
        if self.fail_bulk:
            raise aws.RemoteReadError("simulated outage")
        return {i: dict(self.products[i]) for i in ids if i in self.products and self.products[i]["status"] != "trash"}

    def get_one(self, pid):
        self.calls.append(("one", pid))
        if pid in self.errors:
            return self.errors[pid], None, f"HTTP {self.errors[pid]}"
        if pid in self.products:
            return 200, dict(self.products[pid]), None
        return 404, None, None

    def find_by_sku(self, sku, status):
        self.calls.append(("sku", sku, status))
        live = [p for p in self.products.values() if p["sku"] == sku and p["status"] != "trash"]
        trash = [p for p in self.products.values() if p["sku"] == sku and p["status"] == "trash"]
        extra = self.sku_index.get(sku, [])
        return (live + extra) if status == "any" else trash


def make_repo(rows: list[tuple[str, str, int | None, str, str, dict | None]]) -> FakeSupabaseRepository:
    """rows: (code, ip_status, woo_id, sync_status, sku, response_payload)"""
    products, syncs, candidates = [], [], []
    for n, (code, ip_status, woo_id, sync_status, sku, payload) in enumerate(rows):
        products.append({"internal_product_id": f"ip{n}", "product_code": code, "candidate_id": f"c{n}",
                         "woocommerce_status": ip_status, "title": code, "review_required": False,
                         "review_reason": None, "product_metadata": {}, "updated_at": "t0"})
        syncs.append({"sync_id": f"s{n}", "internal_product_id": f"ip{n}", "woocommerce_product_id": woo_id,
                      "woocommerce_status": sync_status, "product_sku": sku, "response_payload": payload or {},
                      "synced_at": "2026-10-24T08:00:00+00:00", "last_attempt_at": None, "updated_at": "t0",
                      "product_name": code, "product_permalink": None, "error_code": None, "error_message": None})
        candidates.append({"candidate_id": f"c{n}", "identity_status": "IDENTITY_VERIFIED"})
    return FakeSupabaseRepository(tables={"internal_products": products, "woocommerce_product_syncs": syncs,
                                          "product_candidates": candidates, "process_logs": []})


def p(pid: int, sku: str, status: str) -> dict[str, Any]:
    return {"id": pid, "sku": sku, "status": status, "permalink": f"{STORE}/?p={pid}", "date_modified_gmt": None}


def test_checker_reproduces_each_mismatch_kind_and_stays_read_only():
    removed_marker = {"remote_removal": {"state": "REMOTE_DELETED", "woocommerce_product_id": 4}}
    repo = make_repo([
        ("A", "DRAFT_CREATED", 1, "DRAFT_CREATED", "A", None),     # consistent draft
        ("B", "DRAFT_CREATED", 2, "DRAFT_CREATED", "B", None),     # owner published
        ("C", "DRAFT_CREATED", 3, "DRAFT_CREATED", "C", None),     # deleted
        ("D", "FAILED", 4, "FAILED", "D", removed_marker),         # recorded removed, still gone
        ("E", "DRAFT_CREATED", 5, "DRAFT_CREATED", "E", None),     # trashed
        ("F", "READY_FOR_DRAFT", None, "FAILED", "F", None),       # uncertain create
    ])
    before = json.dumps(repo.client.tables, default=str)
    reader = FakeReader({1: p(1, "A", "draft"), 2: p(2, "B", "publish"), 5: p(5, "E", "trash")})
    report = aws.run_check(aws.load_local_records(repo), reader)
    by_code = {r.product_code: r.classification for r in report.results}
    assert by_code == {"A": wrs.CONSISTENT, "B": wrs.REMOTE_PUBLISHED, "C": wrs.REMOTE_REMOVED,
                       "D": wrs.CONSISTENT, "E": wrs.REMOTE_TRASHED, "F": wrs.UNCERTAIN_CREATE}
    assert report.systemic_failure is False
    # The recorded removal needed no per-product read.
    assert ("one", 4) not in reader.calls
    after = json.dumps(repo.client.tables, default=str)
    assert before == after
    plan = aws.build_plan(report)
    assert {i["product_code"] for i in plan["local_reconcile"]} == {"B", "C", "E"}
    assert {i["product_code"] for i in plan["proposals_left_unchanged"]} == {"F"}


def test_checker_owner_restored_product_is_flagged():
    marker = {"remote_removal": {"state": "REMOTE_DELETED", "woocommerce_product_id": 9}}
    repo = make_repo([("R", "FAILED", 9, "FAILED", "R", marker)])
    report = aws.run_check(aws.load_local_records(repo), FakeReader({9: p(9, "R", "publish")}))
    assert report.results[0].classification == wrs.OWNER_MODIFIED


def test_bulk_outage_is_systemic_and_never_a_deletion():
    repo = make_repo([("A", "DRAFT_CREATED", 1, "DRAFT_CREATED", "A", None)])
    report = aws.run_check(aws.load_local_records(repo), FakeReader({}, fail_bulk=True))
    assert report.systemic_failure is True
    assert report.results[0].classification == wrs.API_ACCESS_ERROR


def test_unauthorized_reads_beyond_threshold_are_systemic():
    rows = [(f"P{i}", "DRAFT_CREATED", i, "DRAFT_CREATED", f"P{i}", None) for i in range(1, 5)]
    reader = FakeReader({}, errors={i: 401 for i in range(1, 5)})
    report = aws.run_check(aws.load_local_records(make_repo(rows)), reader)
    assert report.counts == {wrs.API_ACCESS_ERROR: 4}
    assert report.systemic_failure is True


def test_repeated_check_is_idempotent():
    repo = make_repo([("B", "DRAFT_CREATED", 2, "DRAFT_CREATED", "B", None)])
    reader = FakeReader({2: p(2, "B", "publish")})
    first = aws.run_check(aws.load_local_records(repo), reader)
    second = aws.run_check(aws.load_local_records(repo), reader)
    assert [r.as_dict() for r in first.results] == [r.as_dict() for r in second.results]


def test_reader_never_raises_on_single_reads_and_caches_sku_searches():
    class Resp:
        def __init__(self, code, body=None):
            self.status_code, self._body = code, body

        def json(self):
            if self._body is None:
                raise ValueError("no json")
            return self._body

    calls = []

    def http_get(url, **kwargs):
        calls.append((url, kwargs.get("params", {}).get("sku")))
        if url.endswith("/products/1"):
            raise ConnectionError("down")
        if url.endswith("/products/2"):
            return Resp(401, {"code": "woocommerce_rest_cannot_view"})
        return Resp(200, [])

    reader = aws.WooRemoteReader(STORE, "wc/v3", "ck", "cs", 5, http_get=http_get)
    assert reader.get_one(1)[0] is None
    assert reader.get_one(2)[:2] == (401, None)
    assert reader.find_by_sku("X", "any") == []
    assert reader.find_by_sku("X", "any") == []
    assert sum(1 for c in calls if c[1] == "X") == 1


# ---------------------------------------------------------------------
# Local-only reconciliation
# ---------------------------------------------------------------------


def apply_ctx(repo, store: dict[int, dict], tmp_path: Path, writer=None, logs=None) -> aws.ApplyContext:
    def official_writer(repository, sync_record):
        """Stand-in for sync_woocommerce_product_status.synchronize_one_product."""
        product = store.get(int(sync_record["woocommerce_product_id"]))
        ip = repository.client.table("internal_products")
        if product and product["status"] == "publish":
            ip.update({"woocommerce_status": "PUBLISHED", "updated_at": "t1"}).eq(
                "internal_product_id", sync_record["internal_product_id"]).execute()
        elif product is None:
            marker = lifecycle.build_removal_marker(lifecycle.REMOTE_DELETED, sync_record["woocommerce_product_id"],
                                                    sync_record["product_sku"], "test", "t1", "test")
            repository.client.table("woocommerce_product_syncs").update(
                {"woocommerce_status": "FAILED", "response_payload": {"remote_removal": marker}, "updated_at": "t1"}
            ).eq("sync_id", sync_record["sync_id"]).execute()
            ip.update({"woocommerce_status": "FAILED", "updated_at": "t1"}).eq(
                "internal_product_id", sync_record["internal_product_id"]).execute()
        return True

    def loader(repository, code):
        product = repository.client.table("internal_products").select("*").eq("product_code", code).execute().data[0]
        return repository.client.table("woocommerce_product_syncs").select("*").eq(
            "internal_product_id", product["internal_product_id"]).execute().data[0]

    return aws.ApplyContext(repository=repo, reader_factory=lambda: FakeReader(store),
                            sync_writer=writer or official_writer, sync_record_loader=loader,
                            run_dir=tmp_path / "run", log=logs)


def test_apply_reconciles_published_and_deleted_then_is_idempotent(tmp_path):
    repo = make_repo([("B", "DRAFT_CREATED", 2, "DRAFT_CREATED", "B", None),
                      ("C", "DRAFT_CREATED", 3, "DRAFT_CREATED", "C", None)])
    store = {2: p(2, "B", "publish")}
    logged = []
    ctx = apply_ctx(repo, store, tmp_path, logs=lambda **kw: logged.append(kw))
    reports = aws.run_apply(ctx, ["B", "C"], max_products=2)
    assert [r["result"] for r in reports] == [aws.APPLIED, aws.APPLIED]
    assert len(logged) == 2
    assert all(json.loads((tmp_path / "run" / "rollback" / f"s{i}.json").read_text("utf-8"))["after"]
               for i in (0, 1))
    again = aws.run_apply(ctx, ["B", "C"], max_products=2)
    assert [r["result"] for r in again] == [aws.ALREADY_DONE, aws.ALREADY_DONE]


def test_apply_never_touches_non_reconcilable_classes(tmp_path):
    repo = make_repo([("X", "DRAFT_CREATED", 7, "DRAFT_CREATED", "X", None)])
    store = {7: p(7, "OTHER-SKU", "draft")}
    called = []
    ctx = apply_ctx(repo, store, tmp_path, writer=lambda *a: called.append(a))
    reports = aws.run_apply(ctx, ["X"], max_products=1)
    assert reports[0]["result"] == aws.SKIPPED_NOT_RECONCILABLE
    assert called == []


def test_apply_respects_max_products(tmp_path):
    ctx = apply_ctx(make_repo([]), {}, tmp_path)
    with pytest.raises(RuntimeError, match="exceed --max-products"):
        aws.run_apply(ctx, ["A", "B"], max_products=1)


def test_interrupted_reconciliation_stops_and_resumes_without_redoing(tmp_path):
    repo = make_repo([("B", "DRAFT_CREATED", 2, "DRAFT_CREATED", "B", None),
                      ("C", "DRAFT_CREATED", 3, "DRAFT_CREATED", "C", None)])
    store = {2: p(2, "B", "publish")}
    good = apply_ctx(repo, store, tmp_path)
    calls = []

    def failing_writer(repository, sync_record):
        calls.append(sync_record["product_sku"])
        if sync_record["product_sku"] == "C":
            raise TimeoutError("connection dropped mid-write")
        return good.sync_writer(repository, sync_record)

    ctx = apply_ctx(repo, store, tmp_path, writer=failing_writer)
    reports = aws.run_apply(ctx, ["B", "C"], max_products=2)
    assert [r["result"] for r in reports] == [aws.APPLIED, aws.WRITER_FAILED, "STOPPED"]
    checkpoint = json.loads((tmp_path / "run" / "checkpoint.json").read_text("utf-8"))
    assert checkpoint["products"]["B"]["result"] == aws.APPLIED
    # Resume: B is not redone; C is re-read fresh and reconciled.
    resumed = aws.run_apply(good, ["B", "C"], max_products=2)
    assert [r["result"] for r in resumed] == [aws.ALREADY_DONE, aws.APPLIED]
    assert calls == ["B", "C"]


def test_apply_reclassifies_fresh_and_skips_when_state_changed(tmp_path):
    repo = make_repo([("B", "DRAFT_CREATED", 2, "DRAFT_CREATED", "B", None)])
    store = {2: p(2, "B", "pending")}  # owner moved it to pending since the plan
    called = []
    ctx = apply_ctx(repo, store, tmp_path, writer=lambda *a: called.append(a))
    assert aws.run_apply(ctx, ["B"], max_products=1)[0]["result"] == aws.SKIPPED_NOT_RECONCILABLE
    assert called == []


def test_rollback_restores_only_unchanged_rows(tmp_path):
    repo = make_repo([("B", "DRAFT_CREATED", 2, "DRAFT_CREATED", "B", None)])
    ctx = apply_ctx(repo, {2: p(2, "B", "publish")}, tmp_path)
    aws.run_apply(ctx, ["B"], max_products=1)
    ip = lambda: repo.client.table("internal_products").select("*").eq("product_code", "B").execute().data[0]
    assert ip()["woocommerce_status"] == "PUBLISHED"
    assert aws.run_rollback(repo, tmp_path / "run", ["B"])[0]["result"] == "ROLLED_BACK"
    assert ip()["woocommerce_status"] == "DRAFT_CREATED"
    # Second rollback: rows no longer equal the post-reconcile snapshot.
    assert aws.run_rollback(repo, tmp_path / "run", ["B"])[0]["result"] == "REFUSED_CHANGED_SINCE_RECONCILE"


# ---------------------------------------------------------------------
# Writer lock and preflight
# ---------------------------------------------------------------------


def test_concurrent_writer_is_refused(tmp_path):
    path = tmp_path / "w.lock"
    path.write_text(json.dumps({"pid": 999999, "owner": "run_batch", "acquired_at": "t"}), encoding="utf-8")
    with pytest.raises(wl.WriterLockBusy, match="999999"):
        wl.ProductionWriterLock("test", path, alive=lambda pid: True).acquire()
    assert wl.inspect_lock(path, alive=lambda pid: True).state == wl.HELD_BY_OTHER


def test_stale_lock_is_taken_over_and_released(tmp_path):
    path = tmp_path / "w.lock"
    path.write_text(json.dumps({"pid": 999999, "owner": "old", "acquired_at": "t"}), encoding="utf-8")
    lock = wl.ProductionWriterLock("test", path, alive=lambda pid: False)
    with lock:
        assert lock.took_over["pid"] == 999999
        assert wl.inspect_lock(path).state == wl.HELD_BY_SELF
    assert not path.exists()


def test_process_alive_reports_this_process():
    import os

    assert wl.process_alive(os.getpid()) is True
    assert wl.process_alive(0) is False


def test_preflight_blocks_on_a_concurrent_writer_and_unreliable_remote():
    busy = lambda: wl.LockState(wl.HELD_BY_OTHER, {"pid": 1, "owner": "x", "acquired_at": "t"}, Path("w.lock"))
    assert pf.check_writer_lock(busy).status == pf.CheckStatus.FAIL
    free = lambda: wl.LockState(wl.FREE, None, Path("w.lock"))
    assert pf.check_writer_lock(free).status == pf.CheckStatus.PASS

    repo = make_repo([("A", "DRAFT_CREATED", 1, "DRAFT_CREATED", "A", None)])
    outage = pf.check_woo_remote_state(configuration_ok=True, repository=repo, repository_factory=lambda: repo,
                                       reader=FakeReader({}, fail_bulk=True))
    assert outage.status == pf.CheckStatus.FAIL


def test_preflight_reports_isolated_mismatches_as_warnings():
    repo = make_repo([("A", "DRAFT_CREATED", 1, "DRAFT_CREATED", "A", None),
                      ("B", "DRAFT_CREATED", 2, "DRAFT_CREATED", "B", None)])
    reader = FakeReader({1: p(1, "A", "draft"), 2: p(2, "B", "publish")})
    check = pf.check_woo_remote_state(configuration_ok=True, repository=repo, repository_factory=lambda: repo,
                                      reader=reader)
    assert check.status == pf.CheckStatus.WARNING
    assert any("REMOTE_PUBLISHED: 1" in line for line in check.warning_lines)
    consistent = pf.check_woo_remote_state(configuration_ok=True, repository=repo, repository_factory=lambda: repo,
                                           reader=FakeReader({1: p(1, "A", "draft"), 2: p(2, "B", "draft")}))
    assert consistent.status == pf.CheckStatus.PASS


def test_run_batch_production_run_refuses_while_another_writer_holds_the_lock():
    import run_batch

    class BusyLock:
        released = False

        def acquire(self):
            raise wl.WriterLockBusy("held by PID 4242")

        def release(self):
            BusyLock.released = True

    def runner(argv):
        raise AssertionError("no stage may run without the writer lock")

    code = run_batch.main(
        ["--candidate-code", "FAHASA-2026-003-CAN-0002", "--max-candidates", "1", "--non-interactive"],
        repository=FakeSupabaseRepository(tables={}),
        subprocess_runner=runner,
        preflight_runner=lambda: pf.PreflightResult(checks=()),
        writer_lock=BusyLock(),
    )
    assert code == 2
