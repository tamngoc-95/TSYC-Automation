"""End-to-end pilot against the isolated local WordPress lab.

Real WordPress, WooCommerce and Polylang with synthetic data, driven through the
same TSYC reader, writer, runner and adapter production would use. Flatsome
Child and the bulk-link plugins are not in the lab; a step that depends on them
reports BLOCKED_MISSING_DEPENDENCY, and what the lab shows without them is
labelled as the plain-WooCommerce/Polylang baseline, never as the store's.

No production URL, key or .env value is read here. Run files go to
data/output/polylang_local/<run>/ (gitignored).

Usage:
  .venv/Scripts/python.exe tools/wp_lab/provision_lab.py
  .venv/Scripts/python.exe tools/wp_lab/run_local_pilot.py [--lab-dir PATH]
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import html as html_lib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from src.cli_bootstrap import configure_utf8_console  # noqa: E402
from src.domain import language_product_mapping as lpm  # noqa: E402
from src.domain import multilingual_adapter as ma  # noqa: E402
from src.domain import translation_create_state as tcs  # noqa: E402
from src.domain import woo_translation_group as wtg  # noqa: E402
from src.services import polylang_bridge_writer as pbw  # noqa: E402
from src.services import polylang_link_reader as plr  # noqa: E402
from src.services import translation_draft_runner as tdr  # noqa: E402

configure_utf8_console()

# A mock result is never reported from here: offline simulations live in pytest.
PASS_REAL = "PASS_REAL_LOCAL"
BLOCKED = "BLOCKED_MISSING_DEPENDENCY"
FAIL = "FAIL"

PORT = 8765
PORT2 = 8766
STORE = f"http://127.0.0.1:{PORT}"
# Never contacted: require_staging_target needs the host it must refuse.
PRODUCTION_URL_FOR_GUARD = "https://tiemsachyeucon.com"
SKU_PREFIX = "TSYC-LAB-"
AUTH_ID = "lab-synthetic-authorization"
HTTP_TIMEOUT = 180


def default_lab_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "TSYC" / "wp-lab"


def render(content: Mapping[str, Any]) -> tuple[str, str]:
    return str(content.get("description") or ""), str(content.get("short_description") or "")


class Check(Exception):
    """A pilot expectation did not hold."""


def expect(condition: Any, message: str) -> None:
    if not condition:
        raise Check(message)


class Lab:
    def __init__(self, lab_dir: Path) -> None:
        self.dir = lab_dir
        self.php = lab_dir / "php" / "php.exe"
        self.ini = lab_dir / "php" / "php.ini"
        self.cli = lab_dir / "wp-cli" / "wp-cli.phar"
        self.site = lab_dir / "site"
        self.secrets = json.loads((lab_dir / "lab_secrets.json").read_text(encoding="utf-8"))
        self.auth: tuple[str, str] = ("lab-admin", "")
        self.servers: list[subprocess.Popen[bytes]] = []

    def wp(self, *arguments: str, check: bool = True) -> str:
        done = subprocess.run([str(self.php), "-c", str(self.ini), str(self.cli), f"--path={self.site}", *arguments],
                              capture_output=True, encoding="utf-8", errors="replace", timeout=HTTP_TIMEOUT)
        if check and done.returncode != 0:
            raise Check(f"wp {arguments[0]}: {done.stdout[-400:]} {done.stderr[-800:]}")
        return done.stdout.strip()

    def eval_json(self, code: str) -> Any:
        """Run PHP in the lab; the code echoes one JSON value on its last line."""
        with tempfile.NamedTemporaryFile("w", suffix=".php", delete=False, encoding="utf-8") as handle:
            handle.write("<?php\n" + code)
            path = handle.name
        try:
            output = self.wp("eval-file", path)
        finally:
            os.unlink(path)
        return json.loads(output.strip().splitlines()[-1])

    def start_server(self, port: int = PORT, env: Mapping[str, str] | None = None) -> subprocess.Popen[bytes]:
        server = subprocess.Popen(
            [str(self.php), "-c", str(self.ini), "-S", f"127.0.0.1:{port}", "-t", str(self.site),
             str(self.dir / "router.php")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, **(env or {})})
        self.servers.append(server)
        for _ in range(60):
            try:
                requests.get(f"http://127.0.0.1:{port}/wp-json/", timeout=20)
                return server
            except requests.RequestException:
                time.sleep(0.5)
        raise Check(f"The lab server on {port} did not start.")

    def stop_server(self, server: subprocess.Popen[bytes]) -> None:
        server.terminate()
        server.wait(timeout=20)
        self.servers.remove(server)

    def stop_all(self) -> None:
        for server in list(self.servers):
            self.stop_server(server)

    # ---- plain REST -------------------------------------------------------

    def wc(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        return requests.request(method, f"{STORE}/wp-json/wc/v3/{path}", auth=self.auth, timeout=HTTP_TIMEOUT, **kwargs)

    def product(self, pid: int) -> dict[str, Any]:
        response = self.wc("GET", f"products/{pid}", params={"context": "edit"})
        expect(response.status_code == 200, f"#{pid}: HTTP {response.status_code}")
        return response.json()

    def bridge_post(self, path: str, body: Any, headers: Mapping[str, str] | None = None,
                    auth: Any = "default", port: int = PORT) -> requests.Response:
        auth = self.auth if auth == "default" else auth
        return requests.post(f"http://127.0.0.1:{port}/wp-json/tsyc/v1/{path}", auth=auth, json=body,
                             headers={"X-TSYC-Write-Token": self.secrets["write_token"], **(headers or {})},
                             timeout=HTTP_TIMEOUT)

    def bridge_get(self, path: str, auth: Any = "default") -> requests.Response:
        auth = self.auth if auth == "default" else auth
        return requests.get(f"{STORE}/wp-json/tsyc/v1/{path}", auth=auth, timeout=HTTP_TIMEOUT)

    def create_vi_draft(self, code: str, internal_id: str) -> dict[str, Any]:
        """The same shape as TSYC's own VI draft create (status draft, exact SKU,
        internal-product meta, pre-order inventory, no price)."""
        response = self.wc("POST", "products", json={
            "name": f"Sách thử {code}", "status": "draft", "sku": code, "type": "simple",
            "description": "<p>Mô tả tổng hợp cho phòng thí nghiệm.</p>", "short_description": "Tóm tắt.",
            "manage_stock": True, "stock_quantity": 0, "backorders": "yes",
            "meta_data": [{"key": wtg.TSYC_OWNER_META_KEY, "value": internal_id},
                          {"key": "_tsyc_product_code", "value": code}]})
        expect(response.status_code == 201, f"VI create: {response.status_code} {response.text[:300]}")
        return self.product(response.json()["id"])

    def lab_skus_count(self) -> int:
        return self.eval_json("""
$ids = get_posts(array('post_type'=>'product','post_status'=>'any','lang'=>'','fields'=>'ids','posts_per_page'=>-1,'suppress_filters'=>false));
$ids = array_merge($ids, get_posts(array('post_type'=>'product','post_status'=>'trash','lang'=>'','fields'=>'ids','posts_per_page'=>-1,'suppress_filters'=>false)));
echo json_encode(count($ids));""")

    def wipe_products(self) -> None:
        self.eval_json("""
