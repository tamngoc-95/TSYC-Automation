# TSYC Polylang bridge and multilingual integration

Status 2026-11-07, bridge 1.1.0. The bridge has passed on an isolated local WordPress,
on both SQLite and MariaDB, and was re-run on 2026-11-07 at the versions production
runs (WordPress 7.1.4, WooCommerce 11.2.1, Polylang 3.9.1, PHP 8.3; section 5.2). It is **not installed on production or any copy of the
store**, and it has **not met the store's theme hooks**. No EN/DE product, link or mapping
row exists because of it. Staging write tests are not yet allowed: see section 10.

## 1. Confirmed architecture

From the website developer (context; checked independently where section 2 says so):

| Statement | Consequence for TSYC |
|---|---|
| Polylang's language-flag "+" is how a translation is created | New EN/DE products follow the Polylang model, not `wc/v3` alone |
| Each language has its own Woo product ID, linked by Polylang | One book is several Woo IDs: `woocommerce_language_products` |
| `pll-bulk-link-translations` v1/v2 linked products that **already existed** | Legacy role only. They stay installed and are never TSYC's create path or identity evidence |
| The site already syncs price and stock | Not replaced. **Unverified: the source is not available** (section 6) |
| Flatsome Child also holds metadata/inventory code | **Unverified**, same reason |

Two flows, kept separate:

- **New product:** TSYC VI draft → bridge creates the EN/DE drafts → Polylang link → verified read-back → mapping row.
- **Legacy product:** existing copies are only read and classified (`discover_translation_candidates.py`). They are never written, relinked or adopted.

## 2. Polylang 3.8.4 and 3.9.1: verified behaviour

Read in the 3.8.4 plugin source and run on the real code (test IDs refer to section 5).
Every test below passed again on 3.9.1 with WooCommerce 11.2.1 (section 5.2); the 3.9.1
source was not re-read line by line.

| API | Behaviour TSYC depends on | Evidence |
|---|---|---|
| `pll_get_post_language` | Slug, or empty when unassigned | T02 |
| `pll_set_post_language` | **Returns false when the language is already set**, so a false is not a failure. The language must be read back | T02 |
| | **Moving a product onto a taken language slot silently evicts it** from the group. The rest of the group still reads as valid | S5 |
| `pll_get_post_translations` | Includes drafts. Empty map for a product with no group | T02, T05 |
| `pll_save_post_translations` | **Unlinks any current member left out of the array** | S6 |
| | **Silently drops a member whose language differs from its key**, with no error | S6 |
| `wc/v3` product create | Gets the default language (vi) and no link. It has no language or link field | T02 |
| The "+" link | `post-new.php?post_type=product&from_post=ID&new_lang=xx&_wpnonce=…`: an admin screen. The link is made when the edit form is saved | T03B |
| "+" meta copy | Skips protected (`_`-prefixed) meta, so **no `_tsyc_*` and no `_sku`** on plain Polylang | T03B |
| WooCommerce's own Duplicate | **Copies every `_tsyc_*` key.** The same pattern as #4858 (evidence, not proof of how #4858 was made) | T10 |

So "+" cannot be reproduced by a `wc/v3` request, and an honest bridge must use the
public PHP API and read everything back. Plain Polylang and WooCommerce do not sync
price or stock at all (`sync: []`, T11), so whatever syncs them on the store is
entirely the developer's code.

## 3. The bridge

`wordpress/tsyc-polylang-bridge/tsyc-polylang-bridge.php`, one must-use plugin.
Public Polylang API only. The only SQL is its own lock row in the options table, because
`add_option` is an upsert and cannot be a lock (a test enforces both).

| Route | Auth | Purpose | Registered on |
|---|---|---|---|
| `GET tsyc/v1/health` | `edit_products` | Versions and languages | everywhere |
| `GET tsyc/v1/translations/{id}` | `edit_products` | Language, exact VI/EN/DE IDs, identity meta, `consistent`, `issues` | everywhere |
| `GET tsyc/v1/translations/by-request/{id}` | `edit_products` | Reconciliation: found (and how far) or explicitly absent | everywhere |
| `POST tsyc/v1/translations` | `edit_products` + write token | Create and link one missing draft | **enabled non-production only** |
| `POST tsyc/v1/translations/rollback` | `edit_products` + write token | Unlink and trash one unchanged bridge draft | **enabled non-production only** |

