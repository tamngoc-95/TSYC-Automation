"""Two workflows, two gates, and the pre-order trio read-back (offline)."""
from __future__ import annotations

from typing import Any

import pytest

from src.domain import integration_gates as ig
from src.domain import language_product_mapping as lpm
from src.domain import preorder_trio_verification as ptv
from src.domain import product_workflow as pw
from src.domain import woo_catalog_identity as wci

BOOK = "00000000-0000-4000-8000-000000000001"
CODE = "FAHASA-900-CAN-0001"


def row(**overrides: Any) -> lpm.LanguageProduct:
    values = dict(internal_product_id=BOOK, product_code=CODE, language_code="vi", woocommerce_product_id=100,
                  origin=lpm.ORIGIN_TSYC_CREATED, mapping_status=lpm.RECORDED)
    return lpm.LanguageProduct(**{**values, **overrides})


def product(**overrides: Any) -> dict[str, Any]:
    values = {"id": 100, "status": "draft", "sku": CODE, "type": "simple", "regular_price": "", "sale_price": "", "price": "",
              "manage_stock": True, "stock_quantity": 0, "backorders": "yes", "stock_status": "onbackorder",
              "purchasable": False, "name": "VI", "description": "<p>vi</p>", "short_description": "s vi",
              "images": [{"id": 7}, {"id": 8}], "weight": "0.3", "dimensions": {"length": "20", "width": "", "height": ""},
              "catalog_visibility": "visible",
              "attributes": [{"name": "Tác giả", "position": 0, "options": ["A. Lab"]}],
              "meta_data": [{"key": "_tsyc_internal_product_id", "value": BOOK}]}
    return {**values, **overrides}


def contents(*approved: str) -> list[dict[str, Any]]:
    return [{"content_language": lang, "content_status": "APPROVED" if lang in approved else "DRAFTED",
             "review_required": lang not in approved} for lang in ("vi", "en", "de")]


CANDIDATE = {"candidate_code": CODE, "source_evidence": {"origin": "FAHASA_DISCOVERY",
                                                         "supply_status": "PREORDER_PENDING_SUPPLY_CONFIRMATION"}}


def classify(**overrides: Any) -> pw.WorkflowDecision:
    values = dict(row=row(), product=product(), contents=contents("vi", "en", "de"), candidate=CANDIDATE, catalog_conflict=False)
    values.update(overrides)
    return pw.classify(values["row"], values["product"], values["contents"], candidate=values["candidate"],
                       catalog_conflict=values["catalog_conflict"])


# ------------------------------------------------------------- workflows


def test_a_new_unpriced_preorder_draft_with_three_approved_languages_is_eligible() -> None:
    decision = classify()
    assert decision.workflow == pw.NEW_PREORDER_PRODUCT and decision.eligible and decision.automation_may_write


@pytest.mark.parametrize("status", ["publish", "private", "pending", "future"])
def test_an_existing_product_is_never_writable_whatever_else_is_true(status: str) -> None:
    decision = classify(product=product(status=status))
    assert decision.workflow == pw.EXISTING_PUBLISHED_PRODUCT
    assert not decision.eligible and not decision.automation_may_write
    assert decision.blockers == (pw.EXISTING_PRODUCT_PROTECTED,)


@pytest.mark.parametrize("change, blocker", [
    ({"row": row(mapping_status=lpm.REMOTE_REMOVED)}, pw.OWNER_REMOVED_IDENTITY),
    ({"product": product(status="trash")}, pw.OWNER_REMOVED_IDENTITY),
    ({"product": None}, pw.VI_NOT_READ),
    ({"row": row(mapping_status=lpm.CONFLICT)}, pw.VI_MAPPING_NOT_RECORDED),
    ({"candidate": {"candidate_code": "FB-HIST-0001", "source_evidence": {}}}, pw.NOT_PREORDER_CANDIDATE),
    ({"candidate": {"candidate_code": CODE, "source_evidence": {"origin": "FAHASA_DISCOVERY", "supply_status": "IN_STOCK"}}},
     pw.NOT_PREORDER_CANDIDATE),
    ({"candidate": None}, pw.NOT_PREORDER_CANDIDATE),
])
def test_out_of_scope_books(change: dict[str, Any], blocker: str) -> None:
    decision = classify(**change)
    assert decision.workflow == pw.OUT_OF_SCOPE and decision.blockers == (blocker,) and not decision.eligible


