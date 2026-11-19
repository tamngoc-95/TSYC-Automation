# Pre-order first: VI/EN/DE drafts for new books

Status 2026-11-16. This file separates the new pre-order workflow from the
existing-product workflow, and splits the old "Stage D" into two gates so the first
no longer waits for everything the second needs. The bridge itself is described in
`POLYLANG_BRIDGE.md`; nothing there is replaced.

**Nothing here is enabled on production.** No bridge is installed there, no EN/DE
product, link or mapping row has been written, and no price or stock value has been
touched.

## 1. Two workflows

Decided per logical book by `src/domain/product_workflow.py` from TSYC's records and
one remote read. `scripts/plan_translation_drafts.py` (read-only) reports it for every
recorded book.

| | `NEW_PREORDER_PRODUCT` | `EXISTING_PUBLISHED_PRODUCT` |
|---|---|---|
| The VI product is | TSYC's own unpriced draft of a Fahasa pre-order book | published, private, pending or scheduled |
| Automation may | create the EN and DE drafts and link them, where a bridge is allowed | create, link and edit **nothing** |
| The owner | reviews three drafts, prices and publishes them | keeps full control, as today |
| Gate needed | Gate A | Gate B, plus an owner decision |

A book is eligible for the pre-order workflow only when all of these hold:

- its VI mapping row is `RECORDED` and the product was read as a `draft`;
- its candidate is a Fahasa discovery still `PREORDER_PENDING_SUPPLY_CONFIRMATION`;
- the draft carries TSYC's internal product ID and exact SKU;
- no price, and the pre-order inventory shape (managed stock 0, backorders yes);
- VI, EN and DE content are all `APPROVED` and final;
- the mandatory catalog identity check recorded `NEW_CONFIRMED` for this product code. A missing check is a blocker, never "no conflict".

Anything else is not eligible: an owner-removed product (`OWNER_REMOVED_IDENTITY`,
terminal, never recreated), a failed read, a draft that is not a pre-order, an
owner-priced draft. "Eligible" describes the records only. It never authorizes a write.

**Read-only production result, 2026-11-16:** 84 recorded books, all 84
`EXISTING_PUBLISHED_PRODUCT`, 0 `NEW_PREORDER_PRODUCT`. 64 have approved VI, EN and DE
content; 20 lack approved EN/DE. So the pre-order workflow serves **future** Fahasa
drafts, and it has to run between the VI draft's creation and the owner publishing it.

The intended order for a new book: discover, verify identity, write and approve VI,
EN and DE content, create the VI draft (owner-authorized as today), create the EN and
DE drafts from it, read everything back, stop. All three stay drafts until the owner
reviews them.

## 2. Two gates

Defined in `src/domain/integration_gates.py`. The old Stage D mixed them, which made
safe draft creation wait for the store's price and stock code.

**Gate A, `PREORDER_DRAFT_CREATION_SAFETY`.** Creating three linked, unpriced
pre-order drafts changes nothing else.

| Check | What must hold |
|---|---|
| A01 | A genuinely new synthetic logical book is selected |
| A02 | Its approved VI/EN/DE content loads through the production loader and renderer |
| A03 | One canonical VI draft is created |
| A04 | EN and DE drafts are created through the native Polylang bridge |
| A05 | The Polylang group reads back identically from every member |
| A06 | The three IDs are one sellable book |
| A07 | Draft, managed stock 0, backorders yes, on all three |
| A08 | No price field on any of the three |
| A09 | Each language's stored text equals its approved package |
| A10 | Images, contributors and metadata are right on every copy |
| A11 | No other product was created, changed or removed (whole-catalog snapshot) |
| A12 | A repeated run sends nothing and creates no duplicate |
| A13 | Interruption recovery and rollback leave no duplicate and no stray change |
| A14 | The mandatory catalog identity check: exact book and edition, no Woo or Facebook duplicate, no owner-removed match |
| A15 | Creating and linking EN/DE changed nothing on the VI draft: price, stock, backorders, SKU, images, status, owner fields |

In the lab all 15 are required. On a copy of the real site the required subset is A05, A06, A07, A08,
A10, A11 and A15 (`GATE_A_SITE_CHECKS`): what before/after snapshots can show. The others are properties
of TSYC's own code and records. A failed check outside the subset still fails the gate.

**Gate B, `LIVE_COMMERCE_SYNCHRONIZATION_SAFETY`.** How the real site moves price,
stock and backorders between the copies, and how orders behave: B01 price sync, B02
stock, B03 backorders, B04 orders per language, B05 concurrent orders, B06 source of
truth, B07 an owner-entered price is kept.

