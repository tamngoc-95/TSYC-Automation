# Proposal — Standing WooCommerce Draft Authorization for `FAHASA-` Candidates

Status: **PROPOSED — not in effect.** Requires explicit shop-owner adoption
as a CLAUDE.md amendment (the same way section 6.2 was adopted on 2026-09-04).
Until then every `FAHASA-` batch keeps the section 6/6.1 bounded human gate.

## Why FAHASA drafts ask for approval every batch

| | `FB-HIST-` | `FAHASA-` |
|---|---|---|
| Policy | CLAUDE.md 6.2 / 16.1 / 17 exception | Live pipeline: 6 / 6.1 / 17 (6.2 says non-`FB-HIST-` prefixes are "completely unaffected") |
| Derived state at `READY_FOR_DRAFT` | `READY_FOR_DRAFT_HISTORICAL`, `human_gate=False` | `READY_FOR_DRAFT`, `human_gate=True` |
| Code | `scripts/pipeline_state.py` `is_historical_candidate_code()` branch | same function, unconditional fallthrough |
| Dispatch | `AUTOMATABLE_DISPATCH["READY_FOR_DRAFT_HISTORICAL"]` | only `WOO_DRAFT_DISPATCH` when `--allow-woo-draft` is passed (`run_batch.decide_action`) |
| Image rights | 14.7 | 14.8 (images only; 14.8.5 keeps "WooCommerce draft-authorization … rules in effect") |

The owner's Fahasa authorization (14.8, 2026-10-17) covers **images**, not
draft creation. So the repeated question is the system working as written,
not a bug. The `--allow-woo-draft` flag lives only for one process. When a
run is interrupted (for example the 2026-11-21 Supabase TLS timeout, which
also aborted the batch before any write), resuming needs that bounded
authorization stated again.

## Proposed CLAUDE.md section 6.3 (for owner review)

> For a `FAHASA-` candidate, WooCommerce DRAFT creation (`status="draft"`,
> never `"publish"`, no price field, stock 0, backorders allowed) is
> pre-authorized once the candidate reaches `READY_FOR_DRAFT` under the
> **full** live readiness gate (section 16: `IDENTITY_VERIFIED`, APPROVED
> VI/EN/DE content, one VALIDATED publishable main image, no recovery, no
> existing sync) **and** the mandatory catalog-identity check returns
> `NEW_CONFIRMED`. No identity, content, image, duplicate, or owner-removal
> threshold is relaxed (unlike 6.2). Publishing, prices, owner-maintained
> products, `REMOTE_REMOVED` products and the no-blind-retry rule are
> unchanged.

## Implementation once adopted (small, mirrors 6.2)

1. `pipeline_state.py`: `READY_FOR_DRAFT` + `is_fahasa_candidate_code()` →
   `READY_FOR_DRAFT_FAHASA`, `human_gate=False`. The live readiness gate
   itself is unchanged.
2. `run_batch.py`: map `READY_FOR_DRAFT_FAHASA` to the same
   `create_woocommerce_draft.py` arguments as `READY_FOR_DRAFT_HISTORICAL`.
3. Tests: a FAHASA candidate auto-dispatches without `--allow-woo-draft`.
   `FB-2026-` and any other prefix still stops. Recovery, an existing sync,
   and non-`NEW_CONFIRMED` catalog checks still block. The payload stays
   draft-only with no price.
4. Bounded-batch rules (19.1 allowlist and `--max-candidates`) still apply.
