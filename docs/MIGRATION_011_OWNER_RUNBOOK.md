# Migration 011 — owner runbook (Supabase SQL Editor)

Creates `public.woocommerce_language_products`, the local record of which
WooCommerce product is each book's vi / en / de storefront copy
(docs/POLYLANG_EXISTING_ARCHITECTURE.md §5a).

Claude Code cannot apply it: its Supabase connection is read-only and the
project has no other DDL path. Migrations 001–010 were also run by hand in
the SQL Editor (README).

**What it changes:** it adds one new, empty table and three indexes. It
changes no existing table and no existing row. WooCommerce and Polylang
are not touched.

**Side effect:** while a mapping row points at an internal product, a sync
row or a content row, that row cannot be deleted (`on delete restrict`).

## 1. Pre-migration check (read-only)

Expected: `0 | 0 | 3 | t`. Claude Code ran this on 2026-10-30 and got that
result.

```sql
select
  (select count(*) from pg_class
    where relname in ('woocommerce_language_products',
                      'idx_woocommerce_language_products_product_id',
                      'idx_woocommerce_language_products_request_id',
                      'idx_woocommerce_language_products_status')) as name_collisions,
  (select count(*) from pg_constraint
    where conname like 'woocommerce_language_products%') as constraint_collisions,
  (select count(*) from pg_constraint
    where contype = 'p'
      and conrelid in ('public.internal_products'::regclass,
                       'public.woocommerce_product_syncs'::regclass,
                       'public.product_contents'::regclass)) as referenced_primary_keys,
  exists (select 1 from pg_proc where proname = 'gen_random_uuid') as has_gen_random_uuid;
```

Stop if `name_collisions` or `constraint_collisions` is not 0, and report
the result. Do not edit or drop anything to make the check pass.

## 2. The migration (run exactly this)

This is `migrations/011_create_woocommerce_language_products.sql`,
unchanged. A test (`tests/test_migration_011_runbook.py`) keeps this copy
identical to the file.

Every statement uses `if not exists`. If the run stops part-way, run the
whole script again: statements that already ran are skipped.

<!-- migration-011:begin -->
```sql
-- Map one internal product (one physical book) to its WooCommerce
-- product per storefront language (vi / en / de).
--
-- Each language copy is a separate WooCommerce product ID (Polylang,
-- docs/POLYLANG_EXISTING_ARCHITECTURE.md). woocommerce_product_syncs keeps
-- one row per internal product (the VI draft TSYC created); this table is
-- the per-language record the multilingual adapter needs:
--   * TSYC_CREATED rows are products TSYC itself created (proof of
--     ownership is this record, never copied `_tsyc_*` meta);
--   * POLYLANG_LINKED rows are existing copies proven by a staging-proven
--     Polylang link reader; they are never TSYC-owned and never written.
--
-- language_code is the storefront language, not the book's own language
-- (internal_products.language_code). Additive only: no existing table or
-- row changes. Rows are written by approved TSYC scripts only.

create table if not exists public.woocommerce_language_products (
    language_product_id uuid
        primary key
        default gen_random_uuid(),

    internal_product_id uuid
        not null
        references public.internal_products(internal_product_id)
        on delete restrict,

    language_code text
        not null,

    woocommerce_product_id bigint,

    origin text
        not null,

    mapping_status text
        not null
        default 'PENDING_CREATE',

    -- The VI row points at the existing sync row it was derived from.
    sync_id uuid
        references public.woocommerce_product_syncs(sync_id)
        on delete restrict,

    product_content_id uuid
        references public.product_contents(product_content_id)
        on delete restrict,

    -- Idempotency key of a TSYC create request (EN/DE).
    request_id text,

    -- Remote state right after TSYC's last confirmed write.
    baseline_source text,
    baseline_modified_gmt text,
    baseline_text jsonb,

    link_mechanism text,
    link_verified_at timestamptz,
    link_evidence jsonb
        not null
        default '{}'::jsonb,

    created_at timestamptz
        not null
        default now(),

    updated_at timestamptz
        not null
        default now(),

    constraint woocommerce_language_products_language_key
        unique (internal_product_id, language_code),

    constraint woocommerce_language_products_language_check
        check (language_code in ('vi', 'en', 'de')),

    constraint woocommerce_language_products_origin_check
        check (origin in ('TSYC_CREATED', 'POLYLANG_LINKED')),

    constraint woocommerce_language_products_status_check
        check (
            mapping_status in (
                'PENDING_CREATE',
                'RECORDED',
                'REMOTE_REMOVED',
                'CONFLICT'
            )
        ),

    constraint woocommerce_language_products_baseline_source_check
        check (
            baseline_source is null
            or baseline_source in ('CREATE_RESPONSE', 'TSYC_WRITE_RESPONSE')
        ),

    -- A recorded mapping always names its Woo product.
    constraint woocommerce_language_products_recorded_id_check
        check (
            mapping_status = 'PENDING_CREATE'
            or woocommerce_product_id is not null
        ),

    -- Only TSYC's own VI row derives from the sync row.
    constraint woocommerce_language_products_sync_check
        check (
            sync_id is null
            or (language_code = 'vi' and origin = 'TSYC_CREATED')
        ),

    -- A TSYC translation is created with an idempotency key.
    constraint woocommerce_language_products_request_check
        check (
            origin <> 'TSYC_CREATED'
            or language_code = 'vi'
            or request_id is not null
        ),

    -- A linked copy is recorded only with its proven link.
    constraint woocommerce_language_products_link_check
        check (
            origin <> 'POLYLANG_LINKED'
            or (link_mechanism is not null and link_verified_at is not null)
        )
);


create unique index if not exists
idx_woocommerce_language_products_product_id
on public.woocommerce_language_products(
    woocommerce_product_id
)
where woocommerce_product_id is not null;


create unique index if not exists
idx_woocommerce_language_products_request_id
on public.woocommerce_language_products(
    request_id
)
where request_id is not null;


create index if not exists
idx_woocommerce_language_products_status
on public.woocommerce_language_products(
    mapping_status,
    updated_at
);


alter table public.woocommerce_language_products enable row level security;
```
<!-- migration-011:end -->