Rules the code enforces:

- **Gate A passing is never Gate B passing.**
- A Gate A result names where it was measured: `GENERIC_LOCAL_LAB` (plain WordPress,
  WooCommerce, Polylang) or `ACTUAL_SITE_COPY` (an isolated copy running the real
  customization). A lab pass is not a site pass.
- Gate B is `BLOCKED` anywhere the real customization is absent, whatever was
  measured. On a real copy its checks are `OBSERVED`, never passed by a machine: which
  sync direction is right is the owner's and developer's decision.
- Nothing in that module enables a write.

### 2.1 Owner decision, 2026-11-16: Gate B and a draft-only pilot

Gate B is **not required before an unpriced, unpublished, draft-only multilingual pilot**. This is a
scope decision. It is not an authorization to create or change production products, Gate B is
**`DEFERRED`, not passed**, and Gate A stays mandatory in the lab and on a copy of the real site.

The deferral holds only for a pilot that meets every one of these (`DraftOnlyPilotScope`):

1. It creates new VI/EN/DE drafts and nothing else.
2. It leaves all prices empty.
3. It publishes nothing.
4. It accepts no actual customer orders.
5. It modifies no existing published product.
6. It updates no existing owner-maintained product.
7. It does not use production inventory synchronization as a tested capability.

If any one is missing, `defer_gate_b` keeps Gate B at its real status and
`production_pilot_requirements` lists Gate B as required again. With no scope stated, Gate B is
required. A Gate B that failed on the real site is never turned into `DEFERRED`.

## 3. Gate A in the local lab

`tools/wp_lab/run_preorder_gate_a.py`. One synthetic new book goes through the
production code path: `build_woocommerce_payload(preorder=True)` for the VI draft,
`translation_book_input` and `render_content` for the content, the runner, the bridge,
then `preorder_trio_verification.verify_trio` on the read-back. Bystanders that must
come out untouched: an owner's published, priced trio, another book's TSYC draft, and
the owner's own live and trashed books used by the identity check.
The whole catalog is snapshotted before and after with the same script the developer
gets (section 4).

```
.venv/Scripts/python.exe tools/wp_lab/run_preorder_gate_a.py --lab-dir %LOCALAPPDATA%\TSYC\lab-m1107
```

Result, bridge 1.2.0 at production's versions (WordPress 7.1.4, WooCommerce 11.2.1,
Polylang 3.9.1):

| Database | Gate A | Gate B | Report (gitignored) |
|---|---|---|---|
| MariaDB 11.4.5 | **`PASS`, 15 of 15, `GENERIC_LOCAL_LAB`** | `BLOCKED`; `DEFERRED` for a draft-only pilot | `preorder_gate_a/20261116T123233Z` |
| SQLite | **`PASS`, 15 of 15, `GENERIC_LOCAL_LAB`** | `BLOCKED`; `DEFERRED` for a draft-only pilot | `preorder_gate_a/20261116T123541Z` |

Gate A on the real site: **`BLOCKED_REAL_SITE_TEST`**. No isolated copy has been tested.

The first 13 checks passed that morning (`20261116T052713Z`, `20261116T052923Z`); A14 and A15 were added
and all 15 were run again in the afternoon. Both runs: 0 host suspensions, clean shutdown.

Evidence from the MariaDB run:

- A07, A08: all three are `draft`, managed stock 0, backorders `yes`, `onbackorder`, no regular, sale or current price, not purchasable.
- A10: both copies carry the VI draft's two images. Attribute values are unchanged, labels are localized ("Author", "Verlag").
- A11: 6 products before, exactly the 3 new IDs added, 0 removed, 0 existing products changed.
- A15: the VI draft compared field by field (27 fields) between "VI created" and "EN and DE linked": only its two new translation links differ, and it was not saved again.
- A14, through the production catalog reader (every status, trash included). The new book is `NEW_CONFIRMED`. Every other case is refused and makes the workflow ineligible:

  | Case | Result |
  |---|---|
  | Same title and author as a live owner product | `EXISTING_PRODUCT` |
  | The same with a reprint suffix "(Tái bản 2026)" | `EXISTING_PRODUCT` (a reprint is treated as the existing book, not as a new one) |
  | Same as a product in the owner's trash | `REMOTE_REMOVED` |
  | The same, author unknown | `POSSIBLE_DUPLICATE` (a person decides) |
  | Owner-removed, Woo ID gone, known only from TSYC's sync record | `REMOTE_REMOVED` |
  | A Facebook-derived candidate of the same book | `EXISTING_PRODUCT` |
  | The book's own SKU, after its VI draft exists | `EXISTING_PRODUCT` |
  | The catalog read failed (wrong credentials) | `CATALOG_UNAVAILABLE`, never "no conflict" |
  | No usable title | `IDENTITY_CONFLICT` |
