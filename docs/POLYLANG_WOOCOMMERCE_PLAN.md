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
  - Is the current release tested with WooCommerce 10.7 and *free* Polylang
    3.8.4? Some 2.2.x fixes reference Polylang Pro.
  - Does it work with HPOS on this site?
- **Conflict risk**: the two active `pll-bulk-link-translations` versions must be
  reviewed, and one of them deactivated, before testing. They may link
  products independently.
- The vendor states that prices, SKU, stock, attributes, categories, featured
  image and gallery are synchronised across translations. This must be
  **proven on staging**, because shared stock and a single price are the
  reasons for using the add-on at all.

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
