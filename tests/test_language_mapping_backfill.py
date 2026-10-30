"""Offline tests for the VI language-mapping backfill
(scripts/backfill_language_product_mapping.py).

No network, no Supabase, no WooCommerce.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import backfill_language_product_mapping as bf
from src.domain import language_product_mapping as lpm
from support.fake_supabase import FakeSupabaseClient, FakeSupabaseRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIVE = "TSYC-FB-HIST-2026-001-CAN-0001"
GONE = "TSYC-FB-HIST-2026-001-CAN-0002"
IP_LIVE = "11111111-1111-4111-8111-111111111111"
IP_GONE = "33333333-3333-4333-8333-333333333333"


def meta(values):
    return [{"key": k, "value": v} for k, v in values.items()]


def sync_row(code, ipid, woo_id, *, removed=False):
    created = {"id": woo_id, "sku": code, "status": "draft", "name": "Sách", "description": "<p>Mô tả</p>",
               "short_description": "", "date_modified_gmt": "2026-10-20T08:00:00",
               "meta_data": meta({"_tsyc_internal_product_id": ipid})}
    response = {"woocommerce_product": created}
    if removed:
        response["remote_removal"] = {"state": "REMOTE_TRASHED", "recreate": False}
    return {"sync_id": f"sync-{woo_id}", "internal_product_id": ipid, "woocommerce_product_id": woo_id,
            "woocommerce_status": "DRAFT_CREATED", "product_sku": code,
            "request_payload": {"sku": code, "status": "draft",
                                "meta_data": meta({"_tsyc_internal_product_id": ipid})},
            "response_payload": response}


def remote(pid, *, sku=LIVE, owner=IP_LIVE, status="publish", prefix="", modified="2026-10-20T08:00:00"):
    values = {"_tsyc_internal_product_id": owner} if owner else {}
    return {"id": pid, "sku": sku, "status": status, "permalink": f"https://tiemsachyeucon.com{prefix}/product/x/",
            "date_modified_gmt": modified, "meta_data": meta(values)}


class Reader:
    def __init__(self, products):
        self.products, self.calls = products, []

    def list_page(self, params):
        self.calls.append(dict(params))
        return list(self.products), 1


def repository(stored=None):
    return FakeSupabaseRepository({
        "internal_products": [{"internal_product_id": IP_LIVE, "product_code": LIVE},
                              {"internal_product_id": IP_GONE, "product_code": GONE}],
        "woocommerce_product_syncs": [sync_row(LIVE, IP_LIVE, 4100), sync_row(GONE, IP_GONE, 3700, removed=True)],
        lpm.TABLE: list(stored or []),
    })


def run(repo, catalog, tmp_path, *, group=bf.GROUP_RECORDED, allowlist=(LIVE, GONE), confirm=True,
        max_products=10, name="run"):
    return bf.run(repo, Reader(catalog), allowlist=list(allowlist), group=group, max_products=max_products,
                  confirm=confirm, batch_size=1, run_dir=tmp_path / name)


def outcome(summary, code):
    return next(d for d in summary["decisions"] if d["product_code"] == code)


def stored(repo):
    return repo.client.tables[lpm.TABLE]


# --- recorded group ---------------------------------------------------------

def test_verified_recorded_book_is_inserted_verified_checkpointed_and_logged(tmp_path):
    repo = repository()
    summary = run(repo, [remote(4100, modified="2026-10-25T00:00:00")], tmp_path)
    assert outcome(summary, LIVE)["outcome"] == bf.INSERT and outcome(summary, GONE)["outcome"] == bf.OTHER_GROUP
    assert summary["owner_maintained"] == 1
    [row] = stored(repo)
    assert (row["language_code"], row["woocommerce_product_id"], row["origin"], row["mapping_status"],
            row["sync_id"]) == ("vi", 4100, lpm.ORIGIN_TSYC_CREATED, lpm.RECORDED, "sync-4100")
    checkpoint = json.loads((tmp_path / "run" / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["status"] == "COMPLETED" and [i["product_code"] for i in checkpoint["inserted"]] == [LIVE]
    assert (tmp_path / "run" / "before_snapshot.json").exists()
    [log] = repo.client.tables["process_logs"]
    assert log["process_name"] == bf.PROCESS_NAME and log["status"] == "INSERTED"


def test_rerun_is_idempotent_and_reuses_identical_rows(tmp_path):
    repo = repository()
    run(repo, [remote(4100)], tmp_path, name="first")
    summary = run(repo, [remote(4100)], tmp_path, name="second")
    assert outcome(summary, LIVE)["outcome"] == bf.REUSED and len(stored(repo)) == 1


def test_dry_run_writes_nothing(tmp_path):
    repo = repository()
    summary = run(repo, [remote(4100)], tmp_path, confirm=False)
    assert outcome(summary, LIVE)["outcome"] == bf.INSERT
    assert stored(repo) == [] and "process_logs" not in repo.client.tables


@pytest.mark.parametrize("catalog, code", [
    ([], lpm.REMOTE_NOT_READ),                                               # gone remotely, recorded locally
    ([remote(4100, owner=None)], lpm.OWNER_META_MISSING),                    # ownership unknown
    ([remote(4100, owner=IP_GONE)], lpm.OWNER_META_OTHER_BOOK),
    ([remote(4100, sku="OTHER")], lpm.REMOTE_SKU_MISMATCH),
    ([remote(4100, prefix="/en")], lpm.LANGUAGE_MISMATCH),                   # not the VI product
    ([remote(4100), remote(4858, status="draft")], lpm.COPIED_OWNERSHIP_META),  # hand copy carrying TSYC meta
])
def test_contradicted_recorded_book_is_isolated_never_inserted(tmp_path, catalog, code):
    repo = repository()
    summary = run(repo, catalog, tmp_path)
    decision = outcome(summary, LIVE)
    assert decision["outcome"] == bf.ISOLATED and code in decision["reason"]
    assert stored(repo) == []


def test_vi_language_must_be_shown_positively(tmp_path):
    repo = repository()
    unknown = remote(4100, owner=IP_LIVE, status="draft")
    unknown["permalink"] = ""
    summary = run(repo, [unknown], tmp_path)
    assert bf.LANGUAGE_UNVERIFIED in outcome(summary, LIVE)["reason"] and stored(repo) == []


def test_en_de_copies_sharing_the_sku_are_never_linked(tmp_path):
    repo = repository()
    copies = [remote(4100), remote(4101, owner=None, prefix="/en"), remote(4102, owner=None, prefix="/de")]
    summary = run(repo, copies, tmp_path)
    assert outcome(summary, LIVE)["outcome"] == bf.INSERT
    assert lpm.UNMAPPED_SKU_COPY in outcome(summary, LIVE)["findings"]
    assert [(r["language_code"], r["woocommerce_product_id"]) for r in stored(repo)] == [("vi", 4100)]


def test_conflicting_stored_row_is_isolated_and_never_rewritten(tmp_path):
    other = {"language_product_id": "x", "internal_product_id": IP_LIVE, "language_code": "vi",
             "woocommerce_product_id": 9999, "origin": lpm.ORIGIN_TSYC_CREATED, "mapping_status": lpm.RECORDED,
             "sync_id": None, "link_evidence": {}}
    repo = repository(stored=[other])
    summary = run(repo, [remote(4100)], tmp_path)
    assert bf.STORED_CONFLICT in outcome(summary, LIVE)["reason"]
    assert stored(repo) == [other]


def test_allowlist_bounds_the_run_and_unknown_codes_are_not_derived(tmp_path):
    repo = repository()
    summary = run(repo, [remote(4100)], tmp_path, allowlist=["TSYC-NOT-A-BOOK"])
    assert [d["outcome"] for d in summary["decisions"]] == [bf.NOT_DERIVED] and stored(repo) == []


def test_max_products_blocks_the_whole_write(tmp_path):
    repo = repository()
    summary = run(repo, [remote(4100)], tmp_path, max_products=0)
    assert summary["blocked"] and stored(repo) == []


def test_missing_table_blocks_the_write(tmp_path):
    class Missing(FakeSupabaseClient):
        def table(self, name):
            if name == lpm.TABLE:
                raise RuntimeError("{'code': 'PGRST205', 'message': \"Could not find the table "
                                   f"'public.{name}'\"}}")
            return super().table(name)
    repo = repository()
    repo.client = Missing(repo.client.tables)
    summary = run(repo, [remote(4100)], tmp_path)
    assert summary["table_state"] == "NOT_APPLIED" and "Migration 011" in summary["blocked"]
    assert outcome(summary, LIVE)["outcome"] == bf.INSERT  # the dry-run plan still works


# --- removed group ----------------------------------------------------------

def test_removed_book_becomes_a_terminal_tombstone_and_nothing_is_restored(tmp_path):
    repo = repository()
    reader_catalog = [remote(4100)]
    summary = run(repo, reader_catalog, tmp_path, group=bf.GROUP_REMOVED)
    assert outcome(summary, GONE)["outcome"] == bf.INSERT and outcome(summary, LIVE)["outcome"] == bf.OTHER_GROUP
    [row] = stored(repo)
    assert (row["internal_product_id"], row["mapping_status"], row["woocommerce_product_id"], row["sync_id"]) == (
        IP_GONE, lpm.REMOTE_REMOVED, 3700, "sync-3700")
    # The removal evidence stays in the untouched sync row.
    sync = next(s for s in repo.client.tables["woocommerce_product_syncs"] if s["sync_id"] == "sync-3700")
    assert sync["response_payload"]["remote_removal"]["recreate"] is False
    assert lpm.adapter_inputs([lpm.LanguageProduct.from_row(row, GONE)]).blocked_languages == ("vi",)


def test_removed_book_live_again_is_isolated_for_the_owner(tmp_path):
    repo = repository()
    summary = run(repo, [remote(3700, sku=GONE, owner=IP_GONE)], tmp_path, group=bf.GROUP_REMOVED)
    decision = outcome(summary, GONE)
    assert decision["outcome"] == bf.ISOLATED and lpm.REMOTE_PRESENT_AFTER_REMOVAL in decision["reason"]
    assert stored(repo) == []


# --- write failures and rollback --------------------------------------------

def test_failed_insert_stops_and_is_never_retried(tmp_path):
    repo = repository()
    calls = []

    class Failing(FakeSupabaseClient):
        def table(self, name):
            builder = super().table(name)
            if name == lpm.TABLE:
                original = builder.insert

                def insert(payload):
                    calls.append(payload)
                    raise TimeoutError("connection reset")
                builder.insert = insert
                del original
            return builder
    repo.client = Failing(repo.client.tables)
    summary = run(repo, [remote(4100)], tmp_path)
    assert summary["checkpoint"]["status"] == "STOPPED" and "Not retried" in summary["checkpoint"]["stop_reason"]
    assert len(calls) == 1


def test_row_appearing_between_plan_and_write_stops_the_run(tmp_path):
    repo = repository()
    decisions = bf.plan_backfill(allowlist=[LIVE], group=bf.GROUP_RECORDED,
                                 derived=[lpm.derive_vi_mapping(sync_row(LIVE, IP_LIVE, 4100),
                                                                {"internal_product_id": IP_LIVE,
                                                                 "product_code": LIVE})[0]],
                                 derive_findings=[], stored=[], catalog={4100: remote(4100)})
    stored(repo).append({**decisions[0]["row"], "woocommerce_product_id": 1, "language_product_id": "z"})
    (tmp_path / "r").mkdir()
    checkpoint = bf.apply_plan(repo, decisions, run_dir=tmp_path / "r", group=bf.GROUP_RECORDED, batch_size=5)
    assert checkpoint["status"] == "STOPPED" and len(stored(repo)) == 1


def test_rollback_deletes_only_the_runs_unchanged_rows(tmp_path):
    repo = repository()
    run(repo, [remote(4100)], tmp_path, name="first")
    run(repo, [], tmp_path, group=bf.GROUP_REMOVED, name="second")
    assert len(stored(repo)) == 2

    preview = bf.rollback(repo, tmp_path / "first", confirm=False)
    assert preview["deleted"] == [LIVE] and len(stored(repo)) == 2

    gone_row = next(r for r in stored(repo) if r["internal_product_id"] == IP_GONE)
    gone_row["mapping_status"] = lpm.CONFLICT          # changed after the insert: kept
    assert bf.rollback(repo, tmp_path / "second", confirm=True)["kept"] == [GONE]
    assert bf.rollback(repo, tmp_path / "first", confirm=True)["deleted"] == [LIVE]
    assert [r["internal_product_id"] for r in stored(repo)] == [IP_GONE]


# --- allowlist and source safety ---------------------------------------------

def test_allowlist_from_audit_report_and_text_file(tmp_path):
    report = tmp_path / "audit.json"
    report.write_text(json.dumps({"backfill_plan": [{"product_code": LIVE}, {"product_code": GONE}]}),
                      encoding="utf-8")
    assert bf.load_allowlist(report) == [LIVE, GONE]
    text = tmp_path / "codes.txt"
    text.write_text(f"# batch\n{LIVE}\n{LIVE}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        bf.load_allowlist(text)
    with pytest.raises(ValueError):
        (tmp_path / "bad.json").write_text("{}", encoding="utf-8")
        bf.load_allowlist(tmp_path / "bad.json")


def test_catalog_read_is_get_only_and_backfill_never_writes_woocommerce(tmp_path):
    reader = Reader([remote(4100)])
    bf.run(repository(), reader, allowlist=[LIVE], group=bf.GROUP_RECORDED, max_products=5, confirm=False,
           batch_size=1, run_dir=tmp_path / "r")
    assert all(call["status"] == "any" for call in reader.calls)
    source = (PROJECT_ROOT / "scripts/backfill_language_product_mapping.py").read_text(encoding="utf-8")
    for verb in ("requests.post", "requests.put", "requests.delete", "requests.patch", "execute_sql",
                 ".upsert(", ").update(", "regular_price", "\"publish\""):
        assert verb not in source, verb
    assert ".insert(" in source and source.count(".delete()") == 1  # mapping table only