- A12: the repeat sent 0 requests. With the checkpoint deliberately lost, the adapter refused both languages instead of creating again. A second VI create with the same SKU was refused by WooCommerce (HTTP 400).
- A13: a crash after the EN insert, then a new process: EN reconciled, DE created, 3 products claim the book. Rollback of DE and EN: both trashed and unlinked, replays refused, the VI draft and the first book unchanged.

**Defect found by A13 and fixed.** After a crash that left EN half-made, the runner
still sent DE in the same run. The half-made EN draft was not linked yet, so the group
read could not show it. On resume, EN finished, and DE's own snapshot no longer matched
the group, so it stopped as `tsyc_sibling_side_effect` and the whole book needed a
person. Nothing was corrupted: the guard worked, but on TSYC's own write. The runner now
handles one language at a time: a pending language is reconciled first, and the other
sends nothing until then (`WAITING_FOR_UNRECONCILED_LANGUAGE`). The guard was not
loosened. Regression tests are in `tests/test_polylang_bridge.py`.

Regression after the runner change, MariaDB lab, same versions: base pilot 25 `PASS_REAL_LOCAL`, 2
`BLOCKED_MISSING_DEPENDENCY`, 0 `FAIL` (`polylang_local/20261116T050738Z`); hardening pilot 13 `PASS_REAL_LOCAL`,
8 `PASS_SYNTHETIC_HOOK`, 6 `BLOCKED_MISSING_SOURCE`, 0 `FAIL` (`polylang_hardening/20261116T051629Z`).

The same two pilots again in the afternoon, after the Gate A, diagnostics and black-box changes, same
versions: base 25 `PASS_REAL_LOCAL`, 2 `BLOCKED_MISSING_DEPENDENCY`, 0 `FAIL` (`polylang_local/20261116T143525Z`);
hardening 13 `PASS_REAL_LOCAL`, 8 `PASS_SYNTHETIC_HOOK`, 6 `BLOCKED_MISSING_SOURCE`, 0 `FAIL`
(`polylang_hardening/20261116T144350Z`). Both: 0 host suspensions, clean shutdown.

**Re-run 2026-11-19, unchanged code, strictly one pilot at a time:**

| Run | Database | Result | Seconds | Report (gitignored) |
|---|---|---|---|---|
| Gate A | MariaDB 11.4.5 | `PASS` 15 of 15, X01/X02 `PASS` | 128 | `preorder_gate_a/20261119T051732Z` |
| Gate A | SQLite | `PASS` 15 of 15, X01/X02 `PASS` | 109 | `preorder_gate_a/20261119T075902Z` |
| Base pilot | MariaDB | 25 `PASS_REAL_LOCAL`, 2 `BLOCKED_MISSING_DEPENDENCY`, 0 `FAIL` | 334 | `polylang_local/20261119T080159Z` |
| Hardening pilot | MariaDB | 13 `PASS_REAL_LOCAL`, 8 `PASS_SYNTHETIC_HOOK`, 6 `BLOCKED_MISSING_SOURCE`, 0 `FAIL` | 426 | `polylang_hardening/20261119T083115Z` |

All four: 0 host suspensions, 0 `INVALID_HOST_SUSPENDED` checks, no diagnostic capture needed (no
failure), no lab process left running. No stall: the slowest test was H09 at 84 s (usual about 90 s).
The hardening run's event log shows a 3 s Modern Standby (08:37:37Z to 08:37:40Z), well under the 20 s
freeze threshold; the process kept running and no test was affected. Gate B stays `BLOCKED` in the lab
and `DEFERRED` (not passed) for a draft-only pilot; Gate A on the real site stays `BLOCKED_REAL_SITE_TEST`.

**Defect found in review and fixed (2026-11-19).** `site_blackbox.py evaluate` exited `0` for a run that
was `BLOCKED_REAL_SITE_TEST` without any failed check (a missing step, or a trio made by hand), the same
code as `PASS`. A script reading the exit code would have taken it as a pass. `evaluate` now follows the
`run-gate-a` rule: only `PASS` is `0`, `FAIL` is `1`, anything blocked is `4`. Regression test in
`tests/test_site_blackbox.py`. The lab self-test (X01, X02) uses `run-gate-a`, so its results stand.

