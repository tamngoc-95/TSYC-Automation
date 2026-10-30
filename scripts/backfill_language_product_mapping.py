"""
Backfill the VI rows of woocommerce_language_products from TSYC's own
sync records (migrations/011_create_woocommerce_language_products.sql,
src/domain/language_product_mapping.py).

Writes Supabase only, never WordPress/WooCommerce (catalog read is GET).

Every row is re-derived at run time; the allowlist only bounds which books
may be touched. Two groups, run separately (--group):
  recorded  RECORDED rows (TSYC_CREATED, origin = the sync row's own create
            request). Inserted only when the live product still carries the
            book's SKU, TSYC's meta for this book and reads as vi.
  removed   REMOTE_REMOVED tombstones (schema status "terminal: never
            recreated"). Inserted only when the product is absent from the
            catalog read. The removal evidence stays in the sync row the
            tombstone points at; nothing is restored or recreated.

A book is isolated (no row) when any audit finding about it is an ERROR
(copied ownership meta, SKU/language/meta mismatch, ...), when its remote
state contradicts the record, or when a stored row differs. A shared SKU
(EN/DE copies) is never linked. Ownership never comes from remote meta.

Each batch is one INSERT (atomic), re-read and compared, then checkpointed
with the inserted row IDs. A re-run is idempotent: identical stored rows
are reused, never rewritten. Nothing is retried after a failed insert: the
next run re-reads and reconciles. --rollback deletes exactly the rows one
run inserted, and only those still unchanged.

Dry run unless --confirm-insert (or --confirm-rollback).

Usage:
  .venv/Scripts/python.exe scripts/backfill_language_product_mapping.py \
      --allowlist-from-audit data/output/language_mapping/<audit>.json --group recorded --max-products 90
  ... --confirm-insert
  .venv/Scripts/python.exe scripts/backfill_language_product_mapping.py --rollback <run_dir> [--confirm-rollback]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import audit_language_product_mapping as audit_cli  # noqa: E402
from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain import language_product_mapping as lpm  # noqa: E402
from src.domain import woo_translation_group as wtg  # noqa: E402

PROCESS_NAME = "language_mapping_backfill"
RUN_ROOT = audit_cli.REPORT_DIR / "backfill"
DEFAULT_BATCH_SIZE = 10

GROUP_RECORDED = "recorded"
GROUP_REMOVED = "removed"
GROUP_STATUS = {GROUP_RECORDED: lpm.RECORDED, GROUP_REMOVED: lpm.REMOTE_REMOVED}

# Outcomes per allowlisted book.
INSERT = "INSERT"
REUSED = "REUSED"
ISOLATED = "ISOLATED"
OTHER_GROUP = "OTHER_GROUP"
NOT_DERIVED = "NOT_DERIVED"

# Non-ERROR findings that still contradict the record.
REFUSING_CODES = frozenset({
    lpm.REMOTE_NOT_READ,
    lpm.OWNER_META_MISSING,
    lpm.REMOTE_PRESENT_AFTER_REMOVAL,
    lpm.RECOVERY_REQUIRED,
})
LANGUAGE_UNVERIFIED = "LANGUAGE_UNVERIFIED"
STORED_CONFLICT = "STORED_CONFLICT"
# Informational: the owner has worked on the product since TSYC created it.
OWNER_MAINTAINED_CODES = frozenset({lpm.CHANGED_SINCE_BASELINE, lpm.REMOTE_PUBLISHED})
COMPARED_FIELDS = ("internal_product_id", "language_code", "woocommerce_product_id", "origin",
                   "mapping_status", "sync_id")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_allowlist(path: Path) -> list[str]:
    """Product codes of an audit report's backfill_plan, or a plain list."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        report = json.loads(text)
        if "backfill_plan" not in report:
            raise ValueError(f"{path} has no backfill_plan (run the audit with --plan-backfill).")
        codes = [str(r["product_code"]) for r in report["backfill_plan"]]
    else:
        codes = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
    if len(codes) != len(set(codes)):
        raise ValueError(f"{path} lists a product code twice.")
    return codes


# ---------------------------------------------------------------------
# Plan (pure)
# ---------------------------------------------------------------------


