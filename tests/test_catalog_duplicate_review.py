"""Regression tests for the evidence-based CATALOG_DUPLICATE_REVIEW
resolution (scripts/resolve_catalog_duplicate_review.py, 2026-10-23).

Production cases: FB-HIST-2026-IMG-005-CAN-0006 "Bé Chơi Cùng Mặt Trời" vs
sibling volume CAN-0007 "Bé Chơi Cùng Mặt Trăng" (distinct books of one
3-book series), and FB-HIST-2026-IMG-002-CAN-0019 vs owner #1493 (same book).

A review must never weaken the check globally: it clears only the exact
reviewed POSSIBLE matches. No network, no Supabase, no WooCommerce.
"""
from __future__ import annotations

import pipeline_state
import resolve_catalog_duplicate_review as res
from src.domain import woo_catalog_identity as wci
from src.domain.rules import catalog_identity_index as idx
from support.fake_supabase import FakeSupabaseRepository

CODE = "TSYC-FB-HIST-2026-IMG-005-CAN-0006"
SUN = idx.Listing(title="Bé Chơi Cùng Mặt Trời", author="Isabel M. Arques, Angela Pelaez-Vargas")
MOON = idx.IndexEntry(origin=idx.ORIGIN_CANDIDATE, ref="FB-HIST-2026-IMG-005-CAN-0007", title="Bé Chơi Cùng Mặt Trăng")
EVIDENCE = {"summary": "Separate volumes.", "sources": [{"source": "https://www.netabooks.vn/x", "finding": "3 books"}]}


def snap(*local, woo=()):
    return wci.CatalogSnapshot(woo_products=list(woo), complete=True, local_entries=list(local))


def moon_review():
    return frozenset({wci.review_entry_key(MOON.origin, MOON.ref, MOON.title)})


def test_unreviewed_sibling_volume_is_still_a_possible_duplicate():
    result = wci.check_before_create(SUN, product_code=CODE, snapshot=snap(MOON))
    assert result.classification == wci.POSSIBLE_DUPLICATE


def test_reviewed_distinct_match_is_cleared_and_kept_as_evidence():
    result = wci.check_before_create(SUN, product_code=CODE, snapshot=snap(MOON), reviewed_distinct=moon_review())
    assert result.classification == wci.NEW_CONFIRMED and result.create_allowed
    assert result.evidence[0]["reviewed_distinct"] is True


def test_review_does_not_cover_a_new_unreviewed_match():
    other = idx.IndexEntry(origin=idx.ORIGIN_CANDIDATE, ref="X-1", title="Bé Chơi Cùng Mặt Trời Mọc")
    result = wci.check_before_create(SUN, product_code=CODE, snapshot=snap(MOON, other), reviewed_distinct=moon_review())
    assert result.classification == wci.POSSIBLE_DUPLICATE


def test_review_is_void_when_the_other_record_is_renamed():
    renamed = idx.IndexEntry(origin=MOON.origin, ref=MOON.ref, title="Bé Chơi Cùng Mặt Trời Và Mặt Trăng")
    result = wci.check_before_create(SUN, product_code=CODE, snapshot=snap(renamed), reviewed_distinct=moon_review())
    assert result.classification == wci.POSSIBLE_DUPLICATE


def test_review_never_clears_a_certain_match():
    same = idx.IndexEntry(origin=idx.ORIGIN_CANDIDATE, ref=MOON.ref, title=SUN.title)
    reviewed = frozenset({wci.review_entry_key(same.origin, same.ref, same.title)})
    result = wci.check_before_create(SUN, product_code=CODE, snapshot=snap(same), reviewed_distinct=reviewed)
    assert result.classification == wci.EXISTING_PRODUCT


def test_review_never_clears_a_barcode_match():
    snapshot = wci.CatalogSnapshot(woo_products=[], complete=True, barcodes={"OTHER": "8935"})
    reviewed = frozenset({wci.review_entry_key("BARCODE", "OTHER", "")})
    result = wci.check_before_create(SUN, product_code=CODE, snapshot=snapshot, barcode="8935", reviewed_distinct=reviewed)
    assert result.classification == wci.POSSIBLE_DUPLICATE


def test_review_keys_require_the_same_product_code():
    review = {"decision": wci.REVIEW_DISTINCT, "product_code": "OTHER",
              "distinct_from": [{"origin": MOON.origin, "ref": MOON.ref, "title": MOON.title}]}
    assert wci.reviewed_distinct_keys({"product_code": CODE, "product_metadata": {wci.REVIEW_KEY: review}}) == frozenset()
    review["product_code"] = CODE
    assert wci.reviewed_distinct_keys({"product_code": CODE, "product_metadata": {wci.REVIEW_KEY: review}}) == moon_review()


# --- resolver planning --------------------------------------------------