Read-only checks the same day: pipeline audit `PASS_WITH_WARNINGS`, preflight `READY_FOR_BATCH`
(Git `DIRTY` before this commit), language mapping audit identical to 2026-11-14/15/16 (2
`COPIED_OWNERSHIP_META` errors on #4829 and #4858, both owner-gated and never adopted automatically; no
new finding).

### 3.1 The two lab stalls: explained

H09 on 2026-11-07 (704 s instead of about 90 s) and H11 on 2026-11-15 (2722 s instead of about 25 s) were
recorded as unexplained. The Windows System event log explains both: **the laptop entered Modern Standby
inside exactly those tests.**

| Run | Test window (UTC) | Standby in the event log (UTC) | Standby length | Test duration |
|---|---|---|---|---|
| 2026-11-07, SQLite, H09 | about 17:50:50 to 18:02:35 | 17:52:05 to 18:02:37 (Kernel-Power 506, 507) | 632 s | 704 s |
| 2026-11-15, MariaDB, H11 | about 07:25:10 to 08:10:30 | 07:25:24 to 08:10:44 | 2720 s | 2722 s |

The H09 error text itself says so: the subprocess "timed out after **-434** seconds". The process had
finished; the deadline had simply passed while the machine was frozen. Nothing in WordPress, WooCommerce,
Polylang, the bridge or either database hung, and the database engine was never the cause.

What a pilot run does now (`tools/wp_lab/lab_diagnostics.py`, used by all three pilots):

- Asks Windows to stay awake for the run. This stops the idle timer. It cannot stop a closed lid or the power button.
- Watches for a frozen clock. A test that fails while overlapping a suspension is `INVALID_HOST_SUSPENDED`: not a pass, not a product failure, the run exits non-zero, and the test must be run again.
- Keeps the PHP server's per-request log and the MariaDB error log for each run under `<run>/lab_logs/`.
- On any failed test writes `diagnostics_<test>_<time>.json`: lab processes with CPU time and memory, machine memory and load, database connections, open transactions, lock waits and bridge lock rows (or the SQLite journal files), the PHP errors and request log since the test began, and the power events of that window.
- After shutdown checks that no lab process is left running. A leftover fails the run.

Limits: this explains the two recorded stalls. A hang with another cause would now be captured, not
prevented. No such hang has been seen.

What this pass does not say: anything about the store's theme, its bulk-link plugins,
or Gate B.

## 4. Testing the real site without its source

`tools/site_blackbox/` is a package the developer runs on an isolated copy. It needs
no source code, no TSYC repository and no network. `README.md` there is written for the
developer.

| File | Role |
|---|---|
| `tsyc-site-snapshot.php` | Read-only snapshot of every product: status, prices, stock, backorders, SKU, images, language, translation links, related product IDs, and hashes of the text; plus the theme, plugin and must-use plugin names and versions |
| `site_blackbox.py` | The one-command Gate A run, and the step-by-step comparison. Standard library only; it starts nothing but the WP-CLI command it is given |
| `tsyc-bridge-trio-driver.php` | Creates one synthetic trio through the bridge on the copy, in one step or two. Refuses the production host and any environment other than `local` or `staging` |

**Gate A on the copy is one command** for the developer:

```
python site_blackbox.py run-gate-a run1 --user ADMIN_LOGIN --wp "wp --path=/path/to/copy"
```

It snapshots the catalog, creates the VI draft, snapshots, creates EN and DE through the bridge, waits two
minutes, snapshots, moves the test products to the trash, and judges. It refuses as soon as the first
snapshot names the production host, never reuses a folder, and never retries a bridge refusal: a
`tsyc_sibling_side_effect` stop is the finding.

The report answers "did creating and saving the EN/DE drafts change anything?" group by group, for the
VI draft and for every other product in the catalog:

| Group | Fields compared |
|---|---|
| price | regular, sale and current price |
| stock | stock management, quantity, stock status, total sales |
| backorders | backorder setting |
| sku | SKU |
| images | main image, gallery |
| status | status, product type |
| translation_links | language, the full translation map |
| owner_maintained_fields | title, slug, description and short description (hashes), categories, attributes, up-sells, cross-sells, weight |
| saved_again | modified time: a save that changed no field still shows |

Overall result `real_site_compatibility`: `PASS`, `FAIL`, or **`BLOCKED_REAL_SITE_TEST`** whenever there
is no complete, bridge-made run. A trio made by hand with "+" is `BLOCKED_REAL_SITE_TEST` with what was
observed, because it does not test the bridge.

