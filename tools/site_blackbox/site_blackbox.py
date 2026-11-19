"""Compare catalog snapshots taken by tsyc-site-snapshot.php (black-box tests).

Standard library only: this file runs on its own, without the TSYC repository,
a database, a network connection or any credential. It reads snapshot JSON
files and writes a report. It never contacts a website.

  run-gate-a  one command: snapshot, create a synthetic trio through the TSYC
              bridge, snapshot again, clean up, and judge (needs WP-CLI)
  diff        what changed between two snapshots
  evaluate    judge a scenario folder (README.md) against Gate A (site copy)
              and record what was observed for Gate B

A step whose snapshot is missing is BLOCKED, never PASS. A Gate B result is an
OBSERVED fact for a person to read, never a machine pass. Snapshots taken on
the production host are refused: the scenario writes test products.

The only thing run-gate-a starts is the WP-CLI command you give it, with the
two PHP files next to this one. It opens no network connection itself.

Usage:
  python site_blackbox.py run-gate-a RUN_FOLDER --user ADMIN_LOGIN [--wp "wp --path=/var/www/copy"]
  python site_blackbox.py diff BEFORE.json AFTER.json
  python site_blackbox.py evaluate RUN_FOLDER [--json-out REPORT.json]
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

SNAPSHOT_VERSION = 1
PRODUCTION_HOSTS = frozenset({"tiemsachyeucon.com", "www.tiemsachyeucon.com"})

# Every recorded field except the bookkeeping ones is compared.
IGNORED_FIELDS = frozenset({"id"})
SAVE_ONLY_FIELDS = frozenset({"modified_gmt"})            # a save with no other difference
COMMERCE_FIELDS = ("status", "regular_price", "sale_price", "price", "manage_stock", "stock_quantity",
                   "backorders", "stock_status", "sku")
LANGUAGES = ("vi", "en", "de")

PASS, FAIL, BLOCKED, OBSERVED = "PASS", "FAIL", "BLOCKED", "OBSERVED"
# No valid test on a copy of the real site exists. Never a pass.
BLOCKED_REAL_SITE_TEST = "BLOCKED_REAL_SITE_TEST"

# What "did creating the EN/DE drafts change anything?" is answered for, field by field.
FIELD_GROUPS: Mapping[str, tuple[str, ...]] = {
    "price": ("regular_price", "sale_price", "price"),
    "stock": ("manage_stock", "stock_quantity", "stock_status", "total_sales"),
    "backorders": ("backorders",),
    "sku": ("sku",),
    "images": ("image_id", "gallery_image_ids"),
    "status": ("status", "type"),
    "translation_links": ("language", "translations"),
    "owner_maintained_fields": ("title", "slug", "content_sha256", "excerpt_sha256", "category_ids", "attributes_sha256",
                                "upsell_ids", "cross_sell_ids", "weight", "tsyc_internal_product_id", "tsyc_language"),
    "saved_again": ("modified_gmt",),
}

# Scenario steps, in order: file stem -> what the tester did just before it.
STEPS: tuple[tuple[str, str], ...] = (
    ("00_baseline", "Nothing yet: the isolated copy as it is"),
    ("00b_vi_created", "The new VI draft only (stock 0, backorders allowed, no price), before any translation"),
    ("01_trio_created", "New VI draft (stock 0, backorders allowed, no price), then + EN and + DE, all saved as drafts"),
    ("02_price_on_vi", "Regular price 111000 entered on the VI product only"),
    ("03_price_on_en", "Regular price 222000 entered on the EN product only"),
    ("04_stock_on_vi", "Stock quantity 5 entered on the VI product only"),
    ("05_backorders_on_de", "Backorders set to 'Do not allow' on the DE product only"),
    ("06_published", "All three published (backorders set back to 'Allow' on DE first)"),
    ("07_order_on_en", "One order, quantity 1, of the EN product"),
    ("08_order_on_de", "One order, quantity 1, of the DE product"),
    ("09_concurrent_orders", "Two orders placed at the same moment: VI and EN, quantity 1 each"),
    ("10_cleanup", "The three test products moved to the trash, test orders cancelled"),
)


class SnapshotError(ValueError):
    """A snapshot file is unusable. Never read as 'nothing changed'."""


def _decode(raw: bytes) -> str:
    # Windows PowerShell's ">" writes UTF-16; other shells write UTF-8.
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def load_snapshot(path: Path) -> dict[str, Any]:
    try:
        text = _decode(Path(path).read_bytes())
    except (OSError, UnicodeError) as error:
        raise SnapshotError(f"{path}: {type(error).__name__}: {error}") from error
    # WP-CLI may print notices before the JSON line.
    line = next((ln for ln in text.splitlines() if ln.lstrip().startswith('{"tsyc_snapshot"')), None)
    if line is None:
        raise SnapshotError(f"{path}: no snapshot line found.")
    try:
        data = json.loads(line)
    except json.JSONDecodeError as error:
        raise SnapshotError(f"{path}: {error}") from error
    if data.get("tsyc_snapshot") != SNAPSHOT_VERSION or not isinstance(data.get("products"), dict):
        raise SnapshotError(f"{path}: not a version {SNAPSHOT_VERSION} snapshot.")
    if data.get("product_count") != len(data["products"]):
        raise SnapshotError(f"{path}: truncated ({len(data['products'])} of {data.get('product_count')} products).")
    return data


def normalize(product: Mapping[str, Any]) -> dict[str, Any]:
    """Comparable values: WooCommerce stores yes/no and numeric strings."""
    out = {key: value for key, value in product.items() if key not in IGNORED_FIELDS}
    if out.get("manage_stock") in ("yes", "no"):
        out["manage_stock"] = out["manage_stock"] == "yes"
    quantity = out.get("stock_quantity")
    if isinstance(quantity, str):
        try:
            out["stock_quantity"] = None if quantity.strip() == "" else float(quantity)
        except ValueError:
            pass
    elif isinstance(quantity, int):
        out["stock_quantity"] = float(quantity)
    return out


def diff_snapshots(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """added / removed product IDs and, per changed product, field -> [before, after]."""
    old, new = before["products"], after["products"]
    changed: dict[str, dict[str, list[Any]]] = {}
    for pid in sorted(set(old) & set(new), key=int):
        a, b = normalize(old[pid]), normalize(new[pid])
        fields = {name: [a.get(name), b.get(name)] for name in sorted(set(a) | set(b)) if a.get(name) != b.get(name)}
        if fields:
            changed[pid] = fields
    return {"added": sorted((int(p) for p in set(new) - set(old))),
            "removed": sorted((int(p) for p in set(old) - set(new))),
            "changed": changed}


def outside_changes(delta: Mapping[str, Any], allowed_ids: Sequence[int]) -> dict[str, Any]:
    """The part of a diff that touches products outside allowed_ids."""
    allowed = {int(pid) for pid in allowed_ids}
    return {"added": [p for p in delta["added"] if p not in allowed],
            "removed": [p for p in delta["removed"] if p not in allowed],
            "changed": {pid: fields for pid, fields in delta["changed"].items() if int(pid) not in allowed}}


def is_empty(delta: Mapping[str, Any]) -> bool:
    return not delta["added"] and not delta["removed"] and not delta["changed"]


def detect_trio(before: Mapping[str, Any], after: Mapping[str, Any]) -> tuple[dict[str, int] | None, str]:
    """The VI/EN/DE test products: exactly the three products added in step
    01, one per language, each naming the other two."""
    added = [after["products"][str(pid)] for pid in diff_snapshots(before, after)["added"]]
    if len(added) != 3:
        return None, f"Step 01 added {len(added)} products, expected exactly 3."
    by_language = {str(p.get("language") or ""): int(p["id"]) for p in added}
    if set(by_language) != set(LANGUAGES):
        return None, f"The three new products have languages {sorted(by_language)}, expected vi, en, de."
    for product in added:
        if product.get("translations") != by_language:
            return None, (f"#{product['id']} is linked to {product.get('translations')}, expected {by_language} "
                          "(Polylang missing or the + link did not save).")
    return by_language, ""


def _fields(snapshot: Mapping[str, Any], trio: Mapping[str, int], names: Sequence[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for lang, pid in trio.items():
        product = snapshot["products"].get(str(pid))
        out[lang] = {name: normalize(product).get(name) for name in names} if product else {"missing": True}
    return out


def _propagation(before: Mapping[str, Any], after: Mapping[str, Any], trio: Mapping[str, int],
                 edited: str, field: str) -> str:
    """Plain sentence: what the other two copies did after one was edited."""
    was, now = _fields(before, trio, [field]), _fields(after, trio, [field])
    parts = [f"{edited.upper()} {field}: {was[edited].get(field)!r} -> {now[edited].get(field)!r}"]
    for lang in LANGUAGES:
        if lang == edited:
            continue
        a, b = was[lang].get(field), now[lang].get(field)
        if a == b:
            parts.append(f"{lang.upper()} unchanged ({b!r})")
        elif b == now[edited].get(field):
            parts.append(f"{lang.upper()} FOLLOWED ({a!r} -> {b!r})")
        else:
            parts.append(f"{lang.upper()} changed to a different value ({a!r} -> {b!r})")
    return "; ".join(parts)


def evaluate(run_dir: Path) -> dict[str, Any]:
    """Judge one scenario folder. See README.md for the steps."""
    snapshots: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for stem, _ in STEPS:
        path = Path(run_dir) / f"{stem}.json"
        if path.exists():
            try:
                snapshots[stem] = load_snapshot(path)
            except SnapshotError as error:
                problems.append(str(error))
    hosts = sorted({str(s["site"].get("host") or "") for s in snapshots.values()})
    report: dict[str, Any] = {"run_dir": str(run_dir), "hosts": hosts, "snapshot_problems": problems,
                              "steps_present": sorted(snapshots), "site": next(iter(snapshots.values()), {}).get("site"),
                              "status": "EVALUATED", "trio": None, "isolation": {}, "compatibility": {},
                              "real_site_compatibility": BLOCKED_REAL_SITE_TEST,
                              "gate_a_site_checks": [], "gate_b_checks": []}

    def check(target: str, check_id: str, status: str, evidence: str) -> None:
        report[target].append({"id": check_id, "status": status, "evidence": evidence})

    a_ids = ("A05", "A06", "A07", "A08", "A10", "A11", "A15")
    b_ids = ("B01", "B02", "B03", "B04", "B05", "B06", "B07")

    def block_all(reason: str) -> dict[str, Any]:
        for cid in a_ids:
            check("gate_a_site_checks", cid, BLOCKED, reason)
        for cid in b_ids:
            check("gate_b_checks", cid, BLOCKED, reason)
        return report

    if PRODUCTION_HOSTS & set(hosts):
        report["status"] = "INVALID_PRODUCTION_HOST"
        return block_all("A snapshot comes from the production host. The scenario must never be run there.")
    if len(hosts) > 1:
        report["status"] = "INVALID_MIXED_HOSTS"
        return block_all(f"Snapshots come from different hosts: {hosts}.")
    if problems:
        report["status"] = "INVALID_SNAPSHOT"
        return block_all("; ".join(problems))
    if "00_baseline" not in snapshots or "01_trio_created" not in snapshots:
        report["status"] = "BLOCKED_NO_SNAPSHOTS"
        return block_all("Steps 00_baseline and 01_trio_created are required.")

    base, created = snapshots["00_baseline"], snapshots["01_trio_created"]
    trio, reason = detect_trio(base, created)
    if trio is None:
        report["status"] = "TRIO_NOT_DETECTED"
        check("gate_a_site_checks", "A05", FAIL, reason)
        for cid in a_ids[1:]:
            check("gate_a_site_checks", cid, BLOCKED, reason)
        for cid in b_ids:
            check("gate_b_checks", cid, BLOCKED, reason)
        return report
    report["trio"] = trio
    trio_ids = list(trio.values())

    # ---- Gate A on the site copy: what creating and linking the trio did.
    check("gate_a_site_checks", "A05", PASS, f"Three new products, one per language, each linked to the other two: {trio}.")
    members = {lang: normalize(created["products"][str(pid)]) for lang, pid in trio.items()}
    vi = members["vi"]
    same_book = [f"{lang} #{trio[lang]}: {why}" for lang in ("en", "de") for why in (
        "carries another internal product ID" if members[lang].get("tsyc_internal_product_id") != vi.get("tsyc_internal_product_id") else "",
        f"language mark is {members[lang].get('tsyc_language')!r}" if members[lang].get("tsyc_language") != lang else "",
        f"has a different SKU {members[lang].get('sku')!r}" if members[lang].get("sku") not in ("", vi.get("sku")) else "",
        f"is a {members[lang].get('type')!r} product" if members[lang].get("type") != vi.get("type") else "") if why]
    if not vi.get("tsyc_internal_product_id"):
        same_book.insert(0, "the VI draft carries no TSYC internal product ID")
    check("gate_a_site_checks", "A06", FAIL if same_book else PASS,
          ("Not one sellable book: " + " | ".join(same_book)) if same_book
          else f"Same internal product on all three, language marks right, copies have no SKU or the VI SKU ({vi.get('sku')!r}).")
    media = [f"{lang} #{trio[lang]} {name}: {members[lang].get(name)!r}, VI has {vi.get(name)!r}"
             for lang in ("en", "de") for name in ("image_id", "gallery_image_ids", "weight")
             if members[lang].get(name) != vi.get(name)]
    check("gate_a_site_checks", "A10", FAIL if media else PASS,
          ("Copies differ from the VI draft: " + " | ".join(media)) if media
          else f"Both copies carry the VI draft's main image ({vi.get('image_id')}), gallery and weight.")
    shape = _fields(created, trio, ["status", "manage_stock", "stock_quantity", "backorders", "sku"])
    wrong = [f"{lang}: {values}" for lang, values in shape.items()
             if not (values.get("status") == "draft" and values.get("manage_stock") is True
                     and values.get("stock_quantity") == 0.0 and values.get("backorders") == "yes")]
    check("gate_a_site_checks", "A07", FAIL if wrong else PASS,
          ("Not in the pre-order shape: " + " | ".join(wrong)) if wrong else f"All three: draft, managed stock 0, backorders yes. {shape}")
    prices = _fields(created, trio, ["regular_price", "sale_price", "price"])
    priced = [f"{lang}: {values}" for lang, values in prices.items() if any(str(v or "").strip() for v in values.values())]
    check("gate_a_site_checks", "A08", FAIL if priced else PASS,
          ("A price appeared: " + " | ".join(priced)) if priced else "No price field is set on any of the three.")

    # Isolation: at every step, nothing outside the trio may differ from the step before.
    present = [stem for stem, _ in STEPS if stem in snapshots]
    leaks: list[str] = []
    for previous, current in zip(present, present[1:]):
        outside = outside_changes(diff_snapshots(snapshots[previous], snapshots[current]), trio_ids)
        report["isolation"][current] = outside
        if not is_empty(outside):
            leaks.append(f"{current}: added {outside['added']}, removed {outside['removed']}, "
                         f"changed {sorted(outside['changed'])}")
    check("gate_a_site_checks", "A11", FAIL if leaks else PASS,
          ("Products outside the trio changed. " + " | ".join(leaks)) if leaks
          else f"No product outside the trio differs across {len(present)} snapshots ({len(base['products'])} products in the baseline).")

    # Did creating and linking EN/DE change the VI draft itself? Needs the VI-only snapshot.
    vi_changes: dict[str, list[Any]] | None = None
    if "00b_vi_created" in snapshots and str(trio["vi"]) in snapshots["00b_vi_created"]["products"]:
        was, now = normalize(snapshots["00b_vi_created"]["products"][str(trio["vi"])]), members["vi"]
        vi_changes = {name: [was.get(name), now.get(name)] for name in sorted(set(was) | set(now))
                      if was.get(name) != now.get(name) and name != "translations"}
        if now.get("translations") != trio:
            vi_changes["translations"] = [was.get("translations"), now.get("translations")]
        check("gate_a_site_checks", "A15", FAIL if vi_changes else PASS,
              (f"The VI draft #{trio['vi']} changed while EN/DE were created: {vi_changes}") if vi_changes
              else f"The VI draft #{trio['vi']} is identical before and after, apart from its two new translation links.")
    else:
        check("gate_a_site_checks", "A15", BLOCKED, "Snapshot 00b_vi_created (the VI draft before any translation) is missing.")

    # Field by field: what creating the drafts did to the VI draft and to every other product.
    creation = [stem for stem in ("00_baseline", "00b_vi_created", "01_trio_created") if stem in snapshots]
    others: dict[str, set[str]] = {}
    for previous, current in zip(creation, creation[1:]):
        for pid, fields in outside_changes(diff_snapshots(snapshots[previous], snapshots[current]), trio_ids)["changed"].items():
            for name in fields:
                others.setdefault(name, set()).add(pid)

    def vi_verdict(names: Sequence[str]) -> str:
        if vi_changes is None:
            return BLOCKED
        hit = [name for name in names if name in vi_changes]
        return "CHANGED: " + ", ".join(hit) if hit else "UNCHANGED"

    def others_verdict(names: Sequence[str]) -> str:
        hit = sorted({pid for name in names for pid in others.get(name, ())}, key=int)
        return "CHANGED on #" + ", #".join(hit) if hit else "UNCHANGED"

    report["compatibility"] = {
        "vi_draft": {group: vi_verdict(names) for group, names in FIELD_GROUPS.items()},
        "every_other_product": {group: others_verdict(names) for group, names in FIELD_GROUPS.items()},
        "other_products_compared": len(base["products"]),
    }
    if vi_changes is not None and "translations" not in vi_changes and "language" not in vi_changes:
        report["compatibility"]["vi_draft"]["translation_links"] = "UNCHANGED apart from the two new links"

    # ---- Gate B: observations only.
    def step(stem: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        index = [s for s, _ in STEPS].index(stem)
        previous = STEPS[index - 1][0]
        if stem in snapshots and previous in snapshots:
            return snapshots[previous], snapshots[stem]
        return None

    def observe(check_id: str, stems: Sequence[str], describe: Any) -> None:
        pairs = {stem: step(stem) for stem in stems}
        missing = [stem for stem, pair in pairs.items() if pair is None]
        if missing:
            check("gate_b_checks", check_id, BLOCKED, f"Missing snapshots for: {', '.join(missing)} (and the step before each).")
        else:
            check("gate_b_checks", check_id, OBSERVED, describe(pairs))

    observe("B01", ["02_price_on_vi", "03_price_on_en"], lambda p: " || ".join([
        _propagation(*p["02_price_on_vi"], trio, "vi", "regular_price"),
        _propagation(*p["03_price_on_en"], trio, "en", "regular_price")]))
    observe("B02", ["04_stock_on_vi"], lambda p: " || ".join(
        _propagation(*p["04_stock_on_vi"], trio, "vi", name) for name in ("stock_quantity", "manage_stock", "stock_status")))
    observe("B03", ["05_backorders_on_de"], lambda p: _propagation(*p["05_backorders_on_de"], trio, "de", "backorders"))

    def stock_moves(pair: tuple[dict[str, Any], dict[str, Any]]) -> str:
        was, now = _fields(pair[0], trio, ["stock_quantity", "total_sales"]), _fields(pair[1], trio, ["stock_quantity", "total_sales"])
        return ", ".join(f"{lang.upper()} stock {was[lang].get('stock_quantity')!r} -> {now[lang].get('stock_quantity')!r} "
                         f"(sales {was[lang].get('total_sales')!r} -> {now[lang].get('total_sales')!r})" for lang in LANGUAGES)

    observe("B04", ["07_order_on_en", "08_order_on_de"], lambda p: "EN order: " + stock_moves(p["07_order_on_en"])
            + " || DE order: " + stock_moves(p["08_order_on_de"]))
    observe("B05", ["09_concurrent_orders"], lambda p: "VI and EN ordered together: " + stock_moves(p["09_concurrent_orders"]))
    observe("B06", ["02_price_on_vi", "03_price_on_en", "04_stock_on_vi", "07_order_on_en"], lambda p: " || ".join([
        "price VI->others: " + _propagation(*p["02_price_on_vi"], trio, "vi", "regular_price"),
        "price EN->others: " + _propagation(*p["03_price_on_en"], trio, "en", "regular_price"),
        "stock VI->others: " + _propagation(*p["04_stock_on_vi"], trio, "vi", "stock_quantity"),
        "EN order: " + stock_moves(p["07_order_on_en"])]))

    def owner_price(pairs: Mapping[str, Any]) -> str:
        entered = _fields(pairs["02_price_on_vi"][1], trio, ["regular_price"])["vi"]["regular_price"]
        later = {stem: _fields(snapshots[stem], trio, ["regular_price"])["vi"]["regular_price"]
                 for stem in present[present.index("02_price_on_vi"):] if stem != "10_cleanup"}
        kept = all(value == entered for value in later.values())
        return (f"The price entered on VI ({entered!r}) " + ("was kept through every later step." if kept
                else f"did NOT survive: {later}."))

    observe("B07", ["02_price_on_vi", "03_price_on_en"], owner_price)

    report["created_by"] = created_by(created, trio)
    if report["created_by"] != CREATED_BY_BRIDGE:
        # The site's own "+" path says how the site behaves, not whether the
        # TSYC bridge is safe on it: kept as an observation, never a Gate A result.
        for item in report["gate_a_site_checks"]:
            item["evidence"] = (f"Trio made by hand with Polylang's +, not by the TSYC bridge, so this is not a Gate A "
                                f"result. Observed ({item['status']}): {item['evidence']}")
            item["status"] = BLOCKED
    site = {item["status"] for item in report["gate_a_site_checks"]}
    report["real_site_compatibility"] = FAIL if FAIL in site else BLOCKED_REAL_SITE_TEST if site != {PASS} else PASS
    return report


CREATED_BY_BRIDGE = "TSYC_BRIDGE"
CREATED_BY_HAND = "MANUAL_PLUS"


def created_by(snapshot: Mapping[str, Any], trio: Mapping[str, int]) -> str:
    """TSYC_BRIDGE only when both copies carry the bridge's own language mark
    and the VI product's internal ID."""
    products = snapshot["products"]
    internal = str(products[str(trio["vi"])].get("tsyc_internal_product_id") or "")
    marked = all(str(products[str(trio[lang])].get("tsyc_language") or "") == lang
                 and str(products[str(trio[lang])].get("tsyc_internal_product_id") or "") == internal
                 for lang in ("en", "de"))
    return CREATED_BY_BRIDGE if internal and marked else CREATED_BY_HAND