def blocked_product(evidence, logical=()):
    marker = {"classification": wci.POSSIBLE_DUPLICATE, "reason": "Possible duplicate.", "product_code": CODE,
              "evidence": evidence, "logical_products": [list(g) for g in logical], "terminal": False,
              "checked_at": "t0", "check_version": "1.0.0"}
    return {"internal_product_id": "ip-1", "candidate_id": "c-1", "product_code": CODE, "updated_at": "u0",
            "woocommerce_status": "READY_FOR_DRAFT", "product_metadata": {wci.MARKER_KEY: marker}}


MOON_EVIDENCE = [{"origin": MOON.origin, "ref": MOON.ref, "title": MOON.title, "strength": idx.MATCH_POSSIBLE}]
WOO_EVIDENCE = [{"origin": idx.ORIGIN_WOO, "ref": "1493", "title": "Phương Pháp Học Tập Không Giới Hạn (Limitless) - Jim Kwik",
                 "strength": idx.MATCH_POSSIBLE}]


def plan(product, decision, woo_ids=(), refs=(), evidence=EVIDENCE, get_woo=None, run_check=None, syncs=()):
    return res.plan_resolution(
        product=product, syncs=list(syncs), decision=decision, woo_ids=list(woo_ids), distinct_refs=list(refs),
        evidence=evidence, now="t1",
        get_woo=get_woo or (lambda i: (200, {"id": i, "name": "n", "status": "publish"})),
        run_check=run_check or (lambda p: wci.check_before_create(
            SUN, product_code=CODE, snapshot=snap(MOON), reviewed_distinct=wci.reviewed_distinct_keys(p))))


def test_distinct_plan_reruns_the_check_with_the_review():
    result = plan(blocked_product(MOON_EVIDENCE), wci.REVIEW_DISTINCT, refs=[MOON.ref])
    assert result.ok and result.marker["classification"] == wci.NEW_CONFIRMED
    assert result.review["distinct_from"] == [{"origin": MOON.origin, "ref": MOON.ref, "title": MOON.title}]


def test_distinct_plan_refuses_when_the_fresh_check_still_blocks():
    still = lambda p: wci.CreateCheck(wci.POSSIBLE_DUPLICATE, "new match")  # noqa: E731
    assert not plan(blocked_product(MOON_EVIDENCE), wci.REVIEW_DISTINCT, refs=[MOON.ref], run_check=still).ok


def test_distinct_plan_refuses_refs_outside_the_stored_evidence():
    assert not plan(blocked_product(MOON_EVIDENCE), wci.REVIEW_DISTINCT, refs=["SOMETHING-ELSE"]).ok


def test_same_as_existing_plan_writes_a_terminal_marker():
    result = plan(blocked_product(WOO_EVIDENCE, logical=[(1493, 1494, 1495)]), wci.REVIEW_SAME_AS_EXISTING, woo_ids=[1493])
    assert result.ok and result.marker["classification"] == wci.EXISTING_PRODUCT and result.marker["terminal"]


def test_same_as_existing_plan_refuses_trashed_or_unlisted_products():
    product = blocked_product(WOO_EVIDENCE)
    assert not plan(product, wci.REVIEW_SAME_AS_EXISTING, woo_ids=[1493],
                    get_woo=lambda i: (200, {"status": "trash"})).ok
    assert not plan(product, wci.REVIEW_SAME_AS_EXISTING, woo_ids=[999]).ok


def test_plan_refuses_without_sources_or_after_a_create():
    product = blocked_product(MOON_EVIDENCE)
    assert not plan(product, wci.REVIEW_DISTINCT, refs=[MOON.ref], evidence={"summary": "x", "sources": []}).ok
    assert not plan(product, wci.REVIEW_DISTINCT, refs=[MOON.ref], syncs=[{"woocommerce_product_id": 5}]).ok


def test_apply_is_bounded_and_logged():
    repository = FakeSupabaseRepository()
    product = blocked_product(MOON_EVIDENCE)
    repository.client.tables["internal_products"] = [dict(product)]
    result = plan(product, wci.REVIEW_DISTINCT, refs=[MOON.ref])
    res.apply_plan(repository, product, result, "t1")
    stored = repository.client.tables["internal_products"][0]["product_metadata"]
    assert stored[wci.MARKER_KEY]["classification"] == wci.NEW_CONFIRMED
    assert wci.blocking_marker({"product_code": CODE, "product_metadata": stored}) is None
    assert repository.client.tables["process_logs"][-1]["process_name"] == res.TOOL_NAME


def test_existing_product_review_makes_the_candidate_terminal():
    result = plan(blocked_product(WOO_EVIDENCE, logical=[(1493, 1494, 1495)]), wci.REVIEW_SAME_AS_EXISTING, woo_ids=[1493])
    marker = wci.blocking_marker({"product_code": CODE, "product_metadata": {wci.MARKER_KEY: result.marker}})
    assert marker["terminal"] and marker["classification"] == wci.EXISTING_PRODUCT
    assert "EXISTING_WOO_PRODUCT" in pipeline_state.TERMINAL_OR_MANUAL_STATES