The price, stock and order steps are still there for Gate B (B01 to B07): what each other copy did after
one was priced, stocked or ordered. `OBSERVED`, or `BLOCKED` for a step that was not run. They are not
needed for a draft-only pilot.

A report only counts as the real site if the copy carries the same relevant customizations. The snapshot
records the theme and plugin versions for that comparison; comparing them with production automatically
is not built yet, so today a person checks them.

Snapshots from `tiemsachyeucon.com` are refused. The package, including the one-command
run, was run end to end on the lab stack (checks X01, X02 of the Gate A pilot): every group
`UNCHANGED` for the VI draft and for all 12 other products. That is the lab, not the store: its
Gate B lines are only the native baseline, where plain WooCommerce and Polylang propagate nothing.

**What it cannot see:** why the site behaves as it does, changes outside the product
tables it reads, and anything that happens later than the snapshot. The two-minute wait
before each snapshot covers ordinary delayed jobs, not all of them.

## 5. What stands before a production pilot

`integration_gates.production_pilot_requirements` lists what is unmet. Today:

1. Gate A in the local lab: **met**, 15 of 15 (section 3).
2. Gate A on an isolated copy of the real site, with the trio created by the bridge: **`BLOCKED_REAL_SITE_TEST`**. Needs the developer, or another separately approved safe environment carrying the same relevant customizations.
3. The developer's review of the exact bridge file, by commit: **not done**.
4. The owner's authorization naming the site and the books: **not given**. The Gate B scope decision is not this authorization.
5. The pilot stays inside the draft-only scope of section 2.1. Then Gate B is `DEFERRED`.

Gate B is not on this list by the owner's decision of 2026-11-16 (section 2.1). The reasoning: a draft
without a price cannot be bought, the pre-order workflow never writes a price or stock value, and whatever
the site does once the owner prices and publishes a trio is what it already does for the trios the owner
makes by hand. What Gate A on the real copy must still show is that the link itself moves nothing (A11, A15).

Gate B stays required before automation touches any published product, any price or any stock value,
before real orders are taken on automation-made copies as a tested capability, and before anyone states
that the site's sync is safe.

Minimum for the first bounded production pilot, once 1 to 5 are met:

- The bridge installed on production by the developer, with its write routes enabled under a separate written authorization (today both the bridge and the Python writer refuse the production host; changing that is a reviewed code change, not a setting).
- An exact allowlist of at most one or two new Fahasa pre-order books, each `NEW_PREORDER_PRODUCT` and eligible, with a `NEW_CONFIRMED` identity check.
- One book, one language at a time.
- A read-only production snapshot before and after, compared group by group as in section 4.
- Any `tsyc_sibling_side_effect`, uncertain answer or snapshot difference stops the pilot for a person. Nothing is retried or repaired.
- No price, no publish, no stock value, no write to any existing product.

## 6. Remaining risk of cross-language price or stock corruption

| Risk | State |
|---|---|
| A theme hook moves price, stock or backorders between copies when they are linked | Not excluded for the real site. The bridge detects it and stops after the fact; it does not prevent or repair. Closed only by Gate A on a real copy |
| The site's sync after the owner prices or publishes a trio | Unknown. Same behaviour as for the owner's hand-made trios. Gate B |
| Stock per language (live groups already drift, some at -1; `POLYLANG_EXISTING_ARCHITECTURE.md`) | Existing condition, not caused by TSYC. Gate B describes it |
| Writes after the request: scheduled jobs, `shutdown` work | Not seen by the bridge's guard. The black-box snapshots catch what lands before the snapshot |
| Automation overwriting an owner-entered price | Excluded by construction: no TSYC payload can carry a price field, in any workflow |
| Automation touching a published product | Excluded by construction: the workflow, the adapter and the bridge each refuse it |

## 7. Files

| File | Role |
|---|---|
| `src/domain/product_workflow.py` | The two workflows |
| `src/domain/integration_gates.py` | Gate A, Gate B, pilot requirements |
| `src/domain/preorder_trio_verification.py` | Read-back checks A05 to A10 |
| `src/services/translation_draft_runner.py` | One language at a time |
| `tools/wp_lab/run_preorder_gate_a.py` | Gate A in the lab |
| `tools/wp_lab/lab_diagnostics.py` | Stay-awake request, suspension detection, per-run logs, failure captures, cleanup check |
| `tools/site_blackbox/` | The developer's package |
| `tests/test_preorder_workflow_gates.py`, `tests/test_site_blackbox.py`, `tests/test_lab_diagnostics.py` | Offline tests |
