"""
Evidence-based resolution of a CATALOG_DUPLICATE_REVIEW human gate.

The mandatory pre-create catalog check (src.domain.woo_catalog_identity)
stops a product whose title only POSSIBLY matches an existing record. This
tool records the reviewed decision for exactly one product code:

  SAME_AS_EXISTING  the book is the existing WooCommerce product: the
                    product gets the terminal EXISTING_PRODUCT catalog
                    marker (EXISTING_WOO_PRODUCT, never created). Every
                    --woo-id must be in the stored check evidence and is
                    re-read (GET) now: it must exist and not be trashed.

  DISTINCT          the possible matches are different books (e.g. sibling
                    volumes): the exact (origin, ref, title) of each is
                    recorded in product_metadata.catalog_identity_review.
                    A fresh, complete catalog check must then return
                    NEW_CONFIRMED -- the review only ever clears the
                    reviewed POSSIBLE matches; any certain/edition/ISBN/
                    barcode match or any new or renamed match still blocks.
                    The create path applies the same review again
                    immediately before every POST.

The decision and its sources (--evidence-file, JSON) are stored on the row
and in process_logs. Writes only that internal_products row (guarded by
its updated_at), dry run unless --confirm-apply. All remote calls are GET.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import create_woocommerce_draft as cwd  # noqa: E402
import sync_woocommerce_product_status as woo_sync  # noqa: E402
from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain import woo_catalog_identity as wci  # noqa: E402
from src.domain.woocommerce_status import WooCommerceStatus  # noqa: E402

configure_utf8_console()

TOOL_NAME = "resolve_catalog_duplicate_review"
TOOL_VERSION = "1.0.0"

REVIEWABLE = frozenset({wci.POSSIBLE_DUPLICATE, wci.DISTINCT_EDITION})
CREATED_STATES = frozenset({
    WooCommerceStatus.DRAFT_CREATED, WooCommerceStatus.READY_TO_PUBLISH, WooCommerceStatus.PUBLISHED})


@dataclass
class Plan:
    product_code: str
    decision: str
    ok: bool = False
    reason: str = ""
    review: dict[str, Any] = field(default_factory=dict)
    marker: dict[str, Any] | None = None


def validate_evidence(evidence: Any) -> str | None:
    if not isinstance(evidence, dict) or not str(evidence.get("summary") or "").strip():
        return "Evidence needs a non-empty 'summary'."
    sources = evidence.get("sources")
    if not isinstance(sources, list) or not sources:
        return "Evidence needs at least one source."
    for source in sources:
        if not isinstance(source, dict) or not source.get("source") or not source.get("finding"):
            return "Every evidence source needs 'source' and 'finding'."
    return None


def plan_resolution(
    *,
    product: dict[str, Any],
    syncs: list[dict[str, Any]],
    decision: str,
    woo_ids: list[int],
    distinct_refs: list[str],
    evidence: Any,
    get_woo: Callable[[int], tuple[int | None, Any]],
    run_check: Callable[[dict[str, Any]], wci.CreateCheck],
    now: str,
) -> Plan:
    """Pure decision over freshly read state (callables do the GETs)."""
    code = product["product_code"]
    plan = Plan(product_code=code, decision=decision)
    marker = wci.blocking_marker(product)
    if marker is None or marker.get("classification") not in REVIEWABLE:
        plan.reason = f"No reviewable catalog block on {code} (marker: {marker and marker.get('classification')})."
        return plan
    if product.get("woocommerce_status") in CREATED_STATES or any(s.get("woocommerce_product_id") for s in syncs):
        plan.reason = "A WooCommerce product is already linked to this product; nothing to resolve."
        return plan
    problem = validate_evidence(evidence)
    if problem:
        plan.reason = problem
        return plan

    stored = [item for item in marker.get("evidence") or () if isinstance(item, dict)]
    total = marker.get("evidence_total")
    if isinstance(total, int) and total > len(stored):
        plan.reason = (f"The stored check evidence is truncated ({len(stored)} of {total}); "
                       "a review cannot cover matches it cannot cite.")
        return plan
    by_ref = {str(item.get("ref")): item for item in stored}
    review = {
        "decision": decision,
        "product_code": code,
        "reviewed_marker": {k: marker.get(k) for k in ("classification", "reason", "checked_at", "check_version")},
        "evidence": evidence,
        "reviewed_at": now,
        "reviewed_by": f"{TOOL_NAME}/{TOOL_VERSION}",
    }

    if decision == wci.REVIEW_SAME_AS_EXISTING:
        if not woo_ids or distinct_refs:
            plan.reason = "SAME_AS_EXISTING needs --woo-id and no --distinct-from."
            return plan
        in_marker = {int(r) for r in by_ref if r.isdigit() and by_ref[r].get("origin") == wci.idx.ORIGIN_WOO}
        in_marker |= {int(i) for group in marker.get("logical_products") or () for i in group}
        confirmed = []
        for woo_id in woo_ids:
            if woo_id not in in_marker:
                plan.reason = f"Woo #{woo_id} is not in the stored check evidence."
                return plan
            status, body = get_woo(woo_id)
            if status != 200 or not isinstance(body, dict) or body.get("status") in (None, "trash"):
                plan.reason = f"Woo #{woo_id} could not be confirmed live now (HTTP {status})."
                return plan
            confirmed.append({"id": woo_id, "name": body.get("name"), "status": body.get("status")})
        review["existing_products"] = confirmed
        plan.marker = {
            **marker,
            "classification": wci.EXISTING_PRODUCT,
            "reason": (f"Reviewed: the same sellable book as existing Woo "
                       f"{', '.join('#' + str(c['id']) for c in confirmed)}. {evidence['summary']}"),
            "terminal": True,
            "checked_at": now,
            "checked_by": f"{TOOL_NAME}/{TOOL_VERSION}",
        }
    elif decision == wci.REVIEW_DISTINCT:
        if not distinct_refs or woo_ids:
            plan.reason = "DISTINCT needs --distinct-from and no --woo-id."
            return plan
        items = []
        for ref in distinct_refs:
            item = by_ref.get(ref)
            if item is None or item.get("strength") != wci.idx.MATCH_POSSIBLE:
                plan.reason = f"{ref} is not a POSSIBLE match in the stored check evidence."
                return plan
            items.append({"origin": item.get("origin"), "ref": ref, "title": item.get("title")})
        review["distinct_from"] = items
        reviewed_product = {**product, "product_metadata": {**(product.get("product_metadata") or {}),
                                                            wci.REVIEW_KEY: review}}
        check = run_check(reviewed_product)
        if check.classification != wci.NEW_CONFIRMED:
            plan.reason = f"Fresh catalog check with the review is {check.classification}: {check.reason}"
            return plan
        plan.marker = check.marker(product_code=code, checked_at=now, checked_by=f"{TOOL_NAME}/{TOOL_VERSION}")
    else:
        plan.reason = f"Unknown decision {decision}."
        return plan

    plan.review = review
    plan.ok = True
    plan.reason = plan.marker["reason"]
    return plan


def apply_plan(repository: Any, product: dict[str, Any], plan: Plan, now: str) -> None:
    metadata = dict(product.get("product_metadata") or {})
    metadata[wci.REVIEW_KEY] = plan.review
    metadata[wci.MARKER_KEY] = plan.marker
    rows = (repository.client.table("internal_products")
            .update({"product_metadata": metadata, "updated_at": now})
            .eq("internal_product_id", product["internal_product_id"])
            .eq("updated_at", product["updated_at"]).execute().data or [])
    if len(rows) != 1:
        raise RuntimeError("Internal product changed since it was read; nothing written.")
    repository.write_process_log(
        message=f"Catalog duplicate review {plan.decision} for {plan.product_code}: {plan.reason}",
        process_name=TOOL_NAME, candidate_id=product.get("candidate_id"),
        process_step="CATALOG_DUPLICATE_REVIEW", status=plan.marker["classification"],
        error_details={"review": plan.review, "marker": plan.marker},
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record an evidence-based catalog duplicate review decision.")
    parser.add_argument("--product-code", required=True, help="Exact internal product code.")
    parser.add_argument("--decision", required=True, choices=[wci.REVIEW_SAME_AS_EXISTING, wci.REVIEW_DISTINCT])
    parser.add_argument("--woo-id", type=int, action="append", default=[], help="Existing Woo product (SAME_AS_EXISTING).")
    parser.add_argument("--distinct-from", action="append", default=[], help="Reviewed match ref (DISTINCT).")
    parser.add_argument("--evidence-file", required=True, type=Path)
    parser.add_argument("--max-products", type=int, required=True)
    parser.add_argument("--confirm-apply", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    return parser.parse_args()


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_arguments()
    if args.max_products < 1:
        raise RuntimeError("--max-products must allow the one product.")
    evidence = json.loads(args.evidence_file.read_text(encoding="utf-8"))
    store_url = cwd.get_required_environment_variable("WOOCOMMERCE_URL")
    api_version = cwd.os.getenv("WOOCOMMERCE_API_VERSION", "wc/v3").strip()
    consumer_key = cwd.get_required_environment_variable("WOOCOMMERCE_CONSUMER_KEY")
    consumer_secret = cwd.get_required_environment_variable("WOOCOMMERCE_CONSUMER_SECRET")
    timeout = cwd.get_timeout_seconds()
    repository = cwd.SupabaseRepository()

    products = (repository.client.table("internal_products").select("*").eq("product_code", args.product_code)
                .limit(2).execute().data or [])
    if len(products) != 1:
        raise RuntimeError(f"{args.product_code} does not resolve to exactly one internal product.")
    product = products[0]
    syncs = (repository.client.table("woocommerce_product_syncs").select("*")
             .eq("internal_product_id", product["internal_product_id"]).execute().data or [])

    def get_woo(woo_id: int) -> tuple[int | None, Any]:
        try:
            return woo_sync.get_woocommerce_product(
                store_url=store_url, api_version=api_version, consumer_key=consumer_key,
                consumer_secret=consumer_secret, timeout_seconds=timeout, woocommerce_product_id=woo_id)
        except requests.RequestException:
            return None, None

    now = cwd.utc_now()
    plan = plan_resolution(
        product=product, syncs=syncs, decision=args.decision, woo_ids=args.woo_id,
        distinct_refs=args.distinct_from, evidence=evidence, get_woo=get_woo, now=now,
        run_check=lambda reviewed: cwd.run_catalog_identity_check(
            repository=repository, product=reviewed, store_url=store_url, api_version=api_version,
            consumer_key=consumer_key, consumer_secret=consumer_secret, timeout_seconds=timeout),
    )
    print("=" * 72)
    print(f"CATALOG DUPLICATE REVIEW {TOOL_VERSION} ({'APPLY' if args.confirm_apply else 'DRY RUN'})")
    print("=" * 72)
    print(f"{plan.product_code}\n  decision: {plan.decision}\n  ok: {plan.ok}\n  reason: {plan.reason}")
    if plan.marker:
        print(f"  new marker: {plan.marker['classification']} (terminal={plan.marker['terminal']})")
    if not plan.ok:
        return 1
    if args.confirm_apply:
        apply_plan(repository, product, plan, now)
        print("  written.")
    print("No WooCommerce product was created or modified.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"Review resolution failed: {error}")
        sys.exit(1)
