# Polylang for WooCommerce: implementation plan (NOT implemented)

Status: plan only (2026-10-19). Nothing in this plan has been installed,
purchased or written. Every step that changes the live site needs the
owner's explicit decision.

## 1. Current state (read-only checks, 2026-10-19)
| Item | Value |
|---|---|
| WordPress | 7.1.3 |
| WooCommerce | 10.7.0 |
| PHP | 8.3.33 |
| Polylang | 3.8.4 (free) |
| Polylang languages | `vi` (default), `en` (en_GB), `de` (de_DE). Pages are already translated under `/en/` and `/de/`. |
| Polylang for WooCommerce | **not installed**. `wc/v3` products have no `lang` / `translations` fields. |
| Other related plugins | `pll-bulk-link-translations` v1.0 **and** v2.0 are both active (custom; origin unknown) |
| Pre-order plugin | none (pre-order = managed stock 0 + backorders "yes") |
| Cache | LiteSpeed Cache 7.9.1 |

All products exist once and in Vietnamese only. The approved EN/DE texts live in
`product_contents` (language `en` / `de`) and are not sent to WooCommerce.

## 2. Compatibility
- Polylang for WooCommerce 2.2's published requirements are WordPress ≥ 6.5,
  WooCommerce ≥ 9.2 and Polylang ≥ 3.7. The site meets them.
- **Open questions for the vendor before purchase**:
  - The vendor states that free Polylang is supported (see 2a). The open
    question is whether 2.2.7 is tested with *free* Polylang 3.8.4 together
    with WooCommerce 10.7. Some 2.2.x fixes reference Polylang Pro. Staging
    test T1–T9 answers this either way.
  - Does it work with HPOS on this site?
- **Conflict risk**: the two active `pll-bulk-link-translations` versions must be
  reviewed, and one of them deactivated, before testing. They may link
  products independently.
- The vendor states that prices, SKU, stock, attributes, categories, featured
  image and gallery are synchronised across translations. This must be
  **proven on staging**, because shared stock and a single price are the
  reasons for using the add-on at all.

## 2a. Licensing (vendor pages, re-checked 2026-10-19)
- Latest release: Polylang for WooCommerce **2.2.7** (2026-10-12, security fix).
  2.2.6 adapts to WooCommerce 11.2, so 10.7 is inside the supported range.
- The vendor states that the add-on works with **Polylang (free) or Polylang Pro**.
  Pro is only needed for DeepL machine translation, XLIFF import/export, the
  2.2.2 REST discoverability of the taxonomy `lang` field, and variation
  disabling. TSYC needs none of these: TSYC supplies its own approved EN/DE text.
- Price: €99/year for 1 site, with renewals at 50%. The Business Pack bundles it
  with Polylang Pro. Without renewal the plugin keeps working, but there are no
  updates or support.
- One licence also covers a **staging** site and localhost at no extra cost. A
  full refund is possible within 30 days, after a support ticket.
- HPOS compatibility was declared in 1.9 / 1.9.3. Batch REST support for
  products arrived in 2.2.

## 2b. Where approved translations live until then
Approved EN/DE texts stay in `product_contents` (one row per language,
`content_status = APPROVED`) and in the gitignored editorial packages. The
APPROVED overwrite protection keeps them safe. Nothing is pushed to
WooCommerce until stage 5. For FAHASA-2026-002 on 2026-10-19 there are 14
products × vi/en/de = 42 APPROVED rows.

## 3. Target model
One logical book is one translation group: the VI product is the source,
and EN and DE are linked translations of it.
- SKU, stock (qty 0 / backorders "yes"), price (owner-set, once), images,
  attributes and categories are synchronised by the add-on.
- Only the name, description and short description differ per language. They
  come from the APPROVED `product_contents` rows for `en` / `de`.
- Never create independent EN/DE products. They would duplicate SKUs and stock.

## 4. Staged rollout (each stage needs owner go-ahead)
1. **Staging copy** of the live site (database + files). Never test on live.
2. On staging, deactivate one `pll-bulk-link-translations` copy (owner
   decides which), then install Polylang for WooCommerce.
3. On staging, **assign `vi` to every existing product** (Polylang bulk
   language assignment). Check that the shop, cart, checkout, the `/en/`
   and `/de/` URLs and LiteSpeed caching still behave.
4. On staging, take one TSYC draft (e.g. #4868) and create its EN and DE
   translations through the REST API. Read them back and verify:
   - SKU, stock and backorders are shared, and changing VI stock changes
     EN/DE stock;
   - setting a test price on VI propagates;
   - the gallery and attributes are synchronised;
   - `status` stays `draft`;
   - TSYC's exact-SKU duplicate check still finds exactly one
     *translation group*. Today it assumes one product per SKU.
   Staging pass/fail matrix. Every row must pass before stage 5:
   | # | Test | Pass condition |
   |---|---|---|
   | T1 | `GET products/4868` after install | `lang = vi`, `translations = {vi: 4868}` |
   | T2 | `POST products` with `lang=en`, `translations={vi:4868}`, `status=draft` and no price fields | new ID; status draft; same SKU accepted |
   | T3 | `GET products?sku=<sku>` | returns the whole group; TSYC must then filter by `lang=vi` |
   | T4 | Set VI stock 0 → 1 → 0 | EN/DE `stock_quantity` follow; backorders stay `yes` |
   | T5 | Set a test price on VI only | EN/DE price follow (proves one price) |
   | T6 | Gallery/attributes on VI | copied to EN/DE |
   | T7 | Delete the EN translation | VI untouched; group shrinks |
   | T8 | Front end `/en/` and `/de/` product URLs, cart, checkout | work; LiteSpeed purge correct |
   | T9 | Both `pll-bulk-link-translations` states | the kept copy does not relink products |
5. Only after stage 4 passes: TSYC code changes (offline tests first):
   - `create_woocommerce_translations.py`: for a `DRAFT_CREATED`
     automation-owned draft only, create or update its linked EN/DE
     translations from APPROVED content. Draft-only, no price fields, exact
     product IDs, read-back verification, no blind retry, idempotent.
   - The duplicate/SKU reconciliation (`create_woocommerce_draft.py`,
     `sync_woocommerce_product_status.py`) becomes translation-group aware.
   - Published or owner-edited products are never touched
     (owner-edit protection is unchanged).
6. Production: the owner installs the add-on on the live site. TSYC then
   runs step 5 on drafts only, in bounded batches.

## 5. Not decided by automation
- Purchasing and installing the add-on.
- Which bulk-link plugin to keep.
- Whether EN/DE product URLs and slugs should be translated.
- Whether the 12 already-published FAHASA-2026-001 products get translations.
  Those are owner-maintained.
