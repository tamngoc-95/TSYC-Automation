"""Gate A (PREORDER_DRAFT_CREATION_SAFETY) in the isolated local lab.

One synthetic, genuinely new pre-order book goes through the production code
path end to end: the VI draft payload builder, the approved-content loader and
renderer, the runner, the bridge, and a read-back. A whole-catalog snapshot
before and after proves that nothing else was created, changed or removed.

What a pass means: GENERIC_LOCAL_LAB only -- real WordPress, WooCommerce and
Polylang at production's versions, without the store's theme or customization.
It is not a pass on the real site, and it says nothing about Gate B
(LIVE_COMMERCE_SYNCHRONIZATION_SAFETY), which this run always reports BLOCKED.

The run also exercises the developer's black-box package (tools/site_blackbox)
on the real lab stack, so the package is known to work before it is handed over.

Synthetic data on loopback only. No production URL, key or .env value is read.
Run files go to data/output/preorder_gate_a/<run>/ (gitignored).

Usage:
  .venv/Scripts/python.exe tools/wp_lab/provision_lab.py [--db mariadb] [--lab-dir PATH]
  .venv/Scripts/python.exe tools/wp_lab/run_preorder_gate_a.py [--lab-dir PATH]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "site_blackbox"))

import run_local_pilot as base  # noqa: E402
from run_local_pilot import AUTH_ID, HTTP_TIMEOUT, STORE, Check, Lab, Pilot, expect  # noqa: E402

import site_blackbox as bbx  # noqa: E402

from src.domain import candidate_origin  # noqa: E402
from src.domain import integration_gates as ig  # noqa: E402
from src.domain import language_product_mapping as lpm  # noqa: E402
from src.domain import multilingual_adapter as ma  # noqa: E402
from src.domain import preorder_trio_verification as ptv  # noqa: E402
from src.domain import product_workflow as pw  # noqa: E402
from src.domain import translation_book_input as tbi  # noqa: E402
from src.domain import translation_create_state as tcs  # noqa: E402
from src.domain import woo_translation_group as wtg  # noqa: E402
from src.domain.rules import preorder_inventory_rules as pir  # noqa: E402
from src.services import polylang_bridge_writer as pbw  # noqa: E402
from src.services import translation_draft_runner as tdr  # noqa: E402

cwd = base.cwd
BLACKBOX_DIR = HERE.parent / "site_blackbox"
SNAPSHOT_PHP = BLACKBOX_DIR / "tsyc-site-snapshot.php"
DRIVER_PHP = BLACKBOX_DIR / "tsyc-bridge-trio-driver.php"

CODE_PREFIX = f"{candidate_origin.FAHASA_DISCOVERY_CODE_PREFIX}LAB-CAN-"


def uid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def content_rows(internal_id: str, number: int) -> list[dict[str, Any]]:
    """Real-shaped approved product_contents rows for one synthetic book."""
    names = {"vi": f"Cuốn sách thử nghiệm số {number}", "en": f"The Lab Test Book {number}", "de": f"Das Labor-Testbuch {number}"}
    longs = {"vi": "Đoạn một giới thiệu cuốn sách tổng hợp.\n\nĐoạn hai nói về nội dung dành cho trẻ nhỏ.",
             "en": "Paragraph one introduces the synthetic book.\n\nParagraph two describes its content for young children.",
             "de": "Absatz eins stellt das synthetische Buch vor.\n\nAbsatz zwei beschreibt den Inhalt für kleine Kinder."}
    shorts = {"vi": "Sách thử nghiệm cho bé.", "en": "A test book for little ones.", "de": "Ein Testbuch für die Kleinen."}
    details = {"vi": "Nhà xuất bản: Kim Đồng\nSố trang: 64\nKích thước: 20 x 20 cm",
               "en": "Publisher: Kim Đồng\nPages: 64\nDimensions: 20 x 20 cm",
               "de": "Verlag: Kim Đồng\nSeiten: 64\nFormat: 20 x 20 cm"}
    return [{"product_content_id": uid(number * 100 + index), "internal_product_id": internal_id, "content_language": lang,
             "content_status": "APPROVED", "review_required": False,
             "approved_at": f"2026-11-16T08:0{index}:00+00:00", "product_name": names[lang],
             "short_description": shorts[lang], "long_description": longs[lang], "author_summary": "",
             "product_details": details[lang]}
            for index, lang in enumerate(wtg.SUPPORTED_LANGUAGES, start=1)]


class GateA(Pilot):
    def __init__(self, lab: Lab, run_dir: Path) -> None:
        super().__init__(lab, run_dir)
        self.checks: list[ig.CheckResult] = []
        self.extra: list[dict[str, Any]] = []
        self.snapshots: dict[str, dict[str, Any]] = {}

    # ---- helpers ----------------------------------------------------------

    def snapshot(self, label: str, folder: Path | None = None) -> dict[str, Any]:
        """The developer's own snapshot script, run on the real lab stack."""
        path = (folder or self.run_dir) / f"{label}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.lab.wp("eval-file", str(SNAPSHOT_PHP), label), encoding="utf-8")
        self.snapshots[label] = bbx.load_snapshot(path)
        return self.snapshots[label]

    def step(self, check_id: str, fn: Callable[[], dict[str, Any]], *, gate: bool = True) -> bool:
        started = time.time()
        try:
            evidence, status = fn(), ig.CHECK_PASS
        except Check as error:
            evidence, status = {"failed": str(error)}, ig.CHECK_FAIL
        except Exception as error:  # noqa: BLE001 - one check never stops the run
            evidence, status = {"error": f"{type(error).__name__}: {error}", "trace": traceback.format_exc()[-1500:]}, ig.CHECK_FAIL
        text = json.dumps(evidence, ensure_ascii=False, default=str)
        title = ig.GATE_A_CHECKS.get(check_id, check_id)
        if gate:
            self.checks.append(ig.CheckResult(check_id, status, text))
        else:
            self.extra.append({"id": check_id, "status": status, "evidence": evidence})
        print(f"{status:<6} {check_id:<4} {title}  ({time.time() - started:.1f}s)", flush=True)
        if status == ig.CHECK_FAIL:
            print(text[:2500], flush=True)
        return status == ig.CHECK_PASS

    def attachments(self, tag: str) -> list[int]:
        return self.lab.eval_json(f"""
$out = array();
foreach (array('cover','inside') as $name) {{
  $out[] = wp_insert_attachment(array('post_title'=>'lab {tag} ' . $name,'post_mime_type'=>'image/png','post_status'=>'inherit'), 'lab-{tag}-' . $name . '.png');
}}
echo json_encode($out);""")

    def counting_writer(self, sent: list[Any], fault: str | None = None) -> pbw.BridgeDraftWriter:
        def post(*args: Any, **kwargs: Any) -> Any:
            sent.append(kwargs.get("json"))
            return requests.post(*args, **kwargs)
        return self.writer(fault=fault, http_post=post)

    def production_runner(self, writer: pbw.BridgeDraftWriter, store: tcs.CheckpointStore) -> tdr.TranslationDraftRunner:
        return tdr.TranslationDraftRunner(reader=self.reader, writer=writer, store=store,
                                          read_catalog=self.read_catalog, render=cwd.render_content)

    def create_vi(self, number: int) -> dict[str, Any]:
        """The VI draft through TSYC's own payload builder (pre-order branch)."""
        code, internal = f"{CODE_PREFIX}{number:04d}", uid(number)
        rows = content_rows(internal, number)
        media = self.attachments(code.lower())
        product = {"product_code": code, "internal_product_id": internal, "candidate_id": uid(number + 500),
                   "author": "Lab Author A", "product_metadata": {}}
        candidate = {"candidate_code": code, "source_evidence": {"origin": candidate_origin.FAHASA_DISCOVERY_ORIGIN,
                                                                 "supply_status": pir.PREORDER_SUPPLY_STATUS}}
        payload = cwd.build_woocommerce_payload(product, rows[0], [{"wordpress_media_id": m} for m in media],
                                                preorder=pir.is_fahasa_preorder_candidate(candidate))
        response = self.lab.wc("POST", "products", json=payload)
        expect(response.status_code == 201, f"VI create: {response.status_code} {response.text[:400]}")
        vi = self.lab.product(response.json()["id"])
        row = lpm.LanguageProduct(
            internal_product_id=internal, product_code=code, language_code="vi", woocommerce_product_id=int(vi["id"]),
            origin=lpm.ORIGIN_TSYC_CREATED, mapping_status=lpm.RECORDED, baseline_source=lpm.BASELINE_CREATE_RESPONSE,
            product_content_id=rows[0]["product_content_id"], baseline_modified_gmt=vi["date_modified_gmt"],
            baseline_text={f: str(vi.get(f) or "") for f in wtg.TRANSLATED_FIELDS})
        return {"code": code, "internal": internal, "rows": rows, "media": media, "candidate": candidate,
                "payload": payload, "vi": vi, "row": row}

    # ---- Gate A -----------------------------------------------------------

    def a01_new_book(self) -> dict[str, Any]:
        code = f"{CODE_PREFIX}0001"
        candidate = {"candidate_code": code, "source_evidence": {"origin": candidate_origin.FAHASA_DISCOVERY_ORIGIN,
                                                                 "supply_status": pir.PREORDER_SUPPLY_STATUS}}
        expect(pir.is_fahasa_preorder_candidate(candidate), "Not a pre-order candidate.")
        by_sku = self.lab.wc("GET", "products", params={"sku": code, "status": "any"})
        expect(by_sku.status_code == 200 and by_sku.json() == [], f"SKU {code} already exists: {by_sku.text[:200]}")
        title = content_rows(uid(1), 1)[0]["product_name"]
        by_title = self.lab.wc("GET", "products", params={"search": title, "status": "any"})
        expect(by_title.status_code == 200 and by_title.json() == [], f"A product titled like the book exists: {by_title.text[:200]}")
        self.snapshot("s0_before")
        return {"product_code": code, "sku_matches": 0, "title_matches": 0,
                "catalog_products_before": len(self.snapshots["s0_before"]["products"])}

    def a03_vi_draft(self) -> dict[str, Any]:
        self.b1 = self.create_vi(1)
        vi, payload = self.b1["vi"], self.b1["payload"]
        expect(not set(payload) & set(pir.PRICE_FIELDS) and "stock_status" not in payload, f"Payload fields {sorted(payload)}")
        expect(payload["status"] == "draft", payload["status"])
        group = self.reader.read_group(vi["id"])
        expect(group.lang == "vi" and dict(group.translations) == {"vi": vi["id"]}, f"{group}")
        decision = pw.classify(self.b1["row"], vi, self.b1["rows"], candidate=self.b1["candidate"], catalog_conflict=False)
        expect(decision.eligible, f"Workflow: {decision.as_dict()}")
        unchecked = pw.classify(self.b1["row"], vi, self.b1["rows"], candidate=self.b1["candidate"], catalog_conflict=None)
        expect(not unchecked.eligible, "A book without a catalog check was eligible.")
        return {"vi_product_id": vi["id"], "sku": vi["sku"], "status": vi["status"], "workflow": decision.workflow,
                "eligible": decision.eligible, "payload_inventory": {k: payload[k] for k in pir.INVENTORY_FIELDS}}

    def a02_content(self) -> dict[str, Any]:
        prepared = tbi.build_book_input(self.b1["row"], self.b1["rows"], cwd.render_content)
        expect(prepared.ready, f"{prepared.refusal}: {prepared.detail}")
        unapproved = [{**r, "content_status": "DRAFTED"} if r["content_language"] == "de" else r for r in self.b1["rows"]]
        refused = tbi.build_book_input(self.b1["row"], unapproved, cwd.render_content)
        expect(not refused.ready, "A DRAFTED translation was accepted.")
        self.b1["book"] = tdr.BookInput(self.b1["internal"], self.b1["code"], self.b1["row"], prepared.contents, AUTH_ID)
        return {"languages_loaded": sorted(prepared.contents), "unapproved_translation_refused": refused.refusal,
                "content_ids": {lang: c["product_content_id"] for lang, c in prepared.contents.items()}}

    def a04_translations(self) -> dict[str, Any]:
        self.b1["store"] = tcs.CheckpointStore(self.run_dir / "book1_checkpoint.json")
        sent: list[Any] = []
        result = self.production_runner(self.counting_writer(sent), self.b1["store"]).run_book(self.b1["book"], create_authorized=True)
        for lang in wtg.TRANSLATION_LANGUAGES:
            language = result.languages[lang]
            expect(language.result == tdr.RESULT_CREATED, f"{lang}: {language.result} {language.code} {language.detail}")
        expect(len(sent) == 2, f"{len(sent)} create requests for two languages.")
        for body in sent:
            leaked = set(body) & (set(pir.PRICE_FIELDS) | {"status", "sku", "stock_quantity", "manage_stock", "backorders"})
            expect(not leaked, f"Request carried {sorted(leaked)}")
        self.b1["ids"] = {"vi": int(self.b1["vi"]["id"]), **{lang: int(result.languages[lang].product_id) for lang in wtg.TRANSLATION_LANGUAGES}}
        problems = lpm.validate_mappings([self.b1["row"], *result.proposed_rows])
        expect(not problems and len(result.proposed_rows) == 2, f"{[p.as_dict() for p in problems]}")
        return {"ids": self.b1["ids"], "create_requests": len(sent), "proposed_mapping_rows": len(result.proposed_rows),
                "written_to_supabase": 0}

    def read_trio(self) -> None:
        ids = self.b1["ids"]
        products = {lang: self.lab.product(pid) for lang, pid in ids.items()}
        views = {lang: dict(self.reader.read_group(pid).translations) for lang, pid in ids.items()}
        book = self.b1["book"]
        expected = {"vi": {"name": self.b1["payload"]["name"], "description": self.b1["payload"]["description"],
                           "short_description": self.b1["payload"]["short_description"]},
                    **{lang: ma.build_text_payload(book.contents[lang], lang, cwd.render_content)
                       for lang in wtg.TRANSLATION_LANGUAGES}}
        self.b1["products"] = products
        self.b1["verification"] = ptv.verify_trio(
            internal_product_id=self.b1["internal"], product_code=self.b1["code"], products=products,
            group_views=views, expected_text=expected)
        self.b1["views"] = views

    def verified(self, check_id: str, evidence: Callable[[], dict[str, Any]]) -> Callable[[], dict[str, Any]]:
        def run() -> dict[str, Any]:
            if "verification" not in self.b1:
                self.read_trio()
            problems = self.b1["verification"][check_id]
            expect(not problems, "; ".join(problems))
            return evidence()
        return run

    def a11_nothing_else(self) -> dict[str, Any]:
        delta = bbx.diff_snapshots(self.snapshots["s0_before"], self.snapshot("s1_after_create"))
        trio = sorted(self.b1["ids"].values())
        expect(delta["added"] == trio, f"Added {delta['added']}, expected exactly {trio}.")
        expect(not delta["removed"] and not delta["changed"], f"Removed {delta['removed']}, changed {delta['changed']}.")
        return {"products_before": len(self.snapshots["s0_before"]["products"]), "added": delta["added"],
                "removed": delta["removed"], "changed_existing_products": 0,
                "bystanders_unchanged": self.bystanders}

    def a12_repeat(self) -> dict[str, Any]:
        book = self.b1["book"]
        sent: list[Any] = []
        again = self.production_runner(self.counting_writer(sent), self.b1["store"]).run_book(book, create_authorized=True)
        expect(all(v.result == tdr.RESULT_ALREADY_CONFIRMED for v in again.languages.values()),
               f"{ {k: (v.result, v.code) for k, v in again.languages.items()} }")
        expect(not sent, f"A repeat sent {len(sent)} create requests.")
        # The checkpoint is lost: the runner must not create a second EN/DE.
        lost: list[Any] = []
        blind = self.production_runner(self.counting_writer(lost), tcs.CheckpointStore(self.run_dir / "book1_lost_checkpoint.json")
                                       ).run_book(book, create_authorized=True)
        expect(all(v.result != tdr.RESULT_CREATED for v in blind.languages.values()),
               f"A lost checkpoint created again: { {k: v.result for k, v in blind.languages.items()} }")
        # The same VI payload a second time: WooCommerce itself refuses the SKU.
        twin = self.lab.wc("POST", "products", json=self.b1["payload"])
        expect(twin.status_code == 400 and "sku" in twin.text.lower(), f"Second VI create: {twin.status_code} {twin.text[:200]}")
        delta = bbx.diff_snapshots(self.snapshots["s1_after_create"], self.snapshot("s2_after_repeat"))
        expect(bbx.is_empty(delta), f"The repeat changed the catalog: {delta}")
        return {"repeat": {k: v.result for k, v in again.languages.items()}, "create_requests_on_repeat": 0,
                "lost_checkpoint": {k: [v.result, v.action or v.code] for k, v in blind.languages.items()},
                "lost_checkpoint_requests_that_created": 0, "duplicate_vi_sku_http": twin.status_code,
                "catalog_diff_after_repeat": "empty"}

    def a13_recovery_and_rollback(self) -> dict[str, Any]:
        second = self.create_vi(2)
        prepared = tbi.build_book_input(second["row"], second["rows"], cwd.render_content)
        expect(prepared.ready, f"{prepared.refusal}: {prepared.detail}")
        book = tdr.BookInput(second["internal"], second["code"], second["row"], prepared.contents, AUTH_ID)
        path = self.run_dir / "book2_checkpoint.json"
        crashed = self.production_runner(self.writer(fault="after_create"), tcs.CheckpointStore(path)).run_book(book, create_authorized=True)
        expect(crashed.languages["en"].result == tdr.RESULT_UNCERTAIN, f"{crashed.languages['en']}")
        # One language at a time: DE must not be sent while EN is unreconciled.
        expect(crashed.languages["de"].code == tdr.WAITING_FOR_UNRECONCILED_LANGUAGE,
               f"DE ran past an unreconciled EN: {crashed.languages['de']}")
        expect(tcs.CheckpointStore(path).get(second["internal"], "de") is None, "A DE request was recorded during the crash run.")
        # A new process: only the checkpoint on disk survives.
        store = tcs.CheckpointStore(path)
        resumed = self.production_runner(self.writer(), store).run_book(book, create_authorized=True)
        expect(resumed.languages["en"].result == tdr.RESULT_RECONCILED, f"{resumed.languages['en']}")
        expect(resumed.languages["de"].result == tdr.RESULT_CREATED, f"{resumed.languages['de']}")
        vi_id = int(second["vi"]["id"])
        group = dict(self.reader.read_group(vi_id).translations)
        expect(set(group) == {"vi", "en", "de"}, f"{group}")
        claimed = self.lab.eval_json(f"""
$ids = get_posts(array('post_type'=>'product','post_status'=>'any','lang'=>'','fields'=>'ids','posts_per_page'=>-1,'suppress_filters'=>false,
 'meta_query'=>array(array('key'=>'{wtg.TSYC_OWNER_META_KEY}','value'=>'{second['internal']}'))));
echo json_encode(count($ids));""")
        expect(claimed == 3, f"{claimed} products claim book 2 after the resume (expected 3).")

        rolled: dict[str, Any] = {}
        for lang in ("de", "en"):
            operation = store.get(second["internal"], lang)
            outcome = self.writer().rollback(operation.request_id, operation.modified_gmt or "")
            expect(outcome.kind == pbw.CONFIRMED, f"rollback {lang}: {outcome}")
            status = self.lab.eval_json(f"echo json_encode(get_post_status({operation.product_id}));")
            expect(status == "trash", f"{lang} #{operation.product_id} is {status}")
            replay = self.writer().create_translation_draft(self._request_for(operation, book, renderer=cwd.render_content))
            expect(replay.kind == pbw.REFUSED and replay.code == "tsyc_request_rolled_back", f"replay {lang}: {replay}")
            rolled[lang] = {"product_id": operation.product_id, "status": status, "replay": replay.code}
        expect(dict(self.reader.read_group(vi_id).translations) == {"vi": vi_id}, "The VI source is still linked.")
        vi_after = self.lab.product(vi_id)
        for name in ("date_modified_gmt", "status", "sku", "regular_price", "stock_quantity", "backorders", "name"):
            expect(vi_after[name] == second["vi"][name], f"VI {name} changed: {second['vi'][name]!r} -> {vi_after[name]!r}")

        delta = bbx.diff_snapshots(self.snapshots["s2_after_repeat"], self.snapshot("s3_after_recovery"))
        mine = sorted([vi_id, rolled["en"]["product_id"], rolled["de"]["product_id"]])
        expect(delta["added"] == mine, f"Added {delta['added']}, expected {mine}.")
        expect(not delta["removed"] and not delta["changed"], f"Something else changed: {delta}")
        first_trio = dict(self.reader.read_group(self.b1["ids"]["vi"]).translations)
        expect(first_trio == self.b1["ids"], f"Book 1's group changed: {first_trio}")
        return {"crash_point": "after_create (EN)", "after_resume": {k: v.result for k, v in resumed.languages.items()},
                "products_claiming_book_after_resume": claimed, "rollback": rolled,
                "vi_source_unchanged": True, "catalog_diff": {"added": delta["added"], "changed_existing_products": 0},
                "book1_group_unchanged": True}

    # ---- the developer's black-box package, on the real lab stack ----------

    def x01_blackbox_package(self) -> dict[str, Any]:
        folder = self.run_dir / "blackbox"
        self.snapshot("00_baseline", folder)
        raw = self.lab.wp("eval-file", str(DRIVER_PHP), "create", "--user=lab-admin")
        driver = json.loads(raw.strip().splitlines()[-1])
        expect(driver.get("ok") and set(driver["trio"]) == {"vi", "en", "de"}, f"driver: {driver}")
        trio = {k: int(v) for k, v in driver["trio"].items()}
        self.snapshot("01_trio_created", folder)
        # Native baseline for Gate B: plain REST saves, no theme. Orders are not placed.
        for label, lang, body in (("02_price_on_vi", "vi", {"regular_price": "111000"}),
                                  ("03_price_on_en", "en", {"regular_price": "222000"}),
                                  ("04_stock_on_vi", "vi", {"stock_quantity": 5}),
                                  ("05_backorders_on_de", "de", {"backorders": "no"})):
            response = self.lab.wc("PUT", f"products/{trio[lang]}", json=body)
            expect(response.status_code == 200, f"{label}: {response.status_code}")
            self.snapshot(label, folder)
        cleanup = json.loads(self.lab.wp("eval-file", str(DRIVER_PHP), "cleanup", str(trio["vi"]), "--user=lab-admin").strip().splitlines()[-1])
        expect(cleanup.get("ok") and set(cleanup["trashed"]) == {"vi", "en", "de"}, f"cleanup: {cleanup}")
        self.snapshot("10_cleanup", folder)
        report = bbx.evaluate(folder)
        (folder / "blackbox_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        expect(report["status"] == "EVALUATED" and report["trio"] == trio, f"{report['status']} {report['trio']}")
        expect(report["created_by"] == bbx.CREATED_BY_BRIDGE, report["created_by"])
        site = {c["id"]: c["status"] for c in report["gate_a_site_checks"]}
        expect(site == {"A05": bbx.PASS, "A07": bbx.PASS, "A08": bbx.PASS, "A11": bbx.PASS}, f"{report['gate_a_site_checks']}")
        observed = {c["id"]: c["status"] for c in report["gate_b_checks"]}
        expect(observed["B04"] == bbx.BLOCKED and observed["B05"] == bbx.BLOCKED, f"Order steps were not run: {observed}")
        self.blackbox_report = report
        return {"driver_trio": trio, "package_gate_a_checks": site, "package_gate_b_checks": observed,
                "note": "Lab stack only. On this stack the package's Gate B lines are the native baseline, never the store's."}

    def x02_driver_guards(self) -> dict[str, Any]:
        raw = self.lab.wp("eval-file", str(DRIVER_PHP), "create", check=False)
        answer = json.loads(raw.strip().splitlines()[-1])
        expect(answer.get("ok") is False and answer.get("code") == "NO_USER", f"{answer}")
        foreign = self.lab.wp("eval-file", str(DRIVER_PHP), "cleanup", str(self.b1["ids"]["vi"]), "--user=lab-admin", check=False)
        refused = json.loads(foreign.strip().splitlines()[-1])
        expect(refused.get("code") == "NOT_A_DRIVER_PRODUCT", f"{refused}")
        expect(self.lab.product(self.b1["ids"]["vi"])["status"] == "draft", "The driver touched a product it did not make.")
        return {"no_user": answer["code"], "cleanup_of_foreign_product": refused["code"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lab-dir", type=Path, default=base.default_lab_dir())
    parser.add_argument("--out", type=Path, default=base.PROJECT_ROOT / "data" / "output" / "preorder_gate_a")
    args = parser.parse_args(argv)
    run_dir = args.out / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)

    lab = Lab(args.lab_dir.resolve())
    lab.start_database()
    lab.wipe_products()
    lab.auth = ("lab-admin", lab.wp("user", "application-password", "create", "lab-admin", f"gate-a-{time.time()}", "--porcelain"))
    lab.start_server()
    gate = GateA(lab, run_dir)
    gate.reader.auth = lab.auth
    versions: dict[str, Any] = {}
    try:
        versions = requests.get(f"{STORE}/wp-json/tsyc/v1/health", auth=lab.auth, timeout=HTTP_TIMEOUT).json()
        print(json.dumps(versions))
        # Bystanders that must come out untouched: the owner's published, priced
        # trio and an unrelated TSYC draft of another book.
        owner_trio = gate._published_group("BYSTANDER", "lab-bystander")
        _other_book, other_vi = gate.make_book(f"{base.SKU_PREFIX}OTHER", uid(900))
        gate.bystanders = {"owner_published_trio": owner_trio, "unrelated_tsyc_draft": other_vi["id"]}
        gate.b1 = {}

        gate.step("A01", gate.a01_new_book)
        if gate.step("A03", gate.a03_vi_draft) and gate.step("A02", gate.a02_content) and gate.step("A04", gate.a04_translations):
            b1 = gate.b1
            gate.step("A05", gate.verified("A05", lambda: {"group": b1["ids"], "same_from_every_member": True}))
            gate.step("A06", gate.verified("A06", lambda: {
                "internal_product_id": b1["internal"], "skus": {k: p["sku"] for k, p in b1["products"].items()},
                "language_meta": {k: wtg.meta_value(p, wtg.TSYC_LANGUAGE_META_KEY) for k, p in b1["products"].items()}}))
            gate.step("A07", gate.verified("A07", lambda: {k: {f: p[f] for f in ("status", "manage_stock", "stock_quantity", "backorders", "stock_status")}
                                                           for k, p in b1["products"].items()}))
            gate.step("A08", gate.verified("A08", lambda: {k: {f: p.get(f) for f in (*pir.PRICE_FIELDS, "purchasable")}
                                                           for k, p in b1["products"].items()}))
            gate.step("A09", gate.verified("A09", lambda: {k: {"name": p["name"], "matches_approved_package": True}
                                                           for k, p in b1["products"].items()}))
            gate.step("A10", gate.verified("A10", lambda: {k: {"images": [i["id"] for i in p["images"]],
                                                               "attributes": {a["name"]: a["options"] for a in p["attributes"]},
                                                               "weight": p.get("weight")} for k, p in b1["products"].items()}))
            gate.step("A11", gate.a11_nothing_else)
            gate.step("A12", gate.a12_repeat)
            gate.step("A13", gate.a13_recovery_and_rollback)
            gate.step("X01", gate.x01_blackbox_package, gate=False)
            gate.step("X02", gate.x02_driver_guards, gate=False)
        lab.wipe_products()
    finally:
        lab.stop_all()
        lab.stop_database()

    gate_a = ig.evaluate_gate_a(gate.checks, environment=ig.ENV_GENERIC_LOCAL_LAB)
    baseline = ig.check_results(getattr(gate, "blackbox_report", {}).get("gate_b_checks", []))
    gate_b = ig.evaluate_gate_b(baseline, environment=ig.ENV_GENERIC_LOCAL_LAB)
    report = {
        "ran_at": datetime.now(timezone.utc).isoformat(), "versions": versions, "database": lab.database,
        "not_in_lab": ["Flatsome + Flatsome Child", "pll-bulk-link-translations v1/v2", "Duplicate Post RB", "production data"],
        "gate_a": gate_a.as_dict(), "gate_b": gate_b.as_dict(), "blackbox_package_self_test": gate.extra,
        "production_pilot_unmet": ig.production_pilot_requirements(
            gate_a_lab=gate_a, gate_a_site_copy=None, developer_reviewed_bridge=False, owner_authorized_pilot=False),
        "production_writes": {"woo": 0, "polylang_links": 0, "price_or_stock": 0, "publish": 0},
    }
    path = run_dir / "gate_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"GATE A ({gate_a.environment}): {gate_a.status}  {list(gate_a.reasons)}")
    print(f"GATE B ({gate_b.environment}): {gate_b.status}  {list(gate_b.reasons)}")
    print(f"Black-box package self-test: { {item['id']: item['status'] for item in gate.extra} }")
    print(f"Report: {path}")
    package_ok = all(item["status"] == ig.CHECK_PASS for item in gate.extra) and len(gate.extra) == 2
    return 0 if gate_a.status == ig.GATE_PASS and package_ok else 1


if __name__ == "__main__":
    sys.exit(main())
