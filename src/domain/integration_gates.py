"""The two integration gates of the multilingual work (no I/O).

  GATE A  PREORDER_DRAFT_CREATION_SAFETY
          Creating three linked, unpriced pre-order drafts changes nothing
          else. Provable without the store's price/stock customization.
  GATE B  LIVE_COMMERCE_SYNCHRONIZATION_SAFETY
          How the real site moves price, stock and backorders between the
          language copies, and how orders behave. Only the real site's own
          code can show this.

Rules this module enforces, so no report can blur them:

  * Gate A passing never counts as Gate B passing.
  * A Gate A result always carries the environment it was measured in. A
    pass in the generic local lab (plain WordPress, WooCommerce, Polylang) is
    not a pass on a copy of the real site.
  * A Gate B check without evidence from the real site's code is BLOCKED,
    never PASS. With evidence it is OBSERVED: a person decides, never this code.
  * Nothing here enables a write. `production_pilot_requirements` only lists
    what is still unmet.

Owner decision, 2026-11-16: Gate B is not required before an UNPRICED,
UNPUBLISHED, draft-only multilingual pilot. That is a scope decision, not an
authorization to write to production. Gate B is then DEFERRED, never PASSED,
and only while the pilot stays inside `DraftOnlyPilotScope`. Gate A stays
mandatory, in the lab and on a copy of the real site.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

GATE_A = "PREORDER_DRAFT_CREATION_SAFETY"
GATE_B = "LIVE_COMMERCE_SYNCHRONIZATION_SAFETY"

# Where evidence was measured.
ENV_GENERIC_LOCAL_LAB = "GENERIC_LOCAL_LAB"        # no theme, no site customization
ENV_ACTUAL_SITE_COPY = "ACTUAL_SITE_COPY"          # isolated copy running the real customization
ENVIRONMENTS = (ENV_GENERIC_LOCAL_LAB, ENV_ACTUAL_SITE_COPY)

# One check's result.
CHECK_PASS = "PASS"
CHECK_FAIL = "FAIL"
CHECK_BLOCKED = "BLOCKED"
CHECK_OBSERVED = "OBSERVED"        # real-site behaviour recorded for a person; never a pass
CHECK_NOT_RUN = "NOT_RUN"
CHECK_STATUSES = (CHECK_PASS, CHECK_FAIL, CHECK_BLOCKED, CHECK_OBSERVED, CHECK_NOT_RUN)

# One gate's result.
GATE_PASS = "PASS"
GATE_FAIL = "FAIL"
GATE_BLOCKED = "BLOCKED"
GATE_INCOMPLETE = "INCOMPLETE"
GATE_OBSERVED_PENDING_DECISION = "OBSERVED_PENDING_DECISION"
GATE_DEFERRED = "DEFERRED"                         # Gate B only: out of a draft-only pilot's scope. Never a pass.
# Gate A on the real site when no isolated copy has been tested. Never a pass.
BLOCKED_REAL_SITE_TEST = "BLOCKED_REAL_SITE_TEST"

GATE_A_CHECKS: Mapping[str, str] = {
    "A01": "A genuinely new synthetic logical book is selected (workflow NEW_PREORDER_PRODUCT)",
    "A02": "Its approved VI/EN/DE content loads through the production loader and renderer",
    "A03": "One canonical VI draft is created",
    "A04": "EN and DE drafts are created through the native Polylang bridge",
    "A05": "The authoritative Polylang group is read back, identically from every member",
    "A06": "The three product IDs are one sellable book (identity, language, exact IDs)",
    "A07": "Pre-order fields on all three: managed stock 0, backorders yes, draft",
    "A08": "Price fields are empty on all three",
    "A09": "Each language's stored text equals its approved package",
    "A10": "Images, contributors and metadata are correct on every copy",
    "A11": "No other product was created, changed or removed (whole-catalog snapshot)",
    "A12": "A repeated run sends nothing and creates no duplicate",
    "A13": "Interruption recovery and rollback leave no duplicate and no stray change",
    "A14": "Catalog identity check: exact book and edition, no Woo or Facebook duplicate, no owner-removed match",
    "A15": "Creating and linking EN/DE changed nothing on the VI draft (price, stock, backorders, SKU, images, status, owner fields)",
}

# What a copy of the real site can show through before/after snapshots. The
# other checks are properties of TSYC's own code and records, proven in the lab.
GATE_A_SITE_CHECKS = ("A05", "A06", "A07", "A08", "A10", "A11", "A15")

GATE_B_CHECKS: Mapping[str, str] = {
    "B01": "Price synchronization between language copies with the site's customization",
    "B02": "Stock management across language copies",
    "B03": "Backorder setting propagation",
    "B04": "Order behaviour in each language",
    "B05": "Concurrent orders across languages",
    "B06": "Price and stock source of truth (direction)",
    "B07": "An owner-entered selling price is kept and handled correctly",
}

CHECKS: Mapping[str, Mapping[str, str]] = {GATE_A: GATE_A_CHECKS, GATE_B: GATE_B_CHECKS}


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    status: str
    evidence: str = ""

    def __post_init__(self) -> None:
        if self.status not in CHECK_STATUSES:
            raise ValueError(f"Unknown check status {self.status!r}.")


@dataclass(frozen=True)
class GateResult:
    gate: str
    environment: str
    status: str
    checks: Mapping[str, CheckResult] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        titles = CHECKS[self.gate]
        return {"gate": self.gate, "environment": self.environment, "status": self.status,
                "reasons": list(self.reasons),
                "checks": [{"id": cid, "title": titles[cid], "status": self.checks[cid].status,
                            "evidence": self.checks[cid].evidence} for cid in titles]}


def _collect(gate: str, results: Sequence[CheckResult]) -> dict[str, CheckResult]:
    known = CHECKS[gate]
    collected: dict[str, CheckResult] = {}
    for result in results:
        if result.check_id not in known:
            raise ValueError(f"{result.check_id} is not a {gate} check.")
        if result.check_id in collected:
            raise ValueError(f"{result.check_id} reported twice.")
        collected[result.check_id] = result
    for check_id in known:
        collected.setdefault(check_id, CheckResult(check_id, CHECK_NOT_RUN))
    return collected


def evaluate_gate_a(results: Sequence[CheckResult], *, environment: str) -> GateResult:
    """PASS only when every required check passed. OBSERVED is not accepted
    here: Gate A checks are exact comparisons. In the lab every check is
    required; on a copy of the real site, GATE_A_SITE_CHECKS."""
    if environment not in ENVIRONMENTS:
        raise ValueError(f"Unknown environment {environment!r}.")
    checks = _collect(GATE_A, results)
    required = set(GATE_A_SITE_CHECKS) if environment == ENV_ACTUAL_SITE_COPY else set(GATE_A_CHECKS)
    by = lambda status: sorted(cid for cid, c in checks.items()  # noqa: E731
                               if c.status == status and (cid in required or status == CHECK_FAIL))
    if by(CHECK_FAIL):
        return GateResult(GATE_A, environment, GATE_FAIL, checks, tuple(f"{cid} failed" for cid in by(CHECK_FAIL)))
    if by(CHECK_OBSERVED):
        return GateResult(GATE_A, environment, GATE_FAIL, checks,
                          tuple(f"{cid}: an observation is not a Gate A result" for cid in by(CHECK_OBSERVED)))
    if by(CHECK_BLOCKED):
        return GateResult(GATE_A, environment, GATE_BLOCKED, checks, tuple(f"{cid} blocked" for cid in by(CHECK_BLOCKED)))
    if by(CHECK_NOT_RUN):
        return GateResult(GATE_A, environment, GATE_INCOMPLETE, checks, tuple(f"{cid} not run" for cid in by(CHECK_NOT_RUN)))
    return GateResult(GATE_A, environment, GATE_PASS, checks)


def evaluate_gate_b(results: Sequence[CheckResult], *, environment: str) -> GateResult:
    """Never PASS from this function. Outside a copy of the real site every
    check is BLOCKED whatever was reported; on one, complete observations give
    OBSERVED_PENDING_DECISION for the owner and developer to read."""
    if environment not in ENVIRONMENTS:
        raise ValueError(f"Unknown environment {environment!r}.")
    checks = _collect(GATE_B, results)
    if environment != ENV_ACTUAL_SITE_COPY:
        blocked = {cid: CheckResult(cid, CHECK_BLOCKED,
                                    "Needs the real site's customization; a generic lab result is the native "
                                    "baseline only." + (f" Baseline: {c.evidence}" if c.evidence else ""))
                   for cid, c in checks.items()}
        return GateResult(GATE_B, environment, GATE_BLOCKED, blocked,
                          ("The real site's price/stock customization was not present.",))
    by = lambda status: sorted(cid for cid, c in checks.items() if c.status == status)  # noqa: E731
    if by(CHECK_FAIL):
        return GateResult(GATE_B, environment, GATE_FAIL, checks, tuple(f"{cid} failed" for cid in by(CHECK_FAIL)))
    if by(CHECK_PASS):
        # A machine cannot know which sync direction the owner wants.
        return GateResult(GATE_B, environment, GATE_FAIL, checks,
                          tuple(f"{cid}: Gate B checks are observations, not machine passes" for cid in by(CHECK_PASS)))
    if by(CHECK_BLOCKED) or by(CHECK_NOT_RUN):
        missing = by(CHECK_BLOCKED) + by(CHECK_NOT_RUN)
        return GateResult(GATE_B, environment, GATE_BLOCKED, checks, tuple(f"{cid} has no real-site evidence" for cid in sorted(missing)))
    return GateResult(GATE_B, environment, GATE_OBSERVED_PENDING_DECISION, checks,
                      ("Every check was observed on the real customization; the owner and developer decide.",))


@dataclass(frozen=True)
class DraftOnlyPilotScope:
    """The only pilot for which Gate B may be deferred (owner decision
    2026-11-16). Every field must be True; anything else needs Gate B."""

    creates_new_vi_en_de_drafts_only: bool = False
    all_prices_left_empty: bool = False
    nothing_published: bool = False
    no_customer_orders_accepted: bool = False
    no_existing_published_product_modified: bool = False
    no_owner_maintained_product_updated: bool = False
    inventory_sync_not_relied_on: bool = False

    def violations(self) -> list[str]:
        wording = {
            "creates_new_vi_en_de_drafts_only": "The pilot does more than create new VI/EN/DE drafts.",
            "all_prices_left_empty": "A price would be set.",
            "nothing_published": "Something would be published.",
            "no_customer_orders_accepted": "Real customer orders could be accepted.",
            "no_existing_published_product_modified": "An existing published product would be modified.",
            "no_owner_maintained_product_updated": "An owner-maintained product would be updated.",
            "inventory_sync_not_relied_on": "The pilot relies on production inventory synchronization, which is untested.",
        }
        return [text for name, text in wording.items() if getattr(self, name) is not True]


DRAFT_ONLY_PILOT = DraftOnlyPilotScope(True, True, True, True, True, True, True)


def defer_gate_b(gate_b: GateResult | None, scope: DraftOnlyPilotScope) -> GateResult:
    """Gate B as it stands for one pilot. DEFERRED only when the pilot is
    strictly draft-only; otherwise the gate keeps whatever status it has
    (BLOCKED when it was never run). DEFERRED is not a pass and is never
    returned for a gate that FAILED on the real site."""
    current = gate_b if gate_b is not None and gate_b.gate == GATE_B else GateResult(
        GATE_B, ENV_GENERIC_LOCAL_LAB, GATE_BLOCKED, _collect(GATE_B, []), ("Gate B was never run.",))
    violations = scope.violations()
    if violations or current.status == GATE_FAIL:
        return current if not violations else GateResult(
            GATE_B, current.environment, current.status, current.checks,
            current.reasons + tuple(f"Not deferrable: {text}" for text in violations))
    return GateResult(GATE_B, current.environment, GATE_DEFERRED, current.checks,
                      ("Deferred by owner decision 2026-11-16 for a draft-only pilot. Not passed.",
                       f"Underlying status: {current.status}."))


def production_pilot_requirements(
    *,
    gate_a_lab: GateResult | None,
    gate_a_site_copy: GateResult | None,
    developer_reviewed_bridge: bool,
    owner_authorized_pilot: bool,
    scope: DraftOnlyPilotScope = DraftOnlyPilotScope(),
    gate_b: GateResult | None = None,
) -> list[str]:
    """What still stands between today and one authorized production pilot of
    the NEW_PREORDER_PRODUCT workflow (three unpriced drafts). Empty means the
    owner may be asked for the go-ahead; it does not enable anything.

    Gate B is not required for a pilot inside DraftOnlyPilotScope (owner
    decision 2026-11-16): an unpriced draft cannot be bought, and this
    workflow never writes a price or stock value afterwards. A pilot outside
    that scope needs Gate B observed and decided, like any automation that
    touches a published product, a price or stock."""
    unmet: list[str] = []
    for text in scope.violations():
        unmet.append(f"Outside the draft-only scope, so Gate B applies: {text}")
    if scope.violations() or (gate_b is not None and gate_b.status == GATE_FAIL):
        if gate_b is None or gate_b.status != GATE_OBSERVED_PENDING_DECISION:
            unmet.append(f"Gate B is {gate_b.status if gate_b else GATE_BLOCKED}, and it is required for this pilot.")

    def need(result: GateResult | None, environment: str, label: str) -> None:
        if result is None or result.gate != GATE_A or result.environment != environment:
            unmet.append(f"Gate A has not been run {label}.")
        elif result.status != GATE_PASS:
            unmet.append(f"Gate A {label} is {result.status}: {'; '.join(result.reasons) or 'see report'}.")

    need(gate_a_lab, ENV_GENERIC_LOCAL_LAB, "in the local lab")
    if gate_a_site_copy is None:
        unmet.append(f"{BLOCKED_REAL_SITE_TEST}: Gate A has not been run on an isolated copy of the real site.")
    else:
        need(gate_a_site_copy, ENV_ACTUAL_SITE_COPY, "on an isolated copy of the real site")
    if not developer_reviewed_bridge:
        unmet.append("The developer has not reviewed the exact bridge file (by commit).")
    if not owner_authorized_pilot:
        unmet.append("The owner has not authorized a production pilot naming the site and the books.")
    return unmet


def check_results(items: Sequence[Mapping[str, Any]]) -> list[CheckResult]:
    """CheckResults from a report's plain list ({"id", "status", "evidence"}),
    e.g. tools/site_blackbox/site_blackbox.py or the Gate A lab pilot."""
    return [CheckResult(str(item["id"]), str(item["status"]), str(item.get("evidence") or "")) for item in items]


def existing_product_workflow_requirements(
    *, gate_b: GateResult | None, sync_authority_decided: bool, owner_decided_scope: bool
) -> list[str]:
    """What the EXISTING_PUBLISHED_PRODUCT workflow still needs before any
    automation may touch a published book's translations, price or stock."""
    unmet: list[str] = []
    if gate_b is None or gate_b.gate != GATE_B or gate_b.environment != ENV_ACTUAL_SITE_COPY:
        unmet.append("Gate B has not been observed on an isolated copy of the real site.")
    elif gate_b.status != GATE_OBSERVED_PENDING_DECISION:
        unmet.append(f"Gate B is {gate_b.status}: {'; '.join(gate_b.reasons) or 'see report'}.")
    elif not sync_authority_decided:
        unmet.append("Gate B observations await a written owner and developer decision on price and stock authority.")
    if not owner_decided_scope:
        unmet.append("The owner has not decided whether automation may add EN/DE to already published books.")
    return unmet