@pytest.mark.parametrize("change, blocker", [
    ({"product": product(regular_price="120000")}, pw.OWNER_PRICED),
    ({"product": product(sale_price="1")}, pw.OWNER_PRICED),
    ({"product": product(manage_stock=False)}, pw.VI_NOT_PREORDER_SHAPE),
    ({"product": product(stock_quantity=3)}, pw.VI_NOT_PREORDER_SHAPE),
    ({"product": product(backorders="no")}, pw.VI_NOT_PREORDER_SHAPE),
    ({"product": product(sku="OTHER")}, pw.VI_SKU_MISMATCH),
    ({"product": product(meta_data=[])}, pw.VI_NOT_AUTOMATION_OWNED),
    ({"product": product(meta_data=[{"key": "_tsyc_internal_product_id", "value": "someone-else"}])}, pw.VI_NOT_AUTOMATION_OWNED),
    ({"contents": contents("vi", "en")}, pw.CONTENT_NOT_APPROVED),
    ({"contents": contents("en", "de")}, pw.CONTENT_NOT_APPROVED),
    ({"catalog_conflict": True}, pw.CATALOG_IDENTITY_CONFLICT),
    ({"catalog_conflict": None}, pw.CATALOG_CHECK_MISSING),
])
def test_preorder_blockers(change: dict[str, Any], blocker: str) -> None:
    decision = classify(**change)
    assert decision.workflow == pw.NEW_PREORDER_PRODUCT
    assert blocker in decision.blockers and not decision.eligible


def test_catalog_marker_is_never_assumed_clean() -> None:
    def internal(marker: Any) -> dict[str, Any]:
        return {"product_code": CODE, "product_metadata": {wci.MARKER_KEY: marker} if marker is not None else {}}

    assert pw.catalog_conflict_from_marker(None) is None
    assert pw.catalog_conflict_from_marker(internal(None)) is None
    assert pw.catalog_conflict_from_marker(internal({"classification": wci.NEW_CONFIRMED, "product_code": CODE})) is False
    assert pw.catalog_conflict_from_marker(internal({"classification": wci.NEW_CONFIRMED, "product_code": "OTHER"})) is None
    assert pw.catalog_conflict_from_marker(internal({"classification": wci.CATALOG_UNAVAILABLE, "product_code": CODE})) is None
    assert pw.catalog_conflict_from_marker(internal({"classification": wci.EXISTING_PRODUCT, "product_code": CODE})) is True


def test_summary_counts_each_workflow() -> None:
    summary = pw.summarize([classify(), classify(product=product(status="publish")), classify(product=None),
                            classify(contents=contents("vi"))])
    assert summary["by_workflow"] == {pw.NEW_PREORDER_PRODUCT: 2, pw.EXISTING_PUBLISHED_PRODUCT: 1, pw.OUT_OF_SCOPE: 1}
    assert summary["preorder_eligible"] == 1 and summary["blockers"][pw.CONTENT_NOT_APPROVED] == 1


# ------------------------------------------------------------------ gates


def all_a(status: str = ig.CHECK_PASS) -> list[ig.CheckResult]:
    return [ig.CheckResult(cid, status) for cid in ig.GATE_A_CHECKS]


def a_with(check_id: str, status: str) -> list[ig.CheckResult]:
    return [ig.CheckResult(cid, status if cid == check_id else ig.CHECK_PASS) for cid in ig.GATE_A_CHECKS]


def all_b(status: str) -> list[ig.CheckResult]:
    return [ig.CheckResult(cid, status, "seen") for cid in ig.GATE_B_CHECKS]


def test_gate_a_passes_only_with_every_check_passed() -> None:
    assert ig.evaluate_gate_a(all_a(), environment=ig.ENV_GENERIC_LOCAL_LAB).status == ig.GATE_PASS
    assert ig.evaluate_gate_a(all_a()[:-1], environment=ig.ENV_GENERIC_LOCAL_LAB).status == ig.GATE_INCOMPLETE
    assert ig.evaluate_gate_a([], environment=ig.ENV_GENERIC_LOCAL_LAB).status == ig.GATE_INCOMPLETE
    for bad, wanted in ((ig.CHECK_FAIL, ig.GATE_FAIL), (ig.CHECK_BLOCKED, ig.GATE_BLOCKED), (ig.CHECK_OBSERVED, ig.GATE_FAIL)):
        for check_id in ("A13", "A14", "A15"):
            assert ig.evaluate_gate_a(a_with(check_id, bad), environment=ig.ENV_GENERIC_LOCAL_LAB).status == wanted


