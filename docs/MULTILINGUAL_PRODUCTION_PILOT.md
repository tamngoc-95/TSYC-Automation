# Single-book VI/EN/DE production pilot (prepared 2026-11-27, not executed)

Status: **prepared, blocked on the technical gate, not authorized**. Nothing in this file has been
written to WooCommerce, WordPress, Polylang or Supabase.

## 1. Read-only production assessment (2026-11-27)

Pilot candidate: **TSYC-FAHASA-2026-006-CAN-0001**, *Đèn Lồng Của Hải Thần*, VI draft **#5416**.

| Requirement | Evidence |
|---|---|
| Draft status | `draft` (GET context=edit) |
| No selling price | regular, sale and price all empty |
| No orders | `orders?product=5416&status=any`: 0 |
| No owner edits | `date_modified_gmt` 2026-11-26T07:21:40 equals the create time |
| Approved VI/EN/DE | all three APPROVED; validators pass (VI score 100/85, EN/DE translation gate pass) |
| Identity and images | IDENTITY_VERIFIED; post-create verifier VERIFIED; 1 cover image |
| No EN/DE copies or links | the only product with its SKU is #5416; translation-group audit shows 0 multi-row groups |
| Mapping | `audit_language_product_mapping.py --remote --plan-backfill`: NOT_STORED, no findings; backfill dry run: 1 INSERT, not owner-maintained |
| Loader | `translation_book_input.build_book_input` with the planned VI row: READY; EN/DE render through the production renderer with localized headings |

Why this book: it is a standalone title (no in-batch series sibling), has one image, and the shortest
evidence chain of the 11 drafts.

## 2. What the live site can and cannot do

Read-only facts from `wp-json/` and `wc/v3/system_status`:

- WordPress 7.1.5, WooCommerce 11.2.1, free Polylang 3.9.1, Flatsome Child theme. Both `pll-bulk-link-translations` plugins, Duplicate Post RB and All-in-One WP Migration are active.
- `pll/v1` exists but only offers `languages` and `settings`. Neither `wp/v2/product` nor `wp/v2/posts` exposes a `lang` or `translations` field.
- **No REST path sets a product's language or links translations.** A product created with generic `wc/v3` POST becomes an unlinked Vietnamese product, a second VI product, which is why it is never used for EN/DE.
- The only native ways to create a linked translation are:
  1. The wp-admin "+" button. This is the owner's own manual path; the theme's save hooks run on it.
  2. PHP calling `pll_set_post_language` and `pll_save_post_translations`. That is the TSYC bridge.
- There is no `tsyc/*` route on production. The bridge and the Python writer both refuse the production host by design.

The TSYC adapter (`polylang_bridge_writer` plus `translation_draft_runner`) reproduces the native
behaviour correctly in the lab: Gate A passed 15 of 15. That lab has no Flatsome Child and no bulk-link
plugins, and those are the code that may move stock, SKU or meta between linked copies on the real
site. Gate A on a copy of the real site is still `BLOCKED_REAL_SITE_TEST`.

## 3. Minimum changes needed, and a path that does not need the developer

**Step 1. Real-site copy, self-managed.** All-in-One WP Migration is already active on the store.
1. The owner exports the site to a `.wpress` file in wp-admin. The media library may be excluded; themes and plugins must stay in.
2. The owner places the file under the gitignored `data/raw/site_copy/`.
3. TSYC imports it into a fresh local lab. Outbound HTTP is blocked and mail is off.
4. TSYC sets the isolation attestation (I1–I4) and runs the existing `tools/site_blackbox/` Gate A site checks (A05, A06, A07, A08, A10, A11, A15) on #5416's copy, with the trio made by the bridge.

No production write. Not yet verified: whether the lab imports a `.wpress` cleanly. Fallback: unpack the archive's SQL and files directly. Lab PHP blocks `php_curl` and `php_fileinfo`.
- The copy holds customer and order data on this machine. It stays gitignored, is never committed or uploaded, and is deleted after the run.

**Step 2. Production enablement, only if step 1 passes.**
1. A reviewed TSYC code change: a production pilot mode in the bridge and the Python writer, limited to an allowlisted source ID (#5416), with an expiry and a new 32+ character token. Today both refuse production unconditionally.
2. The owner uploads the bridge in wp-admin (Plugins, Upload).
3. The owner adds the pilot constants to `wp-config.php` through the hosting file manager.
4. The owner removes the constants afterwards.

No stock or price synchronization is built or changed.

## 4. Pilot write, when authorized

Exactly two creates, one language at a time (EN first, then DE), through `tsyc/v1/translations`.
Each new draft gets:
- name, description and short description from the approved package (`expected_translations.json`);
- status `draft`, `manage_stock` true, stock 0, backorders `yes`;
- image, gallery, weight and dimensions copied server-side from #5416;
- attribute values from #5416, with localized labels only;
- categories and tags translated through Polylang, with a warning when no translation exists;
- `_tsyc_*` ownership meta;
- the Polylang language, then the group {vi: 5416, en, de}.

Never sent or set: any price field, an SKU, a publish status, or a write to #5416 or any other existing
product. Supabase afterwards: only the VI mapping row (backfill, 1 row) and the EN/DE mapping rows that
the runner records from confirmed answers.

## 5. Read-back checks (all must hold; any miss stops the pilot)

1. Three IDs: 5416 plus the new EN and DE IDs, read via `tsyc/v1` from each member. The group is identical from all three (A05, A06).
2. Language: vi/en/de exactly once each.
3. All three: `draft`, `manage_stock` true, stock 0, backorders `yes`, empty regular, sale and price (A07, A08).
4. EN/DE stored name and description equal the approved payloads (A09). Images and gallery equal #5416's (A10).
5. #5416 unchanged: status, SKU, stock, backorders, prices, images, text and `date_modified_gmt` (A15).
6. Whole catalog: every other product's id, sku, status, date_modified_gmt, prices, stock and backorders are identical to the before-snapshot (A11). Fingerprint 2026-11-27: 1987 products, sha256 `bc199dff093eb459…`. Retake it immediately before the write.
7. No `tsyc_sibling_side_effect` on any member.

## 6. Stop and recovery

- Any refused, uncertain or timed-out answer means stop. Read #5416's group and the SKU and request ID back first. **No retry.**
- A confirmed side effect or snapshot difference means stop. Preserve the before/after snapshots and the bridge's `_tsyc_side_effects` meta. Nothing is repaired automatically.
- Rollback, only if the owner asks: `tsyc/v1/translations/rollback` unlinks and trashes one unchanged bridge draft. It refuses anything non-draft or changed. #5416 is never touched.

Pack (gitignored): `data/output/multilingual_pilot/20261127T124722Z_TSYC-FAHASA-2026-006-CAN-0001/`, holding
`before_vi_product.json`, `before_catalog.json` and `expected_translations.json`.

## 7. Authorization requested (two separate decisions)

**A. Now.** "I authorize exporting tiemsachyeucon.com with All-in-One WP Migration and running Gate A
on a local, isolated copy for #5416. No production write."

**B. Later, only after A passes.**
"I authorize one production pilot on tiemsachyeucon.com:
- create EN and DE drafts for TSYC-FAHASA-2026-006-CAN-0001 (#5416) through the TSYC bridge;
- link the three in Polylang;
- no price, no publish, no change to any other product;
- the bridge is removed or disabled after the read-back."
