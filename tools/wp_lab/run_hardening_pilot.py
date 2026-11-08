"""Hardening pilot: the cases run_local_pilot.py does not reach.

Result labels, never interchangeable:

  PASS_REAL_LOCAL     real WordPress, WooCommerce and Polylang, no theme.
  PASS_SYNTHETIC_HOOK TSYC's guards reacted correctly to an ASSUMED hook from
                      tsyc-lab-synthetic-hooks.php. Says nothing about the store.
  OBSERVED_REAL_SOURCE The developer's code was active: what it did is recorded
                      for a person to read. Never a pass.
  BLOCKED_MISSING_SOURCE Needs the developer's source, which is not installed.
  FAIL                An expectation did not hold.

Synthetic data on the loopback lab only. No production URL, key or .env value is
read. Run files go to data/output/polylang_hardening/<run>/ (gitignored).

Usage:
  .venv/Scripts/python.exe tools/wp_lab/provision_lab.py
  .venv/Scripts/python.exe tools/wp_lab/run_hardening_pilot.py [--lab-dir PATH] [--update-fixture]
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import contextlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_local_pilot as base  # noqa: E402
from run_local_pilot import AUTH_ID, HTTP_TIMEOUT, PORT, PORT2, SKU_PREFIX, Check, Lab, Pilot, expect  # noqa: E402

from src.domain import multilingual_adapter as ma  # noqa: E402
from src.domain import translation_create_state as tcs  # noqa: E402
from src.domain import woo_translation_group as wtg  # noqa: E402
from src.services import polylang_bridge_writer as pbw  # noqa: E402
from src.services import translation_draft_runner as tdr  # noqa: E402

PASS_REAL = base.PASS_REAL
PASS_SYNTHETIC = "PASS_SYNTHETIC_HOOK"
OBSERVED = "OBSERVED_REAL_SOURCE"
BLOCKED_SOURCE = "BLOCKED_MISSING_SOURCE"
FIXTURE = base.PROJECT_ROOT / "tests" / "fixtures" / "php_hooks" / "measured_hook_events.json"
PROTECTED = ("status", "regular_price", "sale_price", "manage_stock", "stock_quantity", "backorders", "stock_status",
             "sku", "date_modified_gmt", "name", "description", "short_description")


def uid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


class Hardening(Pilot):
    def __init__(self, lab: Lab, run_dir: Path) -> None:
        super().__init__(lab, run_dir)
        self.traces: dict[str, Any] = {}
        self.counter = 100
        self.stress_rounds = 12

    # ---- helpers ----------------------------------------------------------

    def next_book(self, tag: str) -> tuple[tdr.BookInput, dict[str, Any]]:
        self.counter += 1
        return self.make_book(f"{SKU_PREFIX}{tag}", uid(self.counter))

    def store_for(self, tag: str) -> tcs.CheckpointStore:
        return tcs.CheckpointStore(self.run_dir / f"{tag}.json")

    def fields(self, pid: int) -> dict[str, Any]:
        product = self.lab.product(pid)
        return {k: product.get(k) for k in PROTECTED}

    def set_modes(self, *modes: str, **options: int) -> None:
        names = json.dumps(list(modes))
        extra = "".join(f"update_option('tsyc_lab_{k}', {int(v)});" for k, v in options.items())
        self.lab.eval_json(f"update_option('tsyc_lab_hook_modes', json_decode('{names}', true)); {extra} echo json_encode(1);")

    @contextlib.contextmanager
    def modes(self, *modes: str, **options: int) -> Iterator[None]:
        self.set_modes(*modes, **options)
        try:
            yield
        finally:
            self.set_modes()

    @contextlib.contextmanager
    def trace(self, name: str, roles: Mapping[str, int]) -> Iterator[None]:
        """Record what really fires, attributing each event to a role, not an ID."""
        trace_file = self.lab.site / "wp-content" / "tsyc-lab-trace" / f"{name}.jsonl"
        trace_file.unlink(missing_ok=True)
        self.lab.eval_json(f"update_option('tsyc_lab_trace_id', '{name}'); echo json_encode(1);")
        try:
            yield
        finally:
            self.lab.eval_json("delete_option('tsyc_lab_trace_id'); echo json_encode(1);")
            events = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines()] if trace_file.exists() else []
            by_id = {int(pid): role for role, pid in roles.items()}
            summary: dict[str, dict[str, Any]] = {}
            for event in events:
                role = by_id.get(int(event["id"]), "NONE" if not event["id"] else "OTHER")
                key = f"{event['hook']}:{event['key']}" if event["hook"].endswith("post_meta") and event["key"] else event["hook"]
                entry = summary.setdefault(key, {"count": 0, "roles": set(), "languages": set()})
                entry["count"] += 1
                entry["roles"].add(role)
                if event.get("lang"):
                    entry["languages"].add(event["lang"])
            self.traces[name] = {key: {"count": e["count"], "roles": sorted(e["roles"]), "languages": sorted(e["languages"])}
                                 for key, e in sorted(summary.items())}

    def body(self, book: tdr.BookInput, vi: Mapping[str, Any], lang: str, request_id: str) -> dict[str, Any]:
        return {"source_id": vi["id"], "lang": lang, "request_id": request_id, "authorization_id": AUTH_ID,
                "internal_product_id": book.internal_product_id, "product_code": book.product_code,
                "expected_source_modified_gmt": vi["date_modified_gmt"], "inventory_profile": "FAHASA_PREORDER",
                "content": {"name": f"Copy {lang}", "description": "", "short_description": ""}}

    def request(self, book: tdr.BookInput, vi: Mapping[str, Any], lang: str, request_id: str) -> pbw.CreateRequest:
        return pbw.CreateRequest(
            source_id=vi["id"], lang=lang, request_id=request_id, authorization_id=AUTH_ID,
            internal_product_id=book.internal_product_id, product_code=book.product_code,
            expected_source_modified_gmt=vi["date_modified_gmt"],
            content={"name": f"Copy {lang}", "description": "", "short_description": ""})

    def owner_sibling(self, vi_id: int, lang: str, *, stock: int = 7, price: str = "10.00") -> int:
        """A published, priced copy the owner made: linked, no TSYC meta, stock above zero."""
        return self.lab.eval_json(f"""
