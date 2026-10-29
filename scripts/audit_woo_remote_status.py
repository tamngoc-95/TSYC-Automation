"""
Local-versus-remote WooCommerce status check for every TSYC-managed product,
with a dry-run repair plan and bounded LOCAL-ONLY reconciliation.

Modes (WooCommerce is only ever read -- GET requests, never a write):

  audit (default)  Read every woocommerce_product_syncs row, read the remote
                   products, classify each one (src/domain/woo_remote_status.py)
                   and print/save the report. No database write.

  --plan           Same as audit, plus the dry-run repair plan: which rows the
                   existing official writer may reconcile locally, and the
                   proposed correction for every other mismatch. No write.

  --apply          Local-only reconciliation of an explicit allowlist
                   (--product-code, repeatable) bounded by --max-products and
                   confirmed with --confirm-reconcile. For each product:
                   fresh re-read + re-classification (never a cached or plan
                   result), rollback snapshot of both local rows, then the
                   existing writer scripts/sync_woocommerce_product_status.py
                   (synchronize_one_product), then re-read and verify the
                   record is CONSISTENT. Only REMOTE_PUBLISHED, REMOTE_TRASHED,
                   REMOTE_REMOVED and LOCAL_STATUS_STALE qualify; every other
                   class is left unchanged. Takes the production writer lock,
                   checkpoints each product (--resume RUN_ID) and stops on a
                   systemic read failure.

  --rollback RUN_ID  Restore the snapshotted local columns of the listed
                   --product-code(s) from that run, only when the rows are
                   still exactly as the reconciliation left them.
                   Requires --confirm-rollback.

Never: a WooCommerce POST/PUT/DELETE, a price or publish change, re-creating
an owner-removed product, clearing an uncertain create, or inferring a
deletion from an HTTP error or a missing SKU.

Usage:
  .venv/Scripts/python.exe scripts/audit_woo_remote_status.py
  .venv/Scripts/python.exe scripts/audit_woo_remote_status.py --plan
  .venv/Scripts/python.exe scripts/audit_woo_remote_status.py --apply \\
      --product-code TSYC-... --max-products 5 --confirm-reconcile
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain import woo_remote_status as wrs  # noqa: E402
from src.services.writer_lock import ProductionWriterLock, WriterLockBusy  # noqa: E402

configure_utf8_console()

CHECKER_NAME = "woo_remote_status_check"
CHECKER_VERSION = "1.0.0"

BULK_CHUNK = 100
REPORT_DIR = PROJECT_ROOT / "data" / "output" / "woo_remote_status"
RUN_DIR = PROJECT_ROOT / "data" / "processed" / "orchestration" / "woo_remote_reconcile"
REMOTE_FIELDS = "id,status,sku,name,permalink,date_modified_gmt"

SYNC_COLUMNS = (
    "sync_id, internal_product_id, woocommerce_product_id, woocommerce_status, product_sku, "
    "response_payload, synced_at, last_attempt_at, updated_at"
)
# Columns the official writer may change; snapshotted for rollback.
SYNC_ROLLBACK_COLUMNS = (
    "woocommerce_status", "product_name", "product_permalink", "response_payload",
    "error_code", "error_message", "last_attempt_at", "updated_at",
)
PRODUCT_ROLLBACK_COLUMNS = (
    "woocommerce_status", "review_required", "review_reason", "product_metadata", "updated_at",
)

APPLIED = "RECONCILED"
SKIPPED_NOT_RECONCILABLE = "SKIPPED_NOT_RECONCILABLE"
SKIPPED_CHANGED = "SKIPPED_CHANGED_SINCE_CHECK"
VERIFY_FAILED = "POST_RECONCILE_VERIFICATION_FAILED"
WRITER_FAILED = "WRITER_FAILED"
ALREADY_DONE = "ALREADY_DONE"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RemoteReadError(RuntimeError):
    """The bulk remote read failed; remote state is unknown."""


class WooRemoteReader:
    """GET-only WooCommerce reader. One instance = one validation pass:
    SKU searches are memoised within it, and nothing persists beyond it."""

    def __init__(
        self,
        store_url: str,
        api_version: str,
        consumer_key: str,
        consumer_secret: str,
        timeout_seconds: int,
        http_get: Callable[..., Any] = requests.get,
    ) -> None:
        self.base = f"{store_url.rstrip('/')}/wp-json/{api_version.strip('/')}"
        self.auth = HTTPBasicAuth(consumer_key, consumer_secret)
        self.timeout = timeout_seconds
        self.http_get = http_get
        self.headers = {"Accept": "application/json", "User-Agent": f"{CHECKER_NAME}/{CHECKER_VERSION}"}
        self._sku_cache: dict[tuple[str, str], list[dict[str, Any]] | None] = {}

    def _get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self.http_get(f"{self.base}/{path}", auth=self.auth, params=dict(params or {}),
                             headers=self.headers, timeout=self.timeout)

    def get_many(self, ids: Sequence[int]) -> dict[int, dict[str, Any]]:
        """Products with status any (all but trash), by exact ID. Raises
        RemoteReadError when any page cannot be read."""
        found: dict[int, dict[str, Any]] = {}
        unique = sorted(set(int(i) for i in ids))
        for start in range(0, len(unique), BULK_CHUNK):
            chunk = unique[start:start + BULK_CHUNK]
            try:
                response = self._get("products", {
                    "include": ",".join(str(i) for i in chunk), "status": "any",
                    "per_page": BULK_CHUNK, "_fields": REMOTE_FIELDS,
                })
                body = response.json() if response.status_code == 200 else None
            except Exception as error:  # noqa: BLE001 - any failure means unknown state
                raise RemoteReadError(f"Bulk read failed: {type(error).__name__}: {error}") from error
            if not isinstance(body, list):
                raise RemoteReadError(f"Bulk read failed: HTTP {getattr(response, 'status_code', None)}.")
            for product in body:
                if isinstance(product, dict) and int(product.get("id", 0)) in chunk:
                    found[int(product["id"])] = product
        return found

    def list_page(self, params: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int]:
        """One page of /products and the total page count. Raises
        RemoteReadError when the page cannot be read."""
        try:
            response = self._get("products", params)
            body = response.json() if response.status_code == 200 else None
            pages = int(response.headers.get("X-WP-TotalPages") or 0)
        except Exception as error:  # noqa: BLE001 - any failure means unknown state
            raise RemoteReadError(f"Page read failed: {type(error).__name__}: {error}") from error
        if not isinstance(body, list):
            raise RemoteReadError(f"Page read failed: HTTP {getattr(response, 'status_code', None)}.")
        return [p for p in body if isinstance(p, dict)], pages

    def get_one(self, product_id: int) -> tuple[int | None, dict[str, Any] | None, str | None]:
        """(http_status, body, error). Never raises."""
        try:
            response = self._get(f"products/{product_id}", {"_fields": REMOTE_FIELDS})
        except Exception as error:  # noqa: BLE001
            return None, None, f"{type(error).__name__}: {error}"
        status = getattr(response, "status_code", None)
        if status == 200:
            try:
                body = response.json()
            except Exception as error:  # noqa: BLE001
                return None, None, f"Unreadable body: {type(error).__name__}"
            return (200, body, None) if isinstance(body, dict) else (None, None, "Unexpected body")
        return status, None, None if status == 404 else f"HTTP {status}"

    def find_by_sku(self, sku: str, status: str) -> list[dict[str, Any]] | None:
        """Exact-SKU products under one status filter; None = uncertain."""
        key = (sku, status)
        if key in self._sku_cache:
            return self._sku_cache[key]
        result: list[dict[str, Any]] | None
        try:
            response = self._get("products", {"sku": sku, "status": status, "per_page": 20, "_fields": REMOTE_FIELDS})
            body = response.json() if response.status_code == 200 else None
            result = [p for p in body if str(p.get("sku") or "") == sku] if isinstance(body, list) else None
        except Exception:  # noqa: BLE001
            result = None
        self._sku_cache[key] = result
        return result


def build_reader(env: Mapping[str, str], http_get: Callable[..., Any] = requests.get) -> WooRemoteReader:
    timeout = int((env.get("WOOCOMMERCE_TIMEOUT_SECONDS") or "30").strip())
    return WooRemoteReader(
        store_url=(env.get("WOOCOMMERCE_URL") or "").strip(),
        api_version=(env.get("WOOCOMMERCE_API_VERSION") or "wc/v3").strip(),
        consumer_key=(env.get("WOOCOMMERCE_CONSUMER_KEY") or "").strip(),
        consumer_secret=(env.get("WOOCOMMERCE_CONSUMER_SECRET") or "").strip(),
        timeout_seconds=timeout,
        http_get=http_get,
    )


# ---------------------------------------------------------------------
# Local records
# ---------------------------------------------------------------------


def _rows(query: Any) -> list[dict[str, Any]]:
    return query.execute().data or []


def load_local_records(repository: Any, product_codes: Iterable[str] | None = None) -> list[wrs.LocalRecord]:
    """Every sync row (optionally only those of exact product codes)."""
    products = _rows(repository.client.table("internal_products").select(
        "internal_product_id, product_code, candidate_id, woocommerce_status, title"))
    if product_codes is not None:
        wanted = set(product_codes)
        products = [p for p in products if p.get("product_code") in wanted]
    by_id = {str(p["internal_product_id"]): p for p in products}
    syncs = _rows(repository.client.table("woocommerce_product_syncs").select(SYNC_COLUMNS))
    syncs = [s for s in syncs if product_codes is None or str(s.get("internal_product_id")) in by_id]
    candidate_ids = sorted({str(p["candidate_id"]) for p in products if p.get("candidate_id")})
    candidates: dict[str, dict[str, Any]] = {}
    for start in range(0, len(candidate_ids), 200):
        for row in _rows(repository.client.table("product_candidates")
                         .select("candidate_id, identity_status")
                         .in_("candidate_id", candidate_ids[start:start + 200])):
            candidates[str(row["candidate_id"])] = row
    records = []
    for sync in sorted(syncs, key=lambda s: str(s.get("sync_id"))):
        product = by_id.get(str(sync.get("internal_product_id")))
        candidate = candidates.get(str((product or {}).get("candidate_id")))
        records.append(wrs.local_record_from_rows(sync, product, candidate))
    return records


# ---------------------------------------------------------------------
# Remote observation and classification
# ---------------------------------------------------------------------


def observe_one(record: wrs.LocalRecord, reader: WooRemoteReader,
                bulk: Mapping[int, dict[str, Any]] | None = None) -> wrs.RemoteObservation:
    """Observation for one record. A bulk hit is used when present; a
    product missing from the bulk read (trash, deleted, or not returned)
    is always re-read directly, and a 404/trash gets the exact-SKU
    searches the removal rule requires."""
    woo_id = int(record.woocommerce_product_id)
    if bulk is not None and woo_id in bulk:
        product = bulk[woo_id]
        if product.get("status") != "trash":
            return wrs.RemoteObservation(http_status=200, product=product, source="BULK")
    status, body, error = reader.get_one(woo_id)
    if status == 404 or (status == 200 and (body or {}).get("status") == "trash"):
        sku_any = reader.find_by_sku(record.sku, "any") if record.sku else []
        sku_trash = reader.find_by_sku(record.sku, "trash") if record.sku and status == 404 else []
        return wrs.RemoteObservation(http_status=status, product=body, error=error,
                                     sku_any=sku_any, sku_trash=sku_trash, source="DIRECT")
    return wrs.RemoteObservation(http_status=status, product=body, error=error, source="DIRECT")


@dataclass
class CheckReport:
    checked_at: str
    results: list[wrs.StatusResult]
    bulk_read_ok: bool
    systemic_failure: bool
    systemic_message: str
    remote_reads: int = 0
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def mismatches(self) -> list[wrs.StatusResult]:
        return [r for r in self.results if r.mismatch]

    def as_dict(self) -> dict[str, Any]:
        return {
            "checker": f"{CHECKER_NAME}/{CHECKER_VERSION}",
            "checked_at": self.checked_at,
            "records": len(self.results),
            "records_with_woo_id": sum(1 for r in self.results if r.woocommerce_product_id is not None),
            "bulk_read_ok": self.bulk_read_ok,
            "systemic_failure": self.systemic_failure,
            "systemic_message": self.systemic_message,
            "counts": self.counts,
            "mismatches": len(self.mismatches),
            "results": [r.as_dict() for r in self.results],
        }


def run_check(records: Sequence[wrs.LocalRecord], reader: WooRemoteReader) -> CheckReport:
    """One validation pass over the given records (read-only)."""
    with_id = [r for r in records if r.woocommerce_product_id is not None]
    try:
        bulk: dict[int, dict[str, Any]] | None = reader.get_many([r.woocommerce_product_id for r in with_id])
        bulk_ok = True
    except RemoteReadError as error:
        bulk, bulk_ok = None, False
        bulk_error = str(error)
    results: list[wrs.StatusResult] = []
    for record in records:
        if record.woocommerce_product_id is None:
            results.append(wrs.classify(record, None))
            continue
        if not bulk_ok:
            results.append(wrs.classify(record, wrs.RemoteObservation(http_status=None, error=bulk_error, source="BULK")))
            continue
        # A recorded removal that the bulk read does not return is still
        # removed: no per-product re-read needed (a restore would appear
        # in the bulk read as a live product).
        if record.removal_state and int(record.woocommerce_product_id) not in bulk:
            results.append(wrs.classify(record, wrs.RemoteObservation(http_status=404, source="BULK_ABSENT")))
            continue
        results.append(wrs.classify(record, observe_one(record, reader, bulk)))
    systemic, message = wrs.assess_systemic(results, bulk_ok)
    counts = dict(collections.Counter(r.classification for r in results))
    return CheckReport(utc_now(), results, bulk_ok, systemic, message, counts=counts)


def build_plan(report: CheckReport) -> dict[str, Any]:
    """Dry-run repair plan. Nothing is written by building it."""
    reconcile = [r for r in report.results if r.action == wrs.ACTION_LOCAL_RECONCILE]
    proposals = [r for r in report.results if r.action == wrs.ACTION_PROPOSE]
    reread = [r for r in report.results if r.action == wrs.ACTION_REREAD]
    return {
        "planned_at": report.checked_at,
        "systemic_failure": report.systemic_failure,
        "local_reconcile": [
            {"product_code": r.product_code, "woocommerce_product_id": r.woocommerce_product_id,
             "classification": r.classification, "writer": "scripts/sync_woocommerce_product_status.py",
             "effect": _effect(r.classification), "reason": r.reason}
            for r in reconcile
        ],
        "proposals_left_unchanged": [
            {"product_code": r.product_code, "woocommerce_product_id": r.woocommerce_product_id,
             "classification": r.classification, "reason": r.reason, "proposed_correction": r.proposed_correction}
            for r in proposals
        ],
        "reread_later": [
            {"product_code": r.product_code, "woocommerce_product_id": r.woocommerce_product_id, "reason": r.reason}
            for r in reread
        ],
    }


def _effect(classification: str) -> str:
    return {
        wrs.REMOTE_PUBLISHED: "internal_products.woocommerce_status DRAFT_CREATED -> PUBLISHED (sync row stays DRAFT_CREATED)",
        wrs.REMOTE_TRASHED: "both rows FAILED + remote_removal marker REMOTE_TRASHED (terminal, never recreated)",
        wrs.REMOTE_REMOVED: "both rows FAILED + remote_removal marker REMOTE_DELETED (terminal, never recreated)",
        wrs.LOCAL_STATUS_STALE: "internal_products.woocommerce_status -> DRAFT_CREATED (matches its sync row and the remote draft)",
    }.get(classification, "none")


# ---------------------------------------------------------------------
# Local-only reconciliation
# ---------------------------------------------------------------------


def _single(rows: list[dict[str, Any]], what: str) -> dict[str, Any]:
    if len(rows) != 1:
        raise RuntimeError(f"Expected exactly one {what}, found {len(rows)}.")
    return rows[0]


def snapshot_rows(repository: Any, record: wrs.LocalRecord) -> dict[str, Any]:
    sync = _single(_rows(repository.client.table("woocommerce_product_syncs").select("*")
                         .eq("sync_id", record.sync_id)), "sync row")
    product = _single(_rows(repository.client.table("internal_products").select("*")
                            .eq("internal_product_id", record.internal_product_id)), "internal product")
    return {
        "sync_id": record.sync_id,
        "internal_product_id": record.internal_product_id,
        "sync": {c: sync.get(c) for c in SYNC_ROLLBACK_COLUMNS},
        "product": {c: product.get(c) for c in PRODUCT_ROLLBACK_COLUMNS},
    }


@dataclass
class ApplyContext:
    repository: Any
    reader_factory: Callable[[], WooRemoteReader]
    sync_writer: Callable[[Any, dict[str, Any]], bool]
    sync_record_loader: Callable[[Any, str], dict[str, Any]]
    run_dir: Path
    log: Callable[..., Any] | None = None


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def load_checkpoint(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "checkpoint.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"products": {}}


def fresh_result(ctx: ApplyContext, product_code: str) -> tuple[wrs.LocalRecord, wrs.StatusResult]:
    """Re-read the local rows and the remote product NOW (new reader: no
    cache from an earlier pass is ever reused before a write)."""
    records = [r for r in load_local_records(ctx.repository, [product_code]) if r.woocommerce_product_id is not None]
    record = _single(records, f"sync row with a Woo ID for {product_code}")
    reader = ctx.reader_factory()
    return record, wrs.classify(record, observe_one(record, reader, None))


def reconcile_one(ctx: ApplyContext, product_code: str, checkpoint: dict[str, Any]) -> dict[str, Any]:
    done = checkpoint["products"].get(product_code)
    if done and done.get("result") in (APPLIED, ALREADY_DONE):
        return {"product_code": product_code, "result": ALREADY_DONE, "detail": "completed in this run earlier"}

    record, before = fresh_result(ctx, product_code)
    report: dict[str, Any] = {"product_code": product_code, "woocommerce_product_id": record.woocommerce_product_id,
                              "classification_before": before.classification}
    if before.classification == wrs.CONSISTENT:
        report.update(result=ALREADY_DONE, detail="already consistent")
        return report
    if before.classification == wrs.API_ACCESS_ERROR:
        report.update(result=SKIPPED_CHANGED, detail=before.reason, api_error=True)
        return report
    if before.classification not in wrs.LOCAL_RECONCILABLE:
        report.update(result=SKIPPED_NOT_RECONCILABLE, detail=f"{before.classification}: {before.reason}")
        return report

    snapshot = snapshot_rows(ctx.repository, record)
    _write_json(ctx.run_dir / "rollback" / f"{record.sync_id}.json", {
        "product_code": product_code, "taken_at": utc_now(), "classification": before.classification,
        "before": snapshot,
    })

    try:
        sync_record = ctx.sync_record_loader(ctx.repository, product_code)
        ctx.sync_writer(ctx.repository, sync_record)
    except Exception as error:  # noqa: BLE001 - recorded, never retried
        report.update(result=WRITER_FAILED, detail=f"{type(error).__name__}: {error}")
        return report

    record_after, after = fresh_result(ctx, product_code)
    after_snapshot = snapshot_rows(ctx.repository, record_after)
    rollback_path = ctx.run_dir / "rollback" / f"{record.sync_id}.json"
    stored = json.loads(rollback_path.read_text(encoding="utf-8"))
    stored["after"] = after_snapshot
    _write_json(rollback_path, stored)

    report["classification_after"] = after.classification
    if after.classification != wrs.CONSISTENT:
        report.update(result=VERIFY_FAILED, detail=after.reason)
        return report
    report.update(result=APPLIED, detail=_effect(before.classification))

    if ctx.log is not None:
        try:
            ctx.log(
                message=(f"Local Woo status reconciled for {product_code} (#{record.woocommerce_product_id}): "
                         f"{before.classification} -> CONSISTENT. Remote unchanged; no price, publish or re-create."),
                process_name=CHECKER_NAME, candidate_id=record.candidate_id,
                process_step="LOCAL_REMOTE_STATUS_RECONCILE", log_level="INFO", status=APPLIED,
                error_details={"woocommerce_product_id": record.woocommerce_product_id, "sku": record.sku,
                               "classification": before.classification, "evidence": before.evidence,
                               "rollback_file": _display_path(rollback_path)},
            )
        except Exception as error:  # noqa: BLE001 - audit failure must not mask the result
            report["log_warning"] = f"{type(error).__name__}: {error}"
    return report


def run_apply(ctx: ApplyContext, product_codes: Sequence[str], max_products: int) -> list[dict[str, Any]]:
    if len(product_codes) > max_products:
        raise RuntimeError(f"{len(product_codes)} product codes exceed --max-products {max_products}.")
    checkpoint = load_checkpoint(ctx.run_dir)
    reports: list[dict[str, Any]] = []
    consecutive_api_errors = 0
    for code in product_codes:
        report = reconcile_one(ctx, code, checkpoint)
        reports.append(report)
        checkpoint["products"][code] = {k: report.get(k) for k in ("result", "detail", "classification_before",
                                                                   "classification_after")}
        checkpoint["updated_at"] = utc_now()
        _write_json(ctx.run_dir / "checkpoint.json", checkpoint)
        consecutive_api_errors = consecutive_api_errors + 1 if report.get("api_error") else 0
        if consecutive_api_errors >= wrs.SYSTEMIC_MIN_ERRORS:
            reports.append({"result": "STOPPED", "detail": "Systemic remote read failure; no further products."})
            break
        if report["result"] in (WRITER_FAILED, VERIFY_FAILED):
            reports.append({"result": "STOPPED", "detail": f"{code}: {report['result']}; stopping for investigation."})
            break
    return reports


def run_rollback(repository: Any, run_dir: Path, product_codes: Sequence[str]) -> list[dict[str, Any]]:
    """Restore the 'before' snapshot only while the rows still equal the
    'after' snapshot (nothing changed them since)."""
    results = []
    for code in product_codes:
        record = _single([r for r in load_local_records(repository, [code]) if r.woocommerce_product_id is not None],
                         f"sync row for {code}")
        path = run_dir / "rollback" / f"{record.sync_id}.json"
        if not path.exists():
            results.append({"product_code": code, "result": "NO_SNAPSHOT"})
            continue
        stored = json.loads(path.read_text(encoding="utf-8"))
        current = snapshot_rows(repository, record)
        if stored.get("after") != json.loads(json.dumps(current, default=str)):
            results.append({"product_code": code, "result": "REFUSED_CHANGED_SINCE_RECONCILE"})
            continue
        before = stored["before"]
        repository.client.table("woocommerce_product_syncs").update(before["sync"]).eq("sync_id", record.sync_id).execute()
        repository.client.table("internal_products").update(before["product"]).eq(
            "internal_product_id", record.internal_product_id).execute()
        results.append({"product_code": code, "result": "ROLLED_BACK"})
    return results


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------


def print_report(report: CheckReport) -> None:
    print("=" * 78)
    print("TSYC WOO REMOTE STATUS CHECK (read-only)")
    print("=" * 78)
    print(f"Checked at: {report.checked_at}")
    print(f"Records: {len(report.results)} "
          f"({sum(1 for r in report.results if r.woocommerce_product_id is not None)} with a Woo ID)")
    print(f"Bulk read: {'OK' if report.bulk_read_ok else 'FAILED'}")
    print(f"Systemic: {'FAILURE - ' if report.systemic_failure else ''}{report.systemic_message}")
    for name in wrs.ALL_CLASSIFICATIONS:
        if report.counts.get(name):
            print(f"  {name:<28} {report.counts[name]}")
    print()
    for r in report.mismatches:
        print(f"- {r.classification:<26} {r.product_code} #{r.woocommerce_product_id} "
              f"local {r.local_sync_status}/{r.local_product_status} remote {r.remote_status}: {r.reason}")


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local-versus-remote Woo status check and local-only reconciliation.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="Also build the dry-run repair plan (no write).")
    mode.add_argument("--apply", action="store_true", help="Local-only reconciliation of exact product codes.")
    mode.add_argument("--rollback", metavar="RUN_ID", help="Restore snapshots of an earlier --apply run.")
    parser.add_argument("--product-code", action="append", default=[], help="Exact product code (repeatable).")
    parser.add_argument("--max-products", type=int, default=0, help="Upper bound for --apply.")
    parser.add_argument("--confirm-reconcile", action="store_true", help="Required for --apply.")
    parser.add_argument("--confirm-rollback", action="store_true", help="Required for --rollback.")
    parser.add_argument("--resume", metavar="RUN_ID", help="Continue an interrupted --apply run.")
    parser.add_argument("--json-out", type=Path, help="Report path (default under data/output/woo_remote_status/).")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_arguments(argv)
    from src.repositories.supabase_repository import SupabaseRepository

    repository = SupabaseRepository()
    env = os.environ

    if args.rollback:
        if not args.confirm_rollback or not args.product_code:
            print("--rollback requires --product-code and --confirm-rollback.", file=sys.stderr)
            return 2
        with ProductionWriterLock(f"{CHECKER_NAME}/rollback"):
            results = run_rollback(repository, RUN_DIR / args.rollback, args.product_code)
        print(json.dumps(results, ensure_ascii=False, indent=1))
        return 0 if all(r["result"] == "ROLLED_BACK" for r in results) else 1

    if args.apply:
        if not args.confirm_reconcile or not args.product_code or args.max_products < 1:
            print("--apply requires --product-code, --max-products >= 1 and --confirm-reconcile.", file=sys.stderr)
            return 2
        import sync_woocommerce_product_status as ss

        run_id = args.resume or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        lookup = {
            "store_url": ss.get_required_environment_variable("WOOCOMMERCE_URL"),
            "api_version": env.get("WOOCOMMERCE_API_VERSION", "wc/v3").strip(),
            "consumer_key": ss.get_required_environment_variable("WOOCOMMERCE_CONSUMER_KEY"),
            "consumer_secret": ss.get_required_environment_variable("WOOCOMMERCE_CONSUMER_SECRET"),
            "timeout_seconds": ss.get_timeout_seconds(),
        }
        ctx = ApplyContext(
            repository=repository,
            reader_factory=lambda: build_reader(env),
            sync_writer=lambda repo, sync_record: ss.synchronize_one_product(
                repository=repo, sync_record=sync_record, **lookup),
            sync_record_loader=lambda repo, code: ss.get_sync_records(repository=repo, product_code=code)[0],
            run_dir=RUN_DIR / run_id,
            log=repository.write_process_log,
        )
        try:
            with ProductionWriterLock(f"{CHECKER_NAME}/apply"):
                reports = run_apply(ctx, args.product_code, args.max_products)
        except WriterLockBusy as error:
            print(f"Refused: {error}", file=sys.stderr)
            return 2
        print(f"Run: {run_id} ({_display_path(ctx.run_dir)})")
        print(json.dumps(reports, ensure_ascii=False, indent=1))
        bad = {WRITER_FAILED, VERIFY_FAILED, "STOPPED"}
        return 1 if any(r.get("result") in bad for r in reports) else 0

    records = load_local_records(repository, args.product_code or None)
    report = run_check(records, build_reader(env))
    print_report(report)
    out = args.json_out or REPORT_DIR / f"remote_status_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    data = report.as_dict()
    if args.plan:
        data["plan"] = build_plan(report)
        plan = data["plan"]
        print()
        print(f"Plan: {len(plan['local_reconcile'])} local reconciliation(s) by the official writer, "
              f"{len(plan['proposals_left_unchanged'])} proposal(s) left unchanged, "
              f"{len(plan['reread_later'])} to re-read. Nothing was written.")
    _write_json(out, data)
    print(f"JSON report: {out}")
    return 1 if report.systemic_failure else 0


if __name__ == "__main__":
    sys.exit(main())