**Fail-closed reads:** Polylang or WooCommerce missing is 503, an unassigned language is 409, and an unknown product is 404. Group defects are reported as `MEMBER_MISSING`, `MEMBER_NOT_A_PRODUCT`, `MEMBER_TRASHED`, `MEMBER_LANGUAGE_MISMATCH`, `MEMBER_LANGUAGE_META_MISMATCH` or `ASYMMETRIC_GROUP`. HTTPS is required outside `local`.

**Write guards.** All must hold, or the routes are not registered at all:
`TSYC_BRIDGE_ENABLE_WRITES === true`, environment type `local` or `staging`, the host
is not `tiemsachyeucon.com` (with or without `www`), and a write token of 32+
characters. The Python writer separately refuses the production host. **The write
routes must never be enabled on production without a separate authorization.**

**Create sequence** (one lock per translation group, shared with rollback, because
saving a group rewrites all of it):

1. Reject any field outside a strict allowlist. Price, stock, status and SKU cannot be sent.
2. Look up the request ID. A finished request only reports, and refuses with `tsyc_link_changed` if the link no longer holds, so a replay can never undo an owner's change. A partial one re-validates its source and resumes.
3. A new request validates the source: exact draft, default language, simple product, exact SKU, owner meta and TSYC's own modified-time baseline together, in the pre-order shape.
4. Refuse if the language is already linked or another product claims this book and language.
5. Snapshot every group member's status, modified time, price, stock, backorders and SKU, and start watching writes to those fields on any product.
6. Insert a draft: `manage_stock=true`, `stock_quantity=0`, `backorders=yes`, **no price, no SKU**, with the snapshot stored on it. Images, weight, dimensions and contributor attributes come from the source on the server. Categories and tags use Polylang's translation of each term; a missing one is a warning, never guessed.
7. `pll_set_post_language`, read back.
8. Compare with the snapshot and revalidate the source, at the last moment before the link.
9. `pll_save_post_translations` with the **full** current group plus the new member, read back from both sides.
10. Compare again: every member unchanged, the new draft still in its shape, the group exactly the old group plus this member.
11. Answer with the exact IDs, the verified read-back and the baseline for later owner-edit checks.

**Side-effect stop.** If step 8 or 10 finds a change, the answer is `409 tsyc_sibling_side_effect` with the exact changes: `MEMBER_CHANGED`, `MEMBER_SAVED`, `OUTSIDE_GROUP_WRITE`, `DRAFT_SHAPE` or `GROUP_CHANGED`. **The bridge detects; it does not prevent and never repairs.** The draft is marked `SIDE_EFFECT`, a replay cannot continue past it, and the runner stops that whole book in this run and every later one. Other books carry on. A rollback uses the same checks and reports the same code.

**Identity and ownership.** No single field proves anything. `_tsyc_internal_product_id`
can be copied (T10), so ownership needs TSYC's own record of the exact Woo ID plus the
remote state:

| Case | Result |
|---|---|
| TSYC-created, unchanged draft | The only writable state; text only |
| Owner-edited TSYC draft | `SKIP_OWNER_MAINTAINED`; rollback refused |
| Published product | Never written; refused as a bridge source |
| WordPress-made or manually linked translation | Never written, never adopted |
| TSYC meta on a product TSYC never recorded | `CONFLICT` (copied meta) |
| Linked copy carrying another book's TSYC meta | `CONFLICT` (wrong book or edition) |
| Link with no SKU and no TSYC record | `UNVERIFIABLE` |
| Removed product | Never recreated |

## 4. TSYC client side

