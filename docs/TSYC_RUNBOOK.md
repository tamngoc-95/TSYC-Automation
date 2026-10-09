# TSYC Production Runbook

## Standard Python runtime
PowerShell: `.\.venv\Scripts\python.exe`
Claude Code Bash: `.venv/Scripts/python.exe`
Never use system Python when `.venv` exists.

## 1. Normal production flow
1. Run preflight.
2. Start a bounded batch (explicit candidate allowlist + `--max-candidates`).
3. Deterministic stages auto-progress to `READY_FOR_DRAFT`.
4. Review any `REVIEW_REQUIRED` / `BLOCKED` / `RECOVERY_REQUIRED` candidates from the grouped result.
5. Resume a reviewed candidate the same way (re-run the batch command for its code).
6. Give one bounded Woo draft approval for the candidates shown as `READY_FOR_DRAFT`.
7. Woo drafts are created serially, reconciled, and audited automatically.
8. Stop. Publishing and final selling price are never automatic.

## 2. Preflight
```
.venv/Scripts/python.exe scripts/preflight_pipeline.py
```
Read-only. Checks Python environment, configuration presence, Supabase read
connectivity, domain/schema drift, the decision engine, the deterministic
pipeline audit, recovery health, WooCommerce read connectivity, orchestrator
safety config, and Git state. Ends with `READY_FOR_BATCH` (exit 0) or
`NOT_READY_FOR_BATCH` (exit 1). `run_batch.py` also runs this in-process
before any non-dry-run batch and refuses to start writes on a blocker.

## 3. Start bounded batch
```
.venv/Scripts/python.exe scripts/run_batch.py ^
  --candidate-code CAN-0012 --candidate-code CAN-0013 --candidate-code CAN-0014 ^
  --max-candidates 5 ^
  --non-interactive
```
`--candidate-codes CAN-0012,CAN-0013,CAN-0014` (comma-separated) works the
same way. No implicit "all"/"newest" mode exists -- the allowlist is always
explicit and bounded by `--max-candidates`. Add `--dry-run` for a read-only
diagnostic pass (skips preflight and never invokes a writer). Add `--verbose`
for the full per-stage trace; the default output is the grouped result below.

## 4. Exception review
The default output groups every candidate into exactly one bucket:
`READY_FOR_DRAFT`, `REVIEW_REQUIRED`, `BLOCKED`, `AUTO_REJECTED`,
`DRAFT_CREATED`, `RECOVERY_REQUIRED`. Each `REVIEW_REQUIRED`/`BLOCKED`
candidate prints its candidate code, derived state, and short reason --
nothing else needs to be read to decide what happened.

## 5. Resume a reviewed candidate
Fix the underlying condition with the normal stage script (e.g. register a
reference source, select/approve an image, resolve an identity conflict),
then re-run `run_batch.py` with the same candidate code -- it re-derives
state and continues from wherever it now stands.

## 6. Woo draft approval
When one or more candidates reach `READY_FOR_DRAFT`, the batch stops and
prints exactly those candidate codes plus one bounded approval request. Add
`--allow-woo-draft` and re-run the same command to authorize exactly that
set:
```
.venv/Scripts/python.exe scripts/run_batch.py ^
  --candidate-code CAN-0012 --candidate-code CAN-0013 ^
  --max-candidates 5 ^
  --non-interactive --allow-woo-draft
```
Never one approval per candidate. Authorization never extends to a
candidate outside the exact codes passed on that invocation.

## 7. Woo reconciliation
`DRAFT_CREATED` candidates automatically reconcile against the remote
product (SKU, status, media) on the next batch pass -- read-only against
WooCommerce, never a duplicate create. An uncertain remote result stops that
candidate as `RECOVERY_REQUIRED`; independent candidates continue.

## 8. Recovery
`RECOVERY_REQUIRED` means remote state could not be confirmed. Never retry
draft creation blindly. Reconcile manually with
`sync_woocommerce_product_status.py`, confirm the real remote state, then
resume the candidate.

## 9. Final audit
```
.venv/Scripts/python.exe scripts/audit_pipeline_state.py
```
Expect `PASS` or `PASS_WITH_WARNINGS` (accepted: `ISBN_MISSING`,
`WEIGHT_MISSING`). Any `ERROR` stops further batch work until investigated.

## 10. Never publish / never auto-price
WooCommerce products are always created `status = draft`, with no
`regular_price`/`sale_price`/`price` field. Publishing and selling-price
entry are shop-owner decisions made later, outside this automation, and are
never performed by any script in this repository.

## 11. Fahasa new-book discovery (read-only)
```
.venv/Scripts/python.exe scripts/discover_fahasa_new_books.py --max-listings 30
.venv/Scripts/python.exe scripts/discover_fahasa_new_books.py --max-listings 30 --reclassify data/output/fahasa_discovery/<report>.json
```
Reads one newest-first Fahasa listing (default: children's books), parses
each product page with the shared Fahasa parser, and classifies every book
against the unified identity index (all Woo statuses incl. trash, Woo sync
records incl. owner-removed products, internal products, candidates, MATCH
references, the Facebook history export): `NEW_CONFIRMED`,
`EXISTING_PRODUCT`, `POSSIBLE_DUPLICATE`, `DISTINCT_EDITION`,
`FACEBOOK_COVERAGE_UNKNOWN`, `REMOTE_REMOVED`. A new volume of a series the
shop already sells is `POSSIBLE_DUPLICATE` (sellable-unit decision).
Writes only `data/output/fahasa_discovery/discovery_<UTC>.json` and the
first-seen ledger; never Supabase, Woo, images or prices. Fahasa images are
recorded `RIGHTS_UNKNOWN` (the 14.7 auto-authorization is FB-HIST only).
`--reclassify` re-applies the current rules to an earlier report without
contacting Fahasa. A live run takes ~20 s per book; run it in the background.

## Git discipline
`git status`, `git diff --check`, review the exact diff, run relevant tests
before every commit.