def plan_backfill(
    *,
    allowlist: Sequence[str],
    group: str,
    derived: Sequence[lpm.LanguageProduct],
    derive_findings: Sequence[lpm.Finding],
    stored: Sequence[lpm.LanguageProduct],
    catalog: Mapping[int, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """One decision per allowlisted book. Only INSERT entries are written."""
    findings = list(derive_findings) + lpm.validate_mappings(derived) + lpm.verify_remote(derived, catalog)
    by_code: dict[str, list[lpm.Finding]] = collections.defaultdict(list)
    for finding in findings:
        by_code[finding.product_code].append(finding)
    rows = {r.product_code: r for r in derived}
    stored_vi = {r.internal_product_id: r for r in stored if r.language_code == wtg.SOURCE_LANGUAGE}
    stored_ids = {r.woocommerce_product_id: r for r in stored if r.woocommerce_product_id is not None}

    decisions = []
    for code in allowlist:
        row = rows.get(code)
        book = by_code.get(code, [])
        notes = sorted({f.code for f in book})
        entry: dict[str, Any] = {"product_code": code, "findings": notes}
        decisions.append(entry)
        if row is None:
            entry.update(outcome=NOT_DERIVED, reason="No VI mapping derivable from the sync record.")
            continue
        entry.update(woocommerce_product_id=row.woocommerce_product_id, mapping_status=row.mapping_status)
        if row.mapping_status != GROUP_STATUS[group]:
            entry.update(outcome=OTHER_GROUP)
            continue

        reasons = [f"{f.severity} {f.code}: {f.detail}" for f in book
                   if f.severity == lpm.ERROR or f.code in REFUSING_CODES]
        if row.mapping_status == lpm.RECORDED:
            shown = wtg.product_language(catalog.get(row.woocommerce_product_id) or {})[0]
            if shown != wtg.SOURCE_LANGUAGE:
                reasons.append(f"{LANGUAGE_UNVERIFIED}: #{row.woocommerce_product_id} reads as {shown!r}, not vi.")
        current = stored_vi.get(row.internal_product_id)
        holder = stored_ids.get(row.woocommerce_product_id)
        if current is not None and any(getattr(current, f) != getattr(row, f) for f in COMPARED_FIELDS):
            reasons.append(f"{STORED_CONFLICT}: a different VI row is already stored for this book.")
        elif holder is not None and holder.internal_product_id != row.internal_product_id:
            reasons.append(f"{STORED_CONFLICT}: #{row.woocommerce_product_id} is stored for {holder.product_code}.")
        if reasons:
            entry.update(outcome=ISOLATED, reason=" | ".join(reasons))
        elif current is not None:
            entry.update(outcome=REUSED)
        else:
            entry.update(outcome=INSERT, owner_maintained=bool(OWNER_MAINTAINED_CODES & set(notes)),
                         row=row.as_row())
    return decisions


# ---------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def _same(stored_row: Mapping[str, Any], planned: Mapping[str, Any]) -> bool:
    for name, value in planned.items():
        current = stored_row.get(name)
        if name == "woocommerce_product_id" and current is not None:
            current = int(current)
        if current != value:
            return False
    return True


def _stored_vi(repository: Any, ipids: Sequence[str]) -> dict[str, dict[str, Any]]:
    rows = (repository.client.table(lpm.TABLE).select("*")
            .in_("internal_product_id", list(ipids)).eq("language_code", wtg.SOURCE_LANGUAGE)
            .execute().data or [])
    return {str(r["internal_product_id"]): r for r in rows}


def apply_plan(repository: Any, decisions: Sequence[dict[str, Any]], *, run_dir: Path, group: str,
               batch_size: int) -> dict[str, Any]:
    """Insert the INSERT decisions serially, batch by batch. Stops at the
    first batch that cannot be verified; never retries it."""
    pending = [d for d in decisions if d["outcome"] == INSERT]
    checkpoint: dict[str, Any] = {"group": group, "started_at": utc_now(), "batches": [], "inserted": [],
                                  "reused_at_write": [], "status": "IN_PROGRESS"}
    path = run_dir / "checkpoint.json"
    _write_json(path, checkpoint)
    for start in range(0, len(pending), batch_size):
        batch = pending[start:start + batch_size]
        codes = [d["product_code"] for d in batch]
        # Fresh re-read just before the write: a row that appeared since
        # planning is reused when identical, otherwise the run stops.
        present = _stored_vi(repository, [d["row"]["internal_product_id"] for d in batch])
        to_insert = []
        for decision in batch:
            existing = present.get(decision["row"]["internal_product_id"])
            if existing is None:
                to_insert.append(decision)
            elif _same(existing, decision["row"]):
                checkpoint["reused_at_write"].append(decision["product_code"])
            else:
                checkpoint["status"] = "STOPPED"
                checkpoint["stop_reason"] = f"{decision['product_code']}: a different row appeared before the write."
                _write_json(path, checkpoint)
                return checkpoint
        if not to_insert:
            continue
        try:
            returned = (repository.client.table(lpm.TABLE).insert([d["row"] for d in to_insert])
                        .execute().data or [])
        except Exception as error:  # noqa: BLE001 - outcome unknown: reconcile, never retry
            checkpoint["status"] = "STOPPED"
            checkpoint["stop_reason"] = (f"Insert of batch {codes} failed ({type(error).__name__}: {error}). "
                                         "Not retried: the next run re-reads and reuses what landed.")
            _write_json(path, checkpoint)
            return checkpoint
        verified = _stored_vi(repository, [d["row"]["internal_product_id"] for d in to_insert])
        bad = [d["product_code"] for d in to_insert
               if d["row"]["internal_product_id"] not in verified
               or not _same(verified[d["row"]["internal_product_id"]], d["row"])]
        if len(returned) != len(to_insert) or bad:
            checkpoint["status"] = "STOPPED"
            checkpoint["stop_reason"] = f"Batch {codes} did not verify (returned {len(returned)}, mismatched {bad})."
            _write_json(path, checkpoint)
            return checkpoint
        inserted = [{"product_code": d["product_code"],
                     "language_product_id": str(verified[d["row"]["internal_product_id"]]["language_product_id"]),
                     "updated_at": verified[d["row"]["internal_product_id"]].get("updated_at"),
                     "row": d["row"]} for d in to_insert]
        checkpoint["inserted"].extend(inserted)
        checkpoint["batches"].append({"index": len(checkpoint["batches"]), "product_codes": codes,
                                      "inserted": len(inserted), "at": utc_now()})
        _write_json(path, checkpoint)
        repository.write_process_log(
            message=f"Backfilled {len(inserted)} VI language mapping row(s) ({group}).",
            process_name=PROCESS_NAME, process_step="INSERT_BATCH", log_level="INFO", status="INSERTED",
            error_details={"run_dir": run_dir.name, "group": group,
                           "rows": [{k: i[k] for k in ("product_code", "language_product_id")} for i in inserted]},
        )
    checkpoint["status"] = "COMPLETED"
    checkpoint["completed_at"] = utc_now()
    _write_json(path, checkpoint)
    return checkpoint


def rollback(repository: Any, run_dir: Path, *, confirm: bool) -> dict[str, Any]:
    """Delete exactly the rows the run inserted, each only if unchanged
    since the insert (same values and updated_at)."""
    checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
    result: dict[str, Any] = {"deleted": [], "kept": [], "missing": []}
    for item in checkpoint.get("inserted", []):
        rows = (repository.client.table(lpm.TABLE).select("*")
                .eq("language_product_id", item["language_product_id"]).execute().data or [])
        if not rows:
            result["missing"].append(item["product_code"])
            continue
        if not _same(rows[0], item["row"]) or rows[0].get("updated_at") != item["updated_at"]:
            result["kept"].append(item["product_code"])
            continue
        if confirm:
            deleted = (repository.client.table(lpm.TABLE).delete()
                       .eq("language_product_id", item["language_product_id"])
                       .eq("updated_at", item["updated_at"]).execute().data or [])
            if len(deleted) != 1:
                result["kept"].append(item["product_code"])
                continue
        result["deleted"].append(item["product_code"])
    if confirm and result["deleted"]:
        repository.write_process_log(
            message=f"Rolled back {len(result['deleted'])} VI language mapping row(s) of run {run_dir.name}.",
            process_name=PROCESS_NAME, process_step="ROLLBACK", log_level="INFO", status="ROLLED_BACK",
            error_details={"run_dir": run_dir.name, **result},
        )
    return result


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------


def run(repository: Any, reader: Any, *, allowlist: Sequence[str], group: str, max_products: int,
        confirm: bool, batch_size: int, run_dir: Path) -> dict[str, Any]:
    syncs, products = audit_cli.load_local(repository)
    derived, derive_findings = audit_cli.derive(syncs, products)
    table_state, stored = audit_cli.load_stored(repository, products)
    catalog = audit_cli.read_catalog(reader)
    decisions = plan_backfill(allowlist=allowlist, group=group, derived=derived,
                              derive_findings=derive_findings, stored=stored, catalog=catalog)
    counts = collections.Counter(d["outcome"] for d in decisions)
    summary: dict[str, Any] = {
        "run": run_dir.name, "checked_at": utc_now(), "group": group, "table_state": table_state,
        "allowlist": len(allowlist), "catalog_products": len(catalog), "outcomes": dict(counts),
        "owner_maintained": sum(1 for d in decisions if d.get("owner_maintained")),
        "confirm": confirm, "decisions": decisions,
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    wanted = {d["row"]["internal_product_id"] for d in decisions if d["outcome"] in (INSERT,)}
    _write_json(run_dir / "before_snapshot.json", {
        "taken_at": utc_now(),
        "stored_rows": [r.as_row() for r in stored],
        "sync_rows": [s for s in syncs if str(s.get("internal_product_id")) in wanted],
        "remote": {str(d["woocommerce_product_id"]): catalog.get(d["woocommerce_product_id"])
                   for d in decisions if d["outcome"] == INSERT},
    })
    _write_json(run_dir / "plan.json", summary)
    if counts[INSERT] > max_products:
        summary["blocked"] = f"{counts[INSERT]} rows to insert, above --max-products {max_products}."
    elif confirm and table_state != audit_cli.TABLE_PRESENT:
        summary["blocked"] = "Migration 011 is not applied: nothing can be written."
    elif confirm:
        summary["checkpoint"] = apply_plan(repository, decisions, run_dir=run_dir, group=group,
                                           batch_size=batch_size)
    _write_json(run_dir / "plan.json", summary)
    return summary


def print_summary(summary: Mapping[str, Any]) -> None:
    print("=" * 78)
    print(f"TSYC VI LANGUAGE MAPPING BACKFILL ({'WRITE' if summary['confirm'] else 'dry run'}) "
          f"group={summary['group']}")
    print("=" * 78)
    print(f"Table: {summary['table_state']}  allowlist: {summary['allowlist']}  "
          f"catalog read: {summary['catalog_products']} products (GET only)")
    print(f"Outcomes: {summary['outcomes']}  (owner-maintained among inserts: {summary['owner_maintained']})")
    for decision in summary["decisions"]:
        if decision["outcome"] in (ISOLATED, NOT_DERIVED):
            print(f"- {decision['outcome']} {decision['product_code']}: {decision.get('reason')}")
    if summary.get("blocked"):
        print(f"BLOCKED: {summary['blocked']}")
    checkpoint = summary.get("checkpoint")
    if checkpoint:
        print(f"Write: {checkpoint['status']}, inserted {len(checkpoint['inserted'])} row(s) in "
              f"{len(checkpoint['batches'])} batch(es), reused at write {len(checkpoint['reused_at_write'])}.")
        if checkpoint.get("stop_reason"):
            print(f"STOP: {checkpoint['stop_reason']}")
    print(f"Run directory: {RUN_ROOT / summary['run']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill VI rows of woocommerce_language_products.")
    parser.add_argument("--allowlist-from-audit", type=Path,
                        help="Audit JSON (its backfill_plan codes) or a text file of product codes.")
    parser.add_argument("--group", choices=sorted(GROUP_STATUS))
    parser.add_argument("--max-products", type=int, help="Hard upper bound on rows inserted.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--confirm-insert", action="store_true", help="Write. Without it: dry run.")
    parser.add_argument("--rollback", type=Path, help="Run directory whose inserted rows to delete.")
    parser.add_argument("--confirm-rollback", action="store_true")
    args = parser.parse_args(argv)

    load_dotenv(PROJECT_ROOT / ".env")
    from src.repositories.supabase_repository import SupabaseRepository

    if args.rollback:
        result = rollback(SupabaseRepository(), args.rollback, confirm=args.confirm_rollback)
        verb = "Deleted" if args.confirm_rollback else "Would delete"
        print(f"{verb} {len(result['deleted'])}; kept (changed) {result['kept']}; missing {result['missing']}.")
        return 1 if result["kept"] else 0
    if not (args.allowlist_from_audit and args.group and args.max_products is not None):
        parser.error("--allowlist-from-audit, --group and --max-products are required.")
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1.")

    import audit_woo_remote_status as wrs_cli
    run_dir = RUN_ROOT / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{args.group}"
    try:
        summary = run(SupabaseRepository(), wrs_cli.build_reader(os.environ),
                      allowlist=load_allowlist(args.allowlist_from_audit), group=args.group,
                      max_products=args.max_products, confirm=args.confirm_insert,
                      batch_size=args.batch_size, run_dir=run_dir)
    except Exception as error:  # noqa: BLE001 - never a clean result; not retried
        print(f"Backfill failed: {type(error).__name__}: {error}. Inserted rows (if any) are in "
              f"{run_dir / 'checkpoint.json'}; re-run the dry run to reconcile.", file=sys.stderr)
        return 2
    print_summary(summary)
    if summary.get("blocked"):
        return 1
    checkpoint = summary.get("checkpoint")
    return 1 if checkpoint and checkpoint["status"] != "COMPLETED" else 0


if __name__ == "__main__":
    configure_utf8_console()
    sys.exit(main())