## 3. Post-migration verification (read-only)

Expected:
- `columns = 17`;
- `primary_keys = 1`, `foreign_keys = 3`, `unique_constraints = 1`;
- `checks = 8`;
- `indexes = 5`;
- `rls = true`;
- `rows = 0`.

```sql
select
  (select count(*) from pg_attribute
    where attrelid = 'public.woocommerce_language_products'::regclass
      and attnum > 0 and not attisdropped) as columns,
  (select count(*) filter (where contype = 'p') from pg_constraint
    where conrelid = 'public.woocommerce_language_products'::regclass) as primary_keys,
  (select count(*) filter (where contype = 'f') from pg_constraint
    where conrelid = 'public.woocommerce_language_products'::regclass) as foreign_keys,
  (select count(*) filter (where contype = 'u') from pg_constraint
    where conrelid = 'public.woocommerce_language_products'::regclass) as unique_constraints,
  (select count(*) filter (where contype = 'c') from pg_constraint
    where conrelid = 'public.woocommerce_language_products'::regclass) as checks,
  (select count(*) from pg_indexes
    where schemaname = 'public' and tablename = 'woocommerce_language_products') as indexes,
  (select relrowsecurity from pg_class
    where oid = 'public.woocommerce_language_products'::regclass) as rls,
  (select count(*) from public.woocommerce_language_products) as rows;
```

Then tell Claude Code that migration 011 is applied. It re-runs this check
itself, and `audit_language_product_mapping.py` then reports `PRESENT`.

## 4. Recovery

- **Nothing is written to the table before it is verified.** Until the
  backfill runs, the table is empty and nothing reads it.
- **Undo before any backfill:**
  `drop table if exists public.woocommerce_language_products;`
  This removes only the new, empty table. Run it only if the
  verification in step 3 fails and you want a clean retry.
- **Undo after a backfill:** do not drop the table. Use the backfill's
  run-scoped rollback, which deletes only that run's rows that are still
  unchanged:

  ```
  scripts/backfill_language_product_mapping.py --rollback <run_dir> --confirm-rollback
  ```

## 5. Backfill write plan (after step 3 passes)

Claude Code runs the plan below under the shop owner's single
authorization. It writes Supabase only, never WooCommerce or Polylang.

1. Fresh read-only audit:
   `audit_language_product_mapping.py --remote --plan-backfill`.
2. Dry run of both groups against that audit's allowlist. The counts must
   match the plan below, or the run stops and reports instead.
3. `--group removed --max-products 94 --confirm-insert`.
4. `--group recorded --max-products 86 --confirm-insert`.
5. Re-audit (expected `PRESENT`, 180 stored rows, no STORED_DIFFERS
   errors), then pipeline audit and preflight.

| Group | Rows | Status written | Origin |
|---|---|---|---|
| removed | 94 | `REMOTE_REMOVED` (terminal, never recreated) | `TSYC_CREATED` |
| recorded | 86 | `RECORDED` | `TSYC_CREATED` |

Never written:
- CAN-0033 (#4657): #4858 is a hand copy carrying TSYC's ownership meta
  (TSYC_RUNBOOK §7b). It needs an owner decision.
- CAN-0010 (#3772): recorded as removed, but it reads `publish` (owner
  decision, see the Woo remote-status audit).
- CAN-0031: it has no Woo ID (recovery tools).
- All EN/DE rows. The 15 copies that share a VI SKU are not links. Only a
  staging-proven Polylang link reader may map them.
