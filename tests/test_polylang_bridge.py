"""Offline tests of the TSYC Polylang bridge client, runner and PHP source rules.

These prove TSYC's own rules against tests/support/fake_bridge.py and a static
read of the PHP file. They are not evidence about WordPress: the real results
come from tools/wp_lab/run_local_pilot.py (docs/POLYLANG_BRIDGE.md).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import requests

from src.domain import language_product_mapping as lpm
from src.domain import multilingual_adapter as ma
from src.domain import translation_create_state as tcs
from src.domain import translation_discovery as td
from src.domain import translation_draft_plan as tdp
from src.domain import woo_translation_group as wtg
from src.services import polylang_bridge_writer as pbw
from src.services import polylang_link_reader as plr
from src.services import translation_draft_runner as tdr
from tests.support.fake_bridge import FakeBridge, Response

ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PHP = ROOT / "wordpress" / "tsyc-polylang-bridge" / "tsyc-polylang-bridge.php"
STORE = "https://lab.example.test"
PRODUCTION = "https://tiemsachyeucon.com"
CODE = "TSYC-LAB-1"
BOOK = "00000000-0000-4000-8000-000000000001"
OTHER_BOOK = "00000000-0000-4000-8000-000000000002"
SOURCE = 100
BASELINE = "2026-11-01T00:00:00"


def render(content: Any) -> tuple[str, str]:
    return content["description"], content["short_description"]


def contents(*languages: str) -> dict[str, dict[str, Any]]:
    return {lang: {"content_status": "APPROVED", "review_required": False, "content_language": lang,
                   "product_content_id": f"content-{lang}", "product_name": f"Book {lang}",
                   "description": f"<p>{lang}</p>", "short_description": f"s {lang}"} for lang in languages}


def vi_row() -> lpm.LanguageProduct:
    return lpm.LanguageProduct(internal_product_id=BOOK, product_code=CODE, language_code="vi",
                               woocommerce_product_id=SOURCE, origin=lpm.ORIGIN_TSYC_CREATED,
                               mapping_status=lpm.RECORDED, baseline_modified_gmt=BASELINE,
                               baseline_text={"name": "VI", "description": "", "short_description": ""})


def book(*languages: str) -> tdr.BookInput:
    return tdr.BookInput(BOOK, CODE, vi_row(), contents(*(languages or ("en", "de"))), "auth-1")


@pytest.fixture
def bridge() -> FakeBridge:
    return FakeBridge(source_id=SOURCE, internal_product_id=BOOK, product_code=CODE, modified_gmt=BASELINE)


def reader_for(bridge: FakeBridge, verified: bool = True) -> plr.EndpointLinkReader:
    return plr.EndpointLinkReader(store_url=STORE, auth=None, verified_on_staging=verified, http_get=bridge.get)


def writer_for(bridge: FakeBridge, verified: bool = True) -> pbw.BridgeDraftWriter:
    return pbw.BridgeDraftWriter(store_url=STORE, production_url=PRODUCTION, auth=None, write_token="t",
                                 verified_on_staging=verified, http_post=bridge.post)


def runner_for(bridge: FakeBridge, store: tcs.CheckpointStore, verified: bool = True) -> tdr.TranslationDraftRunner:
    return tdr.TranslationDraftRunner(reader=reader_for(bridge, verified), writer=writer_for(bridge, verified), store=store,
                                      read_catalog=bridge.read_catalog, render=render)


def request(**overrides: Any) -> pbw.CreateRequest:
    values = dict(source_id=SOURCE, lang="en", request_id="tsyc-tr-en-0001", authorization_id="a",
                  internal_product_id=BOOK, product_code=CODE, expected_source_modified_gmt=BASELINE,
                  content={"name": "n", "description": "d", "short_description": "s"})
    return pbw.CreateRequest(**{**values, **overrides})


def success(**overrides: Any) -> dict[str, Any]:
    return {"operation_id": "tsyc-tr-en-0001", "state": "LINKED", "replayed": False, "product_id": 101, "source_id": SOURCE,
            "lang": "en", "status": "draft", "modified_gmt": BASELINE, "text": {"name": "n"},
            "translations": {"vi": SOURCE, "en": 101}, "read_back": {"verified": True}, **overrides}


# ---------------------------------------------------------------- reader


def group_body(**overrides: Any) -> dict[str, Any]:
    return {"id": 7, "lang": "en", "sku": "", "status": "draft", "translations": {"vi": 6, "en": 7}, "members": {},
            "consistent": True, "issues": [], **overrides}


def test_group_read_accepts_any_language_but_the_vi_map_refuses_a_translation() -> None:
    assert plr.parse_group_response(group_body(), 7).lang == "en"
    with pytest.raises(plr.LinkReadError, match="not a VI source"):
        plr.parse_translation_response(group_body(), 7, mechanism="m", verified_on_staging=True)


@pytest.mark.parametrize("change", [{"consistent": None}, {"consistent": "true"}, {"issues": None}, {"id": 8},
                                    {"lang": "fr"}, {"lang": ""}, {"translations": []},
                                    {"translations": {"vi": 6, "en": 8}}, {"translations": {"vi": 7, "en": 7}}])
def test_malformed_group_answers_raise(change: dict[str, Any]) -> None:
    with pytest.raises(plr.LinkReadError):
        plr.parse_group_response(group_body(**change), 7)


def test_an_answer_without_a_consistency_verdict_is_never_accepted() -> None:
    body = group_body()
    del body["consistent"]
    with pytest.raises(plr.LinkReadError, match="consistency"):
        plr.parse_group_response(body, 7)


@pytest.mark.parametrize("code", ["MEMBER_TRASHED", "MEMBER_LANGUAGE_MISMATCH", "ASYMMETRIC_GROUP", "MEMBER_MISSING"])
def test_a_group_the_bridge_reports_as_inconsistent_raises_with_the_issue(code: str) -> None:
    with pytest.raises(plr.LinkConflictError) as caught:
        plr.parse_group_response(group_body(consistent=False, issues=[{"code": code, "detail": "x"}]), 7)
    assert caught.value.issues[0]["code"] == code


def test_issues_with_a_consistent_flag_still_raise() -> None:
    with pytest.raises(plr.LinkConflictError):
        plr.parse_group_response(group_body(issues=[{"code": "X"}]), 7)


@pytest.mark.parametrize("status", [401, 403, 404, 409, 500, 503])
def test_failed_endpoint_reads_never_mean_no_translations(status: int) -> None:
    reader = plr.EndpointLinkReader(store_url=STORE, auth=None, http_get=lambda *a, **k: Response(status, {"code": "x"}))
    with pytest.raises(plr.LinkReadError):
        reader.read_translations(1)
    with pytest.raises(plr.LinkReadError):
        reader.read_request("tsyc-tr-en-1234")


def test_a_failed_reconciliation_read_never_means_absent() -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise requests.ConnectionError("down")

    with pytest.raises(plr.LinkReadError):
        plr.EndpointLinkReader(store_url=STORE, auth=None, http_get=boom).read_request("tsyc-tr-en-1234")


@pytest.mark.parametrize("body", [{"request_id": "other-request", "found": False}, {"request_id": "tsyc-tr-en-1234"},
                                  {"request_id": "tsyc-tr-en-1234", "found": "no"},
                                  {"request_id": "tsyc-tr-en-1234", "found": True}, []])
def test_malformed_request_reads_raise(body: Any) -> None:
    reader = plr.EndpointLinkReader(store_url=STORE, auth=None, http_get=lambda *a, **k: Response(200, body))
    with pytest.raises(plr.LinkReadError):
        reader.read_request("tsyc-tr-en-1234")


def test_loopback_http_needs_an_explicit_opt_in_and_is_loopback_only() -> None:
    with pytest.raises(plr.StagingTargetError):
        plr.EndpointLinkReader(store_url="http://127.0.0.1:8765", auth=None)
    plr.EndpointLinkReader(store_url="http://127.0.0.1:8765", auth=None, allow_loopback_http=True)
    for url in ("http://tiemsachyeucon.com", "http://192.168.1.5", "http://127.0.0.1.example.test"):
        with pytest.raises(plr.StagingTargetError):
            plr.EndpointLinkReader(store_url=url, auth=None, allow_loopback_http=True)


# ---------------------------------------------------------------- writer


@pytest.mark.parametrize("url", [PRODUCTION, "https://www.tiemsachyeucon.com", "https://TIEMSACHYEUCON.com/", "http://example.test"])
def test_the_writer_cannot_be_pointed_at_production_or_plain_http(url: str) -> None:
    with pytest.raises(plr.StagingTargetError):
        pbw.BridgeDraftWriter(store_url=url, production_url=PRODUCTION, auth=None, write_token="t", allow_loopback_http=True)


def test_the_writer_refuses_when_production_is_unknown() -> None:
    with pytest.raises(plr.StagingTargetError):
        pbw.BridgeDraftWriter(store_url=STORE, production_url="", auth=None, write_token="t")


def test_the_request_body_is_draft_text_only() -> None:
    body = request().as_body()
    assert set(body) == {"source_id", "lang", "request_id", "authorization_id", "internal_product_id", "product_code",
                         "expected_source_modified_gmt", "inventory_profile", "content"}
    assert set(body["content"]) == set(wtg.TRANSLATED_FIELDS)
    assert not ({"status", "sku", "regular_price", "sale_price", "price", "stock_quantity", "backorders", "manage_stock"} & set(body))


@pytest.mark.parametrize("field", ["regular_price", "sale_price", "price", "stock_quantity", "backorders", "status", "sku"])
def test_forbidden_fields_in_content_never_leave_the_client(field: str) -> None:
    with pytest.raises(ValueError):
        request(content={"name": "n", "description": "d", "short_description": "s", field: "x"}).as_body()


def test_unknown_content_fields_never_leave_the_client() -> None:
    with pytest.raises(ValueError):
        request(content={"name": "n", "description": "d", "short_description": "s", "meta_data": []}).as_body()


def test_a_full_success_is_confirmed() -> None:
    outcome = pbw.parse_create_response(201, success(), request())
    assert outcome.kind == pbw.CONFIRMED and outcome.product_id == 101 and not outcome.replayed


@pytest.mark.parametrize("change", [
    {"operation_id": "other"}, {"state": "CREATED"}, {"state": "LANGUAGE_SET"}, {"lang": "de"}, {"source_id": 5},
    {"status": "publish"}, {"product_id": 0}, {"product_id": True}, {"translations": {"vi": SOURCE}},
    {"translations": {"vi": 5, "en": 101}}, {"read_back": {"verified": False}}, {"read_back": None}, {"text": None},
    {"modified_gmt": None}])
def test_a_malformed_success_is_uncertain_never_confirmed(change: dict[str, Any]) -> None:
    outcome = pbw.parse_create_response(201, success(**change), request())
    assert outcome.kind == pbw.UNCERTAIN and outcome.code == "MALFORMED_SUCCESS"


@pytest.mark.parametrize("status,body", [(500, {"code": "tsyc_link_unverified", "message": "m"}), (502, None), (503, {}),
                                         (200, None), (201, []), (403, "<html>WAF</html>"), (404, {"code": "not_ours"}),
                                         (None, None), (302, None)])
def test_uncertain_answers(status: Any, body: Any) -> None:
    assert pbw.parse_create_response(status, body, request()).kind == pbw.UNCERTAIN


@pytest.mark.parametrize("code,status", [("tsyc_translation_exists", 409), ("tsyc_source_changed", 409),
                                         ("tsyc_forbidden_fields", 400), ("tsyc_bad_write_token", 403),
                                         ("rest_no_route", 404), ("tsyc_unauthenticated", 401)])
def test_the_bridges_own_refusals_are_deterministic(code: str, status: int) -> None:
    outcome = pbw.parse_create_response(status, {"code": code, "message": "m"}, request())
    assert outcome.kind == pbw.REFUSED and outcome.code == code


def test_a_busy_answer_is_not_a_final_refusal() -> None:
    assert pbw.parse_create_response(409, {"code": "tsyc_busy", "message": "m"}, request()).kind == pbw.UNCERTAIN


def test_a_connection_error_is_uncertain_and_not_retried(bridge: FakeBridge) -> None:
    bridge.fail_post_with = requests.Timeout("slow")
    outcome = writer_for(bridge).create_translation_draft(request())
    assert outcome.kind == pbw.UNCERTAIN and outcome.code == "NO_RESPONSE" and len(bridge.posts) == 1


# --------------------------------------------------------------- runner


def test_it_creates_both_languages_and_proposes_mapping_rows_without_writing_them(bridge: FakeBridge, tmp_path: Path) -> None:
    store = tcs.CheckpointStore(tmp_path / "c.json")
    result = runner_for(bridge, store).run_book(book(), create_authorized=True)
    assert {l.result for l in result.languages.values()} == {tdr.RESULT_CREATED}
    assert set(bridge.group) == {"vi", "en", "de"}
    rows = result.proposed_rows
    assert {r.language_code for r in rows} == {"en", "de"}
    assert all(r.origin == lpm.ORIGIN_TSYC_CREATED and r.request_id and r.sync_id is None for r in rows)
    assert not lpm.validate_mappings([vi_row(), *rows])


def test_a_repeat_run_posts_nothing(bridge: FakeBridge, tmp_path: Path) -> None:
    store = tcs.CheckpointStore(tmp_path / "c.json")
    runner_for(bridge, store).run_book(book(), create_authorized=True)
    posts = len(bridge.posts)
    again = runner_for(bridge, tcs.CheckpointStore(tmp_path / "c.json")).run_book(book(), create_authorized=True)
    assert {l.result for l in again.languages.values()} == {tdr.RESULT_ALREADY_CONFIRMED}
    assert len(bridge.posts) == posts


def test_without_authorization_nothing_is_sent(bridge: FakeBridge, tmp_path: Path) -> None:
    result = runner_for(bridge, tcs.CheckpointStore(tmp_path / "c.json")).run_book(book(), create_authorized=False)
    assert {l.action for l in result.languages.values()} == {ma.CREATE_NOT_AUTHORIZED}
    assert not bridge.posts


def test_a_mechanism_not_attested_is_never_used(bridge: FakeBridge, tmp_path: Path) -> None:
    result = runner_for(bridge, tcs.CheckpointStore(tmp_path / "c.json"), verified=False).run_book(book(), create_authorized=True)
    assert {l.action for l in result.languages.values()} == {ma.REFUSE}
    assert not bridge.posts


def test_the_operation_is_on_disk_before_the_request_leaves(bridge: FakeBridge, tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    seen: list[str] = []
    original = bridge.post

    def spy(url: str, **kwargs: Any) -> Any:
        stored = json.loads(path.read_text(encoding="utf-8"))["operations"]
        seen.extend(op["state"] for op in stored.values() if op["request_id"] == kwargs["json"]["request_id"])
        return original(url, **kwargs)

    writer = writer_for(bridge)
    writer.http_post = spy
    tdr.TranslationDraftRunner(reader=reader_for(bridge), writer=writer, store=tcs.CheckpointStore(path),
                               read_catalog=bridge.read_catalog, render=render).run_book(book("en"), create_authorized=True)
    assert seen == [tcs.REQUEST_SENT]


def test_a_lost_answer_reconciles_without_a_duplicate(bridge: FakeBridge, tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    bridge.fail_post_with = requests.ConnectionError("lost")
    bridge.land_then_fail = True
    first = runner_for(bridge, tcs.CheckpointStore(path)).run_book(book("en"), create_authorized=True)
    assert first.languages["en"].result == tdr.RESULT_UNCERTAIN
    created = len(bridge.products)
    second = runner_for(bridge, tcs.CheckpointStore(path)).run_book(book("en"), create_authorized=True)
    assert second.languages["en"].result == tdr.RESULT_RECONCILED
    assert len(bridge.products) == created
    assert tcs.CheckpointStore(path).get(BOOK, "en").state == tcs.CONFIRMED


def test_explicit_absence_allows_the_same_request_to_be_sent_once_more(bridge: FakeBridge, tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    bridge.fail_post_with = requests.ConnectionError("never arrived")
    runner_for(bridge, tcs.CheckpointStore(path)).run_book(book("en"), create_authorized=True)
    first_id = tcs.CheckpointStore(path).get(BOOK, "en").request_id
    bridge.fail_post_with = None
    second = runner_for(bridge, tcs.CheckpointStore(path)).run_book(book("en"), create_authorized=True)
    assert second.languages["en"].result == tdr.RESULT_CREATED
    assert tcs.CheckpointStore(path).get(BOOK, "en").request_id == first_id
    assert sum(1 for p in bridge.products.values() if p["_lang"] == "en") == 1


def test_a_failed_reconciliation_read_sends_nothing(bridge: FakeBridge, tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    bridge.fail_post_with = requests.ConnectionError("lost")
    bridge.land_then_fail = True
    runner_for(bridge, tcs.CheckpointStore(path)).run_book(book("en"), create_authorized=True)
    posts = len(bridge.posts)
    bridge.fail_get_with = requests.ConnectionError("read down")
    result = runner_for(bridge, tcs.CheckpointStore(path)).run_book(book("en"), create_authorized=True)
    assert result.languages["en"].code == "RECONCILE_READ_FAILED"
    assert len(bridge.posts) == posts
    assert tcs.CheckpointStore(path).get(BOOK, "en").state == tcs.UNCERTAIN


def test_a_reconciled_request_on_another_product_identity_is_refused(bridge: FakeBridge, tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    bridge.fail_post_with = requests.ConnectionError("lost")
    bridge.land_then_fail = True
    runner_for(bridge, tcs.CheckpointStore(path)).run_book(book("en"), create_authorized=True)
    pid = bridge.group["en"]
    bridge.products[pid]["_lang"] = "de"
    posts = len(bridge.posts)
    result = runner_for(bridge, tcs.CheckpointStore(path)).run_book(book("en"), create_authorized=True)
    assert result.languages["en"].code == "RECONCILE_IDENTITY_MISMATCH"
    assert len(bridge.posts) == posts


def test_a_deterministic_refusal_stays_refused(bridge: FakeBridge, tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    bridge.fail_post_with = Response(409, {"code": "tsyc_source_changed", "message": "edited"})
    first = runner_for(bridge, tcs.CheckpointStore(path)).run_book(book("en"), create_authorized=True)
    assert first.languages["en"].code == "tsyc_source_changed"
    posts = len(bridge.posts)
    bridge.fail_post_with = None
    second = runner_for(bridge, tcs.CheckpointStore(path)).run_book(book("en"), create_authorized=True)
    assert second.languages["en"].result == tdr.RESULT_REFUSED and len(bridge.posts) == posts


def test_a_confirmed_link_that_changed_is_reported_not_repaired(bridge: FakeBridge, tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    runner_for(bridge, tcs.CheckpointStore(path)).run_book(book("en"), create_authorized=True)
    pid = bridge.group.pop("en")
    posts = len(bridge.posts)
    result = runner_for(bridge, tcs.CheckpointStore(path)).run_book(book("en"), create_authorized=True)
    assert result.languages["en"].code == "CONFIRMED_LINK_CHANGED" and result.languages["en"].product_id == pid
    assert len(bridge.posts) == posts


def test_one_failing_language_does_not_stop_the_other(bridge: FakeBridge, tmp_path: Path) -> None:
    runner = runner_for(bridge, tcs.CheckpointStore(tmp_path / "c.json"))
    original = runner._run_language

    def fail_en(the_book: tdr.BookInput, lang: str, authorized: bool, result: tdr.BookResult) -> tdr.LanguageResult:
        if lang == "en":
            raise RuntimeError("boom")
        return original(the_book, lang, authorized, result)

    runner._run_language = fail_en  # type: ignore[method-assign]
    result = runner.run_book(book(), create_authorized=True)
    assert result.languages["en"].result == tdr.RESULT_ERROR
    assert result.languages["de"].result == tdr.RESULT_CREATED


def test_de_waits_while_en_is_unreconciled(bridge: FakeBridge, tmp_path: Path) -> None:
    bridge.fail_post_with = requests.ConnectionError("lost")
    bridge.land_then_fail = True
    result = runner_for(bridge, tcs.CheckpointStore(tmp_path / "c.json")).run_book(book(), create_authorized=True)
    assert result.languages["en"].result == tdr.RESULT_UNCERTAIN
    assert result.languages["de"].action == ma.REFUSE
    assert "de" not in bridge.group


# ------------------------------------------------------------ checkpoints


def test_request_ids_are_stable_and_distinct() -> None:
    first = tcs.make_request_id(BOOK, "en", "c1")
    assert first == tcs.make_request_id(BOOK, "en", "c1")
    assert len({first, tcs.make_request_id(BOOK, "de", "c1"), tcs.make_request_id(BOOK, "en", "c2"),
                tcs.make_request_id(OTHER_BOOK, "en", "c1")}) == 4
    assert re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", first)


@pytest.mark.parametrize("text", ["not json", "{}", json.dumps({"version": 999, "operations": {}}),
                                  json.dumps({"version": 1, "operations": {"x": {"nope": 1}}})])
def test_a_damaged_checkpoint_stops_the_run(tmp_path: Path, text: str) -> None:
    path = tmp_path / "c.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(tcs.CheckpointError):
        tcs.CheckpointStore(path)


def test_a_checkpoint_round_trips_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    store = tcs.CheckpointStore(path)
    operation = tcs.LanguageOperation("r-00000001", BOOK, CODE, "en", SOURCE, "c1")
    operation.move(tcs.REQUEST_SENT)
    store.put(operation)
    loaded = tcs.CheckpointStore(path).get(BOOK, "en")
    assert loaded.state == tcs.REQUEST_SENT and loaded.history[0]["state"] == tcs.REQUEST_SENT
    assert [p.name for p in tmp_path.iterdir()] == ["c.json"]


def test_an_unknown_state_is_refused() -> None:
    with pytest.raises(ValueError):
        tcs.LanguageOperation("r-00000001", BOOK, CODE, "en", SOURCE, "c1").move("DONE")


# ------------------------------------------------- identity and ownership


def product(pid: int, lang: str, *, sku: str = "", owner: str | None = None, status: str = "draft",
            lang_meta: bool = True, **extra: Any) -> dict[str, Any]:
    meta = []
    if owner:
        meta.append({"key": wtg.TSYC_OWNER_META_KEY, "value": owner})
    if owner and lang_meta and lang != "vi":
        meta.append({"key": wtg.TSYC_LANGUAGE_META_KEY, "value": lang})
    return {"id": pid, "sku": sku, "status": status, "name": f"p{pid}", "description": "", "short_description": "",
            "date_modified_gmt": BASELINE, "permalink": f"https://lab.example.test/?post_type=product&p={pid}",
            "meta_data": meta, **extra}


def resolve(extra: dict[int, dict[str, Any]], links: dict[str, int], records: dict[str, ma.TsycProductRecord] | None = None) -> ma.GroupResolution:
    vi = product(SOURCE, "vi", sku=CODE, owner=BOOK)
    products = {SOURCE: vi, **extra}
    return ma.resolve_group(
        product_code=CODE, source_id=SOURCE, products_by_id=products,
        sku_matches=[p for p in products.values() if p["sku"] == CODE], storefront_groups=[],
        tsyc_records={"vi": ma.TsycProductRecord("vi", SOURCE, BOOK), **(records or {})},
        link_map=ma.PolylangLinkMap(SOURCE, links, "m", True))


def test_a_linked_copy_carrying_another_books_meta_is_a_conflict_not_just_unproven() -> None:
    resolution = resolve({101: product(101, "en", owner=OTHER_BOOK)}, {"vi": SOURCE, "en": 101})
    assert resolution.status == ma.CONFLICT
    assert "another book" in resolution.reasons[0]


def test_meta_copied_from_this_book_onto_an_unrecorded_product_is_a_conflict() -> None:
    resolution = resolve({101: product(101, "en", owner=BOOK)}, {"vi": SOURCE, "en": 101})
    assert resolution.status == ma.CONFLICT
    assert "never recorded" in resolution.reasons[0]


def test_a_recorded_translation_missing_from_polylang_is_a_conflict() -> None:
    record = ma.TsycProductRecord("en", 101, BOOK, BASELINE, {})
    resolution = resolve({101: product(101, "en", owner=BOOK)}, {"vi": SOURCE}, {"en": record})
    assert resolution.status == ma.CONFLICT


def test_a_wrong_language_mapping_is_a_conflict() -> None:
    record = ma.TsycProductRecord("en", 101, BOOK, BASELINE, {})
    resolution = resolve({101: product(101, "de", owner=BOOK)}, {"vi": SOURCE, "de": 101}, {"en": record})
    assert resolution.status == ma.CONFLICT


def test_a_shared_sku_without_a_link_never_resolves() -> None:
    assert resolve({101: product(101, "en", sku=CODE, lang_meta=False)}, {"vi": SOURCE}).status != ma.RESOLVED


def test_a_link_without_identity_stays_unverifiable() -> None:
    assert resolve({101: product(101, "en")}, {"vi": SOURCE, "en": 101}).status == ma.UNVERIFIABLE


@pytest.mark.parametrize("field,value", [("regular_price", "9.99"), ("sale_price", "5"), ("stock_quantity", 3),
                                         ("backorders", "no"), ("manage_stock", False)])
def test_price_stock_and_backorder_drift_is_reported_never_corrected(field: str, value: Any) -> None:
    source = {"id": SOURCE, "regular_price": "1.00", "sale_price": "", "manage_stock": True, "stock_quantity": 0, "backorders": "yes"}
    copy = {**source, "id": 101, field: value}
    snapshot = dict(copy)
    resolution = ma.GroupResolution(ma.RESOLVED, SOURCE, {"vi": SOURCE, "en": 101})
    findings = ma.inventory_authority_findings(resolution, {SOURCE: source, 101: copy})
    assert len(findings) == 1 and field in findings[0]
    assert copy == snapshot


def plan_for(member: dict[str, Any], record: ma.TsycProductRecord | None) -> str:
    source = product(SOURCE, "vi", sku=CODE, owner=BOOK)
    resolution = ma.GroupResolution(ma.RESOLVED, SOURCE, {"vi": SOURCE, "en": member["id"]})
    return ma.plan_language_actions(
        resolution, internal_product_id=BOOK, products_by_id={SOURCE: source, member["id"]: member},
        approved_contents=contents("en"), render=render,
        tsyc_records={"vi": ma.TsycProductRecord("vi", SOURCE, BOOK), **({"en": record} if record else {})})["en"]


def test_a_content_mismatch_on_an_unchanged_tsyc_draft_plans_only_a_text_update() -> None:
    text = {"name": "old", "description": "old", "short_description": "old"}
    member = product(101, "en", owner=BOOK, **text)
    assert plan_for(member, ma.TsycProductRecord("en", 101, BOOK, BASELINE, text)) == ma.UPDATE_TEXT_DRAFT


def test_an_owner_edit_is_protected() -> None:
    text = {"name": "old", "description": "old", "short_description": "old"}
    member = product(101, "en", owner=BOOK, **{**text, "description": "owner"})
    assert plan_for(member, ma.TsycProductRecord("en", 101, BOOK, BASELINE, text)) == ma.SKIP_OWNER_MAINTAINED


@pytest.mark.parametrize("member", [product(101, "en"), product(101, "en", status="publish")])
def test_copies_made_inside_wordpress_are_never_written(member: dict[str, Any]) -> None:
    assert plan_for(member, None) == ma.SKIP_OWNER_MAINTAINED


def test_a_published_tsyc_product_is_never_written() -> None:
    text = {"name": "p101", "description": "", "short_description": ""}
    member = product(101, "en", owner=BOOK, status="publish")
    assert plan_for(member, ma.TsycProductRecord("en", 101, BOOK, BASELINE, text)) == ma.SKIP_OWNER_MAINTAINED


# ----------------------------------------------------------- review queue


def candidate(classification: str, bases: list[str] | None = None, status: str = "publish") -> td.Candidate:
    return td.Candidate(1, "en", status, CODE, "n", bases or [], classification)


@pytest.mark.parametrize("candidate_,expected", [
    (candidate(td.VERIFIED_LINK), td.REVIEW_AUTHORITATIVE_LINK_VERIFIED),
    (candidate(td.POTENTIAL_LINK), td.REVIEW_POTENTIAL_LINK),
    (candidate(td.IDENTITY_CONFLICT), td.REVIEW_IDENTITY_CONFLICT),
    (candidate(td.OWNER_MAINTAINED), td.REVIEW_UNLINKED),
    (candidate(td.UNVERIFIABLE, [td.BASIS_SKU]), td.REVIEW_OWNER_MAINTAINED),
    (candidate(td.UNVERIFIABLE, [td.BASIS_SKU], "draft"), td.REVIEW_UNKNOWN),
    (candidate(td.UNVERIFIABLE, [td.BASIS_HREFLANG]), td.REVIEW_UNKNOWN),
    (candidate(td.UNVERIFIABLE, [td.BASIS_IMAGE]), td.REVIEW_UNKNOWN),
])
def test_review_classes(candidate_: td.Candidate, expected: str) -> None:
    assert td.review_class(candidate_) == expected


def test_a_tombstone_is_not_a_live_product_but_its_live_copies_are_queued() -> None:
    row = lpm.LanguageProduct(BOOK, CODE, "vi", 500, lpm.ORIGIN_TSYC_CREATED, lpm.REMOTE_REMOVED)
    catalog = {500: {"id": 500, "sku": CODE, "status": "publish", "permalink": "https://x.test/p/"},
               501: {"id": 501, "sku": CODE, "status": "publish", "permalink": "https://x.test/en/p/"},
               502: {"id": 502, "sku": CODE, "status": "trash", "permalink": "https://x.test/en/q/"},
               503: {"id": 503, "sku": "OTHER", "status": "publish", "permalink": "https://x.test/en/r/"}}
    queue = td.removed_book_copies([row], catalog)
    assert [item["woo_id"] for item in queue] == [501]
    assert queue[0]["review_class"] == td.REVIEW_REMOTE_REMOVED


def test_a_removed_language_is_never_planned_for_creation() -> None:
    row = lpm.LanguageProduct(BOOK, CODE, "en", 500, lpm.ORIGIN_TSYC_CREATED, lpm.REMOTE_REMOVED, request_id="r")
    inputs = lpm.adapter_inputs([row])
    assert inputs.blocked_languages == ("en",) and "en" not in inputs.tsyc_records


def test_duplicated_woo_ids_and_languages_are_errors() -> None:
    rows = [vi_row(),
            lpm.LanguageProduct(BOOK, CODE, "en", SOURCE, lpm.ORIGIN_TSYC_CREATED, lpm.RECORDED, request_id="r1"),
            lpm.LanguageProduct(BOOK, CODE, "en", 101, lpm.ORIGIN_TSYC_CREATED, lpm.RECORDED, request_id="r2")]
    codes = {f.code for f in lpm.validate_mappings(rows)}
    assert {lpm.DUPLICATE_WOO_ID, lpm.DUPLICATE_LANGUAGE} <= codes


# ----------------------------------------------------------- dry-run plan


def preorder(**overrides: Any) -> dict[str, Any]:
    return {"status": "draft", "manage_stock": True, "stock_quantity": 0, "backorders": "yes", "regular_price": "",
            "sale_price": "", **overrides}


def approved(*languages: str) -> list[dict[str, Any]]:
    return [{"content_language": lang, "content_status": "APPROVED", "review_required": False} for lang in languages]


@pytest.mark.parametrize("remote,contents_,blocker", [
    (None, approved("vi", "en", "de"), tdp.VI_NOT_READ),
    (preorder(status="trash"), approved("vi", "en", "de"), tdp.VI_NOT_READ),
    (preorder(status="publish"), approved("vi", "en", "de"), tdp.VI_PUBLISHED),
    (preorder(), approved("vi", "en"), tdp.TRANSLATIONS_NOT_APPROVED),
    (preorder(), [{"content_language": "en", "content_status": "APPROVED", "review_required": True},
                  *approved("vi", "de")], tdp.TRANSLATIONS_NOT_APPROVED),
    (preorder(regular_price="9.99"), approved("vi", "en", "de"), tdp.VI_NOT_PREORDER),
    (preorder(stock_quantity=2), approved("vi", "en", "de"), tdp.VI_NOT_PREORDER),
    (preorder(manage_stock=False), approved("vi", "en", "de"), tdp.VI_NOT_PREORDER),
    (preorder(backorders="no"), approved("vi", "en", "de"), tdp.VI_NOT_PREORDER),
    (preorder(), approved("vi", "en", "de"), tdp.LINK_READER_NOT_INSTALLED),
])
def test_the_dry_run_names_the_first_blocker(remote: Any, contents_: list[dict[str, Any]], blocker: str) -> None:
    assert tdp.plan_book(vi_row(), remote, contents_).blocker == blocker


def test_the_dry_run_never_claims_a_create_is_possible() -> None:
    summary = tdp.summarize([tdp.plan_book(vi_row(), preorder(), approved("vi", "en", "de"))])
    assert summary["creates_possible_today"] == 0
    assert summary["all_languages_approved"] == 1


# ------------------------------------------------------- PHP source rules


@pytest.fixture(scope="module")
def php() -> str:
    return BRIDGE_PHP.read_text(encoding="utf-8")


def code_only(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?m)//.*$", "", source)


@pytest.mark.parametrize("forbidden", [r"\$wpdb", "term_relationships", r"wp_set_object_terms", r"wp_insert_term", "wp_update_term",
                                       "set_regular_price", "set_sale_price", "set_price", "set_sku", r"'publish'\s*\)\s*;\s*$",
                                       r"set_status\(\s*'publish'", "eval\\(", "wp_delete_post"])
def test_the_bridge_uses_no_internal_polylang_write_and_no_price_or_sku_setter(php: str, forbidden: str) -> None:
    assert not re.search(forbidden, without_lock_functions(code_only(php)), flags=re.M), forbidden


LOCK_FUNCTIONS = ("tsyc_bridge_lock_insert", "tsyc_bridge_lock_delete", "tsyc_bridge_acquire_lock")


def without_lock_functions(code: str) -> str:
    for name in LOCK_FUNCTIONS:
        code = re.sub(rf"function {name}\(.*?\n\}}\n", "", code, flags=re.S)
    return code


def test_the_only_sql_is_the_lock_row_in_the_options_table(php: str) -> None:
    code = code_only(php)
    bodies = [re.search(rf"function {name}\(.*?\n\}}\n", code, flags=re.S).group(0) for name in LOCK_FUNCTIONS]
    statements = re.findall(r'"((?:INSERT|DELETE|SELECT|UPDATE|REPLACE)[^"]*)"', "".join(bodies))
    assert len(statements) == 3
    assert all("{$wpdb->options}" in statement for statement in statements)
    assert all("option_name = %s" in statement or "VALUES (%s, %s, 'off')" in statement for statement in statements)
    assert "query(" not in without_lock_functions(code)


def test_the_lock_is_not_an_upsert(php: str) -> None:
    code = code_only(php)
    assert "add_option" not in code and "delete_option" not in code, "add_option is an upsert: two callers can both win."
    assert "INSERT IGNORE" in code


def test_one_lock_per_group_is_taken_by_create_and_rollback(php: str) -> None:
    code = code_only(php)
    assert "function tsyc_bridge_lock_key(int $source_id)" in code
    for route in ("function tsyc_bridge_create_translation", "function tsyc_bridge_rollback_translation"):
        body = re.search(re.escape(route) + r"\(.*?\n\}\n", code, flags=re.S).group(0)
        assert "tsyc_bridge_acquire_lock" in body and "tsyc_bridge_watch_writes(true)" in body, route


def test_the_source_is_rechecked_right_before_the_link(php: str) -> None:
    code = code_only(php)
    body = code[code.index("function tsyc_bridge_create_locked"):code.index("function tsyc_bridge_report_finished")]
    assert body.rindex("tsyc_bridge_validate_source", 0, body.index("pll_save_post_translations")) > body.index("tsyc_bridge_side_effects")
    assert body.index("tsyc_bridge_side_effects($before, $id, true)") > body.index("pll_save_post_translations")


def test_a_side_effect_is_reported_and_never_repaired(php: str) -> None:
    code = code_only(php)
    body = code[code.index("function tsyc_bridge_side_effects"):code.index("function tsyc_bridge_create_translation")]
    for write in ("wp_update_post", "->save(", "pll_save_post_translations", "wp_trash_post"):
        assert write not in body, write
    # The only writes in the stop path are on the operation's own draft.
    assert re.findall(r"update_post_meta\((\$\w+)", body) == ["$id", "$id"]


@pytest.mark.parametrize("api", ["pll_get_post_language", "pll_set_post_language", "pll_get_post_translations", "pll_save_post_translations"])
def test_the_bridge_uses_polylangs_public_api(php: str, api: str) -> None:
    assert api in php


def test_write_routes_are_registered_only_behind_the_guard(php: str) -> None:
    code = code_only(php)
    guard = code.index("if (!tsyc_bridge_writes_enabled())")
    assert code.index("'/health'") < guard
    assert code.index("'/translations/(?P<id>") < guard
    assert code.index("'/translations/by-request/") < guard
    assert guard < code.index("'tsyc_bridge_create_translation',")
    assert guard < code.index("'tsyc_bridge_rollback_translation',")


def test_every_route_has_a_real_permission_callback(php: str) -> None:
    assert "__return_true" not in php
    assert php.count("'permission_callback'") == 5


def test_production_host_is_refused_by_the_write_guard(php: str) -> None:
    assert "TSYC_BRIDGE_PRODUCTION_HOSTS = array('tiemsachyeucon.com')" in php
    assert "array('local', 'staging')" in php
    assert "hash_equals" in php


def test_the_lab_guards_are_not_part_of_the_bridge(php: str) -> None:
    assert "X_TSYC_LAB_FAULT" not in php
    guards = (ROOT / "tools" / "wp_lab" / "tsyc-lab-guards.php").read_text(encoding="utf-8")
    assert "wp_get_environment_type() !== 'local'" in guards
    assert "'127.0.0.1'" in guards


# -------------------------------------------- side-effect stop (client side)


def side_effect_answer(product_id: int = 201) -> Response:
    return Response(409, {"code": "tsyc_sibling_side_effect", "message": "Creating changed protected values.",
                          "data": {"status": 409, "product_id": product_id, "linked": True, "modified_gmt": BASELINE,
                                   "changes": [{"code": "MEMBER_CHANGED", "product_id": SOURCE, "field": "_stock", "before": "0", "after": "7"}]}})


def make_request(lang: str = "en", request_id: str = "tsyc-tr-en-side-effect") -> pbw.CreateRequest:
    return pbw.CreateRequest(source_id=SOURCE, lang=lang, request_id=request_id, authorization_id="a", internal_product_id=BOOK,
                             product_code=CODE, expected_source_modified_gmt=BASELINE,
                             content={"name": "n", "description": "", "short_description": ""})


def test_a_side_effect_answer_is_refused_and_names_what_it_left_behind() -> None:
    answer = side_effect_answer()
    outcome = pbw.parse_create_response(answer.status_code, answer.json(), make_request())
    assert outcome.kind == pbw.REFUSED and outcome.code == pbw.BRIDGE_SIDE_EFFECT
    assert outcome.product_id == 201
    assert outcome.changes[0]["field"] == "_stock"


@pytest.mark.parametrize("data", [{}, {"product_id": "201"}, {"product_id": True}, {"product_id": -1}, {"changes": "x"}, "not an object"])
def test_a_refusal_with_odd_data_names_nothing(data: Any) -> None:
    outcome = pbw.parse_create_response(409, {"code": "tsyc_x", "message": "m", "data": data}, make_request())
    assert outcome.kind == pbw.REFUSED and outcome.product_id is None and outcome.changes == ()


def test_a_refused_source_change_keeps_the_partial_draft_id() -> None:
    answer = {"code": "tsyc_source_changed", "message": "m", "data": {"status": 409, "product_id": 300}}
    outcome = pbw.parse_create_response(409, answer, make_request())
    assert outcome.kind == pbw.REFUSED and outcome.product_id == 300


def test_rollback_reports_a_side_effect_instead_of_success() -> None:
    def post(*_a: Any, **_k: Any) -> Response:
        return Response(409, {"code": pbw.BRIDGE_SIDE_EFFECT, "message": "rolled back, but", "data": {
            "status": 409, "product_id": 7, "rolled_back": True, "changes": [{"code": "MEMBER_CHANGED"}]}})
    writer = pbw.BridgeDraftWriter(store_url=STORE, production_url=PRODUCTION, auth=None, write_token="t", http_post=post)
    outcome = writer.rollback("tsyc-tr-en-x", BASELINE)
    assert outcome.kind == pbw.REFUSED and outcome.code == pbw.BRIDGE_SIDE_EFFECT and outcome.product_id == 7 and outcome.changes


def side_effect_runner(tmp_path: Path) -> tuple[tdr.TranslationDraftRunner, FakeBridge, tcs.CheckpointStore]:
    bridge = FakeBridge(source_id=SOURCE, internal_product_id=BOOK, product_code=CODE, modified_gmt=BASELINE)
    store = tcs.CheckpointStore(tmp_path / "checkpoint.json")
    reader = plr.EndpointLinkReader(store_url=STORE, auth=None, verified_on_staging=True, http_get=bridge.get)
    writer = pbw.BridgeDraftWriter(store_url=STORE, production_url=PRODUCTION, auth=None, write_token="t",
                                   verified_on_staging=True, http_post=bridge.post)
    return tdr.TranslationDraftRunner(reader=reader, writer=writer, store=store, read_catalog=bridge.read_catalog, render=render), bridge, store


def test_a_side_effect_stops_the_whole_book_now_and_on_every_later_run(tmp_path: Path) -> None:
    runner, bridge, store = side_effect_runner(tmp_path)
    bridge.fail_post_with = side_effect_answer()
    first = runner.run_book(book(), create_authorized=True)
    assert first.languages["en"].code == tdr.BOOK_STOPPED_SIDE_EFFECT or first.languages["en"].code == pbw.BRIDGE_SIDE_EFFECT
    assert first.languages["de"].code == tdr.BOOK_STOPPED_SIDE_EFFECT
    assert len(bridge.posts) == 1, "DE must not be sent after EN stopped on a side effect."
    assert store.get(BOOK, "en").product_id == 201

    # A later process, with the store now answering normally, still sends nothing.
    bridge.fail_post_with = None
    reloaded = tcs.CheckpointStore(tmp_path / "checkpoint.json")
    runner.store = reloaded
    again = runner.run_book(book(), create_authorized=True)
    assert all(v.code == tdr.BOOK_STOPPED_SIDE_EFFECT for v in again.languages.values())
    assert len(bridge.posts) == 1
    assert again.proposed_rows == [], "No mapping row may come out of a stopped book."


def test_a_side_effect_on_one_book_does_not_stop_another(tmp_path: Path) -> None:
    runner, bridge, store = side_effect_runner(tmp_path)
    bridge.fail_post_with = side_effect_answer()
    runner.run_book(book(), create_authorized=True)
    assert store.get(OTHER_BOOK, "en") is None
    assert runner._side_effect_stop(tdr.BookInput(OTHER_BOOK, "OTHER", vi_row(), contents("en"), "auth"), "en") is None


def test_an_ordinary_refusal_does_not_stop_the_other_language(tmp_path: Path) -> None:
    runner, bridge, _store = side_effect_runner(tmp_path)
    bridge.fail_post_with = Response(409, {"code": "tsyc_source_changed", "message": "m", "data": {"status": 409}})
    result = runner.run_book(book(), create_authorized=True)
    assert result.languages["en"].code == "tsyc_source_changed"
    assert result.languages["de"].code != tdr.BOOK_STOPPED_SIDE_EFFECT
    assert len(bridge.posts) == 2


def test_busy_is_uncertain_and_is_reconciled_not_retried_blindly(tmp_path: Path) -> None:
    runner, bridge, store = side_effect_runner(tmp_path)
    bridge.fail_post_with = Response(409, {"code": "tsyc_busy", "message": "m", "data": {"status": 409}})
    first = runner.run_book(book("en"), create_authorized=True)
    assert first.languages["en"].result == tdr.RESULT_UNCERTAIN and store.get(BOOK, "en").state == tcs.UNCERTAIN
    bridge.fail_post_with = None
    second = runner.run_book(book("en"), create_authorized=True)
    assert second.languages["en"].result == tdr.RESULT_CREATED, "By-request said absent, so the same request may be sent."
    assert bridge.posts[0]["request_id"] == bridge.posts[-1]["request_id"]


def test_hardening_pilot_labels_never_promote_synthetic_or_observed_results() -> None:
    pilot = (ROOT / "tools" / "wp_lab" / "run_hardening_pilot.py").read_text(encoding="utf-8")
    hooks = (ROOT / "tools" / "wp_lab" / "tsyc-lab-synthetic-hooks.php").read_text(encoding="utf-8")
    assert 'PASS_SYNTHETIC = "PASS_SYNTHETIC_HOOK"' in pilot
    assert 'BLOCKED_SOURCE = "BLOCKED_MISSING_SOURCE"' in pilot
    assert 'OBSERVED = "OBSERVED_REAL_SOURCE"' in pilot
    for name in re.findall(r"def (y\d+_\w+)", pilot):
        body = pilot[pilot.index(f"def {name}"):]
        body = body[:body.index("\n    def ", 10)]
        assert "return PASS_SYNTHETIC" in body and "PASS_REAL" not in body, name
    assert "NOT the store's code" in hooks and "wp_get_environment_type() !== 'local'" in hooks


def test_source_dependent_cases_stay_registered_as_blocked() -> None:
    pilot = (ROOT / "tools" / "wp_lab" / "run_hardening_pilot.py").read_text(encoding="utf-8")
    for test_id in ("B11", "B12", "B13", "B14", "B15", "B16"):
        assert f'("{test_id}"' in pilot, test_id
    assert '"read_by_a_person": "required; this is never a pass"' in pilot


def test_lab_only_plugins_are_not_part_of_the_bridge(php: str) -> None:
    code = code_only(php)
    for marker in ("tsyc_lab_", "HTTP_X_TSYC_LAB", "tsyc-lab-"):
        assert marker not in code, marker