def test_gate_results_refuse_unknown_and_repeated_checks() -> None:
    with pytest.raises(ValueError):
        ig.evaluate_gate_a([ig.CheckResult("B01", ig.CHECK_PASS)], environment=ig.ENV_GENERIC_LOCAL_LAB)
    with pytest.raises(ValueError):
        ig.evaluate_gate_a([ig.CheckResult("A01", ig.CHECK_PASS)] * 2, environment=ig.ENV_GENERIC_LOCAL_LAB)
    with pytest.raises(ValueError):
        ig.evaluate_gate_a(all_a(), environment="PRODUCTION")
    with pytest.raises(ValueError):
        ig.CheckResult("A01", "PASS_SYNTHETIC_HOOK")


@pytest.mark.parametrize("status", ig.CHECK_STATUSES)
def test_gate_b_is_blocked_in_a_generic_lab_whatever_is_reported(status: str) -> None:
    result = ig.evaluate_gate_b(all_b(status), environment=ig.ENV_GENERIC_LOCAL_LAB)
    assert result.status == ig.GATE_BLOCKED
    assert {c.status for c in result.checks.values()} == {ig.CHECK_BLOCKED}


def test_gate_b_never_passes_even_on_the_real_site() -> None:
    site = ig.ENV_ACTUAL_SITE_COPY
    assert ig.evaluate_gate_b(all_b(ig.CHECK_OBSERVED), environment=site).status == ig.GATE_OBSERVED_PENDING_DECISION
    assert ig.evaluate_gate_b(all_b(ig.CHECK_PASS), environment=site).status == ig.GATE_FAIL
    assert ig.evaluate_gate_b(all_b(ig.CHECK_OBSERVED)[:-1], environment=site).status == ig.GATE_BLOCKED
    assert ig.evaluate_gate_b(all_b(ig.CHECK_OBSERVED)[:-1] + [ig.CheckResult("B07", ig.CHECK_FAIL)],
                              environment=site).status == ig.GATE_FAIL
    statuses = {ig.evaluate_gate_b(all_b(s), environment=e).status for s in ig.CHECK_STATUSES for e in ig.ENVIRONMENTS}
    assert ig.GATE_PASS not in statuses


def test_a_lab_pass_of_gate_a_does_not_open_a_production_pilot() -> None:
    lab = ig.evaluate_gate_a(all_a(), environment=ig.ENV_GENERIC_LOCAL_LAB)

    def unmet(**overrides: Any) -> list[str]:
        values = dict(gate_a_lab=lab, gate_a_site_copy=None, developer_reviewed_bridge=True, owner_authorized_pilot=True,
                      scope=ig.DRAFT_ONLY_PILOT)
        return ig.production_pilot_requirements(**{**values, **overrides})

    missing = unmet()
    assert len(missing) == 1 and missing[0].startswith(ig.BLOCKED_REAL_SITE_TEST)
    # A lab result handed in as the site-copy result is not accepted.
    assert unmet(gate_a_site_copy=lab) == ["Gate A has not been run on an isolated copy of the real site."]
    site = ig.evaluate_gate_a(all_a(), environment=ig.ENV_ACTUAL_SITE_COPY)
    assert unmet(gate_a_site_copy=site) == []
    assert len(unmet(gate_a_site_copy=site, developer_reviewed_bridge=False, owner_authorized_pilot=False)) == 2
    failed = ig.evaluate_gate_a(a_with("A11", ig.CHECK_FAIL), environment=ig.ENV_ACTUAL_SITE_COPY)
    assert unmet(gate_a_site_copy=failed)
    assert unmet(gate_a_lab=None, gate_a_site_copy=site) == ["Gate A has not been run in the local lab."]


def test_gate_a_never_stands_in_for_gate_b() -> None:
    gate_a = ig.evaluate_gate_a(all_a(), environment=ig.ENV_ACTUAL_SITE_COPY)
    unmet = ig.existing_product_workflow_requirements(gate_b=gate_a, sync_authority_decided=True, owner_decided_scope=True)
    assert unmet == ["Gate B has not been observed on an isolated copy of the real site."]
    observed = ig.evaluate_gate_b(all_b(ig.CHECK_OBSERVED), environment=ig.ENV_ACTUAL_SITE_COPY)
    assert len(ig.existing_product_workflow_requirements(gate_b=observed, sync_authority_decided=False,
                                                         owner_decided_scope=False)) == 2
    assert ig.existing_product_workflow_requirements(gate_b=observed, sync_authority_decided=True,
                                                     owner_decided_scope=True) == []


# ------------------------------------------------------------ trio check


