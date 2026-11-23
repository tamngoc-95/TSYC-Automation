# CLAUDE.md §6.3 — Standing WooCommerce Draft Authorization for Fahasa Pre-orders

Status: **ADOPTED 2026-11-23** by explicit shop-owner decision. It is
implemented in code (`scripts/pipeline_state.py`, `scripts/run_batch.py`,
`scripts/preflight_pipeline.py`). CLAUDE.md is permission-protected for
automation, so **the owner pastes the text below into CLAUDE.md by hand**.
Until then this file and the code are the record of the decision.

## Behavior before and after

| | Before (≤ 2026-11-23) | After (§6.3) |
|---|---|---|
| Eligible `FAHASA-` at `READY_FOR_DRAFT` | `READY_FOR_DRAFT`, `human_gate=True`; created only with per-run `--allow-woo-draft` | `READY_FOR_DRAFT_FAHASA`, `human_gate=False`; dispatched automatically |
| `FAHASA-` that is not pre-order, not `IDENTITY_VERIFIED`, or fails full readiness | human gate | human gate (unchanged) |
| `FB-2026-` and any other prefix | human gate | human gate (unchanged) |
| `FB-HIST-` | `READY_FOR_DRAFT_HISTORICAL` (§6.2) | unchanged |

Eligibility (`pipeline_state._fahasa_standing_draft_eligible`) requires all of:
- a `FAHASA-` candidate code;
- `source_evidence.origin = FAHASA_DISCOVERY` and `supply_status = PREORDER_PENDING_SUPPLY_CONFIRMATION`;
- `IDENTITY_VERIFIED`;
- `evaluate_readiness` (full live gate) returns `AUTO_PASS`.

These are still checked first and still stop the candidate:
- owner removal (`REMOTE_REMOVED`, terminal);
- recovery states;
- a catalog-duplicate marker;
- an existing sync.

The creator then re-reads every gate, re-checks the SKU (any + trash),
requires catalog identity `NEW_CONFIRMED` and sends `status="draft"`,
`manage_stock=true`, `stock_quantity=0`, `backorders="yes"`, and no price
field. An uncertain create is marked `recovery_required` and is never
retried.

## Exact CLAUDE.md text for the owner to apply

**1. In section 5.1, after the `FB-HIST-` bullet line, add:**

```
WooCommerce DRAFT creation for an eligible new Fahasa pre-order (`FAHASA-`) candidate that passed the full live readiness gate (section 6.3).
```

**2. In section 6.2, replace the sentence**
`Live-pipeline candidates (`FB-2026-...` and any future non-`FB-HIST-` prefix) are completely unaffected and keep every rule in 6/6.1 unchanged.`
**with:**

```
Live-pipeline candidates (`FB-2026-...` and any future non-`FB-HIST-` prefix) are unaffected by this section; `FAHASA-` pre-order candidates are covered only by section 6.3.
```

**3. Insert a new section after section 6.2 (before `7. Hard Deny Rules`):**

```
---

6.3 Standing Fahasa pre-order draft authorization (explicit shop-owner business authorization)
Adopted 2026-11-23, explicit business authorization from the shop owner, scoped ONLY to new Fahasa-discovered pre-order candidates: candidate_code prefixed `FAHASA-`, source_evidence.origin = FAHASA_DISCOVERY and supply_status = PREORDER_PENDING_SUPPLY_CONFIRMATION (`src.domain.rules.preorder_inventory_rules.is_fahasa_preorder_candidate`).
For such a candidate, WooCommerce DRAFT creation (`status="draft"`, never `"publish"`, no price field, manage_stock=true, stock_quantity=0, backorders="yes") is pre-authorized once the candidate is IDENTITY_VERIFIED and reaches READY_FOR_DRAFT under the FULL live readiness gate (section 16, `evaluate_readiness`), and the mandatory catalog-identity check returns NEW_CONFIRMED immediately before the POST. It does NOT require the per-batch human authorization in 6/6.1; `scripts/run_batch.py` dispatches it without `--allow-woo-draft` (derived state `READY_FOR_DRAFT_FAHASA`, `human_gate=False`). Exact allowlist and --max-candidates (19.1) still apply.
Unlike 6.2, NO threshold is relaxed: identity, content (VI/EN/DE approved), image, image-rights, duplicate/catalog-identity, owner-removal (REMOTE_REMOVED never recreated), recovery and existing-sync gates apply in full. A FAHASA- candidate failing any eligibility condition keeps the 6/6.1 human gate.
This authorization never touches: WooCommerce publish (2.4/7), selling-price automation (2.5/7), existing or owner-maintained products (14.8.4), or the no-blind-retry rule (2.6/18/19.3).
```

**4. In section 17, after the `FB-HIST-` exception paragraph, add:**

```
Exception: for an eligible Fahasa pre-order (`FAHASA-`) candidate under section 6.3, the standing shop-owner authorization is the authorization -- no per-batch confirmation is requested. Every other requirement on this page applies identically.
```

**5. In section 7, in the closing Note, after "(section 6.2)", add:** `and the standing Fahasa pre-order draft authorization (section 6.3)`.