def _print_report(report: Mapping[str, Any]) -> None:
    print(f"Status: {report['status']}   host: {', '.join(report['hosts']) or '-'}   trio: {report['trio']}   "
          f"created by: {report.get('created_by', '-')}")
    print(f"Draft creation on this site (Gate A): {report['real_site_compatibility']}")
    for scope, groups in report.get("compatibility", {}).items():
        if isinstance(groups, dict):
            print(f"  {scope}: " + "; ".join(f"{group} {value}" for group, value in groups.items()))
    for title, key in (("GATE A on this site copy (PREORDER_DRAFT_CREATION_SAFETY)", "gate_a_site_checks"),
                       ("GATE B (LIVE_COMMERCE_SYNCHRONIZATION_SAFETY): observations for a person", "gate_b_checks")):
        print(f"\n{title}")
        for item in report[key]:
            print(f"  {item['status']:<9} {item['id']}  {item['evidence']}")


class HarnessError(RuntimeError):
    """The one-command run stopped. Nothing is retried."""


def _json_line(text: str, marker: str) -> dict[str, Any]:
    line = next((ln for ln in text.splitlines() if ln.lstrip().startswith(marker)), None)
    if line is None:
        raise HarnessError(f"No answer from WP-CLI. Output was: {text.strip()[-600:]}")
    return json.loads(line)