| Code | Role |
|---|---|
| `src/services/polylang_link_reader.py` | Strict read client. Any failure raises; it never means "no translations" or "absent" |
| `src/services/polylang_bridge_writer.py` | Sorts each answer into `CONFIRMED`, `REFUSED` or `UNCERTAIN`. Never retries. Refuses the production host |
| `src/services/translation_draft_runner.py` | One book, language by language, failures isolated, except a side-effect stop, which halts the book. Proposes mapping rows; **writes nothing to Supabase** |
| `src/domain/translation_create_state.py` | Stable request IDs and an atomic per-language checkpoint, written **before** the request leaves |
| `src/domain/multilingual_adapter.py` | Group resolution and the per-language plan |
| `src/domain/translation_book_input.py` | Builds the runner's `BookInput` from the real records: the RECORDED VI row with its baseline, plus exactly one APPROVED, final row per language. Refuses, with one code, a missing or ambiguous row, a translation approved before its VI source (`TRANSLATION_PREDATES_VI_APPROVAL`), or text the payload rejects. Renders through `create_woocommerce_draft.render_content`, the VI create path's own renderer |
| `scripts/discover_translation_candidates.py` | Read-only classification and review queue of existing copies |
| `scripts/plan_translation_drafts.py` | Read-only dry run: the first blocker per book, plus whether `translation_book_input` yields a valid input (`content_input`) |

**Content rendering (2026-11-14).** Before this date `build_description` rendered the
Vietnamese section headings ("Tác giả", "Thông tin sản phẩm") into every language, and the
author detector missed plural and titled forms ("The authors of this book are X and Y.",
"Dieses Buch stammt von Dr. X."). Approved rows that would have rendered a Vietnamese
heading inside an EN/DE description: 8 EN, 2 DE. All were metadata-only and are now dropped.
EN/DE headings are now "About the author" / "Product details" and "Über den Autor / die Autorin" /
"Produktdetails". An unknown language is refused. No live product was affected: TSYC has
never created an EN/DE product. Old and new detectors were compared on all 409 approved rows:
the `long_description`/`short_description` results are identical, so no approved prose is newly
flagged. Exactly 11 `author_summary` values changed: the 10 EN/DE rows above, plus one VI row
`Tác giả của ấn phẩm là TS. X.` (the generated template, missed because of `TS.`). The other 7
VI author notes still render under "Tác giả".
| `scripts/inventory_php_hooks.py` | Read-only inventory of the developer's PHP hooks (`POLYLANG_HOOK_INVENTORY.md`) |

Recovery rule: after any uncertain answer the runner reads by request ID. A found
product is finished through the same request ID (it cannot create a second one). An
explicit "absent" allows the same request again. A failed read sends nothing. While one
language is unreconciled, the other waits, because the unrecorded draft makes the group
a `CONFLICT`.

## 5. Local lab results

Isolated lab: `tools/wp_lab/` provisions WordPress 7.1.4, WooCommerce 11.2.1 (HPOS on),
Polylang 3.9.1 and portable PHP 8.3 with checksums verified, on loopback only, using SQLite or MariaDB 11.4.5, synthetic data, outbound HTTP blocked and mail off. No production value is read. These are production's versions as read from `wc/v3/system_status` on 2026-11-07; the 2026-11-03 runs below used WooCommerce 10.7.0 and Polylang 3.8.4 (`--woocommerce-version` / `--polylang-version` reproduce them). An existing lab keeps the version it was built with, and the provisioner refuses a lab whose installed version differs from the one requested, so upgrade into a fresh `--lab-dir`.

```
.venv/Scripts/python.exe tools/wp_lab/provision_lab.py [--db mariadb] [--lab-dir PATH]
.venv/Scripts/python.exe tools/wp_lab/run_local_pilot.py [--lab-dir PATH]
.venv/Scripts/python.exe tools/wp_lab/run_hardening_pilot.py [--lab-dir PATH]
```

**Base pilot, bridge 1.1.0, 2026-11-03: 23 `PASS_REAL_LOCAL`, 2 `BLOCKED_MISSING_DEPENDENCY`, 0 `FAIL`, on both databases.** Reports are in `data/output/polylang_local/` (gitignored).

**Not in the lab:** Flatsome and Flatsome Child, both bulk-link plugins, Duplicate Post RB, production data. Every pass below means "on plain WordPress, WooCommerce and Polylang", not "on the store".