$p = new WC_Product_Simple();
$p->set_name('Owner copy {lang}');
$p->set_status('publish');
$p->set_regular_price('{price}');
$p->set_manage_stock(true);
$p->set_stock_quantity({stock});
$p->set_backorders('no');
$id = $p->save();
pll_set_post_language($id, '{lang}');
$group = pll_get_post_translations({vi_id});
$group['vi'] = {vi_id};
$group['{lang}'] = $id;
pll_save_post_translations($group);
echo json_encode($id);""")

    def group_of(self, pid: int) -> dict[str, int]:
        return dict(self.reader.read_group(pid).translations)

    def state_of(self, pid: int) -> str:
        return self.lab.eval_json(f"echo json_encode(get_post_meta({pid}, '_tsyc_bridge_state', true));")

    def counting_writer(self, sent: list[Any], fault: str | None = None) -> pbw.BridgeDraftWriter:
        def post(*args: Any, **kwargs: Any) -> Any:
            sent.append(kwargs.get("json"))
            return requests.post(*args, **kwargs)
        return self.writer(fault=fault, http_post=post)

    # ---- real local: creates ----------------------------------------------

    def h01_en_from_vi(self) -> dict[str, Any]:
        book, vi = self.next_book("H01")
        before = self.fields(vi["id"])
        only = tdr.BookInput(book.internal_product_id, book.product_code, book.vi_row, {"en": book.contents["en"]}, AUTH_ID)
        with self.trace("bridge_create_en", {"SOURCE": vi["id"]}):
            result = self.runner(self.writer(), self.store_for("h01")).run_book(only, create_authorized=True)
        en = result.languages["en"]
        expect(en.result == tdr.RESULT_CREATED, f"{en}")
        expect(self.fields(vi["id"]) == before, f"The source changed: {before} -> {self.fields(vi['id'])}")
        copy = self.lab.product(en.product_id)
        expect((copy["status"], copy["regular_price"], copy["sale_price"], copy["sku"]) == ("draft", "", "", ""), f"{copy}")
        expect(self.group_of(vi["id"]) == {"vi": vi["id"], "en": en.product_id}, f"{self.group_of(vi['id'])}")
        self._h01 = (book, vi, en.product_id)
        return {"source_unchanged": True, "copy": en.product_id, "copy_inventory": [copy["stock_quantity"], copy["backorders"]]}

    def h02_de_after_en(self) -> dict[str, Any]:
        book, vi, en_id = self._h01
        before = {pid: self.fields(pid) for pid in (vi["id"], en_id)}
        request = self.request(book, vi, "de", "tsyc-tr-de-h02-after-en")
        with self.trace("bridge_create_de_after_en", {"SOURCE": vi["id"], "SIBLING_DRAFT": en_id}):
            outcome = self.writer().create_translation_draft(request)
        expect(outcome.kind == pbw.CONFIRMED, f"{outcome}")
        for pid, snapshot in before.items():
            expect(self.fields(pid) == snapshot, f"#{pid} changed: {snapshot} -> {self.fields(pid)}")
        group = self.group_of(en_id)
        expect(group == {"vi": vi["id"], "en": en_id, "de": outcome.product_id}, f"{group}")
        expect(all(self.group_of(pid) == group for pid in group.values()), "Asymmetric group.")
        self._h02 = (book, vi, en_id, outcome.product_id, outcome.modified_gmt)
        return {"group": group, "source_and_en_unchanged": True}

    def h03_save_events(self) -> dict[str, Any]:
        """Which hooks really fire. The list a theme hook would have to be on to react."""
        book, vi = self.next_book("H03")
        ids = self._published_group("EVENTS", "lab-events")
        with self.trace("wc_rest_vi_price_update", {"SOURCE": ids["vi"], "SIBLING": ids["en"]}):
            self.lab.wc("PUT", f"products/{ids['vi']}", json={"regular_price": "11.00"})
        with self.trace("wc_rest_en_stock_update", {"SOURCE": ids["vi"], "TARGET": ids["en"]}):
            self.lab.wc("PUT", f"products/{ids['en']}", json={"stock_quantity": 3})
        with self.trace("order_on_en_backorder", {"SOURCE": ids["vi"], "TARGET": ids["en"]}):
            self._order(ids["en"])
        create = self.writer().create_translation_draft(self.request(book, vi, "en", "tsyc-tr-en-h03-events"))
        expect(create.kind == pbw.CONFIRMED, f"{create}")
        with self.trace("bridge_rollback", {"SOURCE": vi["id"], "TARGET": create.product_id}):
            back = self.writer().rollback(create.request_id, create.modified_gmt or "")
        expect(back.kind == pbw.CONFIRMED, f"{back}")
        created = self.traces["bridge_create_en"]
        saved_roles = {role for key, entry in created.items() if key.startswith(("save_post", "woocommerce_update_product", "woocommerce_new_product")) for role in entry["roles"]}
        expect(saved_roles <= {"OTHER"}, f"A bridge create saved something other than its own draft: {saved_roles}")
        return {"scenarios": sorted(self.traces), "bridge_create_saves_only_its_own_draft": True,
                "language_at_save_post_product": created.get("save_post_product", {}).get("languages"),
                "language_at_woocommerce_new_product": created.get("woocommerce_new_product", {}).get("languages"),
                "source_touched_only_by": sorted(k for k, e in created.items() if "SOURCE" in e["roles"]),
                "finding": "Every save hook fires on the new copy before it is linked, so a save-time sync has no siblings to "
                           "reach during a bridge create. The source is touched only by set_object_terms, so a hook there can."}

    def h04_preorder_shape(self) -> dict[str, Any]:
        book, vi = self.next_book("H04")
        outcome = self.writer().create_translation_draft(self.request(book, vi, "en", "tsyc-tr-en-h04-shape"))
        expect(outcome.kind == pbw.CONFIRMED, f"{outcome}")
        shape = self.lab.eval_json(f"""
$p = wc_get_product({outcome.product_id});
echo json_encode(array('manage'=>$p->get_manage_stock('edit'),'stock'=>$p->get_stock_quantity('edit'),'backorders'=>$p->get_backorders('edit'),
 'status'=>$p->get_stock_status('edit'),'purchasable'=>$p->is_purchasable(),'backorders_allowed'=>$p->backorders_allowed(),
 'on_backorder'=>$p->is_on_backorder(1),'price'=>$p->get_price('edit'),'sku'=>$p->get_sku('edit')));""")
        expect(shape["manage"] is True and shape["stock"] == 0 and shape["backorders"] == "yes", f"{shape}")
        expect(shape["status"] == "onbackorder", f"{shape}")
        expect(shape["purchasable"] is False, f"An unpriced draft is purchasable: {shape}")
        expect(shape["price"] == "" and shape["sku"] == "", f"{shape}")
        return shape

    def h05_sibling_with_stock(self) -> dict[str, Any]:
        book, vi = self.next_book("H05")
        de = self.owner_sibling(vi["id"], "de")
        before = self.fields(de)
        outcome = self.writer().create_translation_draft(self.request(book, vi, "en", "tsyc-tr-en-h05-sibling"))
        expect(outcome.kind == pbw.CONFIRMED, f"{outcome}")
        expect(self.fields(de) == before, f"The owner's DE copy changed: {before} -> {self.fields(de)}")
        expect(before["stock_quantity"] == 7 and before["regular_price"] == "10.00", f"{before}")
        group = self.group_of(vi["id"])
        expect(group == {"vi": vi["id"], "de": de, "en": outcome.product_id}, f"{group}")
        # TSYC never adopts it: no SKU and no record means its identity is unproven.
        plan = self.runner(self.writer(), self.store_for("h05")).run_book(book, create_authorized=True)
        expect(plan.resolution_status != ma.RESOLVED, f"{plan.resolution_status}")
        return {"owner_copy_untouched": before, "group": group, "runner_on_this_group": plan.resolution_status}

    def h06_price_native(self) -> dict[str, Any]:
        ids = self._published_group("H06", "lab-h06")
        read = lambda: {lang: self.lab.product(pid)["regular_price"] for lang, pid in ids.items()}  # noqa: E731
        self.lab.wc("PUT", f"products/{ids['vi']}", json={"regular_price": "12.00"})
        after_vi = read()
        self.lab.wc("PUT", f"products/{ids['en']}", json={"regular_price": "14.00"})
        after_en = read()
        expect(after_vi == {"vi": "12.00", "en": "10.00", "de": "10.00"}, f"{after_vi}")
        expect(after_en == {"vi": "12.00", "en": "14.00", "de": "10.00"}, f"{after_en}")
        return {"vi_to_siblings": after_vi, "en_to_siblings": after_en,
                "note": "Native baseline only: no direction propagates. The store's own direction needs the source (T11)."}

    def h07_sku(self) -> dict[str, Any]:
        book, vi = self.next_book("H07")
        evidence: dict[str, Any] = {}
        # Through the WooCommerce API a second product cannot take the SKU.
        other = self.lab.wc("POST", "products", json={"name": "Other", "status": "draft", "sku": book.product_code})
        expect(other.status_code == 400 and "sku" in other.json().get("code", ""), f"{other.status_code} {other.text[:200]}")
        evidence["woo_api_second_product"] = other.json()["code"]
        # But raw meta can: what a metadata hook would do.
        ids = self.lab.eval_json(f"""
