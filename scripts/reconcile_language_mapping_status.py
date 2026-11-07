"""
Move stored VI rows of woocommerce_language_products from RECORDED to
REMOTE_REMOVED when their own sync row was reconciled to a confirmed owner
removal after the backfill (src.domain.language_product_mapping
.plan_removal_transition).

Background (2026-11-07): audit_woo_remote_status.py --apply correctly
recorded the owner's trash of FAHASA-002-CAN-0013 #4928 and CAN-0014 #4930
on the sync rows; the mapping rows, written earlier by the insert-only
backfill, still said RECORDED (audit: STORED_DIFFERS_FROM_SYNC).

Writes Supabase only, one column (mapping_status) of exact rows. Never
WordPress/WooCommerce: the catalog read is GET, status any.

  - Exact --product-code list and --max-products; dry run unless
    --confirm-update. Holds the production writer lock.
  - Each row is re-planned from a fresh read just before its write, then
    updated compare-and-set on (language_product_id, RECORDED, Woo ID,
    updated_at), read back and checkpointed. A row that changed since the
    read, an update that does not return exactly one row, or a read-back
    that does not match stops the run. Nothing is retried.
  - A re-run is idempotent: an ALREADY_REMOVED row is reported, not written.
  - --rollback RUN_DIR --confirm-rollback restores RECORDED only for rows
    still exactly as this run left them.

Usage:
  .venv/Scripts/python.exe scripts/reconcile_language_mapping_status.py \
      --product-code TSYC-... [--product-code ...] --max-products N [--confirm-update]
  .venv/Scripts/python.exe scripts/reconcile_language_mapping_status.py --rollback <run_dir> [--confirm-rollback]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import audit_language_product_mapping as audit_cli  # noqa: E402
from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain import language_product_mapping as lpm  # noqa: E402
from src.domain import woo_translation_group as wtg  # noqa: E402

PROCESS_NAME = "language_mapping_status_reconcile"
RUN_ROOT = audit_cli.REPORT_DIR / "status_reconcile"

UPDATED = "UPDATED"
WOULD_UPDATE = "WOULD_UPDATE"
STOPPED = "STOPPED"
NOT_REACHED = "NOT_REACHED"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


@dataclass
class Context:
    repository: Any
    catalog: Mapping[int, Mapping[str, Any]]
    run_dir: Path
    confirm: bool
    log: Callable[..., Any] | None = None


def _load(repository: Any) -> tuple[dict[str, lpm.LanguageProduct], dict[str, lpm.LanguageProduct],
                                    dict[str, dict[str, Any]], list[lpm.LanguageProduct], dict[str, dict[str, Any]]]:
    """Fresh read: derived VI rows, stored VI rows (by product code), sync
    rows by id, every stored row, and raw stored rows by language_product_id."""
    syncs, products = audit_cli.load_local(repository)
    derived, _ = audit_cli.derive(syncs, products)
    table_state, stored = audit_cli.load_stored(repository, products)
    if table_state != audit_cli.TABLE_PRESENT:
        raise RuntimeError("woocommerce_language_products is not present.")
    raw = {str(r["language_product_id"]): r
           for r in (repository.client.table(lpm.TABLE).select("*").execute().data or [])}
    derived_by_code = {r.product_code: r for r in derived}
    stored_by_code = {r.product_code: r for r in stored if r.language_code == wtg.SOURCE_LANGUAGE}
    syncs_by_id = {str(s.get("sync_id")): s for s in syncs}
    return derived_by_code, stored_by_code, syncs_by_id, stored, raw


def _raw_for(raw: Mapping[str, Mapping[str, Any]], row: lpm.LanguageProduct) -> Mapping[str, Any] | None:
    for item in raw.values():
        if (str(item.get("internal_product_id")) == row.internal_product_id
                and item.get("language_code") == row.language_code):
            return item
    return None


def plan(ctx: Context, codes: Sequence[str]) -> list[dict[str, Any]]:
    derived, stored, syncs, all_stored, raw = _load(ctx.repository)
    decisions = []
    for code in codes:
        row = stored.get(code)
        sync = syncs.get(str(row.sync_id)) if row and row.sync_id else None
        remote = ctx.catalog.get(row.woocommerce_product_id) if row and row.woocommerce_product_id else None
        outcome, reason = lpm.plan_removal_transition(row, derived.get(code), sync, remote, all_stored)
        entry: dict[str, Any] = {"product_code": code, "outcome": outcome, "reason": reason}
        if row is not None:
            current = _raw_for(raw, row)
            entry.update(woocommerce_product_id=row.woocommerce_product_id,
                         language_product_id=str(current["language_product_id"]) if current else None,
                         before=dict(current) if current else None)
        decisions.append(entry)
    return decisions


def _read_row(repository: Any, language_product_id: str) -> dict[str, Any] | None:
    rows = (repository.client.table(lpm.TABLE).select("*")
            .eq("language_product_id", language_product_id).execute().data or [])
    return rows[0] if len(rows) == 1 else None


def apply(ctx: Context, codes: Sequence[str]) -> dict[str, Any]:
    checkpoint: dict[str, Any] = {"started_at": utc_now(), "status": "IN_PROGRESS", "products": {}}
    path = ctx.run_dir / "checkpoint.json"
    _write_json(path, checkpoint)
    for index, code in enumerate(codes):
        # Re-plan from a fresh read immediately before this one write.
        decision = plan(ctx, [code])[0]
        record: dict[str, Any] = {k: decision.get(k) for k in
                                  ("outcome", "reason", "woocommerce_product_id", "language_product_id", "before")}
        checkpoint["products"][code] = record
        if decision["outcome"] != lpm.TRANSITION_TO_REMOVED:
            record["result"] = decision["outcome"]
            _write_json(path, checkpoint)
            continue
        before = decision["before"]
        try:
            returned = (ctx.repository.client.table(lpm.TABLE)
                        .update({"mapping_status": lpm.REMOTE_REMOVED})
                        .eq("language_product_id", before["language_product_id"])
                        .eq("mapping_status", lpm.RECORDED)
                        .eq("woocommerce_product_id", before["woocommerce_product_id"])
                        .eq("updated_at", before["updated_at"])
                        .execute().data or [])
        except Exception as error:  # noqa: BLE001 - outcome unknown: stop, re-read next run
            record.update(result=STOPPED, detail=f"Update failed ({type(error).__name__}: {error}); not retried.")
            checkpoint.update(status=STOPPED, stop_reason=f"{code}: {record['detail']}")
            break
        after = _read_row(ctx.repository, before["language_product_id"])
        expected = {**before, "mapping_status": lpm.REMOTE_REMOVED}
        drift = [k for k in before if k != "updated_at" and (after or {}).get(k) != expected.get(k)]
        if len(returned) != 1 or after is None or drift:
            record.update(result=STOPPED, after=after,
                          detail=f"Read-back did not verify (returned {len(returned)}, differing {drift}).")
            checkpoint.update(status=STOPPED, stop_reason=f"{code}: {record['detail']}")
            break
        record.update(result=UPDATED, after=after)
        _write_json(path, checkpoint)
        if ctx.log:
            ctx.log(message=f"Language mapping {code} vi: RECORDED -> REMOTE_REMOVED (sync row confirms removal).",
                    process_name=PROCESS_NAME, process_step="TRANSITION", log_level="INFO", status="UPDATED",
                    error_details={"run_dir": ctx.run_dir.name, "product_code": code,
                                   "language_product_id": before["language_product_id"],
                                   "woocommerce_product_id": before["woocommerce_product_id"]})
    else:
        checkpoint["status"] = "COMPLETED"
    for code in codes:
        checkpoint["products"].setdefault(code, {"result": NOT_REACHED})
    checkpoint["finished_at"] = utc_now()
    _write_json(path, checkpoint)
    return checkpoint


def rollback(repository: Any, run_dir: Path, *, confirm: bool, log: Callable[..., Any] | None = None
             ) -> dict[str, list[str]]:
    """Restore RECORDED only where the row is still exactly what the run wrote."""
    checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
    result: dict[str, list[str]] = {"restored": [], "kept": [], "missing": []}
    for code, record in checkpoint.get("products", {}).items():
        if record.get("result") != UPDATED:
            continue
        after = record["after"]
        current = _read_row(repository, after["language_product_id"])
        if current is None:
            result["missing"].append(code)
            continue
        if current != after:
            result["kept"].append(code)
            continue
        if confirm:
            returned = (repository.client.table(lpm.TABLE)
                        .update({"mapping_status": lpm.RECORDED})
                        .eq("language_product_id", after["language_product_id"])
                        .eq("mapping_status", lpm.REMOTE_REMOVED)
                        .eq("updated_at", after["updated_at"])
                        .execute().data or [])
            if len(returned) != 1:
                result["kept"].append(code)
                continue
        result["restored"].append(code)
    if confirm and result["restored"] and log:
        log(message=f"Rolled back {len(result['restored'])} language mapping transition(s) of {run_dir.name}.",
            process_name=PROCESS_NAME, process_step="ROLLBACK", log_level="INFO", status="ROLLED_BACK",
            error_details={"run_dir": run_dir.name, **result})
    return result


def run(ctx: Context, codes: Sequence[str], max_products: int) -> dict[str, Any]:
    ctx.run_dir.mkdir(parents=True, exist_ok=True)
    decisions = plan(ctx, codes)
    eligible = [d for d in decisions if d["outcome"] == lpm.TRANSITION_TO_REMOVED]
    summary: dict[str, Any] = {"run": ctx.run_dir.name, "checked_at": utc_now(), "confirm": ctx.confirm,
                               "catalog_products": len(ctx.catalog), "decisions": decisions}
    _write_json(ctx.run_dir / "plan.json", summary)
    if len(eligible) > max_products:
        summary["blocked"] = f"{len(eligible)} transitions, above --max-products {max_products}."
    elif ctx.confirm and eligible:
        summary["checkpoint"] = apply(ctx, [d["product_code"] for d in eligible])
    _write_json(ctx.run_dir / "plan.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RECORDED -> REMOTE_REMOVED for VI mapping rows (exact codes).")
    parser.add_argument("--product-code", action="append", default=[])
    parser.add_argument("--max-products", type=int)
    parser.add_argument("--confirm-update", action="store_true", help="Write. Without it: dry run.")
    parser.add_argument("--rollback", type=Path, help="Run directory to restore.")
    parser.add_argument("--confirm-rollback", action="store_true")
    args = parser.parse_args(argv)

    load_dotenv(PROJECT_ROOT / ".env")
    from src.repositories.supabase_repository import SupabaseRepository
    from src.services.writer_lock import ProductionWriterLock, WriterLockBusy

    repository = SupabaseRepository()
    if args.rollback:
        with ProductionWriterLock(f"{PROCESS_NAME}/rollback"):
            result = rollback(repository, args.rollback, confirm=args.confirm_rollback,
                              log=repository.write_process_log)
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result["kept"] or result["missing"] else 0
    if not args.product_code or args.max_products is None or args.max_products < 1:
        parser.error("--product-code (repeatable) and --max-products >= 1 are required.")
    if len(set(args.product_code)) != len(args.product_code):
        parser.error("A product code is listed twice.")

    import audit_woo_remote_status as wrs_cli
    catalog = audit_cli.read_catalog(wrs_cli.build_reader(os.environ))
    ctx = Context(repository=repository, catalog=catalog, confirm=args.confirm_update,
                  run_dir=RUN_ROOT / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}",
                  log=repository.write_process_log)
    try:
        with ProductionWriterLock(f"{PROCESS_NAME}/apply"):
            summary = run(ctx, args.product_code, args.max_products)
    except WriterLockBusy as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 2

    print(f"LANGUAGE MAPPING STATUS RECONCILE ({'WRITE' if ctx.confirm else 'dry run'}); "
          f"catalog read {summary['catalog_products']} products (GET only)")
    for d in summary["decisions"]:
        print(f"- {d['outcome']:<22} {d['product_code']} #{d.get('woocommerce_product_id')}: {d['reason']}")
    if summary.get("blocked"):
        print(f"BLOCKED: {summary['blocked']}")
        return 1
    checkpoint = summary.get("checkpoint")
    if checkpoint:
        for code, record in checkpoint["products"].items():
            print(f"  {record.get('result'):<22} {code}")
        print(f"Write: {checkpoint['status']}" + (f" ({checkpoint['stop_reason']})" if checkpoint.get("stop_reason") else ""))
    print(f"Run directory: {ctx.run_dir}")
    return 1 if checkpoint and checkpoint["status"] != "COMPLETED" else 0


if __name__ == "__main__":
    configure_utf8_console()
    sys.exit(main())