| Test | What ran | Result |
|---|---|---|
| T01 | VI draft through `wc/v3`, TSYC's shape | `PASS_REAL_LOCAL` |
| T02 | Language assignment and its semantics | `PASS_REAL_LOCAL` |
| T03 / T04 | Linked EN and DE drafts through the bridge | `PASS_REAL_LOCAL` |
| T03B | Polylang's real "+" screen in a logged-in session | `PASS_REAL_LOCAL` |
| T05 / T06 | Same group from every member; adapter `RESOLVED`, ownership and rows valid | `PASS_REAL_LOCAL` |
| T07 | Repeat run sends nothing; replay finds the same ID; other request or body refused | `PASS_REAL_LOCAL` |
| T08 | Lost answer, request that never arrived, failed reconciliation read (zero POSTs) | `PASS_REAL_LOCAL` |
| T09 | Wrong-edition request refused; a wrongly linked group is `CONFLICT` | `PASS_REAL_LOCAL` |
| T10 | Edited source, published source, edited copy, Duplicate-copied meta | `PASS_REAL_LOCAL` |
| T11 | **Price and stock sync** | `BLOCKED_MISSING_DEPENDENCY` |
| T12 | Zero-stock backorder; EN and DE orders | `PASS_REAL_LOCAL` (native only) |
| T12B | **Parallel ordering across languages** | `BLOCKED_MISSING_DEPENDENCY` |
| T13 / T13B | Crash after create and after language, resume in a new process; resume refuses an owner-edited source | `PASS_REAL_LOCAL` |
| T14 | Rollback, refused replay, full cleanup | `PASS_REAL_LOCAL` |
| S1–S4 | Auth, validation, guards, Polylang off | `PASS_REAL_LOCAL` |
| S5–S8 | Group defects, Polylang semantics, four racing requests on two PHP servers (one product), copied facts | `PASS_REAL_LOCAL` |

The lab found real defects, all now fixed with regression coverage: another book's meta
on a linked copy was only `UNVERIFIABLE`; a replay could relink after an owner's unlink;
a resume could link onto an edited source.

**Limits:** MariaDB 11.4.5 is MySQL-family, not necessarily what production runs. PHP's built-in server is not the store's web server, and the lab has no persistent object cache.

### 5.1 Hardening pilot, 2026-11-03

`run_hardening_pilot.py`, on SQLite and MariaDB alike: **13 `PASS_REAL_LOCAL`, 8 `PASS_SYNTHETIC_HOOK`, 6 `BLOCKED_MISSING_SOURCE`, 0 `FAIL`.** Reports are in `data/output/polylang_hardening/` (gitignored).

The labels are not interchangeable. `PASS_SYNTHETIC_HOOK` uses `tsyc-lab-synthetic-hooks.php`, TSYC's own guesses at what the theme might do. It proves that TSYC's guards react correctly **if** the store behaves that way, and says nothing about whether it does.

| Test | What ran | Result |
|---|---|---|
| H01 / H02 | EN from a VI draft; DE after EN. Source and EN unchanged, field by field | `PASS_REAL_LOCAL` |
| H03 | Hooks really fired during create, rollback, price and stock saves and an order | `PASS_REAL_LOCAL` |
| H04 | Stock 0, backorders yes: `onbackorder`, not purchasable while unpriced | `PASS_REAL_LOCAL` |
| H05 | Owner's published sibling, stock 7, price set: untouched; TSYC does not adopt it | `PASS_REAL_LOCAL` |
| H06 | Price saved on VI and on EN: neither propagates natively | `PASS_REAL_LOCAL` (native only) |
| H07 | Duplicate SKU, below | `PASS_REAL_LOCAL` |
| H08 | Published source and copy refused; published sibling unchanged | `PASS_REAL_LOCAL` |
| H09 | 12 rounds of racing EN and DE, rollback during create, real owner saves during create, lock expiry | `PASS_REAL_LOCAL` |
| H10 | Crash after link; sibling changed during an outage; missing snapshot | `PASS_REAL_LOCAL` |
| H11 | Rollback of a middle member, the last member, a partial draft and beside a stocked sibling; recreate only with a new request | `PASS_REAL_LOCAL` |
| H12 | Native "+" before and after the bridge | `PASS_REAL_LOCAL` |
| H13 | Storefront hreflang matches the bridge read (L1, L2); a draft is in the read, not in hreflang | `PASS_REAL_LOCAL` |
| Y01–Y08 | Assumed stock push, identical push, price pull, SKU copy, sibling save, relink, write outside the group, price sync in both directions | `PASS_SYNTHETIC_HOOK` |
| B11–B16 | Real price and stock sync (T11), parallel ordering across languages (T12B), bridge create with real hooks, metadata sync, bulk-link interference, Duplicate Post RB | `BLOCKED_MISSING_SOURCE` |