def trio() -> dict[str, dict[str, Any]]:
    def copy(pid: int, lang: str, label: str) -> dict[str, Any]:
        return product(id=pid, sku="", name=lang.upper(), description=f"<p>{lang}</p>", short_description=f"s {lang}",
                       attributes=[{"name": label, "position": 0, "options": ["A. Lab"]}],
                       meta_data=[{"key": "_tsyc_internal_product_id", "value": BOOK}, {"key": "_tsyc_language", "value": lang}])
    return {"vi": product(), "en": copy(101, "en", "Author"), "de": copy(102, "de", "Autorin")}


def verify(products: dict[str, dict[str, Any]], views: dict[str, dict[str, int]] | None = None) -> dict[str, list[str]]:
    ids = {"vi": 100, "en": 101, "de": 102}
    text = {lang: {"name": "VI" if lang == "vi" else lang.upper(), "description": f"<p>{lang}</p>",
                   "short_description": f"s {lang}"} for lang in ids}
    return ptv.verify_trio(internal_product_id=BOOK, product_code=CODE, products=products,
                           group_views=views or {lang: dict(ids) for lang in ids}, expected_text=text)


def test_a_correct_trio_has_no_problems() -> None:
    assert verify(trio()) == {check: [] for check in ptv.CHECK_IDS}


def change(lang: str, **fields: Any) -> dict[str, dict[str, Any]]:
    products = trio()
    products[lang] = {**products[lang], **fields}
    return products


@pytest.mark.parametrize("products, check", [
    (change("en", status="publish"), "A07"),
    (change("de", stock_quantity=4), "A07"),
    (change("de", backorders="no"), "A07"),
    (change("en", manage_stock=False), "A07"),
    (change("vi", stock_status="instock"), "A07"),
    (change("en", regular_price="99000"), "A08"),
    (change("de", sale_price="1"), "A08"),
    (change("vi", price="5"), "A08"),
    (change("en", purchasable=True), "A08"),
    (change("de", description="<p>en</p>"), "A09"),
    (change("en", name="Other title"), "A09"),
    (change("en", images=[{"id": 7}]), "A10"),
    (change("de", attributes=[{"name": "Autorin", "position": 0, "options": ["Someone else"]}]), "A10"),
    (change("de", weight="0.9"), "A10"),
    (change("vi", images=[]), "A10"),
    (change("en", sku="ANOTHER-BOOK"), "A06"),
    (change("de", meta_data=[{"key": "_tsyc_internal_product_id", "value": "other"}, {"key": "_tsyc_language", "value": "de"}]), "A06"),
    (change("de", meta_data=[{"key": "_tsyc_internal_product_id", "value": BOOK}, {"key": "_tsyc_language", "value": "en"}]), "A06"),
    (change("vi", sku="WRONG"), "A06"),
])
def test_each_defect_is_reported_under_its_own_check(products: dict[str, dict[str, Any]], check: str) -> None:
    problems = verify(products)
    assert problems[check], problems
    assert all(not found for name, found in problems.items() if name != check), problems


def test_a_copied_vi_sku_is_one_logical_book_and_labels_may_differ() -> None:
    assert verify(change("en", sku=CODE))["A06"] == []


def test_group_must_read_the_same_from_every_member() -> None:
    ids = {"vi": 100, "en": 101, "de": 102}
    views = {"vi": dict(ids), "en": dict(ids), "de": {"vi": 100, "de": 102}}
    assert verify(trio(), views)["A05"]
    assert verify(trio(), {"vi": dict(ids), "en": dict(ids)})["A05"]


def test_a_missing_member_fails_every_check() -> None:
    products = trio()
    del products["de"]
    assert all(verify(products).values())


# ------------------------------------------- Gate B deferral (owner decision)


def test_gate_b_is_deferred_only_for_a_strictly_draft_only_pilot_and_never_passed() -> None:
    blocked = ig.evaluate_gate_b([], environment=ig.ENV_GENERIC_LOCAL_LAB)
    deferred = ig.defer_gate_b(blocked, ig.DRAFT_ONLY_PILOT)
    assert deferred.status == ig.GATE_DEFERRED and deferred.status != ig.GATE_PASS
    assert "Not passed" in deferred.reasons[0] and ig.GATE_BLOCKED in deferred.reasons[1]
    assert ig.defer_gate_b(None, ig.DRAFT_ONLY_PILOT).status == ig.GATE_DEFERRED
    # A Gate A result handed in is not Gate B.
    gate_a = ig.evaluate_gate_a(all_a(), environment=ig.ENV_ACTUAL_SITE_COPY)
    assert ig.defer_gate_b(gate_a, ig.DRAFT_ONLY_PILOT).gate == ig.GATE_B
    # A Gate B that FAILED on the real site is never hidden behind a deferral.
    failed = ig.evaluate_gate_b(all_b(ig.CHECK_OBSERVED)[:-1] + [ig.CheckResult("B07", ig.CHECK_FAIL)],
                                environment=ig.ENV_ACTUAL_SITE_COPY)
    assert ig.defer_gate_b(failed, ig.DRAFT_ONLY_PILOT).status == ig.GATE_FAIL