$p = new WC_Product_Simple(); $p->set_name('Raw copy'); $p->set_status('draft'); $id = $p->save();
pll_set_post_language($id, 'en');
update_post_meta($id, '_sku', '{book.product_code}');
$after = wc_get_product($id);
echo json_encode(array('id'=>$id,'raw'=>get_post_meta($id,'_sku',true),'wc'=>$after->get_sku('edit'),
 'unique_check'=>wc_product_has_unique_sku($id, '{book.product_code}'),'by_sku'=>wc_get_product_id_by_sku('{book.product_code}')));""")
        evidence["raw_meta_copy"] = ids
        expect(ids["raw"] == book.product_code, f"{ids}")
        # A save of the raw copy must not fail or change the shared SKU.
        saved = self.lab.wc("PUT", f"products/{ids['id']}", json={"short_description": "Saved."})
        evidence["save_of_raw_copy"] = saved.status_code
        # An edit screen or API client that resends the value the product already has.
        resend = self.lab.wc("PUT", f"products/{ids['id']}", json={"sku": book.product_code})
        evidence["resend_same_sku_on_raw_copy"] = {"http": resend.status_code, "code": resend.json().get("code")}
        evidence["sku_after_resend"] = self.lab.product(ids["id"])["sku"]
        source_resend = self.lab.wc("PUT", f"products/{vi['id']}", json={"sku": book.product_code})
        evidence["resend_same_sku_on_original"] = {"http": source_resend.status_code, "code": source_resend.json().get("code")}
        # That resend moved the source's modified time, which TSYC reads as an owner edit.
        book = tdr.BookInput(book.internal_product_id, book.product_code, self._row_with_baseline(book, self.lab.product(vi["id"])),
                             book.contents, AUTH_ID)
        matches = self.lab.wc("GET", "products", params={"sku": book.product_code, "status": "any", "context": "edit"}).json()
        evidence["sku_query_returns"] = sorted(p["id"] for p in matches)
        # TSYC must not treat the unlinked twin as anything but a conflict.
        result = self.runner(self.writer(), self.store_for("h07")).run_book(book, create_authorized=True)
        expect(result.resolution_status == ma.CONFLICT and result.languages["en"].action == ma.REFUSE,
               f"{result.resolution_status} {result.languages['en']}")
        evidence["unlinked_sku_twin"] = result.resolution_status
        # An explicit SKU on a bridge request is impossible.
        forbidden = self.lab.bridge_post("translations", {**self.body(book, vi, "en", "tsyc-tr-en-h07-sku"), "sku": "X"})
        expect(forbidden.status_code == 400 and forbidden.json()["code"] == "tsyc_forbidden_fields", forbidden.text[:200])
        evidence["sku_in_request"] = forbidden.json()["code"]
        return evidence

    @staticmethod
    def _row_with_baseline(book: tdr.BookInput, product: Mapping[str, Any]) -> Any:
        return base.lpm.LanguageProduct(**{**book.vi_row.__dict__, "baseline_modified_gmt": product["date_modified_gmt"]})

    def h08_owner_published(self) -> dict[str, Any]:
        book, vi, en_id, de_id, de_modified = self._h02
        evidence: dict[str, Any] = {}
        # The owner publishes TSYC's EN draft.
        self.lab.wc("PUT", f"products/{en_id}", json={"status": "publish", "regular_price": "15.00"})
        published = self.fields(en_id)
        replay = self.writer().rollback("tsyc-tr-de-h02-after-en", "1999-01-01T00:00:00")
        expect(replay.kind == pbw.REFUSED and replay.code == "tsyc_rollback_refused", f"{replay}")
        evidence["rollback_with_stale_baseline"] = replay.code
        status = self.lab.eval_json(f"echo json_encode(get_post_meta({en_id},'_tsyc_request_id',true));")
        refused = self.writer().rollback(status, str(published["date_modified_gmt"]))
        expect(refused.code == "tsyc_rollback_refused", f"{refused}")
        evidence["rollback_of_published"] = refused.detail
        # A DE create against a group with a published member leaves it alone.
        book2, vi2 = self.next_book("H08")
        de2 = self.owner_sibling(vi2["id"], "de", stock=4, price="9.00")
        before = self.fields(de2)
        created = self.writer().create_translation_draft(self.request(book2, vi2, "en", "tsyc-tr-en-h08-published"))
        expect(created.kind == pbw.CONFIRMED and self.fields(de2) == before, f"{created}")
        evidence["published_sibling_unchanged"] = before["status"]
        # A published source is never a bridge source (T10b) and an edited copy is never rolled back (T10c).
        self.lab.wc("PUT", f"products/{vi2['id']}", json={"status": "publish"})
        now = self.lab.product(vi2["id"])
        source = self.writer().create_translation_draft(pbw.CreateRequest(
            **{**self.request(book2, vi2, "en", "tsyc-tr-en-h08-other").__dict__, "expected_source_modified_gmt": now["date_modified_gmt"]}))
        expect(source.code == "tsyc_source_not_draft", f"{source}")
        evidence["published_source"] = source.code
        return evidence

    # ---- real local: concurrency -------------------------------------------

    def h09_concurrency(self) -> dict[str, Any]:
        evidence: dict[str, Any] = {}
        server = self.lab.start_server(PORT2)
        try:
            # (a) EN and DE at once, DE arriving inside EN's link window: the
            # defect found 2026-11-03 left {vi, en} with both answering 201.
            book, vi = self.next_book("H09A")
            with futures.ThreadPoolExecutor(2) as pool:
                en = pool.submit(self.lab.bridge_post, "translations", self.body(book, vi, "en", "tsyc-tr-en-h09a-race"),
                                 {"X-TSYC-Lab-Delay": "before_link=5"}, "default", PORT)
                time.sleep(1.5)
                de = pool.submit(self.lab.bridge_post, "translations", self.body(book, vi, "de", "tsyc-tr-de-h09a-race"),
                                 None, "default", PORT2)
                answers = (en.result(), de.result())
            expect(answers[0].status_code == 201 and answers[1].status_code == 409 and answers[1].json()["code"] == "tsyc_busy",
                   f"{[a.text[:200] for a in answers]}")
            retry = self.lab.bridge_post("translations", self.body(book, vi, "de", "tsyc-tr-de-h09a-race"))
            expect(retry.status_code == 201, retry.text[:300])
            group = self.group_of(vi["id"])
            expect(set(group) == {"vi", "en", "de"}, f"Lost a member: {group}")
            evidence["en_de_race"] = {"second_writer": "tsyc_busy", "group_after_retry": sorted(group)}

            # (b) Unaided: many parallel pairs, no delay, no member may be lost.
            lost = []
            for round_number in range(self.stress_rounds):
                book_r, vi_r = self.next_book(f"H09R{round_number}")
                with futures.ThreadPoolExecutor(2) as pool:
                    pair = list(pool.map(
                        lambda job: self.lab.bridge_post("translations", self.body(book_r, vi_r, job[0], f"tsyc-tr-{job[0]}-h09r{round_number}"),
                                                         None, "default", job[1]),
                        (("en", PORT), ("de", PORT2))))
                linked = self.group_of(vi_r["id"])
                for lang, answer in zip(("en", "de"), pair):
                    if answer.status_code == 201 and linked.get(lang) != answer.json()["product_id"]:
                        lost.append((round_number, lang, linked))
            expect(not lost, f"A confirmed member was lost: {lost}")
            evidence["unaided_rounds_without_lost_member"] = self.stress_rounds

            # (c) Rollback of EN while DE is being created.
            book_c, vi_c = self.next_book("H09C")
            first = self.writer().create_translation_draft(self.request(book_c, vi_c, "en", "tsyc-tr-en-h09c-race"))
            expect(first.kind == pbw.CONFIRMED, f"{first}")
            with futures.ThreadPoolExecutor(2) as pool:
                de_answer = pool.submit(self.lab.bridge_post, "translations", self.body(book_c, vi_c, "de", "tsyc-tr-de-h09c-race"),
                                        {"X-TSYC-Lab-Delay": "before_link=5"}, "default", PORT)
                time.sleep(1.5)
                back = pool.submit(self.lab.bridge_post, "translations/rollback",
                                   {"request_id": "tsyc-tr-en-h09c-race", "expected_modified_gmt": first.modified_gmt}, None, "default", PORT2)
                results = (de_answer.result(), back.result())
            expect(results[0].status_code == 201, results[0].text[:300])
            expect(results[1].status_code == 409 and results[1].json()["code"] == "tsyc_busy", results[1].text[:300])
            evidence["rollback_during_create"] = results[1].json()["code"]

            # (d) A real owner save from another request, between the insert and the last check.
            book_d, vi_d = self.next_book("H09D")
            outcome = self._create_during_owner_edit(book_d, vi_d, "after_create", "tsyc-tr-en-h09d-edit")
            expect(outcome.kind == pbw.REFUSED and outcome.code == "tsyc_source_changed" and outcome.product_id, f"{outcome}")
            expect(self.group_of(vi_d["id"]) == {"vi": vi_d["id"]}, f"Linked anyway: {self.group_of(vi_d['id'])}")
            evidence["owner_edit_before_link"] = {"code": outcome.code, "partial_draft_reported": outcome.product_id}

            # (e) The same, after the last check: inside the link window.
            book_e, vi_e = self.next_book("H09E")
            late = self._create_during_owner_edit(book_e, vi_e, "before_link", "tsyc-tr-en-h09e-edit")
            expect(late.kind == pbw.REFUSED and late.code == pbw.BRIDGE_SIDE_EFFECT, f"{late}")
            expect(any(c.get("field") == "modified_gmt" and c.get("product_id") == vi_e["id"] for c in late.changes), f"{late.changes}")
            expect(self.state_of(late.product_id) == "SIDE_EFFECT", self.state_of(late.product_id))
            evidence["owner_edit_inside_link_window"] = late.code
        finally:
            self.lab.stop_server(server)

        # (f) An expired lock is taken over, a live one is not, and the old holder cannot free the new one's.
        locks = self.lab.eval_json("""
