# TSYC Polylang bridge and multilingual integration

Status 2026-11-02. The bridge has passed on an isolated local WordPress. It is
**not installed on production or any copy of the store**, and it has **not met the
store's theme hooks**. No EN/DE product, link or mapping row exists because of it.

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

## 2. Polylang 3.8.4: verified behaviour

Read in the plugin source and run on the real code (test IDs refer to section 5).

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
Public Polylang API only; no direct database or term writes (a test enforces this).

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

**Create sequence** (per source and language, under an atomic lock):

1. Reject any field outside a strict allowlist. Price, stock, status and SKU cannot be sent.
2. Look up the request ID. A finished request only reports, and refuses with `tsyc_link_changed` if the link no longer holds, so a replay can never undo an owner's change. A partial one re-validates its source and resumes.
3. A new request validates the source: exact draft, default language, simple product, exact SKU, owner meta and TSYC's own modified-time baseline together, in the pre-order shape.
4. Refuse if the language is already linked or another product claims this book and language.
5. Insert a draft: `manage_stock=true`, `stock_quantity=0`, `backorders=yes`, **no price, no SKU**. Images, weight, dimensions and contributor attributes come from the source on the server. Categories and tags use Polylang's translation of each term; a missing one is a warning, never guessed.
6. `pll_set_post_language`, read back. Then `pll_save_post_translations` with the **full** current group plus the new member, read back from both sides.
7. Answer with the exact IDs, the verified read-back and the baseline for later owner-edit checks.

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
| `src/services/translation_draft_runner.py` | One book, language by language, failures isolated. Proposes mapping rows; **writes nothing to Supabase** |
| `src/domain/translation_create_state.py` | Stable request IDs and an atomic per-language checkpoint, written **before** the request leaves |
| `src/domain/multilingual_adapter.py` | Group resolution and the per-language plan |
| `scripts/discover_translation_candidates.py` | Read-only classification and review queue of existing copies |
| `scripts/plan_translation_drafts.py` | Read-only dry run: the first blocker per book |

Recovery rule: after any uncertain answer the runner reads by request ID. A found
product is finished through the same request ID (it cannot create a second one). An
explicit "absent" allows the same request again. A failed read sends nothing. While one
language is unreconciled, the other waits, because the unrecorded draft makes the group
a `CONFLICT`.

## 5. Local lab results

Isolated lab: `tools/wp_lab/` provisions WordPress 7.1.4, WooCommerce 10.7.0 (HPOS on),
Polylang 3.8.4 and portable PHP 8.3 with checksums verified, on loopback only, using SQLite, synthetic data, outbound HTTP blocked and mail off. No production value is read.

```
.venv/Scripts/python.exe tools/wp_lab/provision_lab.py
.venv/Scripts/python.exe tools/wp_lab/run_local_pilot.py
```

Run 2026-11-02 (`data/output/polylang_local/20261102T091845Z/`, gitignored): **23 `PASS_REAL_LOCAL`, 2 `BLOCKED_MISSING_DEPENDENCY`, 0 `FAIL`.**

**Not in the lab:** Flatsome and Flatsome Child, both bulk-link plugins, Duplicate Post RB, MySQL, production data. Every pass below means "on plain WordPress, WooCommerce and Polylang", not "on the store".

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

**Limits:** SQLite, not MySQL. The concurrency results (S7, T12B baseline) need repeating on MySQL. Hreflang was not exercised, so L1/L2's hreflang comparison in `POLYLANG_LOCAL_INTEGRATION_TEST_PLAN.md` is still open.

## 6. Price, stock and pre-orders

| Field | Source of truth | Status |
|---|---|---|
| Regular and sale price | Owner only for TSYC. Direction between languages: **unknown** | Needs Flatsome Child source |
| Stock quantity, stock management, backorders | **Unknown.** VI is not assumed to be authoritative | Needs Flatsome Child source |
| SKU | VI, copied as raw meta by something on the store | Needs source. The bridge sets none on a copy |
| Publication status | Owner only | Policy |

**What the lab shows (native baseline, not the store):** each language keeps its own
price and stock; a save on one touches no other; EN and DE backorder orders each drop
only their own copy to -1. This resembles the -1 EN/DE groups in the production audit
but does not prove their cause. **Production price and stock sync remains unverified.**

Risk to test first: the bridge's own save on a new copy may trigger the theme's sync
hooks and push `stock 0 / backorders yes` onto siblings. This is why write routes stay
off anywhere those hooks run until Stage D passes.

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

**The bridge as designed serves future drafts. It cannot add EN/DE to any book recorded
today**, because every live one is published and owner-maintained. Whether automation
may ever do that is an owner decision (section 9).

## 8. Roadmap and gates

| Stage | Gate | Status |
|---|---|---|
| A. Existing architecture verified | Hook list with fields and direction, from the real source | **Partial:** Polylang and WooCommerce verified; Flatsome Child and bulk-link not |
| B. Polylang read-only integration | Bridge read and client pass real tests; a read returns the same IDs as production hreflang | **Local only.** Not installed anywhere real |
| C. Local WordPress pilot | Matrix passes on real plugins | **Passed on the native stack** |
| D. Price, stock, backorders with the real customization | T11, T12B and the hook risk in section 6 run with the real theme on MySQL | **Blocked: developer source** |
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
