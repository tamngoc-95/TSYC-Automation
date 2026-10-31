# Local WordPress integration test (Polylang link mechanism)

Status 2026-10-31: plan only. Nothing here touches production.

## Purpose

This plan proves on an isolated local WordPress what
`POLYLANG_EXISTING_ARCHITECTURE.md` section 7 needs before any EN/DE link
or mapping row can be trusted:
- how the existing hooks behave;
- that one link mechanism reads and writes Polylang's own links.

A local copy replaces the staging site that does not exist. It is enough for
the hook matrix and the endpoint contract. It does not reproduce production
data, so the production numbers stay with the read-only audits.

## 1. What TSYC already has (repository code, offline-tested)

| Step | Code | Status |
|---|---|---|
| VI ownership records | `woocommerce_language_products` (migration 011), `backfill_language_product_mapping.py` | 180 vi rows live (86 `RECORDED`, 94 `REMOTE_REMOVED`) |
| Mapping audit with explicit blocked books | `audit_language_product_mapping.py` 1.1.0 | `blocked_books`: CAN-0033, CAN-0010, CAN-0031 |
| Storefront link graph | `audit_polylang_catalog.py`, `src/domain/polylang_link_graph.py` | hreflang only, published pages only |
| EN/DE candidate discovery | `discover_translation_candidates.py`, `src/domain/translation_discovery.py` | read-only, plans no row without `VERIFIED_LINK` |
| Group resolution and write planning | `src/domain/multilingual_adapter.py` | fail-closed: `UNVERIFIABLE` without a staging-proven map |
| Link-map client | `src/services/polylang_link_reader.py` (`GET tsyc/v1/translations/{id}`) | strict parser; `require_staging_target` refuses the production host and non-https URLs |
| Create-and-link writer | `TranslationDraftWriter` (contract only) | **not implemented on purpose** |
| Hook-assumption tests | `tests/support/fake_polylang_store.py` | each hook is a switch; this proves TSYC's rules, not the live hooks |

Missing pieces:
- the endpoint itself;
- the hook source;
- an isolated WordPress to run them in;
- an EN/DE mapping writer. That writer is written only after the matrix below passes.

## 2. Inputs from the website developer (the only real dependency)

1. A zip of `wp-content/themes/flatsome-child/` and both
   `pll-bulk-link-translations*` plugin folders. TSYC reviews the zip offline
   and lists every `add_action`/`add_filter`, together with the fields each
   one copies and the direction of the copy.
2. The Flatsome parent theme for a local install, under the developer's own
   licence. TSYC does not buy or download it.
3. How the existing EN/DE copies were made (Duplicate Post RB, Polylang "+",
   or by hand). Also, whether the bulk-link plugins run on `save_post` or only
   from an admin screen.
4. The endpoint (option B, as a mu-plugin) built on Polylang's public PHP API
   only:
   - `GET tsyc/v1/translations/{id}`, wrapping `pll_get_post_translations` and `pll_get_post_language`;
   - later `POST tsyc/v1/translations`: draft only, idempotent per `request_id`, no price, stock or SKU fields.

TSYC asks for nothing on production: no plugin purchase, no theme setting
and no change to a link.

## 3. Environment (local, isolated)

- Docker `wordpress` + `mariadb` (or `wp-env`), on an https hostname such as
  `https://tsyc-local.test` (mkcert). `require_staging_target` accepts only
  https and refuses the production host.
- Versions matching production:
  - WordPress current;
  - WooCommerce 10.7.0 with HPOS on;
  - Polylang 3.8.4 free, languages vi (default, prefix hidden), en (en_GB), de (de_DE), `sync: []`;
  - Duplicate Post RB 1.6.8;
  - Flatsome + Flatsome Child;
  - both bulk-link plugins.
- **No production database copy, no customer or order data.** All data is synthetic:
  - one TSYC-style VI draft carrying every `_tsyc_*` key;
  - one published VI+EN+DE group sharing a SKU;
  - one VI+EN+DE group without a SKU;
  - one unlinked EN copy.
- Payments: a test gateway only. Mail is off (mail catcher) and outbound feeds are off.
- New API keys used only locally. Never reuse production keys or `.env` values.

## 4. Test matrix (each step is read back with the TSYC read-only tools)

| # | Step | Expected evidence | TSYC tool |
|---|---|---|---|
| L1 | Baseline crawl | groups match the seed data | `audit_polylang_catalog.py` |
| L2 | `GET tsyc/v1/translations/{id}` on every seed product | the same IDs as hreflang for published members; drafts included | `EndpointLinkReader` |
| L3 | Endpoint failures: 401, 404, malformed body, wrong ID | `LinkReadError`, never "no translations" | `EndpointLinkReader` |
| L4 | Create EN with "+" from the TSYC VI draft | does `_tsyc_*` get copied? (`COPIED_OWNERSHIP_META` expected if so) | `audit_language_product_mapping.py`, `discover_translation_candidates.py` |
| L5 | Duplicate Post RB copy of a TSYC product, then a SKU edit | reproduces the #4858 pattern; reported as `IDENTITY_CONFLICT` | discovery |
| L6 | Change VI stock; then order EN; then two parallel orders | which stock moves (inventory authority) | catalog audit drift |
| L7 | Change VI price; then save EN only | does any copy overwrite VI? | catalog audit drift |
| L8 | Save VI with both bulk-link plugins active | any relink | L1 diff |
| L9 | Backorders `yes` on VI | does EN/DE follow? | catalog audit |
| L10 | Discovery with the endpoint map attested for the local run | `VERIFIED_LINK` only where map, SKU and hreflang agree; the mapping plan lists only those | discovery + adapter |

Record each run in a dated file (versions, seed IDs, results) under
`data/output/polylang_local/`. That folder is gitignored.

## 5. Exit criteria before any production step

- L1–L10 are documented. The hook direction and the inventory authority are known.
- The endpoint passes L2/L3. The exact mechanism version is attested as
  `verified_on_staging` (the attestation is the recorded local run).
- The owner and developer decide:
  - the price and stock authority;
  - whether the bulk-link plugins stay active;
  - what to do with CAN-0003 and CAN-0033.
- Only then is an EN/DE mapping writer written. It inserts `POLYLANG_LINKED`
  rows for `VERIFIED_LINK` candidates only, as a bounded, checkpointed batch
  with rollback, following the VI backfill pattern.