$key = 'tsyc_bridge_lock_987654';
delete_option($key);
tsyc_bridge_lock_insert($key, (time() - 500) . ':stale');
$taken = tsyc_bridge_acquire_lock($key);
$second = tsyc_bridge_acquire_lock($key);
tsyc_bridge_release_lock($key, '1:someone-else');
$third = tsyc_bridge_acquire_lock($key);
tsyc_bridge_release_lock($key, $taken);
$fourth = tsyc_bridge_acquire_lock($key);
tsyc_bridge_release_lock($key, (string) $fourth);
echo json_encode(array('took_expired'=>$taken !== null,'second_refused'=>$second === null,'other_cannot_release'=>$third === null,'holder_released'=>$fourth !== null));""")
        expect(all(locks.values()), f"{locks}")
        evidence["lock"] = locks
        return evidence

    def _create_during_owner_edit(self, book: tdr.BookInput, vi: Mapping[str, Any], stage: str, request_id: str) -> pbw.CreateOutcome:
        """The create pauses at one stage while a separate request saves the source.
        The second server is needed: the first is single-threaded and busy."""
        writer = self.writer()
        writer.extra_headers = {"X-TSYC-Lab-Delay": f"{stage}=6"}
        with futures.ThreadPoolExecutor(1) as pool:
            create = pool.submit(writer.create_translation_draft, self.request(book, vi, "en", request_id))
            time.sleep(2.5)  # A modified time has one-second resolution.
            edit = requests.put(f"http://127.0.0.1:{PORT2}/wp-json/wc/v3/products/{vi['id']}", auth=self.lab.auth,
                                json={"short_description": "Owner edit from another request."}, timeout=HTTP_TIMEOUT)
            expect(edit.status_code == 200, f"Owner edit: {edit.status_code}")
            return create.result()

    # ---- real local: interrupted and rollback -------------------------------

    def h10_interrupted(self) -> dict[str, Any]:
        evidence: dict[str, Any] = {}
        # The link is written, the worker dies before the state is stamped.
        book, vi = self.next_book("H10A")
        store = self.store_for("h10a")
        only = tdr.BookInput(book.internal_product_id, book.product_code, book.vi_row, {"en": book.contents["en"]}, AUTH_ID)
        before = self.lab.lab_skus_count()
        first = self.runner(self.writer(fault="after_link"), store).run_book(only, create_authorized=True)
        expect(first.languages["en"].result == tdr.RESULT_UNCERTAIN, f"{first.languages['en']}")
        found = self.reader.read_request(store.get(book.internal_product_id, "en").request_id)
        expect(found.found and found.state == "LANGUAGE_SET" and found.translations.get("vi") == vi["id"], f"{found}")
        reloaded = self.store_for("h10a")
        again = self.runner(self.writer(), reloaded).run_book(only, create_authorized=True)
        expect(again.languages["en"].result == tdr.RESULT_RECONCILED, f"{again.languages['en']}")
        expect(self.lab.lab_skus_count() == before + 1, "A duplicate appeared.")
        evidence["crash_after_link"] = {"state_at_crash": found.state, "resume": again.languages["en"].result}

        # What the first attempt did to a sibling cannot be hidden by a crash.
        book_b, vi_b = self.next_book("H10B")
        de = self.owner_sibling(vi_b["id"], "de")
        crash = self.writer(fault="after_create").create_translation_draft(self.request(book_b, vi_b, "en", "tsyc-tr-en-h10b"))
        expect(crash.kind == pbw.UNCERTAIN, f"{crash}")
        self.lab.wc("PUT", f"products/{de}", json={"stock_quantity": 2})
        resumed = self.writer().create_translation_draft(self.request(book_b, vi_b, "en", "tsyc-tr-en-h10b"))
        expect(resumed.kind == pbw.REFUSED and resumed.code == pbw.BRIDGE_SIDE_EFFECT, f"{resumed}")
        expect(any(c.get("product_id") == de and c.get("field") == "_stock" for c in resumed.changes), f"{resumed.changes}")
        evidence["sibling_changed_during_outage"] = resumed.code
        again_b = self.writer().create_translation_draft(self.request(book_b, vi_b, "en", "tsyc-tr-en-h10b"))
        expect(again_b.code == pbw.BRIDGE_SIDE_EFFECT, f"{again_b}")
        evidence["replay_after_stop"] = again_b.code

        # A partial draft without its snapshot is never finished.
        book_c, vi_c = self.next_book("H10C")
        self.writer(fault="after_create").create_translation_draft(self.request(book_c, vi_c, "en", "tsyc-tr-en-h10c"))
        found_c = self.reader.read_request("tsyc-tr-en-h10c")
        self.lab.eval_json(f"delete_post_meta({found_c.product_id}, '_tsyc_group_snapshot'); echo json_encode(1);")
        missing = self.writer().create_translation_draft(self.request(book_c, vi_c, "en", "tsyc-tr-en-h10c"))
        expect(missing.code == "tsyc_snapshot_missing", f"{missing}")
        evidence["snapshot_missing"] = missing.code
        return evidence

    def h11_rollback(self) -> dict[str, Any]:
        evidence: dict[str, Any] = {}
        book, vi = self.next_book("H11")
        en = self.writer().create_translation_draft(self.request(book, vi, "en", "tsyc-tr-en-h11"))
        de = self.writer().create_translation_draft(self.request(book, vi, "de", "tsyc-tr-de-h11"))
        before_vi = self.fields(vi["id"])
        before_de = self.fields(de.product_id)
        # The middle member goes: the other two stay linked to each other.
        first = self.writer().rollback(en.request_id, en.modified_gmt or "")
        expect(first.kind == pbw.CONFIRMED, f"{first}")
        group = self.group_of(de.product_id)
        expect(group == {"vi": vi["id"], "de": de.product_id}, f"{group}")
        expect(self.fields(vi["id"]) == before_vi and self.fields(de.product_id) == before_de, "A sibling changed.")
        evidence["middle_member"] = group
        # The last member: the source is alone again and otherwise untouched.
        last = self.writer().rollback(de.request_id, de.modified_gmt or "")
        expect(last.kind == pbw.CONFIRMED, f"{last}")
        expect(self.group_of(vi["id"]) == {"vi": vi["id"]} and self.fields(vi["id"]) == before_vi, "The source changed.")
        evidence["last_member"] = "source alone and unchanged"
        # A replay is refused, while a NEW request (new approved content) may create again.
        replay = self.writer().create_translation_draft(self.request(book, vi, "en", "tsyc-tr-en-h11"))
        expect(replay.code == "tsyc_request_rolled_back", f"{replay}")
        fresh = self.writer().create_translation_draft(self.request(book, vi, "en", "tsyc-tr-en-h11-new-content"))
        expect(fresh.kind == pbw.CONFIRMED and fresh.product_id != en.product_id, f"{fresh}")
        evidence["recreate_after_rollback"] = {"same_request": replay.code, "new_request": "new product"}
        # A partial draft that never linked can be undone.
        book_p, vi_p = self.next_book("H11P")
        self.writer(fault="after_create").create_translation_draft(self.request(book_p, vi_p, "en", "tsyc-tr-en-h11p"))
        found = self.reader.read_request("tsyc-tr-en-h11p")
        modified = self.lab.product(found.product_id)["date_modified_gmt"]
        partial = self.writer().rollback("tsyc-tr-en-h11p", modified)
        expect(partial.kind == pbw.CONFIRMED and self.group_of(vi_p["id"]) == {"vi": vi_p["id"]}, f"{partial}")
        evidence["partial_draft"] = "rolled back"
        # With an owner's stocked sibling in the group, a rollback leaves it alone.
        book_s, vi_s = self.next_book("H11S")
        sibling = self.owner_sibling(vi_s["id"], "de")
        before = self.fields(sibling)
        made = self.writer().create_translation_draft(self.request(book_s, vi_s, "en", "tsyc-tr-en-h11s"))
        undone = self.writer().rollback(made.request_id, made.modified_gmt or "")
        expect(undone.kind == pbw.CONFIRMED and self.fields(sibling) == before, f"{undone}")
        expect(self.group_of(sibling) == {"vi": vi_s["id"], "de": sibling}, f"{self.group_of(sibling)}")
        evidence["stocked_sibling_unchanged"] = before["stock_quantity"]
        return evidence

    # ---- real local: native "+" --------------------------------------------

    def h12_native_plus(self) -> dict[str, Any]:
        evidence: dict[str, Any] = {}
        session = self.admin_session()
        # (a) A person makes EN with "+"; the bridge adds DE and keeps that EN.
        book, vi = self.next_book("H12A")
        links = self.plus_links(session, vi["id"], "en")
        expect(links, "No + link.")
        plus_en = self.plus_save(session, links[0], vi["id"], "en", "Person's EN")
        de = self.writer().create_translation_draft(self.request(book, vi, "de", "tsyc-tr-de-h12a"))
        expect(de.kind == pbw.CONFIRMED, f"{de}")
        group = self.group_of(vi["id"])
        expect(group == {"vi": vi["id"], "en": plus_en, "de": de.product_id}, f"{group}")
        evidence["plus_en_then_bridge_de"] = group
        # TSYC plans nothing over it: it has neither the SKU nor a TSYC record.
        plan = self.runner(self.writer(), self.store_for("h12a")).run_book(book, create_authorized=True)
        expect(plan.languages["en"].action == ma.REFUSE, f"{plan.languages['en']}")
        evidence["runner_on_person_made_en"] = plan.languages["en"].action
        # And the bridge refuses a create on that taken slot.
        taken = self.writer().create_translation_draft(self.request(book, vi, "en", "tsyc-tr-en-h12a"))
        expect(taken.code == "tsyc_translation_exists", f"{taken}")
        evidence["bridge_on_taken_slot"] = taken.code

        # (b) The bridge makes EN; a person makes DE with "+" from the VI draft.
        book_b, vi_b = self.next_book("H12B")
        en = self.writer().create_translation_draft(self.request(book_b, vi_b, "en", "tsyc-tr-en-h12b"))
        before_en = self.fields(en.product_id)
        links = self.plus_links(session, vi_b["id"], "de")
        expect(links, "No + link for DE.")
        plus_de = self.plus_save(session, links[0], vi_b["id"], "de", "Person's DE")
        group_b = self.group_of(en.product_id)
        expect(group_b == {"vi": vi_b["id"], "en": en.product_id, "de": plus_de}, f"{group_b}")
        expect(self.fields(en.product_id) == before_en, "The bridge's EN changed.")
        evidence["bridge_en_then_plus_de"] = group_b
        evidence["plus_de_copied_tsyc_meta"] = self.lab.eval_json(
            f"echo json_encode(array_values(array_filter(array_keys(get_post_meta({plus_de})), function($k){{return strpos($k,'_tsyc')===0;}})));")
        # Polylang's metabox still prints the add link for a taken slot, hidden, so
        # the URL stays reachable; (c) shows what using it does.
        evidence["add_link_present_in_html_for_taken_slot"] = bool(self.plus_links(session, vi_b["id"], "en"))

        # (c) A hand-made URL forces a second EN onto the taken slot.
        book_c, vi_c = self.next_book("H12C")
        en_c = self.writer().create_translation_draft(self.request(book_c, vi_c, "en", "tsyc-tr-en-h12c"))
        forced = self.plus_save(session, self.plus_links(session, vi_c["id"], "en")[0], vi_c["id"], "en", "Forced second EN")
        group_c = self.group_of(vi_c["id"])
        evidence["forced_second_en"] = {"group": group_c, "bridge_en": en_c.product_id, "forced": forced,
                                        "evicted_bridge_en": group_c.get("en") != en_c.product_id}
        if group_c.get("en") != en_c.product_id:
            replay = self.writer().create_translation_draft(self.request(book_c, vi_c, "en", "tsyc-tr-en-h12c"))
            expect(replay.code == "tsyc_link_changed", f"{replay}")
            expect(self.group_of(vi_c["id"]) == group_c, "A replay undid a person's change.")
            evidence["forced_second_en"]["replay"] = replay.code
        return evidence

    def h13_hreflang(self) -> dict[str, Any]:
        """L1/L2: the storefront's hreflang against the bridge's read, on published pages."""
        ids = self._published_group("H13", "lab-h13")
        pages = {lang: self.lab.product(pid)["permalink"] for lang, pid in ids.items()}
        seen: dict[str, dict[str, str]] = {}
        for lang, url in pages.items():
            page = requests.get(url, timeout=HTTP_TIMEOUT)
            expect(page.status_code == 200, f"{lang} page {url}: {page.status_code}")
            seen[lang] = dict(wtg.parse_hreflang_links(page.text))
        wanted = {lang: wtg.normalise_url(url) for lang, url in pages.items()}
        for lang, links in seen.items():
            expect({k: wtg.normalise_url(v) for k, v in links.items() if k in wanted} == wanted,
                   f"{lang}: hreflang {links}, permalinks {wanted}")
        expect(self.group_of(ids["vi"]) == ids, f"{self.group_of(ids['vi'])} != {ids}")
        # A draft sibling is in Polylang's map, and on no page: hreflang alone cannot prove absence.
        book, vi = self.next_book("H13D")
        draft = self.writer().create_translation_draft(self.request(book, vi, "en", "tsyc-tr-en-h13"))
        expect(draft.kind == pbw.CONFIRMED and self.group_of(vi["id"]).get("en") == draft.product_id, f"{draft}")
        self.lab.wc("PUT", f"products/{ids['de']}", json={"status": "draft"})
        reduced = dict(wtg.parse_hreflang_links(requests.get(pages["vi"], timeout=HTTP_TIMEOUT).text))
        expect("de" not in reduced, f"A draft appears in hreflang: {reduced}")
        expect("de" in self.group_of(ids["vi"]), "The bridge read must still see the draft.")
        return {"hreflang_matches_bridge_read": True, "members": sorted(ids),
                "draft_member": "in the bridge read, absent from hreflang"}

    # ---- synthetic hooks: TSYC's reactions under assumed behaviour ---------

    def y01_stock_pushed_to_sibling(self) -> tuple[str, dict[str, Any]]:
        book, vi = self.next_book("Y01")
        de = self.owner_sibling(vi["id"], "de")
        with self.modes("inventory_push_on_link"):
            outcome = self.writer().create_translation_draft(self.request(book, vi, "en", "tsyc-tr-en-y01"))
        expect(outcome.kind == pbw.REFUSED and outcome.code == pbw.BRIDGE_SIDE_EFFECT, f"{outcome}")
        # The hook runs for each member as the group is saved, so the stocked
        # owner copy is what gets pushed: onto the VI draft and onto the new copy.
        hit = [c for c in outcome.changes if c.get("product_id") == vi["id"] and c.get("field") == "_stock"]
        expect(hit and hit[0]["before"] == "0" and hit[0]["after"] == "7", f"{outcome.changes}")
        expect(any(c.get("code") == "DRAFT_SHAPE" and c.get("field") == "_stock" for c in outcome.changes), f"{outcome.changes}")
        expect(self.lab.product(vi["id"])["stock_quantity"] == 7, "The assumed hook did not run; the test proves nothing.")
        expect(self.lab.product(de)["stock_quantity"] == 7, f"{self.lab.product(de)['stock_quantity']}")
        expect(self.state_of(outcome.product_id) == "SIDE_EFFECT", self.state_of(outcome.product_id))
        return PASS_SYNTHETIC, {"reported": list(outcome.changes), "left_unrepaired": True, "state": "SIDE_EFFECT",
                                "meaning": "On a link event a stocked sibling can overwrite the VI draft, not only the reverse."}

    def y02_same_value_push(self) -> tuple[str, dict[str, Any]]:
        book, _vi = self.next_book("Y02")
        with self.modes("inventory_push_on_link"):
            result = self.runner(self.writer(), self.store_for("y02")).run_book(book, create_authorized=True)
        expect(result.languages["en"].result == tdr.RESULT_CREATED, f"{result.languages['en']}")
        expect(result.languages["de"].result == tdr.RESULT_CREATED, f"{result.languages['de']}")
        return PASS_SYNTHETIC, {"result": "both created",
                                "meaning": "Pushing identical 0/yes changes nothing and fires no meta hook (update_post_meta "
                                           "skips an equal value), so it neither stops nor can be seen. It is also harmless."}

    def y03_price_pulled_onto_copy(self) -> tuple[str, dict[str, Any]]:
        book, vi = self.next_book("Y03")
        self.owner_sibling(vi["id"], "de")
        with self.modes("price_pull_on_link"):
            outcome = self.writer().create_translation_draft(self.request(book, vi, "en", "tsyc-tr-en-y03"))
        expect(outcome.code == pbw.BRIDGE_SIDE_EFFECT, f"{outcome}")
        expect(any(c.get("code") == "DRAFT_SHAPE" and c.get("field") == "_regular_price" for c in outcome.changes), f"{outcome.changes}")
        return PASS_SYNTHETIC, {"changes": list(outcome.changes)}

    def y04_sku_copied_by_store(self) -> tuple[str, dict[str, Any]]:
        book, vi = self.next_book("Y04")
        with self.modes("sku_copy_on_link"):
            result = self.runner(self.writer(), self.store_for("y04")).run_book(book, create_authorized=True)
        expect(result.languages["en"].result == tdr.RESULT_CREATED and result.languages["de"].result == tdr.RESULT_CREATED,
               f"{result.languages}")
        copy = self.lab.product(result.languages["en"].product_id)
        expect(copy["sku"] == book.product_code, f"{copy['sku']}")
        rerun = self.runner(self.writer(), self.store_for("y04")).run_book(book, create_authorized=True)
        expect(all(v.result == tdr.RESULT_ALREADY_CONFIRMED for v in rerun.languages.values()), f"{rerun.languages}")
        link_map = self.reader.read_translations(vi["id"])
        runner = self.runner(self.writer(), self.store_for("y04"))
        resolution, _p, _i = runner.resolve(book, link_map)
        expect(resolution.status == ma.RESOLVED, f"{resolution.status} {resolution.reasons}")
        return PASS_SYNTHETIC, {"copy_sku": copy["sku"], "adapter": resolution.status,
                                "meaning": "The VI SKU on a linked TSYC copy is accepted as one logical product."}

    def y05_sibling_saved(self) -> tuple[str, dict[str, Any]]:
        book, _vi = self.next_book("Y05")
        sent: list[Any] = []
        store = self.store_for("y05")
        with self.modes("touch_siblings_on_link"):
            result = self.runner(self.counting_writer(sent), store).run_book(book, create_authorized=True)
            expect(result.languages["en"].result == tdr.RESULT_REFUSED and result.languages["en"].code == pbw.BRIDGE_SIDE_EFFECT,
                   f"{result.languages['en']}")
            expect(result.languages["de"].code == tdr.BOOK_STOPPED_SIDE_EFFECT, f"{result.languages['de']}")
            expect(len(sent) == 1, f"{len(sent)} POSTs.")
            again = self.runner(self.counting_writer(sent), self.store_for("y05")).run_book(book, create_authorized=True)
        expect(len(sent) == 1 and all(v.code == tdr.BOOK_STOPPED_SIDE_EFFECT for v in again.languages.values()), f"{again.languages}")
        return PASS_SYNTHETIC, {"en": result.languages["en"].code, "de": result.languages["de"].code, "posts": len(sent),
                                "meaning": "The book stops for a person, in this run and the next."}

    def y06_relinked(self) -> tuple[str, dict[str, Any]]:
        book, vi = self.next_book("Y06")
        target = self.lab.eval_json("""
