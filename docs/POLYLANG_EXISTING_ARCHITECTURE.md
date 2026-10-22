# Existing multilingual architecture (Polylang, kept as is)

Status 2026-10-22: the website developer confirmed the architecture below.
It stays in place, and Polylang for WooCommerce is **not** purchased for now
(this supersedes the add-on route in `POLYLANG_WOOCOMMERCE_PLAN.md`, which is
kept for reference). Everything here was inspected **read-only**: GETs over
REST and public storefront pages only. Nothing was written, linked, installed
or deactivated on production.

## 1. Components

| Component | What was verified (2026-10-22) | Source of the fact |
|---|---|---|
| Polylang 3.8.4 (free) | Languages `vi` (default, prefix hidden), `en` (en_GB), `de` (de_DE). Translated post type: `product` only. Translated taxonomies: `product_cat`, `product_tag`, `product_shipping_class`. **`sync: []`**: Polylang's own synchronization (custom fields, taxonomies, featured image, dates) is OFF. `force_lang = 1` (language from the URL directory), `media_support = false`. | `GET pll/v1/settings`, `GET pll/v1/languages` |
| Polylang REST | Only `pll/v1/languages` and `pll/v1/settings`. There is no endpoint that reads or sets a product's language or translation links, and `wc/v3` / `wp/v2/product` carry no `lang`/`translations` field. A product created through `wc/v3` gets the default language (vi) and no links: every TSYC draft so far is VI. | `GET /wp-json/` route index, product reads |
| pll-bulk-link-translations **v1.0** | Plugin description: "one-time tool: automatically links products with the same slug root (VN/EN/DE…) as translations of each other in Polylang. Delete/deactivate after use." **Active.** No author, no REST routes. | `GET wp/v2/plugins`, route index |
| pll-bulk-link-translations-v2 **v2.0** | Plugin description: "one-time tool: links product translations based on the shared featured image (primary) and slug root (secondary). Delete/deactivate after use." **Active.** No author, no REST routes. | same |
| Flatsome Child 3.0 (active theme) | Developer: contains metadata-copy logic and inventory-synchronization hooks. **Source not inspected**: `functions.php` executes on request and cannot be read over HTTP; the theme and plugin folders return 403. | developer statement; `GET wp/v2/themes` |
| Duplicate Post RB 1.6.8 | Active. A plausible origin of the EN/DE copies: they carry the VI SKU and featured image but none of TSYC's `_tsyc_*` meta. | `GET wp/v2/plugins`, product meta |
| WooCommerce 10.7.0, HPOS on | Each language copy is an independent product with its own ID, price, stock and backorders fields. | product reads |

**What is NOT known yet** (it needs the source files or a staging copy, see section 6):
- which hooks Flatsome Child registers (`save_post`, `woocommerce_update_product`,
  `woocommerce_product_set_stock`, `woocommerce_reduce_order_stock`, …), which
  fields they copy, and in which direction;
- whether a price edit on one language reaches the others;
- whether an EN/DE order decrements the VI stock (shared inventory) or only the
  ordered copy, and how two concurrent orders in different languages behave;
- whether saving a translation can overwrite the VI product;
- what the bulk-link plugins do on every request. Their descriptions say "run once",
  but they are still active, and it is unknown whether they hook `save_post` or
  only run from an admin action.

## 2. Catalog link graph (storefront hreflang, read-only)

`scripts/audit_polylang_catalog.py` reads every product over `wc/v3` and every
published product page, then builds translation groups only from `hreflang`
links that agree with each other (`src/domain/polylang_link_graph.py`). It never
infers a link from a slug, title or image. A draft has no public page, so its
links are invisible.

### 2a. Results (full run 2026-10-22)
- **Coverage:** 1,986 products, 1,971 published pages, all read.
- **Groups:** 440 translation groups, **0 conflicted**: no asymmetric link, no link to an unknown URL, no language mismatch, no duplicate language.
  - Shapes: 419 VI+EN+DE, 9 VI+DE, 3 VI+EN, and 9 EN+DE with **no published VI**. The last include TSYC CAN-0003, whose VI is a draft; the other 8 are owner books such as "Utopia", "The Magic" and "The Power".
