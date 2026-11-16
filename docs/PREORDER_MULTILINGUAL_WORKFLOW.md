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

## 3. Gate A in the local lab

`tools/wp_lab/run_preorder_gate_a.py`. One synthetic new book goes through the
production code path: `build_woocommerce_payload(preorder=True)` for the VI draft,
`translation_book_input` and `render_content` for the content, the runner, the bridge,
then `preorder_trio_verification.verify_trio` on the read-back. Bystanders that must
come out untouched: an owner's published, priced trio and another book's TSYC draft.
The whole catalog is snapshotted before and after with the same script the developer
gets (section 4).

```
.venv/Scripts/python.exe tools/wp_lab/run_preorder_gate_a.py --lab-dir %LOCALAPPDATA%\TSYC\lab-m1107
```

Result, bridge 1.2.0 at production's versions (WordPress 7.1.4, WooCommerce 11.2.1,
Polylang 3.9.1):

| Database | Gate A | Gate B | Report (gitignored) |
|---|---|---|---|
| MariaDB 11.4.5 | **`PASS`, 13 of 13, `GENERIC_LOCAL_LAB`** | `BLOCKED` | `preorder_gate_a/20261116T052713Z` |
| SQLite | **`PASS`, 13 of 13, `GENERIC_LOCAL_LAB`** | `BLOCKED` | `preorder_gate_a/20261116T052923Z` |

Evidence from the MariaDB run:

- A07, A08: all three are `draft`, managed stock 0, backorders `yes`, `onbackorder`, no regular, sale or current price, not purchasable.
- A10: both copies carry the VI draft's two images. Attribute values are unchanged, labels are localized ("Author", "Verlag").
- A11: 4 products before, exactly the 3 new IDs added, 0 removed, 0 existing products changed.
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
8 `PASS_SYNTHETIC_HOOK`, 6 `BLOCKED_MISSING_SOURCE`, 0 `FAIL` (`polylang_hardening/20261116T051629Z`). Neither of
the two earlier unexplained lab stalls recurred; they remain unexplained.

What this pass does not say: anything about the store's theme, its bulk-link plugins,
or Gate B.

## 4. Testing the real site without its source

`tools/site_blackbox/` is a package the developer runs on an isolated copy. It needs
no source code, no TSYC repository and no network. `README.md` there is written for the
developer.

| File | Role |
|---|---|
| `tsyc-site-snapshot.php` | Read-only snapshot of every product: status, prices, stock, backorders, SKU, images, language, translation links, related product IDs, and hashes of the text |
| `site_blackbox.py` | Compares snapshots step by step. Standard library only |
| `tsyc-bridge-trio-driver.php` | Creates one synthetic trio through the bridge on the copy. Refuses the production host and any environment other than `local` or `staging` |

The developer snapshots the copy, creates a test trio, then prices, stocks, publishes
and orders it, with a snapshot after each step. The tool reports:

- **Gate A on that copy** (A05, A07, A08, A11): pass or fail. A11 fails if any product outside the trio differs at any step. A trio made by hand with "+" is reported `BLOCKED` with what was observed, because it does not test the bridge.
- **Gate B** (B01 to B07): what each other copy did after one was priced, stocked or ordered. `OBSERVED`, or `BLOCKED` for a step that was not run.

Snapshots from `tiemsachyeucon.com` are refused. The package was run end to end on
the lab stack (checks X01, X02 of the Gate A pilot), where its Gate B lines are only
the native baseline: plain WooCommerce and Polylang propagate nothing.

**What it cannot see:** why the site behaves as it does, changes outside the product
tables it reads, and anything that happens later than the snapshot. The two-minute wait
before each snapshot covers ordinary delayed jobs, not all of them.

## 5. What stands before a production pilot

`integration_gates.production_pilot_requirements` lists what is unmet. Today:

1. Gate A in the local lab: **met** (section 3).
2. Gate A on an isolated copy of the real site, with the trio created by the bridge: **not run**. Needs the developer.
3. The developer's review of the exact bridge file, by commit: **not done**.
4. The owner's authorization naming the site and the books: **not given**.

Gate B is not on this list, and that is a proposal for the owner and developer to
accept, not a decision already made. The reasoning: a draft without a price cannot be
bought, the pre-order workflow never writes a price or stock value, and whatever the
site does once the owner prices and publishes a trio is what it already does for the
trios the owner makes by hand. What Gate A on the real copy must still show is that
the link itself moves nothing (check A11).

Gate B stays required before automation touches any published product, any price or
any stock value, and before anyone states that the site's sync is safe.

A first production pilot, once all four are met, would be one book, one language at
a time, with production snapshotted read-only before and after.

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
| `tools/site_blackbox/` | The developer's package |
| `tests/test_preorder_workflow_gates.py`, `tests/test_site_blackbox.py` | Offline tests |