**Defects this found and fixed (bridge 1.1.0):**

| Defect | Evidence before the fix | Now |
|---|---|---|
| EN and DE had separate locks, while saving a group rewrites all of it | Both answered `201` with a verified read-back; the final group was `{vi, en}`, DE silently gone | One lock per group; the loser gets `tsyc_busy` |
| The "atomic" lock was `add_option`, which is an upsert | With one lock per group, a confirmed member was still lost in round 5 of 6 | `INSERT IGNORE` lock row; compare-and-delete on release and expiry |
| Rollback took no lock | By code review; the race is now covered by H09 | Shares the group lock |
| The source was validated only before the insert | By code review; an owner edit during the create went unchecked | Revalidated right before the link |
| Nothing checked what a create did to siblings | By code review; section 6 only named it as a risk | Snapshot, write watcher and side-effect stop |

**Measured facts (no theme):**

- A bridge create saves only its own draft. Its save hooks all fire before the link: `save_post_product` sees no language yet, `woocommerce_new_product` sees `en`. So a sync on those hooks has no siblings to reach during a create.
- **The source is touched only by `set_object_terms`, as the group is saved.** A theme hook there runs once per member, so a stocked sibling can overwrite the VI draft, not only the reverse (Y01).
- `update_post_meta` with an equal value fires no hook. Pushing identical values cannot be seen, and is harmless (Y02).
- A modified time has one-second resolution, so a same-second sibling save shows only through the save watcher.
- Polylang's edit screen always prints the add link for a language, hidden when it is taken. **Opening that URL and saving silently evicts the existing translation**, which stays a draft outside the group. A bridge replay then refuses with `tsyc_link_changed` and does not undo the person's change (H12).
- A person's "+" copy has no `_tsyc_*` meta. The bridge keeps it in the group and never adopts it.

### 5.2 Re-run at production versions, 2026-11-07

Production was read (`wc/v3/system_status`, GET) at WordPress 7.1.4, WooCommerce **11.2.1**
and Polylang **3.9.1**, PHP 8.3.36, while every earlier run used 10.7.0 / 3.8.4. Two fresh
labs were provisioned at exactly those versions (the 2026-11-03 labs are kept as baseline).

| Run | Database | Result | Report (gitignored) |
|---|---|---|---|
| Base pilot | MariaDB 11.4.5 | 23 `PASS_REAL_LOCAL`, 2 `BLOCKED_MISSING_DEPENDENCY` (T11, T12B), 0 `FAIL` | `polylang_local/20261107T170422Z` |
| Base pilot | SQLite | 23 `PASS_REAL_LOCAL`, 2 `BLOCKED_MISSING_DEPENDENCY`, 0 `FAIL` | `polylang_local/20261107T174319Z` |
| Hardening pilot | MariaDB 11.4.5 | 13 `PASS_REAL_LOCAL`, 8 `PASS_SYNTHETIC_HOOK`, 6 `BLOCKED_MISSING_SOURCE`, 0 `FAIL` | `polylang_hardening/20261107T171300Z` |
| Hardening pilot, first | SQLite | 12 `PASS_REAL_LOCAL`, **1 `FAIL` (H09)**, 8 `PASS_SYNTHETIC_HOOK`, 6 `BLOCKED_MISSING_SOURCE` | `polylang_hardening/20261107T174855Z` |
| H09 alone | SQLite | `PASS_REAL_LOCAL` (98.7 s) | `polylang_hardening/20261107T201741Z` |
| Hardening pilot, second | SQLite | 13 `PASS_REAL_LOCAL`, 8 `PASS_SYNTHETIC_HOOK`, 6 `BLOCKED_MISSING_SOURCE`, 0 `FAIL` | `polylang_hardening/20261107T202129Z` |