if (wp_get_environment_type() !== 'local' || strpos(home_url(), 'http://127.0.0.1') !== 0) { exit(1); }
$ids = get_posts(array('post_type'=>'product','post_status'=>array('any','trash','auto-draft'),'lang'=>'','fields'=>'ids','posts_per_page'=>-1,'suppress_filters'=>false));
foreach ($ids as $id) { wp_delete_post($id, true); }
echo json_encode(count($ids));""")


class Pilot:
    def __init__(self, lab: Lab, run_dir: Path) -> None:
        self.lab = lab
        self.run_dir = run_dir
        self.results: list[dict[str, Any]] = []
        self.book: tdr.BookInput | None = None
        self.vi: dict[str, Any] = {}
        self.store = tcs.CheckpointStore(run_dir / "pilot_checkpoint.json")
        self.reader = plr.EndpointLinkReader(store_url=STORE, auth=lab.auth, allow_loopback_http=True,
                                             verified_on_staging=True, timeout_seconds=HTTP_TIMEOUT)

    # ---- helpers ----------------------------------------------------------

    def writer(self, fault: str | None = None, http_post: Callable[..., Any] = requests.post) -> pbw.BridgeDraftWriter:
        return pbw.BridgeDraftWriter(
            store_url=STORE, production_url=PRODUCTION_URL_FOR_GUARD, auth=self.lab.auth,
            write_token=self.lab.secrets["write_token"], verified_on_staging=True, allow_loopback_http=True,
            timeout_seconds=HTTP_TIMEOUT, http_post=http_post,
            extra_headers={"X-TSYC-Lab-Fault": fault} if fault else None)

    def read_catalog(self, ids: Any, code: str) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
        products = {int(pid): self.lab.product(int(pid)) for pid in ids}
        response = self.lab.wc("GET", "products", params={"sku": code, "status": "any", "context": "edit"})
        expect(response.status_code == 200, f"SKU read: {response.status_code}")
        return products, [{**p, "permalink": products.get(p["id"], p).get("permalink", p.get("permalink"))}
                          for p in response.json()]

    def runner(self, writer: pbw.BridgeDraftWriter, store: tcs.CheckpointStore | None = None) -> tdr.TranslationDraftRunner:
        return tdr.TranslationDraftRunner(reader=self.reader, writer=writer, store=store or self.store,
                                          read_catalog=self.read_catalog, render=render)

    def make_book(self, code: str, internal_id: str) -> tuple[tdr.BookInput, dict[str, Any]]:
        vi = self.lab.create_vi_draft(code, internal_id)
        row = lpm.LanguageProduct(
            internal_product_id=internal_id, product_code=code, language_code="vi",
            woocommerce_product_id=int(vi["id"]), origin=lpm.ORIGIN_TSYC_CREATED, mapping_status=lpm.RECORDED,
            baseline_source=lpm.BASELINE_CREATE_RESPONSE, baseline_modified_gmt=vi["date_modified_gmt"],
            baseline_text={f: str(vi.get(f) or "") for f in wtg.TRANSLATED_FIELDS})
        contents = {lang: {"content_status": "APPROVED", "review_required": False, "content_language": lang,
                           "product_content_id": f"content-{code}-{lang}", "product_name": f"Lab book {code} ({lang})",
                           "description": f"<p>Synthetic {lang} description.</p>", "short_description": f"Short {lang}."}
                    for lang in ("en", "de")}
        return tdr.BookInput(internal_id, code, row, contents, AUTH_ID), vi

    def record(self, test_id: str, title: str, fn: Callable[[], dict[str, Any] | tuple[str, dict[str, Any]]]) -> None:
        started = time.time()
        try:
            outcome = fn()
            status, evidence = outcome if isinstance(outcome, tuple) else (PASS_REAL, outcome)
        except Check as error:
            status, evidence = FAIL, {"failed": str(error)}
        except Exception as error:  # noqa: BLE001 - one test never stops the pilot
            status, evidence = FAIL, {"error": f"{type(error).__name__}: {error}", "trace": traceback.format_exc()[-1500:]}
        self.results.append({"id": test_id, "title": title, "status": status, "seconds": round(time.time() - started, 1),
                             "evidence": evidence})
        print(f"{status:<28} {test_id:<5} {title}", flush=True)
        if status == FAIL:
            print(json.dumps(evidence, ensure_ascii=False, indent=1, default=str)[:2500], flush=True)

    # ---- matrix -----------------------------------------------------------

    def t01_vi_draft(self) -> dict[str, Any]:
        self.book, self.vi = self.make_book(f"{SKU_PREFIX}0001", "00000000-0000-4000-8000-000000000001")
        vi = self.vi
        expect(vi["status"] == "draft", vi["status"])
        expect(vi["regular_price"] == "" and vi["sale_price"] == "", "A price appeared.")
        expect(vi["manage_stock"] is True and vi["stock_quantity"] == 0 and vi["backorders"] == "yes", "Inventory shape.")
        return {"id": vi["id"], "sku": vi["sku"], "status": vi["status"], "permalink": vi["permalink"]}

    def t02_vi_language(self) -> dict[str, Any]:
        group = self.reader.read_group(self.vi["id"])
        expect(group.lang == "vi", f"wc/v3 create got {group.lang}")
        expect(dict(group.translations) == {"vi": self.vi["id"]}, f"{group.translations}")
        semantics = self.lab.eval_json(f"echo json_encode(pll_set_post_language({self.vi['id']}, 'vi'));")
        expect(semantics is False, "pll_set_post_language should be false when the language is already set.")
        return {"language_from_wc_v3_create": group.lang, "translations": dict(group.translations),
                "pll_set_post_language_same_language_returns": semantics}

    def _run_lang_check(self, lang: str) -> dict[str, Any]:
        result = self.runner(self.writer()).run_book(self.book, create_authorized=True)
        language = result.languages[lang]
        expect(language.result in (tdr.RESULT_CREATED, tdr.RESULT_ALREADY_CONFIRMED),
               f"{lang}: {language.result} {language.action} {language.code} {language.detail}")
        product = self.lab.product(int(language.product_id))
        expect(product["status"] == "draft", "Not a draft.")
        expect(product["regular_price"] == "" and product["sale_price"] == "", "A price appeared.")
        expect(product["manage_stock"] is True and product["stock_quantity"] == 0 and product["backorders"] == "yes",
               "Inventory shape.")
        expect(product["sku"] == "", f"SKU {product['sku']!r} on a bridge-created copy.")
        expect(wtg.meta_value(product, wtg.TSYC_LANGUAGE_META_KEY) == lang, "Language meta.")
        return {"product_id": product["id"], "status": product["status"], "sku": product["sku"],
                "result": language.result, "permalink": product["permalink"]}

    def t03_en(self) -> dict[str, Any]:
        return self._run_lang_check("en")

    def t04_de(self) -> dict[str, Any]:
        return self._run_lang_check("de")

    def t03b_plus_flow(self) -> dict[str, Any]:
        """Polylang's real "+" screen, driven with a logged-in browser session."""
        book, vi = self.make_book(f"{SKU_PREFIX}PLUS", "00000000-0000-4000-8000-00000000000a")
        session = requests.Session()
        session.post(f"{STORE}/wp-login.php", data={"log": "lab-admin", "pwd": self.lab.secrets["admin_password"],
                                                    "wp-submit": "Log In", "testcookie": "1"},
                     cookies={"wordpress_test_cookie": "WP Cookie check"}, timeout=HTTP_TIMEOUT, allow_redirects=False)
        edit = session.get(f"{STORE}/wp-admin/post.php", params={"post": vi["id"], "action": "edit"}, timeout=HTTP_TIMEOUT)
        expect(edit.status_code == 200, f"edit screen {edit.status_code}")
        links = re.findall(r'href="([^"]*post-new\.php[^"]*new_lang=en[^"]*)"', edit.text)
        links += re.findall(r'href=\\?"([^"]*post-new\.php[^"]*new_lang=en[^"]*)', edit.text)
        expect(links, "The VI edit screen shows no Polylang + link for EN.")
        link = html_lib.unescape(links[0]).replace("\\/", "/")
        screen = session.get(link, timeout=HTTP_TIMEOUT)
        expect(screen.status_code == 200, f"post-new {screen.status_code}")
        form = re.search(r"""<form[^>]*\bid=["']post["'][^>]*>(.*)</form>""", screen.text, re.S)
        expect(form, "No classic editor form on the + screen.")
        fields: dict[str, str] = {}
        for tag in re.findall(r"<input\b[^>]*>", form.group(1)):
            name = re.search(r"""\bname=["']([^"']+)["']""", tag)
            value = re.search(r"""\bvalue=(?:"([^"]*)"|'([^']*)')""", tag)
            kind = re.search(r"""\btype=["']([^"']+)["']""", tag)
            if name and (kind is None or kind.group(1).lower() not in ("checkbox", "radio", "submit", "button", "file")):
                fields[name.group(1)] = html_lib.unescape((value.group(1) or value.group(2) or "")) if value else ""
        expect("post_ID" in fields, f"No post_ID among {sorted(fields)[:20]}")
        new_id = int(fields["post_ID"])
        fields.update({"post_title": "Lab book via plus (en)", "content": "Plus flow text", "post_status": "draft",
                       "save": "Save Draft", "product-type": "simple", "post_lang_choice": "en",
                       "post_tr_lang[vi]": str(vi["id"]), "action": "editpost", "originalaction": "editpost",
                       "post_type": "product"})
        save = session.post(f"{STORE}/wp-admin/post.php", data=fields, timeout=HTTP_TIMEOUT, allow_redirects=False)
        expect(save.status_code in (200, 302), f"save {save.status_code}")
        group = self.reader.read_group(vi["id"])
        copied = self.lab.eval_json(f"""
$meta = get_post_meta({new_id});
$keys = array_keys($meta); sort($keys);
echo json_encode(array('keys'=>$keys,'language'=>pll_get_post_language({new_id}),'status'=>get_post_status({new_id}),
 'sku'=>get_post_meta({new_id},'_sku',true),'tsyc'=>array_values(array_filter($keys,function($k){{return strpos($k,'_tsyc')===0;}}))));""")
        expect(group.translations.get("en") == new_id, f"The + flow did not link: {dict(group.translations)}")
        expect(copied["language"] == "en", f"language {copied['language']}")
        self._plus_new_id, self._plus_vi = new_id, vi["id"]
        return {"new_product": new_id, "link_from_form": True, "translations": dict(group.translations),
                "copied_tsyc_meta_keys": copied["tsyc"], "sku_on_copy": copied["sku"], "language": copied["language"],
                "note": "Plain Polylang: no Flatsome Child hooks in the lab. wc/v3 alone cannot do this (T02: default language, no link)."}

    def t05_readback(self) -> dict[str, Any]:
        ids = dict(self.reader.read_group(self.vi["id"]).translations)
        expect(set(ids) == {"vi", "en", "de"}, f"{ids}")
        expect(len(set(ids.values())) == 3, f"{ids}")
        views = {lang: dict(self.reader.read_group(pid).translations) for lang, pid in ids.items()}
        expect(all(v == ids for v in views.values()), f"Asymmetric: {views}")
        return {"translations": ids, "same_group_from_every_member": True}

    def t06_validate(self) -> dict[str, Any]:
        runner = self.runner(self.writer())
        link_map = self.reader.read_translations(self.vi["id"])
        resolution, products, inputs = runner.resolve(self.book, link_map)
        expect(resolution.status == ma.RESOLVED, f"{resolution.status}: {resolution.reasons}")
        owners = {lang: ma.ownership_of(products[pid], lang, internal_product_id=self.book.internal_product_id,
                                        record=inputs.tsyc_records.get(lang))
                  for lang, pid in resolution.members.items() if lang != "vi"}
        expect(all(o == ma.OWN_TSYC_DRAFT_UNCHANGED for o in owners.values()), f"{owners}")
        for lang, pid in resolution.members.items():
            live = self.lab.eval_json(f"echo json_encode(pll_get_post_language({pid}));")
            expect(live == lang, f"#{pid} is {live}, mapped {lang}")
        rows = self.runner(self.writer()).run_book(self.book, create_authorized=True).proposed_rows
        problems = lpm.validate_mappings([self.book.vi_row, *rows])
        expect(not problems, f"{[p.as_dict() for p in problems]}")
        plan = ma.plan_language_actions(resolution, internal_product_id=self.book.internal_product_id,
                                        products_by_id=products, approved_contents=self.book.contents, render=render,
                                        tsyc_records=inputs.tsyc_records, create_authorized=True,
                                        link_mechanism=self.writer())
        expect(set(plan.values()) == {ma.NO_OP}, f"{plan}")
        return {"resolution": resolution.status, "members": resolution.members, "ownership": owners,
                "plan_after_create": plan, "proposed_mapping_rows": len(rows), "written_to_supabase": 0}

    def t07_repeat(self) -> dict[str, Any]:
        before = self.lab.lab_skus_count()
        result = self.runner(self.writer()).run_book(self.book, create_authorized=True)
        expect(all(v.result == tdr.RESULT_ALREADY_CONFIRMED for v in result.languages.values()),
               f"{ {k: v.result for k, v in result.languages.items()} }")
        operation = self.store.get(self.book.internal_product_id, "en")
        replay = self.writer().create_translation_draft(self._request_for(operation))
        expect(replay.kind == pbw.CONFIRMED and replay.replayed and replay.product_id == operation.product_id,
               f"replay {replay}")
        other = pbw.CreateRequest(
            source_id=self.vi["id"], lang="en", request_id="tsyc-tr-en-another-request-id", authorization_id=AUTH_ID,
            internal_product_id=self.book.internal_product_id, product_code=self.book.product_code,
            expected_source_modified_gmt=self.vi["date_modified_gmt"],
            content={"name": "x", "description": "", "short_description": ""})
        second = self.writer().create_translation_draft(other)
        expect(second.kind == pbw.REFUSED and second.code == "tsyc_translation_exists", f"{second}")
        conflict = pbw.CreateRequest(**{**other.__dict__, "request_id": operation.request_id})
        third = self.writer().create_translation_draft(conflict)
        expect(third.kind == pbw.REFUSED and third.code == "tsyc_request_conflict", f"{third}")
        after = self.lab.lab_skus_count()
        expect(before == after, f"Products {before} -> {after}.")
        return {"products_before": before, "products_after": after, "replay_http": replay.http_status,
                "other_request": second.code, "same_id_other_body": third.code}

    def _request_for(self, operation: tcs.LanguageOperation, book: tdr.BookInput | None = None) -> pbw.CreateRequest:
        book = book or self.book
        return pbw.CreateRequest(
            source_id=operation.source_id, lang=operation.lang, request_id=operation.request_id,
            authorization_id=AUTH_ID, internal_product_id=operation.internal_product_id,
            product_code=operation.product_code,
            expected_source_modified_gmt=operation.expected_source_modified_gmt or "",
            content=ma.build_text_payload(book.contents[operation.lang], operation.lang, render))

    def t08_lost_response(self) -> dict[str, Any]:
        book, vi = self.make_book(f"{SKU_PREFIX}0008", "00000000-0000-4000-8000-000000000008")
        store = tcs.CheckpointStore(self.run_dir / "t08.json")
        before = self.lab.lab_skus_count()
        # (a) The create landed, then the answer was lost.
        first = self.runner(self.writer(fault="before_response"), store).run_book(book, create_authorized=True)
        expect(first.languages["en"].result == tdr.RESULT_UNCERTAIN, f"{first.languages['en']}")
        operation = store.get(book.internal_product_id, "en")
        expect(operation.state == tcs.UNCERTAIN, operation.state)
        found = self.reader.read_request(operation.request_id)
        expect(found.found and found.state == "LINKED", f"{found}")
        # DE must wait: the unreconciled EN draft carries this book's meta, so
        # the group is a CONFLICT until EN is recorded.
        expect(first.languages["de"].action == ma.REFUSE, f"DE while EN is uncertain: {first.languages['de']}")
        count_mid = self.lab.lab_skus_count()
        expect(count_mid == before + 1, f"{before} -> {count_mid}")
        second = self.runner(self.writer(), store).run_book(book, create_authorized=True)
        expect(second.languages["en"].result == tdr.RESULT_RECONCILED, f"{second.languages['en']}")
        expect(second.languages["de"].result == tdr.RESULT_CREATED, f"{second.languages['de']}")
        expect(self.lab.lab_skus_count() == before + 2, "A retry created a duplicate.")

        # (b) The request never reached the store: reconcile proves absence first.
        book2, _vi2 = self.make_book(f"{SKU_PREFIX}0009", "00000000-0000-4000-8000-000000000009")
        calls: list[str] = []

        def refuse_connection(*_a: Any, **_k: Any) -> Any:
            calls.append("post")
            raise requests.ConnectionError("simulated: never reached the store")

        store2 = tcs.CheckpointStore(self.run_dir / "t08b.json")
        lost = self.runner(self.writer(http_post=refuse_connection), store2).run_book(book2, create_authorized=True)
        expect(lost.languages["en"].result == tdr.RESULT_UNCERTAIN, f"{lost.languages['en']}")
        pending = store2.get(book2.internal_product_id, "en")
        expect(self.reader.read_request(pending.request_id).found is False, "Should be absent.")
        retried = self.runner(self.writer(), store2).run_book(book2, create_authorized=True)
        expect(retried.languages["en"].result == tdr.RESULT_CREATED, f"{retried.languages['en']}")

        # (c) A failed reconciliation read must not become a retry.
        store3 = tcs.CheckpointStore(self.run_dir / "t08c.json")
        book3, _vi3 = self.make_book(f"{SKU_PREFIX}0010", "00000000-0000-4000-8000-000000000010")
        self.runner(self.writer(fault="before_response"), store3).run_book(book3, create_authorized=True)
        count_before_blind = self.lab.lab_skus_count()

        def failing_get(*_a: Any, **_k: Any) -> Any:
            raise requests.ConnectionError("simulated read failure")

        broken_reader = plr.EndpointLinkReader(store_url=STORE, auth=self.lab.auth, allow_loopback_http=True,
                                               verified_on_staging=True, http_get=failing_get)
        blind_posts: list[str] = []

        def must_not_post(*_a: Any, **_k: Any) -> Any:
            blind_posts.append("post")
            raise AssertionError("POST during a failed reconciliation")

        blocked = tdr.TranslationDraftRunner(reader=broken_reader, writer=self.writer(http_post=must_not_post), store=store3,
                                             read_catalog=self.read_catalog, render=render).run_book(book3, create_authorized=True)
        expect(blocked.languages["en"].code == "RECONCILE_READ_FAILED", f"{blocked.languages['en']}")
        expect(not blind_posts, "A POST was sent without a reconciliation read.")
        expect(self.lab.lab_skus_count() == count_before_blind, "Products changed.")
        return {"a_lost_answer": "UNCERTAIN -> by-request found LINKED -> RECONCILED, no duplicate",
                "b_never_arrived": "UNCERTAIN -> by-request absent -> sent once",
                "c_failed_read": "stays UNCERTAIN, zero POSTs"}

    def t09_editions(self) -> dict[str, Any]:
        book_a, vi_a = self.make_book(f"{SKU_PREFIX}ED-A", "00000000-0000-4000-8000-0000000000e1")
        book_b, vi_b = self.make_book(f"{SKU_PREFIX}ED-B", "00000000-0000-4000-8000-0000000000e2")
        wrong = pbw.CreateRequest(
            source_id=vi_a["id"], lang="en", request_id="tsyc-tr-en-wrong-edition-0001", authorization_id=AUTH_ID,
            internal_product_id=book_b.internal_product_id, product_code=book_b.product_code,
            expected_source_modified_gmt=vi_a["date_modified_gmt"],
            content={"name": "x", "description": "", "short_description": ""})
        refused = self.writer().create_translation_draft(wrong)
        expect(refused.kind == pbw.REFUSED and refused.code == "tsyc_source_identity", f"{refused}")
        # A bulk-link style match that really links another edition in Polylang.
        result_b = self.runner(self.writer(), tcs.CheckpointStore(self.run_dir / "t09b.json")).run_book(
            book_b, create_authorized=True)
        en_of_b = result_b.languages["en"].product_id
        self.lab.eval_json(f"""
$g = pll_get_post_translations({vi_b['id']});
unset($g['en']);
pll_save_post_translations($g);
pll_save_post_translations(array('vi'=>{vi_a['id']},'en'=>{en_of_b}));
echo json_encode(pll_get_post_translations({vi_a['id']}));""")
        store_a = tcs.CheckpointStore(self.run_dir / "t09a.json")
        runner = self.runner(self.writer(), store_a)
        link_map = self.reader.read_translations(vi_a["id"])
        resolution, _p, _i = runner.resolve(book_a, link_map)
        expect(resolution.status != ma.RESOLVED, f"{resolution.status}")
        expect(resolution.status == ma.CONFLICT, f"{resolution.status} {resolution.reasons}")
        run = runner.run_book(book_a, create_authorized=True)
        expect(run.languages["en"].action == ma.REFUSE, f"{run.languages['en']}")
        return {"bridge_refusal": refused.code, "wrongly_linked_group": dict(link_map.translations),
                "adapter": resolution.status, "reasons": resolution.reasons, "plan_en": run.languages["en"].action}

    def t10_owner_protection(self) -> dict[str, Any]:
        evidence: dict[str, Any] = {}
        # (a) The owner edited the VI draft after TSYC's baseline.
        book, vi = self.make_book(f"{SKU_PREFIX}OWN1", "00000000-0000-4000-8000-0000000000f1")
        time.sleep(1.1)
        self.lab.wc("PUT", f"products/{vi['id']}", json={"short_description": "Owner rewrote this."})
        res = self.runner(self.writer(), tcs.CheckpointStore(self.run_dir / "t10a.json")).run_book(book, create_authorized=True)
        expect(res.languages["en"].result in (tdr.RESULT_REFUSED, tdr.RESULT_SKIPPED), f"{res.languages['en']}")
        evidence["edited_source"] = res.languages["en"].code or res.languages["en"].action

        # (b) A published VI product is owner territory.
        book2, vi2 = self.make_book(f"{SKU_PREFIX}OWN2", "00000000-0000-4000-8000-0000000000f2")
        self.lab.wc("PUT", f"products/{vi2['id']}", json={"status": "publish"})
        now = self.lab.product(vi2["id"])
        refused = self.writer().create_translation_draft(pbw.CreateRequest(
            source_id=vi2["id"], lang="en", request_id="tsyc-tr-en-published-source", authorization_id=AUTH_ID,
            internal_product_id=book2.internal_product_id, product_code=book2.product_code,
            expected_source_modified_gmt=now["date_modified_gmt"],
            content={"name": "x", "description": "", "short_description": ""}))
        expect(refused.code == "tsyc_source_not_draft", f"{refused}")
        evidence["published_source"] = refused.code

        # (c) The owner edited TSYC's EN draft.
        operation = self.store.get(self.book.internal_product_id, "de")
        time.sleep(1.1)
        self.lab.wc("PUT", f"products/{operation.product_id}", json={"description": "<p>Owner German text.</p>"})
        runner = self.runner(self.writer())
        resolution, products, inputs = runner.resolve(self.book, self.reader.read_translations(self.vi["id"]))
        owner = ma.ownership_of(products[operation.product_id], "de",
                                internal_product_id=self.book.internal_product_id, record=inputs.tsyc_records["de"])
        expect(owner == ma.OWN_TSYC_DRAFT_EDITED, owner)
        plan = ma.plan_language_actions(resolution, internal_product_id=self.book.internal_product_id,
                                        products_by_id=products, approved_contents=self.book.contents, render=render,
                                        tsyc_records=inputs.tsyc_records, create_authorized=True,
                                        link_mechanism=self.writer())
        expect(plan["de"] == ma.SKIP_OWNER_MAINTAINED, f"{plan}")
        back = self.writer().rollback(operation.request_id, operation.modified_gmt or "")
        expect(back.kind == pbw.REFUSED and back.code == "tsyc_rollback_refused", f"{back}")
        evidence.update({"edited_copy": owner, "plan": plan["de"], "rollback": back.code})
        # Restore the checkpoint's view for the later rollback test.
        operation.modified_gmt = self.lab.product(operation.product_id)["date_modified_gmt"]
        self.store.put(operation)

        # (d) WooCommerce's own Duplicate copies TSYC's meta: the #4858 pattern.
        book3, vi3 = self.make_book(f"{SKU_PREFIX}OWN3", "00000000-0000-4000-8000-0000000000f3")
        copy = self.lab.eval_json(f"""
include_once WC_ABSPATH . 'includes/admin/class-wc-admin-duplicate-product.php';
$d = new WC_Admin_Duplicate_Product();
$new = $d->product_duplicate(wc_get_product({vi3['id']}));
$new->set_sku('{book3.product_code}-CAN-44');
$new->save();
echo json_encode(array('id'=>$new->get_id(),'owner'=>get_post_meta($new->get_id(),'{wtg.TSYC_OWNER_META_KEY}',true),'sku'=>$new->get_sku()));""")
        expect(copy["owner"] == book3.internal_product_id,
               "Plain WooCommerce Duplicate did not copy the meta; revisit the #4858 explanation.")
        products3 = {vi3["id"]: self.lab.product(vi3["id"]), copy["id"]: self.lab.product(copy["id"])}
        inputs3 = lpm.adapter_inputs([book3.vi_row])
        resolved3 = ma.resolve_group(
            product_code=book3.product_code, source_id=vi3["id"], products_by_id=products3,
            sku_matches=[products3[vi3["id"]]], storefront_groups=[], tsyc_records=inputs3.tsyc_records,
            link_map=self.reader.read_translations(vi3["id"]))
        expect(resolved3.status == ma.CONFLICT, f"{resolved3.status}")
        evidence["copied_meta_duplicate"] = {"id": copy["id"], "woo_duplicate_copies_tsyc_meta": True,
                                             "adapter": resolved3.status, "reasons": resolved3.reasons}
        return evidence

    def _published_group(self, code: str, internal_id: str) -> dict[str, int]:
        """A sellable VI+EN+DE group built the way the store's copies look, for
        native behaviour tests only (a price is set in the lab only)."""
        return self.lab.eval_json(f"""
$ids = array();
foreach (array('vi','en','de') as $lang) {{
  $p = new WC_Product_Simple();
  $p->set_name('Group {code} ' . $lang);
  $p->set_status('publish');
  $p->set_regular_price('10.00');
  $p->set_manage_stock(true);
  $p->set_stock_quantity(0);
  $p->set_backorders('yes');
  $p->update_meta_data('_tsyc_lab', '{internal_id}');
  $id = $p->save();
  pll_set_post_language($id, $lang);
  $ids[$lang] = $id;
}}
pll_save_post_translations($ids);
echo json_encode($ids);""")

    def t11_price_stock(self) -> tuple[str, dict[str, Any]]:
        ids = self._published_group("PRICE", "lab-price")
        snapshot = lambda: {lang: {k: self.lab.product(pid)[k] for k in ("regular_price", "stock_quantity", "backorders", "sku")}
                            for lang, pid in ids.items()}
        steps: dict[str, Any] = {"start": snapshot()}
        self.lab.wc("PUT", f"products/{ids['vi']}", json={"regular_price": "12.00"})
        steps["1_vi_price_changed"] = snapshot()
        self.lab.wc("PUT", f"products/{ids['en']}", json={"regular_price": "14.00"})
        steps["3_en_price_changed"] = snapshot()
        self.lab.wc("PUT", f"products/{ids['vi']}", json={"stock_quantity": 5})
        steps["5_vi_stock_changed"] = snapshot()
        before_save = self.lab.product(ids["vi"])
        self.lab.wc("PUT", f"products/{ids['de']}", json={"short_description": "No price in this save."})
        steps["11_de_saved_without_price"] = snapshot()
        expect(steps["1_vi_price_changed"]["en"]["regular_price"] == "10.00", "Native price propagated?")
        expect(steps["3_en_price_changed"]["vi"]["regular_price"] == "12.00", "Native EN price reached VI?")
        expect(steps["5_vi_stock_changed"]["en"]["stock_quantity"] == 0, "Native stock propagated?")
        expect(self.lab.product(ids["vi"])["date_modified_gmt"] == before_save["date_modified_gmt"], "A DE save touched VI.")
        return BLOCKED, {
            "blocked_on": "Flatsome Child source (the store's price and stock sync) is not in the lab.",
            "not_verified": "The store's real price and stock synchronisation, its direction, and any EN/DE save effect.",
            "native_baseline_real_local": "Plain WooCommerce + Polylang (sync: []) propagate nothing: each language keeps "
                                          "its own price and stock. So the sync the developer describes lives entirely in "
                                          "their theme code.",
            "steps": steps}

    def _order(self, pid: int, quantity: int = 1) -> dict[str, Any]:
        return self.lab.eval_json(f"""
add_filter('woocommerce_email_enabled_customer_processing_order','__return_false');
$product = wc_get_product({pid});
$before = array('in_stock'=>$product->is_in_stock(),'purchasable'=>$product->is_purchasable(),'backorders_allowed'=>$product->backorders_allowed(),'stock_status'=>$product->get_stock_status());
$order = wc_create_order(array('status'=>'pending'));
$order->add_product($product, {quantity});
$order->set_payment_method('cod');
$order->calculate_totals();
$order->save();
$order->update_status('processing','lab',true);
wc_maybe_reduce_stock_levels($order->get_id());
clearstatcache();
wp_cache_flush();
$after = wc_get_product({pid});
$order_id = $order->get_id();
echo json_encode(array('before'=>$before,'stock_after'=>$after->get_stock_quantity(),'status_after'=>$after->get_stock_status(),'order'=>$order_id,'hpos'=>Automattic\\WooCommerce\\Utilities\\OrderUtil::custom_orders_table_usage_is_enabled()));""")

    def t12_backorders(self) -> dict[str, Any]:
        ids = self._published_group("BACK", "lab-back")
        en = self._order(ids["en"])
        de = self._order(ids["de"])
        expect(en["before"]["purchasable"] and en["before"]["backorders_allowed"], f"{en}")
        expect(en["stock_after"] == -1 and de["stock_after"] == -1, f"{en} {de}")
        vi = self.lab.product(ids["vi"])
        expect(vi["stock_quantity"] == 0, f"VI changed: {vi['stock_quantity']}")
        return {"en_order": en, "de_order": de, "vi_stock_after_en_and_de_orders": vi["stock_quantity"],
                "finding": "With no theme hooks, EN and DE orders on backorder each drop only their own copy to -1; "
                           "this matches the -1 EN/DE groups seen in the production audit but does not prove their cause."}

    def t12b_parallel(self) -> tuple[str, dict[str, Any]]:
        ids = self._published_group("PARA", "lab-para")
        pid = ids["en"]
        self.lab.wc("PUT", f"products/{pid}", json={"stock_quantity": 10})
        with futures.ThreadPoolExecutor(4) as pool:
            done = list(pool.map(lambda _i: self._order(pid), range(4)))
        final = self.lab.product(pid)["stock_quantity"]
        expect(final == 6, f"Parallel orders: stock {final}, expected 6.")
        return BLOCKED, {"blocked_on": "Cross-language parallel behaviour depends on the theme's stock sync.",
                         "native_baseline_real_local": f"4 concurrent order processes on one EN copy: 10 -> {final}, no lost "
                                                       "update, on SQLite (production runs MySQL).",
                         "orders": [d["order"] for d in done]}

    def t13_interrupted(self) -> dict[str, Any]:
        book, _vi = self.make_book(f"{SKU_PREFIX}0013", "00000000-0000-4000-8000-000000000013")
        store = tcs.CheckpointStore(self.run_dir / "t13.json")
        states: dict[str, Any] = {}
        for stage, lang in (("after_create", "en"), ("after_language", "de")):
            before = self.lab.lab_skus_count()
            only = tdr.BookInput(book.internal_product_id, book.product_code, book.vi_row,
                                 {lang: book.contents[lang]}, AUTH_ID)
            first = self.runner(self.writer(fault=stage), store).run_book(only, create_authorized=True)
            expect(first.languages[lang].result == tdr.RESULT_UNCERTAIN, f"{stage}: {first.languages[lang]}")
            found = self.reader.read_request(store.get(book.internal_product_id, lang).request_id)
            expect(found.found, f"{stage}: not found")
            states[stage] = {"state_at_crash": found.state, "language": found.lang,
                             "linked": found.translations.get("vi") is not None}
            # A fresh process: reload the checkpoint from disk.
            reloaded = tcs.CheckpointStore(self.run_dir / "t13.json")
            again = self.runner(self.writer(), reloaded).run_book(only, create_authorized=True)
            expect(again.languages[lang].result == tdr.RESULT_RECONCILED, f"{stage}: {again.languages[lang]}")
            expect(self.lab.lab_skus_count() == before + 1, f"{stage}: duplicate.")
            store = reloaded
        group = self.reader.read_group(int(book.vi_row.woocommerce_product_id))
        expect(set(group.translations) == {"vi", "en", "de"}, f"{group.translations}")
        return {"crash_points": states, "final_group": dict(group.translations)}

    def t13b_resume_after_owner_edit(self) -> dict[str, Any]:
        """A partial create must not finish linking onto a source the owner edited."""
        book, vi = self.make_book(f"{SKU_PREFIX}0014", "00000000-0000-4000-8000-000000000014")
        store = tcs.CheckpointStore(self.run_dir / "t13b.json")
        only = tdr.BookInput(book.internal_product_id, book.product_code, book.vi_row, {"en": book.contents["en"]}, AUTH_ID)
        self.runner(self.writer(fault="after_create"), store).run_book(only, create_authorized=True)
        time.sleep(1.1)
        self.lab.wc("PUT", f"products/{vi['id']}", json={"short_description": "Owner edit during the outage."})
        again = self.runner(self.writer(), store).run_book(only, create_authorized=True)
        expect(again.languages["en"].result == tdr.RESULT_REFUSED and again.languages["en"].code == "tsyc_source_changed",
               f"{again.languages['en']}")
        group = self.reader.read_group(vi["id"])
        expect(set(group.translations) == {"vi"}, f"Linked anyway: {group.translations}")
        return {"result": again.languages["en"].code, "source_group_untouched": dict(group.translations),
                "partial_draft_left_for_review": store.get(book.internal_product_id, "en").request_id}

    def t14_rollback(self) -> dict[str, Any]:
        operation = self.store.get(self.book.internal_product_id, "de")
        result = self.writer().rollback(operation.request_id, operation.modified_gmt or "")
        expect(result.kind == pbw.CONFIRMED, f"{result}")
        group = dict(self.reader.read_group(self.vi["id"]).translations)
        expect(set(group) == {"vi", "en"}, f"{group}")
        status = self.lab.eval_json(f"echo json_encode(get_post_status({operation.product_id}));")
        expect(status == "trash", status)
        again = self.writer().rollback(operation.request_id, operation.modified_gmt or "")
        expect(again.kind == pbw.CONFIRMED, f"{again}")
        replay = self.writer().create_translation_draft(self._request_for(operation))
        expect(replay.kind == pbw.REFUSED and replay.code == "tsyc_request_rolled_back", f"{replay}")
        en = dict(self.reader.read_group(group["en"]).translations)
        expect(en == group, f"{en}")
        self.lab.wipe_products()
        remaining = self.lab.lab_skus_count()
        expect(remaining == 0, f"{remaining} products left.")
        return {"group_after_rollback": group, "rolled_back_status": status, "replay_refused": replay.code,
                "lab_products_after_cleanup": remaining}

    # ---- bridge safety ----------------------------------------------------

    def s1_auth(self) -> dict[str, Any]:
        pid = self.vi["id"]
        anonymous = requests.get(f"{STORE}/wp-json/tsyc/v1/translations/{pid}", timeout=HTTP_TIMEOUT).status_code
        wrong = self.lab.bridge_get(f"translations/{pid}", auth=("lab-admin", "wrong wrong wrong")).status_code
        self.lab.wp("user", "create", "lab-subscriber", "sub@example.invalid", "--role=subscriber", "--porcelain", check=False)
        sub_password = self.lab.wp("user", "application-password", "create", "lab-subscriber", f"p{time.time()}", "--porcelain")
        subscriber = self.lab.bridge_get(f"translations/{pid}", auth=("lab-subscriber", sub_password)).status_code
        expect(anonymous == 401 and wrong == 401 and subscriber == 403, f"{anonymous} {wrong} {subscriber}")
        write_anonymous = requests.post(f"{STORE}/wp-json/tsyc/v1/translations", json={}, timeout=HTTP_TIMEOUT).status_code
        no_token = requests.post(f"{STORE}/wp-json/tsyc/v1/translations", auth=self.lab.auth, json={}, timeout=HTTP_TIMEOUT).status_code
        bad_token = requests.post(f"{STORE}/wp-json/tsyc/v1/translations", auth=self.lab.auth, json={},
                                  headers={"X-TSYC-Write-Token": "x" * 64}, timeout=HTTP_TIMEOUT).status_code
        expect(write_anonymous == 401 and no_token == 403 and bad_token == 403, f"{write_anonymous} {no_token} {bad_token}")
        return {"read": {"anonymous": anonymous, "wrong_password": wrong, "subscriber": subscriber},
                "write": {"anonymous": write_anonymous, "no_token": no_token, "wrong_token": bad_token}}

    def s2_validation(self) -> dict[str, Any]:
        good = {"source_id": self.vi["id"], "lang": "en", "request_id": "tsyc-tr-validate-0001", "authorization_id": AUTH_ID,
                "internal_product_id": self.book.internal_product_id, "product_code": self.book.product_code,
                "expected_source_modified_gmt": self.vi["date_modified_gmt"], "inventory_profile": "FAHASA_PREORDER",
                "content": {"name": "n", "description": "", "short_description": ""}}
        cases = {
            "regular_price": ({**good, "regular_price": "9.99"}, "tsyc_forbidden_fields"),
            "status_publish": ({**good, "status": "publish"}, "tsyc_forbidden_fields"),
            "sku": ({**good, "sku": "X"}, "tsyc_forbidden_fields"),
            "price_in_content": ({**good, "content": {**good["content"], "regular_price": "1"}}, "tsyc_bad_content"),
            "default_language": ({**good, "lang": "vi"}, "tsyc_bad_language"),
            "unknown_language": ({**good, "lang": "fr"}, "tsyc_bad_language"),
            "bad_request_id": ({**good, "request_id": "a b"}, "tsyc_bad_request_id"),
            "string_source": ({**good, "source_id": str(self.vi["id"])}, "tsyc_bad_source_id"),
            "missing_field": ({k: v for k, v in good.items() if k != "authorization_id"}, "tsyc_missing_field"),
            "bad_profile": ({**good, "inventory_profile": "ANY"}, "tsyc_bad_inventory_profile"),
            "json_array_body": ([1, 2], "tsyc_forbidden_fields"),
        }
        seen = {}
        before = self.lab.lab_skus_count()
        for name, (body, code) in cases.items():
            response = self.lab.bridge_post("translations", body)
            seen[name] = response.json().get("code")
            expect(response.status_code == 400 and seen[name] == code, f"{name}: {response.status_code} {response.text[:200]}")
        expect(self.lab.lab_skus_count() == before, "A refused request created a product.")
        bad_id = self.lab.bridge_get("translations/999999").status_code
        non_numeric = self.lab.bridge_get("translations/abc").status_code
        expect(bad_id == 404 and non_numeric == 404, f"{bad_id} {non_numeric}")
        return {"refused": seen, "unknown_product": bad_id, "non_numeric_id": non_numeric}

    def s3_guards(self) -> dict[str, Any]:
        token = "t" * 40
        truth = self.lab.eval_json(f"""echo json_encode(array(
 'lab'=>tsyc_bridge_writes_allowed(true,'local','127.0.0.1','{token}'),
 'staging'=>tsyc_bridge_writes_allowed(true,'staging','staging.example.test','{token}'),
 'production_env'=>tsyc_bridge_writes_allowed(true,'production','staging.example.test','{token}'),
 'production_host'=>tsyc_bridge_writes_allowed(true,'staging','tiemsachyeucon.com','{token}'),
 'production_host_www'=>tsyc_bridge_writes_allowed(true,'local','WWW.tiemsachyeucon.com','{token}'),
 'flag_off'=>tsyc_bridge_writes_allowed(false,'local','127.0.0.1','{token}'),
 'short_token'=>tsyc_bridge_writes_allowed(true,'local','127.0.0.1','short'),
));""")
        expect(truth["lab"] and truth["staging"], f"{truth}")
        expect(not any(truth[k] for k in ("production_env", "production_host", "production_host_www", "flag_off", "short_token")), f"{truth}")
        for target in (PRODUCTION_URL_FOR_GUARD, "https://www.tiemsachyeucon.com"):
            try:
                pbw.BridgeDraftWriter(store_url=target, production_url=PRODUCTION_URL_FOR_GUARD, auth=None, write_token="x",
                                      allow_loopback_http=True)
                raise Check(f"The writer accepted {target}.")
            except plr.StagingTargetError:
                pass
        # A server whose writes flag is off must not even have the write routes.
        server = self.lab.start_server(PORT2, env={"TSYC_LAB_WRITES_OFF": "1"})
        try:
            no_route = requests.post(f"http://127.0.0.1:{PORT2}/wp-json/tsyc/v1/translations", auth=self.lab.auth, json={},
                                     headers={"X-TSYC-Write-Token": self.lab.secrets["write_token"]},
                                     timeout=HTTP_TIMEOUT)
            read_ok = requests.get(f"http://127.0.0.1:{PORT2}/wp-json/tsyc/v1/translations/{self.vi['id']}", auth=self.lab.auth,
                                   timeout=HTTP_TIMEOUT).status_code
        finally:
            self.lab.stop_server(server)
        expect(no_route.status_code == 404 and read_ok == 200, f"{no_route.status_code} {read_ok}")
        return {"allowed": truth, "writer_refuses_production": True, "writes_off_post": no_route.status_code, "writes_off_read": read_ok}

    def s4_polylang_off(self) -> dict[str, Any]:
        self.lab.wp("plugin", "deactivate", "polylang")
        try:
            status = self.lab.bridge_get(f"translations/{self.vi['id']}").status_code
            try:
                self.reader.read_translations(self.vi["id"])
                raised = False
            except plr.LinkReadError:
                raised = True
        finally:
            self.lab.wp("plugin", "activate", "polylang")
        expect(status == 503 and raised, f"{status} {raised}")
        return {"http": status, "client": "LinkReadError, never 'no translations'"}

    def s5_group_defects(self) -> dict[str, Any]:
        """The bridge must report real defects in Polylang's data."""
        book, vi = self.make_book(f"{SKU_PREFIX}DEF", "00000000-0000-4000-8000-0000000000d1")
        store = tcs.CheckpointStore(self.run_dir / "s5.json")
        result = self.runner(self.writer(), store).run_book(book, create_authorized=True)
        en = result.languages["en"].product_id
        self.lab.eval_json(f"wp_trash_post({en}); echo json_encode(1);")
        trashed = self.lab.bridge_get(f"translations/{vi['id']}").json()
        expect(trashed["consistent"] is False and any(i["code"] == "MEMBER_TRASHED" for i in trashed["issues"]), f"{trashed}")
        try:
            self.reader.read_translations(vi["id"])
            raise Check("A trashed member was accepted.")
        except plr.LinkConflictError as conflict:
            codes = [i.get("code") for i in conflict.issues]
        self.lab.eval_json(f"wp_untrash_post({en}); wp_update_post(array('ID'=>{en},'post_status'=>'draft')); pll_set_post_language({en}, 'de'); echo json_encode(1);")
        # Moving EN onto the taken DE slot silently evicts it: the rest of the
        # group still reads as valid, so the evicted product and TSYC's own
        # records are what must expose the change.
        group_view = self.lab.bridge_get(f"translations/{vi['id']}").json()
        expect("en" not in group_view["translations"], f"{group_view['translations']}")
        evicted = self.lab.bridge_get(f"translations/{en}").json()
        expect(evicted["consistent"] is False and any(i["code"] == "MEMBER_LANGUAGE_META_MISMATCH" for i in evicted["issues"]),
               f"{evicted}")
        rerun = self.runner(self.writer(), store).run_book(book, create_authorized=True)
        expect(rerun.languages["en"].result == tdr.RESULT_ERROR and rerun.languages["en"].code == "LinkConflictError",
               f"{rerun.languages['en']}")
        return {"trashed_member": codes, "remaining_group_after_eviction": group_view["translations"],
                "replay_after_owner_unlink": self._replay_after_unlink(),
                "evicted_member_issues": evicted["issues"], "confirmed_operation_on_rerun": rerun.languages["en"].code,
                "polylang_semantics": "pll_set_post_language onto a taken slot silently evicts the product from the group."}

    def _replay_after_unlink(self) -> str:
        """Replaying a finished request after the owner unlinked it must not relink."""
        book, vi = self.make_book(f"{SKU_PREFIX}UNL", "00000000-0000-4000-8000-0000000000d2")
        store = tcs.CheckpointStore(self.run_dir / "s5b.json")
        only = tdr.BookInput(book.internal_product_id, book.product_code, book.vi_row, {"en": book.contents["en"]}, AUTH_ID)
        self.runner(self.writer(), store).run_book(only, create_authorized=True)
        operation = store.get(book.internal_product_id, "en")
        self.lab.eval_json(f"pll_save_post_translations(array('vi'=>{vi['id']})); echo json_encode(1);")
        replay = self.writer().create_translation_draft(self._request_for(operation, book))
        expect(replay.kind == pbw.REFUSED and replay.code == "tsyc_link_changed", f"{replay}")
        group = dict(self.reader.read_group(vi["id"]).translations)
        expect(group == {"vi": vi["id"]}, f"Relinked: {group}")
        return replay.code or ""

    def s8_copied_facts(self) -> dict[str, Any]:
        """Images, physical facts, contributors and translated terms on a new copy."""
        book, vi = self.make_book(f"{SKU_PREFIX}FACT", "00000000-0000-4000-8000-0000000000b1")
        setup = self.lab.eval_json(f"""
$attachment = wp_insert_attachment(array('post_title'=>'lab cover','post_mime_type'=>'image/png','post_status'=>'inherit'), 'lab-cover.png');
$gallery = wp_insert_attachment(array('post_title'=>'lab inside','post_mime_type'=>'image/png','post_status'=>'inherit'), 'lab-inside.png');
$terms = array();
foreach (array('vi','en','de') as $lang) {{ $t = wp_insert_term('Lab cat {int(time.time())} ' . $lang, 'product_cat'); pll_set_term_language($t['term_id'], $lang); $terms[$lang] = $t['term_id']; }}
pll_save_term_translations(array('vi'=>$terms['vi'],'en'=>$terms['en']));
global $wpdb;
$product = wc_get_product({vi['id']});
$product->set_image_id($attachment);
$product->set_gallery_image_ids(array($gallery));
$product->set_weight('0.3');
$product->set_length('20');
$product->set_category_ids(array($terms['vi']));
$attribute = new WC_Product_Attribute();
$attribute->set_name('Tác giả');
$attribute->set_options(array('Tác giả tổng hợp'));
$attribute->set_visible(true);
$product->set_attributes(array($attribute));
$product->save();
echo json_encode(array('cover'=>$attachment,'gallery'=>$gallery,'terms'=>$terms,'modified'=>str_replace(' ','T',get_post($product->get_id())->post_modified_gmt)));""")
        row = lpm.LanguageProduct(**{**book.vi_row.__dict__, "baseline_modified_gmt": setup["modified"]})
        book = tdr.BookInput(book.internal_product_id, book.product_code, row, book.contents, AUTH_ID)
        result = self.runner(self.writer(), tcs.CheckpointStore(self.run_dir / "s8.json")).run_book(book, create_authorized=True)
        en, de = result.languages["en"].product_id, result.languages["de"].product_id
        expect(en and de, f"{result.languages}")
        facts = self.lab.eval_json(f"""
$out = array();
foreach (array('en'=>{en},'de'=>{de}) as $lang=>$id) {{
  $p = wc_get_product($id);
  $attributes = array();
  foreach ($p->get_attributes('edit') as $a) {{ $attributes[$a->get_name()] = $a->get_options(); }}
  $out[$lang] = array('image'=>$p->get_image_id('edit'),'gallery'=>$p->get_gallery_image_ids('edit'),'weight'=>$p->get_weight('edit'),'length'=>$p->get_length('edit'),
    'categories'=>$p->get_category_ids('edit'),'attributes'=>$attributes,'warnings'=>get_post_meta($id,'_tsyc_bridge_warnings',true),'sku'=>$p->get_sku('edit'),'price'=>$p->get_regular_price('edit'));
}}
echo json_encode($out);""")
        for lang in ("en", "de"):
            copy = facts[lang]
            expect(int(copy["image"]) == setup["cover"] and copy["gallery"] == [setup["gallery"]], f"{lang} images {copy}")
            expect(copy["weight"] == "0.3" and copy["length"] == "20", f"{lang} physical {copy}")
            expect(copy["attributes"].get("Tác giả") == ["Tác giả tổng hợp"], f"{lang} attributes {copy}")
            expect(copy["sku"] == "" and copy["price"] == "", f"{lang} {copy}")
        expect(facts["en"]["categories"] == [setup["terms"]["en"]], f"EN category {facts['en']}")
        expect(any(w.startswith("CATEGORY_NOT_TRANSLATED") for w in facts["de"]["warnings"]), f"DE warnings {facts['de']}")
        expect(setup["terms"]["vi"] not in facts["de"]["categories"], f"DE got the VI term: {facts['de']}")
        return {"copies": facts, "note": "Attribute labels stay Vietnamese on the copy (reported as a warning)."}

    def s6_save_translations_semantics(self) -> dict[str, Any]:
        """The Polylang behaviours the bridge is built around, on the real code."""
        data = self.lab.eval_json("""
$ids = array();
foreach (array('vi','en','de') as $lang) { $p = new WC_Product_Simple(); $p->set_name('sem ' . $lang); $p->set_status('draft'); $id = $p->save(); pll_set_post_language($id, $lang); $ids[$lang] = $id; }
pll_save_post_translations($ids);
$full = pll_get_post_translations($ids['vi']);
pll_save_post_translations(array('vi'=>$ids['vi'],'en'=>$ids['en']));
$after_partial = pll_get_post_translations($ids['vi']);
$de_after = pll_get_post_translations($ids['de']);
$wrong = pll_save_post_translations(array('vi'=>$ids['vi'],'en'=>$ids['en'],'de'=>$ids['vi']));
echo json_encode(array('ids'=>$ids,'full'=>$full,'after_partial'=>$after_partial,'de_after_partial'=>$de_after,'wrong_language_member_returned'=>$wrong));""")
        expect("de" not in data["after_partial"], "A partial save should unlink.")
        expect("de" not in data["wrong_language_member_returned"] or data["wrong_language_member_returned"]["de"] != data["ids"]["vi"],
               "A wrong-language member should be dropped.")
        return {"partial_save_unlinks_omitted_member": True, "wrong_language_member_silently_dropped": True, "data": data}

    def s7_concurrent_duplicates(self) -> dict[str, Any]:
        """Two real PHP servers racing on one book and language."""
        book, vi = self.make_book(f"{SKU_PREFIX}RACE", "00000000-0000-4000-8000-0000000000c1")
        server = self.lab.start_server(PORT2)
        try:
            body = lambda rid: {  # noqa: E731
                "source_id": vi["id"], "lang": "en", "request_id": rid, "authorization_id": AUTH_ID,
                "internal_product_id": book.internal_product_id, "product_code": book.product_code,
                "expected_source_modified_gmt": vi["date_modified_gmt"], "inventory_profile": "FAHASA_PREORDER",
                "content": {"name": "Race", "description": "", "short_description": ""}}
            jobs = [(PORT, "tsyc-tr-en-race-same-id"), (PORT2, "tsyc-tr-en-race-same-id"),
                    (PORT, "tsyc-tr-en-race-other-1"), (PORT2, "tsyc-tr-en-race-other-2")]
            with futures.ThreadPoolExecutor(4) as pool:
                answers = list(pool.map(lambda job: self.lab.bridge_post("translations", body(job[1]), port=job[0]), jobs))
        finally:
            self.lab.stop_server(server)
        codes = [(a.status_code, a.json().get("code") or a.json().get("product_id")) for a in answers]
        group = self.reader.read_group(vi["id"])
        claimed = self.lab.eval_json(f"""
$ids = get_posts(array('post_type'=>'product','post_status'=>'any','lang'=>'','fields'=>'ids','posts_per_page'=>-1,'suppress_filters'=>false,
 'meta_query'=>array(array('key'=>'{wtg.TSYC_OWNER_META_KEY}','value'=>'{book.internal_product_id}'),array('key'=>'{wtg.TSYC_LANGUAGE_META_KEY}','value'=>'en'))));
echo json_encode(array_values($ids));""")
        expect(len(claimed) == 1, f"{len(claimed)} EN products: {claimed}; answers {codes}")
        expect(group.translations.get("en") == claimed[0], f"{group.translations} {claimed}")
        return {"answers": codes, "en_products": claimed, "translations": dict(group.translations)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab-dir", type=Path, default=default_lab_dir())
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "data" / "output" / "polylang_local")
    args = parser.parse_args(argv)
    run_dir = args.out / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)

    lab = Lab(args.lab_dir.resolve())
    lab.wipe_products()
    lab.auth = ("lab-admin", lab.wp("user", "application-password", "create", "lab-admin", f"pilot-{time.time()}", "--porcelain"))
    lab.start_server()
    pilot = Pilot(lab, run_dir)
    pilot.reader.auth = lab.auth
    versions: dict[str, Any] = {}
    try:
        versions = requests.get(f"{STORE}/wp-json/tsyc/v1/health", auth=lab.auth, timeout=HTTP_TIMEOUT).json()
        print(json.dumps(versions))
        matrix = [
            ("T01", "Create VI draft through wc/v3 (TSYC's own shape)", pilot.t01_vi_draft),
            ("T02", "VI language assigned; pll_set_post_language semantics", pilot.t02_vi_language),
            ("S6", "Polylang save_translations semantics on the real code", pilot.s6_save_translations_semantics),
            ("S1", "Authentication and capability", pilot.s1_auth),
            ("S2", "Strict request validation; forbidden fields", pilot.s2_validation),
            ("S3", "Production guards; write routes absent when off", pilot.s3_guards),
            ("S4", "Polylang unavailable fails closed", pilot.s4_polylang_off),
            ("T03", "Create linked EN draft through the bridge", pilot.t03_en),
            ("T04", "Create linked DE draft through the bridge", pilot.t04_de),
            ("T05", "Read back authoritative relationships from each member", pilot.t05_readback),
            ("T06", "Adapter: exact IDs, languages, ownership, mapping rows", pilot.t06_validate),
            ("T07", "Repeat the operation: no duplicates", pilot.t07_repeat),
            ("T03B", "Polylang's real + screen (no theme hooks)", pilot.t03b_plus_flow),
            ("T08", "Lost response, absent request, failed reconciliation", pilot.t08_lost_response),
            ("T09", "Different editions cannot be linked accidentally", pilot.t09_editions),
            ("T10", "Owner-maintained products are protected", pilot.t10_owner_protection),
            ("T11", "Price and stock synchronisation", pilot.t11_price_stock),
            ("T12", "Zero-stock backorders; EN and DE orders", pilot.t12_backorders),
            ("T12B", "Parallel ordering", pilot.t12b_parallel),
            ("T13", "Interrupted execution and resume", pilot.t13_interrupted),
            ("T13B", "A resume refuses a source the owner edited", pilot.t13b_resume_after_owner_edit),
            ("S5", "Bridge reports real group defects", pilot.s5_group_defects),
            ("S7", "Concurrent duplicate requests on two PHP servers", pilot.s7_concurrent_duplicates),
            ("S8", "Images, physical facts, contributors, translated terms", pilot.s8_copied_facts),
            ("T14", "Rollback and cleanup of test products", pilot.t14_rollback),
        ]
        for test_id, title, fn in matrix:
            pilot.record(test_id, title, fn)
    finally:
        lab.stop_all()
    report = {"ran_at": datetime.now(timezone.utc).isoformat(), "versions": versions,
              "not_in_lab": ["Flatsome + Flatsome Child", "pll-bulk-link-translations v1/v2", "Duplicate Post RB", "MySQL (SQLite here)", "production data"],
              "results": pilot.results}
    path = run_dir / "pilot_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    counts: dict[str, int] = {}
    for item in pilot.results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    print(json.dumps(counts))
    print(f"Report: {path}")
    return 1 if counts.get(FAIL) else 0


if __name__ == "__main__":
    sys.exit(main())
