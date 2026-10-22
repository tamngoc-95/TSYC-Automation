# Polylang for WooCommerce: implementation plan (NOT implemented)

Status: plan only (2026-10-19). Nothing in this plan has been installed or
purchased on any site. Every step that changes the live site needs the
owner's explicit decision.
2026-10-22: offline groundwork only. That is the read-only audit, the
translation-group domain rules, the payload/planner and the offline tests.
No staging exists, so no stage-4 test has run.

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

~~All products exist once and in Vietnamese only.~~ **Corrected 2026-10-22**
(read-only re-check, section 1a). The approved TSYC EN/DE texts live in
`product_contents` (language `en` / `de`) and TSYC does not send them to
WooCommerce.

## 1a. Re-check 2026-10-22 (read-only GETs only, nothing written)
| Item | Value |
|---|---|
| WordPress / WooCommerce / PHP | 7.1.3 / 10.7.0 / 8.3.36 |
| Polylang | 3.8.4 free. REST `pll/v1` exposes `languages` and `settings` only |
| Polylang for WooCommerce | still **not installed**; `wc/v3` products still have no `lang` / `translations` |
| HPOS | enabled, compatibility sync off |
| Staging | **none found**: no staging variable in code or config, `staging./stage./dev./test.` subdomains do not resolve, `wp_environment_type` not reported. Production must never stand in for staging |
| Backup tooling on live | All-in-One WP Migration 7.112 (can produce the staging copy) |
| Live payment / mail plugins | WooPayments, PayPal Payments, WP Mail SMTP, MailPoet, Google/TikTok/Reddit/Snapchat feeds. A staging clone must disable all of them (section 4a) |

**The `product` post type is already translatable in free Polylang.** The live
catalog holds 1,986 products: 874 VI (unprefixed), 554 EN (`/en/product/…`) and
558 DE (`/de/product/…`), all published except 15 VI drafts. Product
categories are translated too (30 vi / 25 en / 21 de). These are
**independent per-language products**: each has its own price, stock and
backorders, and nothing synchronises them.

8 TSYC SKUs already exist as VI + EN (+ DE) copies sharing one SKU
(`scripts/audit_woo_translation_groups.py`, 0 errors, 11 warnings):
- `TSYC-FB-2026-001-CAN-0003`: VI #3311 is a **draft** but EN #4014 and DE #4018
  are **published and sellable**.
- `TSYC-FB-HIST-2026-AUTOIMPORT-CAN-0022`: backorders already drifted
  (VI `notify`, EN/DE `yes`).
- The other 6 groups are consistent today, but only by manual discipline.

Consequences for TSYC (fixed 2026-10-22):
- `create_woocommerce_draft.find_product_by_sku()` used to return the first
  of several exact-SKU matches and reconcile local state onto it. It could
  have recorded an EN/DE copy as TSYC's draft. It now refuses a multi-row SKU
  (CLAUDE.md 5.2). The removal and remote-loss reconcilers already refused
  multiple matches.
- `src/domain/woo_translation_group.py` models one logical book in both
  modes (manual copies today, add-on links later). It never picks a winner in
  an inconsistent group.

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

## 3a. Migrating the existing manual copies
The ~1,100 existing EN/DE products are owner-maintained, and most of them have
no SKU (1,896 of 1,986 products have none). With the add-on installed, Polylang's
existing translation links would make the add-on start synchronising price
and stock **from whichever side is saved next**. Before installing on live,
staging must show what the add-on does to an existing linked VI/EN/DE trio
whose prices or stock differ (add test T10). TSYC never merges, deletes or
relinks the existing copies. That is an owner decision.

## 4a. Staging setup plan (owner action; nothing exists yet)
1. Create a separate host or subdomain (e.g. `staging.tiemsachyeucon.com`) with
   its own database, protected by HTTP basic auth. The hosting panel's
   staging feature is preferred; otherwise use an All-in-One WP Migration
   export and import.
2. Immediately after import, before the first page load:
   - Settings → Reading → "Discourage search engines"; set
     `WP_ENVIRONMENT_TYPE` to `staging` in `wp-config.php`.
   - WooPayments and PayPal Payments → test/sandbox mode or deactivated. Set
     every gateway except "Cash on delivery" to disabled.
   - WP Mail SMTP → "Do not send" / email log only. Pause MailPoet
     sending.
   - Deactivate the Google, TikTok, Reddit and Snapchat feed plugins, the
     Packlink PRO connection and the Jetpack/WooCommerce.com connection
     (they would push staging data to live accounts).
   - LiteSpeed Cache → deactivate or purge-only.
3. Create **new** WooCommerce REST keys and a new WordPress application
   password on staging. Never copy the live keys.
4. Add them to `.env` as `WOOCOMMERCE_STAGING_URL`,
   `WOOCOMMERCE_STAGING_CONSUMER_KEY`, `WOOCOMMERCE_STAGING_CONSUMER_SECRET`.
   The staging runner must refuse to start if the staging URL's host
   equals the live `WOOCOMMERCE_URL` host or resolves to the same IP, or if
   any staging key equals a live key.
5. Isolation proof before any test (read-only): `system_status` shows
   the staging `home_url`, `environment_type = staging`, payment gateways
   disabled, and product IDs ≥ the live maximum or a distinct catalog
   fingerprint.
6. Buy the add-on (€99/yr, covers staging) and install it on staging only.
7. Rollback: the All-in-One export taken in step 1 restores staging. Live
   is never touched.

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
   | T10 | Existing manual trio (e.g. the CAN-0022 clone, whose backorders differ) after install | add-on behaviour documented; no silent price/stock overwrite on the live-equivalent data |

   Offline status (2026-10-22): T1–T7 plus repeat-sync, uncertain-create and
   rollback are covered by `tests/test_woo_translation_group.py` against an
   in-memory model of the vendor contract (`tests/support/fake_pllwc_store.py`).
   That proves TSYC's own rules, **not** the add-on. T1–T10 on real staging
   are still required.
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
- Whether to create the staging site (section 4a).
- What to do about `TSYC-FB-2026-001-CAN-0003`: EN/DE are published while
  VI is a draft. Also whether to align the CAN-0022 backorders setting.
- How the ~1,100 existing manual EN/DE products are brought under the add-on
  (section 3a).