**The H09 failure.** One `wp eval-file` call inside H09 blocked for ~430 s past its
subprocess timeout; the test took 704 s against a normal 78–101 s. No bridge answer or
group state was wrong in the evidence: the harness never got the PHP result back. It did not
recur in an isolated re-run or a full re-run, and never on MariaDB. It is recorded as an
intermittent lab stall on SQLite (single writer, PHP's built-in server), **not explained and
not excused**: if it recurs, find which evaluation blocks before trusting H09 on SQLite.

**Measured hook events changed with the upgrade.** Same database, old versus new versions:

| Scenario | 10.7.0 / 3.8.4 | 11.2.1 / 3.9.1 |
|---|---|---|
| `bridge_create_en`, `bridge_create_de_after_en`, `bridge_rollback` | — | **identical** |
| `wc_rest_vi_price_update` | `set_object_terms` 1× on the source | not fired |
| `wc_rest_en_stock_update` | `set_object_terms` 2× | 1× |
| `order_on_en_backorder` | `set_object_terms` 1×, `edit_post` 2× | not fired, `edit_post` 3× |

Control: old MariaDB versus old SQLite, and new MariaDB versus new SQLite, are identical in all
6 scenarios, so the change is the version upgrade, not the database. The link path the guard
depends on is unchanged. What changed is which core hooks a plain price, stock or order save
fires, which is exactly where a theme's sync code would hang. `tests/fixtures/php_hooks/
measured_hook_events.json` (read by `inventory_php_hooks.py`) is refreshed from the MariaDB
run, so the hook inventory judges the developer's code against the versions production runs.
**This makes the developer source (B11–B16) more important, not less**: a theme hook written
against `set_object_terms` on a price save would silently stop firing on 11.2.1 / 3.9.1.

Still not verified, unchanged: price, stock and backorder sync, parallel ordering, and every
Flatsome Child, bulk-link and Duplicate Post RB behaviour. Those need the real code (B11–B16).

## 6. Price, stock and pre-orders

| Field | Source of truth | Status |
|---|---|---|
| Regular and sale price | Owner only for TSYC. Direction between languages: **unknown** | Needs Flatsome Child source |
| Stock quantity, stock management, backorders | **Unknown.** VI is not assumed to be authoritative | Needs Flatsome Child source |
| SKU | VI, copied as raw meta by something on the store | Needs source. The bridge sets none on a copy, and a hook giving it the VI SKU is accepted as one logical book (Y04); any other SKU stops it |
| Publication status | Owner only | Policy |

**What the lab shows (native baseline, not the store):** each language keeps its own
price and stock; a save on one touches no other; EN and DE backorder orders each drop
only their own copy to -1. This resembles the -1 EN/DE groups in the production audit
but does not prove their cause. **Production price and stock sync remains unverified.**

**Remaining risks:**

| Risk | What TSYC does today | What closes it |
|---|---|---|
| A theme hook on the link event moves stock, price or backorders between members | Detected and stopped, **after the write has landed, and not repaired**. This is why write routes stay off wherever those hooks run until Stage D passes | Reading the source, then B13 |
| Writes that happen after the request: scheduled jobs, `shutdown` work | **Not seen by the guard**, which ends with the request | Hook inventory, then rereading the group later in B13 |
| Raw SQL writes outside the group | **Not seen**; raw SQL inside the group is caught by the snapshot | Hook inventory (`raw_sql_write`) |
| Owner or theme saves after creation | Outside the guard. TSYC sends nothing further (Y08), and `inventory_authority_findings` only reports differences | Owner and developer decide the authority |
| Real hooks save siblings on every link (183 live groups show same-second saves) | Every create stops as `MEMBER_SAVED`, and a sibling save also moves the baseline, which refuses the next language. Safe, but creates would be unusable | Design decision after B13. **The guard is not to be loosened without that evidence** |
| Stock direction, and orders on EN or DE | Unknown. Natively each copy drops to -1 alone, like the live -1 groups, which does not prove their cause | T11, T12B with the real theme |
| A raw-copied SKU | Save without a SKU works. **Resending the same SKU fails with HTTP 400 `product_invalid_sku`**. `wc_get_product_id_by_sku` returns only the first match. `?sku=` returns all of them. An unlinked twin is a `CONFLICT` (H07). The wp-admin edit screen was not tested | Whether the theme copies it, then the same test through wp-admin |
| A forced "+" URL evicts a translation | Detected on the next run (`CONFIRMED_LINK_CHANGED`, `tsyc_link_changed`), never undone | Accepted; reported |

Pre-order defaults apply only to a new authorized Fahasa draft (`draft`, stock 0,
backorders `yes`, no price), never to an existing published product.

## 7. Live state, 2026-11-02 (read-only)

| Item | Result |
|---|---|
| Mapping rows | 180 vi: 86 `RECORDED`, 94 `REMOTE_REMOVED`; unchanged since 2026-10-30. No EN/DE rows |
| Blocked books | CAN-0033 `BLOCKED_COPIED_OWNERSHIP_META` (#4858), AUTOIMPORT-CAN-0010 `BLOCKED_OWNER_DECISION` (#3772), CAN-0031 `BLOCKED_CREATE_UNRESOLVED` |
| **New drift** | **FAHASA-002-CAN-0013 #4928 and CAN-0014 #4930 are in the trash**, while their rows still say `RECORDED` |
| Recorded VI products | **84 published**, 2 trashed, **0 drafts** |
| Approved content | 66 of 86 books have approved VI, EN and DE; 20 have VI only |
| Dry run | `VI_PUBLISHED_OWNER_TERRITORY` 84, `VI_NOT_READ` 2; creates possible today: **0** |
| Existing copies | 35: `POTENTIAL_LINK` 15 (was 13: FB-2026-001-CAN-0003's VI #3311 is now published), `UNKNOWN` 20 (hreflang without SKU), `AUTHORITATIVE_LINK_VERIFIED` 0. Mapping inserts allowed: 0 |

Update 2026-11-07 (read-only, plus local-only reconciliation):

| Item | Result |
|---|---|
| Versions | WordPress 7.1.4, WooCommerce 11.2.1, Polylang **3.9.1** (was 3.8.4); no `tsyc/*` route on production |
| Woo catalog | 0 drafts; 1958 published in the morning, 1966 by the afternoon |
| #4928, #4930 | Sync rows reconciled to `REMOTE_TRASHED`, mapping rows moved to `REMOTE_REMOVED` (`reconcile_language_mapping_status.py`). Mapping: 84 `RECORDED`, 96 `REMOTE_REMOVED` |
| Copied ownership meta | A second case: #4829 carries IMG-003-CAN-0034's `_tsyc_*` keys (TSYC's product is #4595), SKU `…CAN-51`, live again since 2026-11-07 16:17 GMT. `COPIED_OWNERSHIP_META`, never adopted |
| Owner's native "+" use | The owner created VI→EN→DE trios by hand today, without SKUs (e.g. #5301/#5307/#5308, #5309/#5312/#5313). This is the create path the developer described |

**The bridge as designed serves future drafts. It cannot add EN/DE to any book recorded
today**, because every live one is published and owner-maintained. Whether automation
may ever do that is an owner decision (section 9).

## 8. Roadmap and gates

| Stage | Gate | Status |
|---|---|---|
| A. Existing architecture verified | Hook list with fields and direction, from the real source | **Partial:** Polylang and WooCommerce verified; tooling ready; Flatsome Child and bulk-link not read |
| B. Polylang read-only integration | Bridge read and client pass real tests; a read returns the same IDs as production hreflang | **Local only** (H13). Not installed anywhere real |
| C. Local WordPress pilot | Matrix passes on real plugins | **Passed on the native stack, on SQLite and MariaDB, at production's versions (2026-11-07; one intermittent SQLite H09 stall, section 5.2)** |
| D. Price, stock, backorders with the real customization | B11–B16 run with the real theme on MariaDB and are read by a person | **Blocked: developer source** |
| E. Controlled create interface | Stage D passes with no sibling side effect | Not started |
| F. One authorized logical-book pilot, isolated copy | Owner authorization; isolation proof | Not started |
| G. Production proposal and limited pilot | Separate owner authorization; write-guard change reviewed with the developer | Not started |
| H. VI/EN/DE drafts in the standard Fahasa workflow | Several clean pilot batches | Not started |

Passing one stage says nothing about the next.

## 9. Open items

**Developer:**
1. A zip of `flatsome-child/` and both `pll-bulk-link-translations*` folders (and how the existing copies were made).
2. The Flatsome parent theme under their own licence, for the lab.
3. Review of the bridge file, then a decision on who installs the read routes.

**Owner:**
1. Should automation ever add EN/DE to already published VI books? Today: no, and 84 of 86 books are in that state.
2. #4928 and #4930 are trashed: confirm that was intended. They then follow the existing terminal `REMOTE_REMOVED` handling and are never recreated.
3. CAN-0033 #4858 and CAN-0010 #3772: unchanged, still yours to decide.
4. Contributor labels ("Tác giả") stay Vietnamese on EN/DE copies (warning `ATTRIBUTE_LABELS_NOT_LOCALIZED`). Decide after seeing how the existing copies show them.
5. Confirm or change the EN/DE description headings (`DESCRIPTION_HEADINGS` in `scripts/create_woocommerce_draft.py`), especially the German author heading "Über den Autor / die Autorin". They appear only when an approved row has a substantive author note or a non-attribute details list.

## 10. Acceptance criteria for staging write tests

All must hold before `TSYC_BRIDGE_ENABLE_WRITES` is set on any site other than the local
lab. **None is met yet except where marked.**

**A. Source read**
1. The `flatsome-child` and both bulk-link sources are received and kept out of Git.
2. `inventory_php_hooks.py` reports `INVENTORIED` on them. Any suspected secret has gone to the developer and been rotated if real.
3. A person has read every HIGH and MEDIUM row and every unresolved relevant callback, and section 6 states the real fields, direction and triggers.

**B. Real code in the lab, on MariaDB**
4. The theme, parent theme and both bulk-link plugins are installed in the lab.
5. B11–B16 report `OBSERVED_REAL_SOURCE`, not `BLOCKED_MISSING_SOURCE`, and a person has read each one. These can never count as a pass by themselves.
6. Both pilots report 0 `FAIL` with the real code active. Any changed expectation is explained in this file.
7. B13: a bridge create and a rollback leave every sibling unchanged, reread again later to cover deferred work. Any remaining side effect is explained and accepted in writing by the owner and developer. The guard is not loosened to pass.
8. The wp-admin save of a copy with a raw-copied SKU is tested, if the theme copies SKUs.

**C. Decisions recorded**
9. Owner and developer: price and stock authority, the status of the bulk-link plugins, and who installs the bridge.
10. The developer has reviewed the exact bridge file, identified by commit.

**D. The staging site**
11. Its own host, not `tiemsachyeucon.com` or `www.`, with `WP_ENVIRONMENT_TYPE=staging`, HTTPS and a real isolation proof.
12. No production API key, no customer or order data, mail and payments off, and no job or plugin syncing to production.
13. A new write token of 32+ characters, not in Git. `GET tsyc/v1/health` shows `writes_enabled: true` there.
14. Production is checked read-only just before and after: no write route, and no product, link, price or stock change.

**E. The test itself**
15. A separate explicit owner authorization for that run, naming the site and the synthetic book.
16. One logical book, one language at a time, with rollback rehearsed first in the lab.
17. No Supabase mapping row is written. `verified_on_staging` is set only for that recorded run.
18. **Any `tsyc_sibling_side_effect`, `FAIL` or uncertain result stops the test** for a person. Nothing is repaired automatically.

Already met: the guard exists and passes on the lab (H09–H11, Y01–Y08), and the production host is refused by both the bridge and the writer (S3).

Passing all of this allows staging tests only. Production stays Stage G, under a separate authorization.