$p = new WC_Product_Simple(); $p->set_name('Other book DE'); $p->set_status('draft'); $id = $p->save();
pll_set_post_language($id, 'de'); echo json_encode($id);""")
        with self.modes("relink_on_link", relink_target=target):
            outcome = self.writer().create_translation_draft(self.request(book, vi, "en", "tsyc-tr-en-y06"))
        expect(outcome.code == pbw.BRIDGE_SIDE_EFFECT, f"{outcome}")
        expect(any(c.get("code") == "GROUP_CHANGED" for c in outcome.changes), f"{outcome.changes}")
        return PASS_SYNTHETIC, {"changes": list(outcome.changes), "meaning": "A bulk-link style match on link is reported, never kept as verified."}

    def y07_outside_group(self) -> tuple[str, dict[str, Any]]:
        book, vi = self.next_book("Y07")
        target = self.lab.eval_json("""
$p = new WC_Product_Simple(); $p->set_name('Unrelated'); $p->set_status('draft'); $id = $p->save(); echo json_encode($id);""")
        with self.modes("outside_write_on_link", outside_target=target):
            outcome = self.writer().create_translation_draft(self.request(book, vi, "en", "tsyc-tr-en-y07"))
        expect(outcome.code == pbw.BRIDGE_SIDE_EFFECT, f"{outcome}")
        expect(any(c.get("code") == "OUTSIDE_GROUP_WRITE" and c.get("product_id") == target for c in outcome.changes), f"{outcome.changes}")
        return PASS_SYNTHETIC, {"changes": list(outcome.changes)}

    def y08_price_directions(self) -> tuple[str, dict[str, Any]]:
        evidence: dict[str, Any] = {}
        for mode, writer_lang in (("price_push_vi_on_save", "vi"), ("price_push_any_on_save", "en")):
            book, vi = self.next_book(f"Y08{writer_lang.upper()}")
            store = self.store_for(f"y08{writer_lang}")
            created = self.runner(self.writer(), store).run_book(book, create_authorized=True)
            ids = {"vi": vi["id"], "en": created.languages["en"].product_id, "de": created.languages["de"].product_id}
            with self.modes(mode):
                self.lab.wc("PUT", f"products/{ids[writer_lang]}", json={"regular_price": "21.00"})
            prices = {lang: self.lab.product(pid)["regular_price"] for lang, pid in ids.items()}
            expect(prices["en"] == "21.00" and prices["de"] == "21.00" and prices["vi"] == "21.00", f"{mode}: {prices}")
            sent: list[Any] = []
            rerun = self.runner(self.counting_writer(sent), self.store_for(f"y08{writer_lang}")).run_book(book, create_authorized=True)
            expect(not sent, f"{mode}: TSYC wrote after a sync: {sent}")
            evidence[mode] = {"prices": prices, "tsyc_posts_afterwards": 0, "rerun": {k: v.result for k, v in rerun.languages.items()}}
        return PASS_SYNTHETIC, {**evidence, "meaning": "Whatever the direction, TSYC sends no price and no further write."}

    # ---- source-dependent ---------------------------------------------------

    def real_source_present(self) -> dict[str, Any]:
        themes = json.loads(self.lab.wp("theme", "list", "--status=active", "--format=json") or "[]")
        plugins = json.loads(self.lab.wp("plugin", "list", "--format=json") or "[]")
        return {"flatsome_child": any(t.get("name") == "flatsome-child" for t in themes),
                "bulk_link": sorted(p["name"] for p in plugins if str(p["name"]).startswith("pll-bulk-link") and p.get("status") == "active")}

    def blocked(self, what: str, needs: str, then: str) -> tuple[str, dict[str, Any]]:
        return BLOCKED_SOURCE, {"not_verified": what, "needs": needs, "unblocks_with": then}

    def observe_or_block(self, what: str, needs: str, then: str, observe: Any) -> tuple[str, dict[str, Any]]:
        present = self.real_source_present()
        if not present["flatsome_child"]:
            return self.blocked(what, needs, then)
        return OBSERVED, {"source": present, "observed": observe(), "read_by_a_person": "required; this is never a pass"}

    def _observe_price_stock(self) -> dict[str, Any]:
        ids = self._published_group("REAL", "lab-real")
        snapshot = lambda: {lang: {k: self.lab.product(pid)[k] for k in ("regular_price", "stock_quantity", "backorders", "sku", "date_modified_gmt")}  # noqa: E731
                            for lang, pid in ids.items()}
        steps = {"start": snapshot()}
        for label, lang, change in (("vi_price", "vi", {"regular_price": "12.00"}), ("en_price", "en", {"regular_price": "14.00"}),
                                    ("vi_stock", "vi", {"stock_quantity": 5}), ("de_stock", "de", {"stock_quantity": 2}),
                                    ("vi_backorders", "vi", {"backorders": "no"}), ("de_save_only", "de", {"short_description": "x"})):
            time.sleep(1.1)
            self.lab.wc("PUT", f"products/{ids[lang]}", json=change)
            steps[label] = snapshot()
        return steps

    def _observe_bridge(self) -> dict[str, Any]:
        book, vi = self.next_book("REALB")
        de = self.owner_sibling(vi["id"], "de")
        before = self.fields(de)
        outcome = self.writer().create_translation_draft(self.request(book, vi, "en", "tsyc-tr-en-real-bridge"))
        return {"outcome": outcome.kind, "code": outcome.code, "changes": list(outcome.changes), "sibling_before": before, "sibling_after": self.fields(de)}

    def _observe_parallel(self) -> dict[str, Any]:
        ids = self._published_group("REALP", "lab-realp")
        self.lab.wc("PUT", f"products/{ids['en']}", json={"stock_quantity": 10})
        with futures.ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda lang: self._order(ids[lang]), ("en", "de", "en", "de")))
        return {lang: self.lab.product(pid)["stock_quantity"] for lang, pid in ids.items()}

    def _bulk_link_blocked(self) -> tuple[str, dict[str, Any]]:
        present = self.real_source_present()
        if not present["bulk_link"]:
            return self.blocked("Whether either bulk-link plugin reacts to a save or a bridge create, and any relink it makes.",
                                "pll-bulk-link-translations v1 and v2", "Activate both in the lab, rerun; hook inventory lists their triggers.")
        return OBSERVED, {"source": present, "observed": self._observe_bridge(), "read_by_a_person": "required; this is never a pass"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab-dir", type=Path, default=base.default_lab_dir())
    parser.add_argument("--out", type=Path, default=base.PROJECT_ROOT / "data" / "output" / "polylang_hardening")
    parser.add_argument("--update-fixture", action="store_true", help="Refresh tests/fixtures/php_hooks/measured_hook_events.json.")
    parser.add_argument("--only", help="Comma-separated test IDs (development only).")
    args = parser.parse_args(argv)
    run_dir = args.out / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)

    lab = Lab(args.lab_dir.resolve())
    lab.start_database()
    lab.wipe_products()
    lab.auth = ("lab-admin", lab.wp("user", "application-password", "create", "lab-admin", f"hardening-{time.time()}", "--porcelain"))
    lab.start_server()
    pilot = Hardening(lab, run_dir)
    pilot.reader.auth = lab.auth
    versions: dict[str, Any] = {}
    try:
        pilot.set_modes()
        versions = requests.get(f"{base.STORE}/wp-json/tsyc/v1/health", auth=lab.auth, timeout=HTTP_TIMEOUT).json()
        print(json.dumps(versions))
        matrix = [
            ("H01", "Create EN from an existing VI draft", pilot.h01_en_from_vi),
            ("H02", "Create DE after EN", pilot.h02_de_after_en),
            ("H03", "Product save events really fired", pilot.h03_save_events),
            ("H04", "Stock 0, backorders yes", pilot.h04_preorder_shape),
            ("H05", "Existing sibling with stock above zero", pilot.h05_sibling_with_stock),
            ("H06", "Price in both directions (native)", pilot.h06_price_native),
            ("H07", "Duplicate SKU handling", pilot.h07_sku),
            ("H08", "Owner-edited and published protection", pilot.h08_owner_published),
            ("H09", "Concurrent updates", pilot.h09_concurrency),
            ("H10", "Interrupted create and resume", pilot.h10_interrupted),
            ("H11", "Rollback", pilot.h11_rollback),
            ("H12", "Native Polylang + next to the bridge", pilot.h12_native_plus),
            ("H13", "Storefront hreflang against the bridge read (L1, L2)", pilot.h13_hreflang),
            ("Y01", "Assumed: stock pushed onto a stocked sibling", pilot.y01_stock_pushed_to_sibling),
            ("Y02", "Assumed: identical values pushed", pilot.y02_same_value_push),
            ("Y03", "Assumed: price pulled onto the new copy", pilot.y03_price_pulled_onto_copy),
            ("Y04", "Assumed: store copies the VI SKU", pilot.y04_sku_copied_by_store),
            ("Y05", "Assumed: siblings saved on link", pilot.y05_sibling_saved),
            ("Y06", "Assumed: relink on link", pilot.y06_relinked),
            ("Y07", "Assumed: write outside the group", pilot.y07_outside_group),
            ("Y08", "Assumed: price sync in either direction", pilot.y08_price_directions),
            ("B11", "Real price and stock sync (T11)", lambda: pilot.observe_or_block(
                "The store's price, stock and backorder sync: fields, direction, triggers.", "flatsome-child source and the parent theme",
                "Install and activate in the lab on MariaDB, rerun.", pilot._observe_price_stock)),
            ("B12", "Real parallel ordering across languages (T12B)", lambda: pilot.observe_or_block(
                "Cross-language stock under concurrent orders.", "flatsome-child source", "As B11; parallel orders on MariaDB.", pilot._observe_parallel)),
            ("B13", "Bridge create with the real theme hooks", lambda: pilot.observe_or_block(
                "Whether the real hooks trigger on a bridge create, and what the guard then reports.", "flatsome-child source",
                "As B11. Stage D gate.", pilot._observe_bridge)),
            ("B14", "Flatsome Child metadata synchronisation", lambda: pilot.blocked(
                "Which meta the theme copies and when, including SKU and _tsyc_*.", "flatsome-child source",
                "inventory_php_hooks.py on the source, then B13.")),
            ("B15", "Bulk-link plugin interference", pilot._bulk_link_blocked),
            ("B16", "Duplicate Post RB copy with the real hooks", lambda: pilot.blocked(
                "A Duplicate Post RB copy plus SKU edit (L5); only WooCommerce's own Duplicate has run.", "Duplicate Post RB 1.6.8 and flatsome-child",
                "Install both in the lab, rerun L5.")),
        ]
        only = set(args.only.split(",")) if args.only else None
        for test_id, title, fn in matrix:
            if only is None or test_id in only:
                pilot.record(test_id, title, fn)
    finally:
        with contextlib.suppress(Exception):
            pilot.set_modes()
            lab.eval_json("delete_option('tsyc_lab_trace_id'); echo json_encode(1);")
        lab.stop_all()
    with contextlib.suppress(Exception):
        lab.wipe_products()
    lab.stop_database()
    report = {"ran_at": datetime.now(timezone.utc).isoformat(), "versions": versions, "database": lab.database,
              "not_in_lab": ["Flatsome + Flatsome Child", "pll-bulk-link-translations v1/v2", "Duplicate Post RB", "production data"],
              "measured_hook_events": pilot.traces, "results": pilot.results}
    path = run_dir / "hardening_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    if args.update_fixture:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps({"measured_on": versions, "note": "Plain WordPress, WooCommerce and Polylang in the lab; no theme.",
                                       "scenarios": pilot.traces}, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for item in pilot.results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    print(json.dumps(counts))
    print(f"Report: {path}")
    return 1 if counts.get(base.FAIL) else 0


if __name__ == "__main__":
    sys.exit(main())