- **Unlinked:** 672 published products have no translation link: 428 VI, **123 EN and 121 DE**.
  - The unlinked EN/DE products are owner copies the bulk-link plugins did not match. Some look like duplicates (DE #699 and #700 are both "Männer sind vom Mars…").
  - They are listed in the JSON report and never merged automatically.
- **Inside the 440 groups, values per field:**

  | Field | Groups in sync | Groups with drift |
  |---|---|---|
  | SKU | 440 | 0 |
  | Featured image | 440 | 0 |
  | regular_price | 439 | 1 (#3468: VI 17.94 / EN, DE 17.99) |
  | sale_price | 431 | 9 (mostly a VI sale price not on EN/DE) |
  | stock_quantity | 418 | **22** |
  | backorders | 426 | 14 |
  | manage_stock | 437 | 3 |

  - 10 groups carry the TSYC SKU on VI only (AUTOIMPORT copies of 2026-09-13).
- **Inventory is not reliably shared.** In 20 of the 22 stock-drift groups EN = DE ≠ VI (e.g. VI 0, EN 1, DE 1), and group #2519/#2522/#2523 has EN/DE at **−1** (oversold).
  - This fits a hook that copies stock when a copy is created or linked, while later VI orders or edits decrement only the VI product.
  - It does not fit a live shared-stock hook. Staging must settle this.
- **Sibling writes on save.** In 183 groups, two or more members carry the identical `date_modified_gmt`, to the second. That is behavioural evidence of a hook writing to siblings on save.
- **SKU copying.** The later copies (from 2026-09-23) share the VI SKU, although WooCommerce refuses a duplicate SKU through the admin and REST. The SKU must therefore be copied as raw meta, which fits the developer's "metadata copy" hook. The earlier copies (2026-09-13) have no SKU.

## 3. CAN-0003 and CAN-0022

- `TSYC-FB-2026-001-CAN-0003`:
  - VI #3311 is TSYC's **draft**. Its link cannot be seen, because a draft has no public page.
  - EN #4014 and DE #4018 are **published** and link each other. Both are purchasable on backorder at 9.99, stock 0, backorders `yes`. The VI draft carries the same values.
  - Timestamps: DE #4018 was created at 2026-09-23 16:29:10, and EN #4014 was *modified* in that same second. VI #3311 was modified 27 s later (16:29:37).
  - The same-second EN change is behavioural evidence that creating or linking a copy **writes to its siblings**. The later VI change fits either a manual link from the VI edit screen or a sibling hook. Evidence only, not proof.
- `TSYC-FB-HIST-2026-AUTOIMPORT-CAN-0022`:
  - All three are published, priced 13.99, stock 0.
  - Backorders differ: VI #3780 is `notify` (VI last modified 2026-10-07), while EN #3926 and DE #3927 are `yes`.
  - DE #3927 was created at 16:50:45, and EN was modified in that same second, the same pattern as CAN-0003.
  - Either the hooks do not copy `backorders`, or the VI change on 10-07 was made without saving its siblings.

## 4. Risks for TSYC

1. **A translation can become the price/stock authority.** If a metadata-copy hook
   copies fields on save, saving any copy pushes its values onto the VI product.
   TSYC therefore never saves an existing linked copy until staging shows the hook
   direction (test `test_meta_copy_hook_would_let_a_translation_save_overwrite_the_vi_price`).
2. **Unsafe automatic relinking.** Both bulk-link plugins match on slug root or
   featured image. A different edition sharing a cover, or a slug collision, could be
   linked into a group. If either plugin also acts on save, a TSYC draft could be
   linked silently. Running v1 and v2 one after the other can produce different
   groups, and the last write wins in Polylang.
3. **Duplicate execution.** Two active plugins with overlapping purposes may both
   hook the same event. This is unverified until the source is read.
4. **No supported link mechanism.** Without Polylang Pro or a developer endpoint, an
   API-created EN/DE product would be a second *VI* product with a duplicate
   SKU. TSYC must not create EN/DE products at all today.
5. **Pre-order defaults.** stock 0 / backorders `yes` is safe for a VI-only TSYC
   draft, which has no siblings and so no hook can propagate anything. It is never
   applied to an existing published translation group (CLAUDE.md 14.8.4).

## 5. TSYC adapter (`src/domain/multilingual_adapter.py`)

One logical book is TSYC's VI product: the sync row's Woo ID plus its SKU.
- **`resolve_group`** proves the translation group from two independent sources that must agree:
  - the exact-SKU matches (copies keep the SKU), and
  - the storefront hreflang group.

  The possible results:

  | Result | Meaning |
  |---|---|
  | `RESOLVED` | Every member is proven by both sources. |
  | `SOURCE_ONLY` | The VI product has no translation. |
  | `UNVERIFIABLE` | A draft or unlinked member's link cannot be proven. CAN-0003 is this case. |
  | `CONFLICT` | The evidence disagrees: a duplicate language, a linked copy without the SKU, a link to another product (including bulk-link mismatches), an unknown draft language, or an unread page. |
  | `SOURCE_INVALID` | The product is not TSYC's VI source. |
- **`plan_language_actions`** plans per language:
  - `SKIP_OWNER_MAINTAINED`: any published copy, or any copy without TSYC's ownership meta;
  - `UPDATE_TEXT_DRAFT` / `NO_OP`: a TSYC-owned draft only, text only, idempotent;
  - `CREATE_NOT_AUTHORIZED` → `CREATE_BLOCKED_NO_LINK_MECHANISM` → `CREATE_AND_LINK`: the last only with authorization **and** a link mechanism proven on staging;
  - `RECONCILE_FIRST`: after an interrupted write;
  - `REFUSE`: for anything not `RESOLVED` / `SOURCE_ONLY`.
- **`build_text_payload` / `assert_text_only`**: name, description and short
  description only. Price, stock, backorders, SKU and status can never be sent.
- **`inventory_authority_findings`**: reports where a copy's price or stock differs
  from the VI authority, and never corrects it.
- Language of a draft: a `?post_type=product&p=N` link carries no prefix. Only
  TSYC's own products are known: vi by default, or the recorded `_tsyc_language`
  meta. Any other draft's language is unknown, which is an ERROR and is never
  guessed (fixed in `woo_translation_group.product_language`).

No writer is wired to the adapter yet. Offline tests:
`tests/test_multilingual_adapter.py` against `tests/support/fake_polylang_store.py`.
Every hook there is a switch the test turns on or off. The tests prove TSYC's
rules under each assumption, not the live hooks.

## 6. Needed from the developer (read-only, no production change)

1. The source of `wp-content/themes/flatsome-child/functions.php` and any included
   files, plus both `pll-bulk-link-translations*` plugin folders (a zip is enough).
   TSYC reads them offline and documents every `add_action`/`add_filter`, the copied
   fields and their direction.
2. How the existing EN/DE copies were made (Duplicate Post RB, Polylang's "+"
   button, manual), and how a new translation is meant to be linked today.
3. Whether either bulk-link plugin acts on `save_post`, or only from an admin
   screen.

## 7. Staging pilot (one product, nothing on production)

Prerequisite: an isolated staging copy (`POLYLANG_WOOCOMMERCE_PLAN.md` section 4a:
own host and database, payments, mail and feeds disabled, new keys, isolation proof).
1. Run `audit_polylang_catalog.py` on staging. Its groups must equal production's.
2. Take one TSYC draft with no translations (e.g. a FAHASA-2026-002 draft). Create
   its EN copy the way the developer does today, then read back the VI, EN, links,
   meta and stock.
3. Hook matrix on that pair, read back after each step:

   | Step | Question it answers |
   |---|---|
   | Change the VI stock | Does stock follow to EN? |
   | Order in EN (test gateway) | Which stock decrements? |
   | Two orders in parallel | How do concurrent orders behave? |
   | Change the VI price | Does the price follow? |
   | Save EN only | Does VI change (authority direction)? |
   | Backorders `yes` on VI | Does EN follow? |
   | Save VI with both bulk-link plugins active | Does any relink happen? |

4. Only when the matrix is documented: choose the link mechanism. The options are
   a developer endpoint around `pll_set_post_language` / `pll_save_post_translations`
   with strict auth, the existing manual "+" flow, or Polylang Pro later. Then
   implement the writer behind `LinkMechanism` with `verified_on_staging = True`.

## 8. Decisions left to the owner and developer
- Whether the bulk-link plugins stay active after their one-time run. Their own
  descriptions say to deactivate them. TSYC does not touch them.
- Which language copy is the price/stock authority (TSYC assumes VI).
- What to do about CAN-0003 (EN/DE sellable while VI is a draft) and the CAN-0022
  backorders difference.
- Whether the existing system can support TSYC automation without a paid plugin:
  yes for VI drafts and approved EN/DE text kept in `product_contents`. Linked
  EN/DE drafts need one supported, staging-proven link mechanism.