SCOPE_FIELDS = ("creates_new_vi_en_de_drafts_only", "all_prices_left_empty", "nothing_published",
                "no_customer_orders_accepted", "no_existing_published_product_modified",
                "no_owner_maintained_product_updated", "inventory_sync_not_relied_on")


def test_the_scope_has_exactly_the_seven_conditions_of_the_owner_decision() -> None:
    assert tuple(ig.DraftOnlyPilotScope.__dataclass_fields__) == SCOPE_FIELDS
    assert ig.DRAFT_ONLY_PILOT.violations() == []
    assert len(ig.DraftOnlyPilotScope().violations()) == 7      # nothing is assumed


@pytest.mark.parametrize("field", SCOPE_FIELDS)
def test_any_step_outside_the_scope_brings_gate_b_back(field: str) -> None:
    scope = ig.DraftOnlyPilotScope(**{name: name != field for name in SCOPE_FIELDS})
    assert len(scope.violations()) == 1
    blocked = ig.evaluate_gate_b([], environment=ig.ENV_GENERIC_LOCAL_LAB)
    kept = ig.defer_gate_b(blocked, scope)
    assert kept.status == ig.GATE_BLOCKED and any("Not deferrable" in reason for reason in kept.reasons)
    lab = ig.evaluate_gate_a(all_a(), environment=ig.ENV_GENERIC_LOCAL_LAB)
    site = ig.evaluate_gate_a(all_a(), environment=ig.ENV_ACTUAL_SITE_COPY)
    unmet = ig.production_pilot_requirements(gate_a_lab=lab, gate_a_site_copy=site, developer_reviewed_bridge=True,
                                             owner_authorized_pilot=True, scope=scope, gate_b=blocked)
    assert len(unmet) == 2 and "Gate B is BLOCKED, and it is required" in unmet[1]


def test_without_a_stated_scope_gate_b_is_required() -> None:
    lab = ig.evaluate_gate_a(all_a(), environment=ig.ENV_GENERIC_LOCAL_LAB)
    site = ig.evaluate_gate_a(all_a(), environment=ig.ENV_ACTUAL_SITE_COPY)
    unmet = ig.production_pilot_requirements(gate_a_lab=lab, gate_a_site_copy=site, developer_reviewed_bridge=True,
                                             owner_authorized_pilot=True)
    assert len(unmet) == 8


def test_deferring_gate_b_never_weakens_gate_a() -> None:
    lab = ig.evaluate_gate_a(a_with("A14", ig.CHECK_FAIL), environment=ig.ENV_GENERIC_LOCAL_LAB)
    unmet = ig.production_pilot_requirements(gate_a_lab=lab, gate_a_site_copy=None, developer_reviewed_bridge=True,
                                             owner_authorized_pilot=True, scope=ig.DRAFT_ONLY_PILOT)
    assert any("Gate A in the local lab is FAIL" in text for text in unmet)
    assert any(text.startswith(ig.BLOCKED_REAL_SITE_TEST) for text in unmet)


def test_a_site_copy_needs_its_own_subset_and_any_failure_counts() -> None:
    site = ig.ENV_ACTUAL_SITE_COPY
    subset = [ig.CheckResult(cid, ig.CHECK_PASS) for cid in ig.GATE_A_SITE_CHECKS]
    assert ig.evaluate_gate_a(subset, environment=site).status == ig.GATE_PASS
    assert ig.evaluate_gate_a(subset, environment=ig.ENV_GENERIC_LOCAL_LAB).status == ig.GATE_INCOMPLETE
    assert ig.evaluate_gate_a(subset[:-1], environment=site).status == ig.GATE_INCOMPLETE
    assert ig.evaluate_gate_a(subset[:-1] + [ig.CheckResult("A15", ig.CHECK_BLOCKED)], environment=site).status == ig.GATE_BLOCKED
    # A failed check outside the subset still fails the gate.
    assert ig.evaluate_gate_a(subset + [ig.CheckResult("A12", ig.CHECK_FAIL)], environment=site).status == ig.GATE_FAIL
    assert set(ig.GATE_A_SITE_CHECKS) < set(ig.GATE_A_CHECKS) and {"A11", "A15"} <= set(ig.GATE_A_SITE_CHECKS)