def _text(raw: Any) -> str:
    return _decode(raw) if isinstance(raw, bytes) else str(raw or "")


def run_gate_a(
    run_dir: Path, *, wp: Sequence[str], user: str, wait_seconds: float = 120.0, cleanup: bool = True,
    run: Callable[..., Any] | None = None, sleep: Callable[[float], None] = time.sleep,
    say: Callable[[str], None] = print, timeout: int = 900,
) -> dict[str, Any]:
    """Snapshot, create one synthetic trio through the TSYC bridge, snapshot,
    clean up, judge. Every write is made by tsyc-bridge-trio-driver.php, which
    refuses the production host and any environment but local or staging; this
    function also refuses as soon as the first snapshot names the production host."""
    here = Path(__file__).resolve().parent
    run_dir = Path(run_dir)
    run = run or subprocess.run
    run_dir.mkdir(parents=True, exist_ok=True)
    if any(run_dir.glob("*.json")):
        raise HarnessError(f"{run_dir} already holds snapshots. Use a new, empty folder for each run.")

    def call(*arguments: str) -> str:
        done = run([*wp, *arguments], capture_output=True, timeout=timeout)
        out = _text(done.stdout)
        if done.returncode not in (0, 3, 4):
            raise HarnessError(f"WP-CLI failed ({done.returncode}) on {arguments[:3]}: {(out + _text(done.stderr)).strip()[-600:]}")
        return out

    def snapshot(stem: str) -> dict[str, Any]:
        say(f"snapshot {stem} ...")
        (run_dir / f"{stem}.json").write_text(call("eval-file", str(here / "tsyc-site-snapshot.php"), stem), encoding="utf-8")
        return load_snapshot(run_dir / f"{stem}.json")

    def driver(*arguments: str) -> dict[str, Any]:
        answer = _json_line(call("eval-file", str(here / "tsyc-bridge-trio-driver.php"), *arguments, f"--user={user}"),
                            '{"tsyc_driver"')
        (run_dir / f"driver_{arguments[0]}.txt").write_text(json.dumps(answer, ensure_ascii=False, indent=1), encoding="utf-8")
        return answer

    first = snapshot("00_baseline")
    host = str(first["site"].get("host") or "")
    if host in PRODUCTION_HOSTS:
        raise HarnessError("This is the production host. Nothing was written. Run this only on an isolated copy.")
    say(f"site {host}, {len(first['products'])} products, environment {first['site'].get('environment_type')!r}")

    made = driver("create-vi")
    if not made.get("ok"):
        raise HarnessError(f"The driver refused to start: {made.get('code')}: {made.get('message')}")
    vi_id = int(made["trio"]["vi"])
    snapshot("00b_vi_created")
    translated = driver("translate", str(vi_id))
    if not translated.get("ok"):
        # A refusal or a side-effect stop is the finding. Snapshot it; retry nothing.
        say(f"The bridge stopped: {translated.get('answers')}")
    say(f"waiting {wait_seconds:.0f} s so delayed jobs on the site can run ...")
    sleep(wait_seconds)
    snapshot("01_trio_created")
    if cleanup:
        cleaned = driver("cleanup", str(vi_id))
        say(f"test products moved to the trash: {cleaned.get('trashed')}")
        snapshot("10_cleanup")
    report = evaluate(run_dir)
    report["harness"] = {"vi_id": vi_id, "translate": translated, "waited_seconds": wait_seconds, "cleaned_up": cleanup}
    (run_dir / "blackbox_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    harness = commands.add_parser("run-gate-a", help="One command: snapshot, create a test trio through the bridge, judge.")
    harness.add_argument("run_dir", type=Path)
    harness.add_argument("--user", required=True, help="A WordPress login that may edit products.")
    harness.add_argument("--wp", default="wp", help='How to start WP-CLI here, e.g. "wp --path=/var/www/copy".')
    harness.add_argument("--wait", type=float, default=120.0, help="Seconds to wait before the after-snapshot (default 120).")
    harness.add_argument("--keep", action="store_true", help="Leave the test products in place (for the price and stock steps).")
    diff = commands.add_parser("diff", help="What changed between two snapshots.")
    diff.add_argument("before", type=Path)
    diff.add_argument("after", type=Path)
    judge = commands.add_parser("evaluate", help="Judge a scenario folder.")
    judge.add_argument("run_dir", type=Path)
    judge.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    try:
        if args.command == "run-gate-a":
            try:
                report = run_gate_a(args.run_dir, wp=shlex.split(args.wp, posix=os.name != "nt"), user=args.user,
                                    wait_seconds=args.wait, cleanup=not args.keep)
            except HarnessError as error:
                print(f"{BLOCKED_REAL_SITE_TEST}: {error}", file=sys.stderr)
                return 4
            _print_report(report)
            print(f"\nReport: {args.run_dir / 'blackbox_report.json'}")
            return {PASS: 0, FAIL: 1}.get(report["real_site_compatibility"], 4)
        if args.command == "diff":
            print(json.dumps(diff_snapshots(load_snapshot(args.before), load_snapshot(args.after)), ensure_ascii=False, indent=1))
            return 0
        report = evaluate(args.run_dir)
    except SnapshotError as error:
        print(f"Unusable snapshot: {error}", file=sys.stderr)
        return 2
    out = args.json_out or args.run_dir / "blackbox_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    _print_report(report)
    print(f"\nReport: {out}")
    if report["status"] != "EVALUATED":
        return 4
    # Same rule as run-gate-a: only PASS is 0. A blocked run (missing step, trio made
    # by hand) must never exit with the code a script reads as success.
    return {PASS: 0, FAIL: 1}.get(report["real_site_compatibility"], 4)


if __name__ == "__main__":
    sys.exit(main())
