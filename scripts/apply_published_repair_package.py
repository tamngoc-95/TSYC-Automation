"""
Apply an owner-authorized published-product repair package (option A).

The package (package_v2.json, built read-only from the live store) names,
per published product, the exact rendered fragments to remove (duplicated
author lines) and the "Tác giả" attribute to add. For each authorized Woo ID:

  1. reconcile: the checkpoint says APPLIED -> skip. Otherwise read the
     product (view and edit context). Exact id, SKU, name and status
     "publish" are required. The rendered description must still match the
     package baseline (sha256 + date_modified): any owner edit made after the
     package was built isolates the product (OWNER_EDITED, no write). If the
     product already carries the exact repaired result, it is recorded as
     ALREADY_APPLIED (reconciled, no write).
  2. the raw (edit-context) description is repaired by removing each
     fragment's lines, which must match exactly once (WordPress stores raw
     text without the <p> wrappers / with CRLF and indentation).
  3. a versioned rollback record (raw description + attributes + preserved
     fields) is saved BEFORE the write.
  4. PUT only {"description", "attributes"}. Never status, prices, stock,
     images, categories, SKU, slug, name or short description.
  5. re-read and verify: raw description == expected, rendered description
     sha256 == package after_sha256, attributes == package, every preserved
     field unchanged.

An uncertain PUT is never retried: the product is re-read once; if the
repair is not provably present the batch stops with UNCERTAIN. Re-running
resumes from the checkpoint.

--rollback restores one product from its latest rollback record, only while
the product still carries exactly the repaired result.

Usage:
  .venv/Scripts/python.exe scripts/apply_published_repair_package.py \
      --package data/processed/orchestration/published_repair_package_20261019/package_v2.json \
      --woo-id 4765 [--woo-id ...] --max-products 12 [--non-interactive --confirm-apply]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests
from dotenv import load_dotenv
from requests.auth import HTTPBasicAuth

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import create_woocommerce_draft as cwd  # noqa: E402
from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain.rules import woo_presentation  # noqa: E402

configure_utf8_console()

APPLIER_NAME = "published_repair_package_applier"
APPLIER_VERSION = "1.0.0"

APPLIED = "APPLIED"
ALREADY_APPLIED = "ALREADY_APPLIED"
DRY_RUN = "DRY_RUN"
OWNER_EDITED = "ISOLATED_OWNER_EDITED"
IDENTITY_MISMATCH = "ISOLATED_IDENTITY_MISMATCH"
FRAGMENT_MISMATCH = "ISOLATED_FRAGMENT_NOT_UNIQUE"
NOT_IN_PACKAGE = "NOT_IN_PACKAGE"
UNCERTAIN = "UPDATE_RESULT_UNCERTAIN"
VERIFY_FAILED = "POST_UPDATE_VERIFICATION_FAILED"
ROLLED_BACK = "ROLLED_BACK"
ROLLBACK_REFUSED = "ROLLBACK_REFUSED"

SENT_FIELDS = frozenset({"description", "attributes"})
PRESERVED_FIELDS = (
    "status", "sku", "name", "slug", "permalink", "regular_price", "sale_price", "price",
    "manage_stock", "stock_quantity", "backorders", "stock_status", "catalog_visibility",
    "short_description", "date_created",
)


def sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _ids(product: dict[str, Any], field: str) -> list[Any]:
    return [entry.get("id") for entry in product.get(field) or [] if isinstance(entry, dict)]


def preserved_snapshot(product: dict[str, Any]) -> dict[str, Any]:
    snapshot = {field: product.get(field) for field in PRESERVED_FIELDS}
    for field in ("images", "categories", "tags"):
        snapshot[f"{field}_ids"] = _ids(product, field)
    return snapshot


def raw_fragment_pattern(rendered_fragment: str) -> re.Pattern[str]:
    """
    Pure: the raw-content pattern for one rendered fragment. Each rendered
    line must be a whole raw line, allowing leading/trailing blanks, an
    optional <p>...</p> wrapper (wpautop adds it on render) and LF or CRLF.
    """
    parts = []
    for line in rendered_fragment.strip("\n").split("\n"):
        inner = re.fullmatch(r"<p>(.*)</p>", line)
        body = re.escape(inner.group(1)) if inner else re.escape(line)
        if inner:
            body = r"(?:<p>)?" + body + r"(?:</p>)?"
        parts.append(r"^[ \t]*" + body + r"[ \t]*(?:\r?\n|\Z)")
    return re.compile("".join(parts), re.MULTILINE)


def repair_raw_description(raw: str, rendered_fragments: list[str]) -> str | None:
    """Pure: the raw description with every fragment removed, or None when
    any fragment does not match exactly once."""
    result = raw
    for fragment in rendered_fragments:
        pattern = raw_fragment_pattern(fragment)
        if len(pattern.findall(result)) != 1:
            return None
        result = pattern.sub("", result, count=1)
    return result


def attributes_payload(attributes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": attribute.get("id", 0),
            "name": attribute["name"],
            "options": list(attribute["options"]),
            "visible": attribute.get("visible", True),
            "variation": attribute.get("variation", False),
            "position": attribute.get("position", position),
        }
        for position, attribute in enumerate(attributes)
    ]


def classify(item: dict[str, Any], view: dict[str, Any] | None, raw: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """
    Pure: (stop_code, detail). stop_code None means the product may be
    written; ALREADY_APPLIED means it already carries the exact repair.
    """
    if not view or not raw or str(view.get("id")) != str(item["woo_id"]) or str(raw.get("id")) != str(item["woo_id"]):
        return IDENTITY_MISMATCH, "product could not be read under its exact id"
    if view.get("sku") != item["sku"] or view.get("name") != item["name"]:
        return IDENTITY_MISMATCH, f"sku/name are {view.get('sku')!r}/{view.get('name')!r}"
    if view.get("status") != "publish":
        return IDENTITY_MISMATCH, f"status is {view.get('status')!r}, not 'publish'"

    expected_attributes = woo_presentation.comparable_attributes(item["change"]["attributes_after"])
    live_sha = sha256(view.get("description") or "")
    if (
        live_sha == item["change"]["description"]["after_sha256"]
        and woo_presentation.comparable_attributes(raw.get("attributes")) == expected_attributes
    ):
        return ALREADY_APPLIED, None

    precondition = item["precondition"]
    if live_sha != precondition["description_sha256"] or view.get("date_modified") != precondition["date_modified"]:
        return OWNER_EDITED, "description or date_modified changed since the package baseline"
    if woo_presentation.comparable_attributes(raw.get("attributes")) != woo_presentation.comparable_attributes(
        item["rollback"]["attributes"]
    ):
        return OWNER_EDITED, "attributes changed since the package baseline"
    if repair_raw_description(raw.get("description") or "", item["change"]["description"]["remove_exact_fragments"]) is None:
        return FRAGMENT_MISMATCH, "a fragment does not occur exactly once in the raw description"
    return None, None


def verify(item: dict[str, Any], before_raw: dict[str, Any], expected_raw: str,
           view_after: dict[str, Any] | None, raw_after: dict[str, Any] | None) -> list[str]:
    """Pure: problems after the PUT (empty = verified)."""
    if not view_after or not raw_after:
        return ["product could not be re-read"]
    problems = []
    if (raw_after.get("description") or "") != expected_raw:
        problems.append("raw description is not the expected repair")
    if sha256(view_after.get("description") or "") != item["change"]["description"]["after_sha256"]:
        problems.append("rendered description differs from the package after_sha256")
    if woo_presentation.comparable_attributes(raw_after.get("attributes")) != woo_presentation.comparable_attributes(
        item["change"]["attributes_after"]
    ):
        problems.append("attributes not applied")
    before, after = preserved_snapshot(before_raw), preserved_snapshot(raw_after)
    problems += [f"{field} changed" for field in before if before[field] != after[field]]
    return problems


class WooClient:
    """Read and field-limited update for one product id."""

    def __init__(self, store_url: str, api_version: str, key: str, secret: str, timeout: int) -> None:
        self.store_url, self.api_version, self.timeout = store_url, api_version, timeout
        self.auth = HTTPBasicAuth(key, secret)
        self.headers = {"Accept": "application/json", "User-Agent": f"{APPLIER_NAME}/{APPLIER_VERSION}"}

    def _url(self, product_id: int | str) -> str:
        return cwd.build_api_url(self.store_url, self.api_version, f"products/{product_id}")

    def get(self, product_id: int | str, context: str) -> dict[str, Any] | None:
        response = requests.get(self._url(product_id), auth=self.auth, params={"context": context},
                                headers=self.headers, timeout=self.timeout)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def update(self, product_id: int | str, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) != SENT_FIELDS:
            raise RuntimeError(f"Refusing to send fields other than {sorted(SENT_FIELDS)}.")
        response = requests.put(self._url(product_id), auth=self.auth, json=payload,
                                headers=self.headers, timeout=self.timeout)
        response.raise_for_status()
        return response.json()


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)


def load_checkpoint(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"products": {}}


def apply_one(item: dict[str, Any], client: Any, package_dir: Path, checkpoint: dict[str, Any],
              checkpoint_path: Path, dry_run: bool) -> dict[str, Any]:
    woo_id = str(item["woo_id"])
    previous = checkpoint["products"].get(woo_id, {})
    if previous.get("result") in (APPLIED, ALREADY_APPLIED):
        return {"woo_id": item["woo_id"], "result": previous["result"], "detail": "checkpoint", **{
            k: previous[k] for k in ("rollback_record",) if k in previous}}

    view, raw = client.get(woo_id, "view"), client.get(woo_id, "edit")
    stop, detail = classify(item, view, raw)
    report: dict[str, Any] = {"woo_id": item["woo_id"], "sku": item["sku"], "result": stop, "detail": detail}
    if stop is not None or dry_run:
        report["result"] = stop or DRY_RUN
        if stop is not None and not dry_run:
            checkpoint["products"][woo_id] = report
            _write_json(checkpoint_path, checkpoint)
        return report

    expected_raw = repair_raw_description(raw["description"], item["change"]["description"]["remove_exact_fragments"])
    payload = {"description": expected_raw, "attributes": attributes_payload(item["change"]["attributes_after"])}
    rollback_path = package_dir / "rollback" / f"{woo_id}_{_now_stamp()}.json"
    _write_json(rollback_path, {
        "woo_id": item["woo_id"], "sku": item["sku"], "saved_at": datetime.now(timezone.utc).isoformat(),
        "applier": f"{APPLIER_NAME}/{APPLIER_VERSION}",
        "restore_payload": {"description": raw.get("description") or "",
                            "attributes": attributes_payload(raw.get("attributes") or [])},
        "rendered_before_sha256": sha256(view.get("description") or ""),
        "repaired_rendered_sha256": item["change"]["description"]["after_sha256"],
        "repaired_raw_sha256": sha256(expected_raw),
        "preserved_fields_before": preserved_snapshot(raw),
        "date_modified_before": raw.get("date_modified"),
    })
    report["rollback_record"] = str(rollback_path.relative_to(PROJECT_ROOT)) if rollback_path.is_relative_to(PROJECT_ROOT) else str(rollback_path)
    checkpoint["products"][woo_id] = {**report, "result": "WRITING"}
    _write_json(checkpoint_path, checkpoint)

    try:
        client.update(woo_id, payload)
    except (requests.Timeout, requests.ConnectionError) as error:
        # Never retried: re-read once to reconcile.
        problems = verify(item, raw, expected_raw, client.get(woo_id, "view"), client.get(woo_id, "edit"))
        if problems:
            report.update(result=UNCERTAIN, detail=f"{type(error).__name__}; {problems}")
            checkpoint["products"][woo_id] = report
            _write_json(checkpoint_path, checkpoint)
            return report
        report["detail"] = f"{type(error).__name__} during PUT; repair verified present on re-read"

    problems = verify(item, raw, expected_raw, client.get(woo_id, "view"), client.get(woo_id, "edit"))
    report["result"] = VERIFY_FAILED if problems else APPLIED
    if problems:
        report["detail"] = "; ".join(problems)
    report["fields_changed"] = sorted(SENT_FIELDS)
    checkpoint["products"][woo_id] = report
    _write_json(checkpoint_path, checkpoint)
    return report


def rollback_one(woo_id: int, client: Any, package_dir: Path, dry_run: bool) -> dict[str, Any]:
    records = sorted((package_dir / "rollback").glob(f"{woo_id}_*.json"))
    if not records:
        return {"woo_id": woo_id, "result": ROLLBACK_REFUSED, "detail": "no rollback record"}
    record = json.loads(records[-1].read_text(encoding="utf-8"))
    view, raw = client.get(woo_id, "view"), client.get(woo_id, "edit")
    if not view or sha256(view.get("description") or "") != record["repaired_rendered_sha256"]:
        return {"woo_id": woo_id, "result": ROLLBACK_REFUSED,
                "detail": "product no longer carries exactly the repaired result (edited since?)"}
    if dry_run:
        return {"woo_id": woo_id, "result": DRY_RUN, "record": str(records[-1])}
    client.update(woo_id, record["restore_payload"])
    after_view = client.get(woo_id, "view")
    ok = after_view and sha256(after_view.get("description") or "") == record["rendered_before_sha256"]
    return {"woo_id": woo_id, "result": ROLLED_BACK if ok else VERIFY_FAILED, "record": str(records[-1])}


def run(package_path: Path, woo_ids: list[int], max_products: int, client: Any, dry_run: bool,
        log: Callable[..., Any] | None = None) -> list[dict[str, Any]]:
    if len(woo_ids) > max_products:
        raise RuntimeError(f"{len(woo_ids)} Woo IDs exceed --max-products {max_products}.")
    package = json.loads(package_path.read_text(encoding="utf-8"))
    items = {item["woo_id"]: item for item in package["items"] if "change" in item}
    package_dir = package_path.parent
    checkpoint_path = package_dir / f"apply_checkpoint_v{package.get('package_version', 0)}.json"
    checkpoint = load_checkpoint(checkpoint_path)
    reports = []
    for woo_id in woo_ids:
        if woo_id not in items:
            reports.append({"woo_id": woo_id, "result": NOT_IN_PACKAGE})
            continue
        report = apply_one(items[woo_id], client, package_dir, checkpoint, checkpoint_path, dry_run)
        reports.append(report)
        print(f"PUBLISHED_REPAIR_RESULT {json.dumps(report, ensure_ascii=False, default=str)}")
        if log is not None and report["result"] in (APPLIED, VERIFY_FAILED, UNCERTAIN):
            try:
                log(message=f"Published product {woo_id} repair package v{package.get('package_version')}: {report['result']}",
                    process_name=APPLIER_NAME, process_step="APPLY_PUBLISHED_REPAIR",
                    log_level="INFO" if report["result"] == APPLIED else "ERROR", status=report["result"],
                    error_details={k: report.get(k) for k in ("woo_id", "sku", "detail", "rollback_record", "fields_changed")})
            except Exception as error:  # audit failure must not mask the result
                print(f"Warning: process_logs entry failed: {type(error).__name__}: {error}")
        if report["result"] in (UNCERTAIN, VERIFY_FAILED):
            print("Stopping the batch: reconcile this product before continuing.")
            break
    return reports


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply an owner-authorized published-product repair package.")
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--woo-id", type=int, action="append", required=True, help="Exact authorized Woo ID. Repeat.")
    parser.add_argument("--max-products", type=int, required=True)
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--confirm-apply", action="store_true", help="Required to write; otherwise a dry run.")
    parser.add_argument("--rollback", action="store_true", help="Restore the given product(s) from the latest rollback record.")
    return parser.parse_args()


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_arguments()
    store_url = cwd.get_required_environment_variable("WOOCOMMERCE_URL")
    if not store_url.startswith("https://"):
        raise RuntimeError("WOOCOMMERCE_URL must use HTTPS.")
    client = WooClient(
        store_url=store_url,
        api_version=cwd.os.getenv("WOOCOMMERCE_API_VERSION", "wc/v3").strip(),
        key=cwd.get_required_environment_variable("WOOCOMMERCE_CONSUMER_KEY"),
        secret=cwd.get_required_environment_variable("WOOCOMMERCE_CONSUMER_SECRET"),
        timeout=cwd.get_timeout_seconds(),
    )
    dry_run = not args.confirm_apply
    if args.rollback:
        for woo_id in args.woo_id[: args.max_products]:
            print(f"PUBLISHED_REPAIR_ROLLBACK {json.dumps(rollback_one(woo_id, client, args.package.parent, dry_run), ensure_ascii=False)}")
        return
    from src.repositories.supabase_repository import SupabaseRepository

    reports = run(args.package, args.woo_id, args.max_products, client, dry_run,
                  log=None if dry_run else SupabaseRepository().write_process_log)
    summary: dict[str, int] = {}
    for report in reports:
        summary[report["result"]] = summary.get(report["result"], 0) + 1
    print(f"PUBLISHED_REPAIR_SUMMARY {json.dumps(summary)}")


if __name__ == "__main__":
    main()
