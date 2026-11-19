# TSYC black-box test package

For the website developer. It shows what the real site does when three linked
language copies of one new pre-order book are created as drafts, **without anyone
handing over theme or plugin source code**.

It works by taking a read-only snapshot of the product catalog before and after
each step and comparing them.

The main question is one command (part 1). The price, stock and order steps (part 2)
are optional and only matter later.

## Rules

- **Run it only on an isolated copy of the site.** Never on `tiemsachyeucon.com`.
  It creates test products. The tool refuses as soon as a snapshot names the
  production host, and the driver refuses to run there.
- The copy must carry the same theme, child theme and plugins as the live site,
  or the result says nothing about the live site.
- The copy must not send mail, take payments, or sync anything back to production.
- Nothing here reads source code, customer data, orders, users or keys.
- Snapshots contain product titles, SKUs, prices, stock, image IDs, translation
  links, SHA-256 hashes of the descriptions (not the descriptions), and the names
  and versions of the theme and plugins. Look through one before you send it.

## Files

| File | What it does |
|---|---|
| `tsyc-site-snapshot.php` | **Read-only.** Prints one JSON snapshot of every product |
| `tsyc-bridge-trio-driver.php` | **Writes test products**: one synthetic VI draft, then EN and DE through the TSYC bridge. Refuses production and any environment but `local` or `staging` |
| `site_blackbox.py` | Runs part 1 in one command, and compares snapshots. Python 3.9+, standard library only. It opens no network connection; the only thing it starts is your WP-CLI command |

## Part 1: does creating the drafts change anything? (one command)

Needs WP-CLI, and the TSYC bridge on the copy: put `tsyc-polylang-bridge.php` in
`wp-content/mu-plugins/`, and in the copy's `wp-config.php`:

```php
define('WP_ENVIRONMENT_TYPE', 'staging');
define('TSYC_BRIDGE_ENABLE_WRITES', true);
define('TSYC_BRIDGE_WRITE_TOKEN', 'any-random-text-of-32-or-more-characters');
```

Then, from this folder, with a new empty folder name for the run:

```
python site_blackbox.py run-gate-a run1 --user YOUR_ADMIN_LOGIN --wp "wp --path=/path/to/the/copy"
```

What it does, in order:

1. Snapshot of the whole catalog (`00_baseline.json`). Stops here if this is production.
2. One synthetic VI draft: stock managed, quantity 0, backorders allowed, **no price** (`00b_vi_created.json`).
3. An EN and a DE draft through the bridge, linked to it.
4. Waits two minutes, so delayed jobs on the site can run (`--wait SECONDS` to change).
5. Snapshot (`01_trio_created.json`).
6. Moves the three test products to the trash (`10_cleanup.json`). Add `--keep` to leave them for part 2.
7. Prints the result and writes `run1/blackbox_report.json`.

**Please send back the whole `run1` folder.**

If the bridge stops with `tsyc_sibling_side_effect`, **that is the result**. Nothing
is retried. It means something on the site changed another product while the copy
was being linked, and the report names what.

### Reading the result

```
Draft creation on this site (Gate A): PASS
  vi_draft: price UNCHANGED; stock UNCHANGED; backorders UNCHANGED; sku UNCHANGED; images UNCHANGED; ...
  every_other_product: price UNCHANGED; stock UNCHANGED; ...
```

- `vi_draft`: did creating and saving EN and DE change the VI draft itself?
- `every_other_product`: did it change any other product in the catalog?

Each line covers price, stock, backorders, SKU, images, status, translation links,
owner-maintained fields (title, slug, texts, categories, attributes, up-sells,
cross-sells, weight) and "saved again" (a save that changed no field still shows).

| Result | Meaning |
|---|---|
| `PASS` | Three linked, unpriced pre-order drafts, and nothing else changed |
| `FAIL` | Something else changed, or the drafts are not as intended. The report says which products and fields |
| `BLOCKED_REAL_SITE_TEST` | No valid run: the bridge refused, a step is missing, or the trio was made by hand. **Never read as a pass** |

Exit codes, for `run-gate-a` and `evaluate` alike: `0` PASS, `1` FAIL, `2` unusable
snapshot file, `4` blocked. Only a `PASS` exits `0`; a blocked run (a missing step, a
trio made by hand, a production snapshot) is always `4`.

Status: the whole package, including the one-command run, has been run end to end on
TSYC's local test stack (plain WordPress, WooCommerce and Polylang, MariaDB and SQLite,
last on 2026-11-19). It has **not** been run on a copy of the real site yet, so the
real-site result is still `BLOCKED_REAL_SITE_TEST`.

## Part 2 (optional): price, stock and orders

Not needed for a pilot that only creates unpriced drafts. It describes how the site
syncs the copies once they are priced and sold.

Run part 1 with `--keep`, then do each step in wp-admin as you normally would. After
each step wait two minutes, then take a snapshot **into the same folder** with exactly
this file name:

```
wp eval-file tsyc-site-snapshot.php 02_price_on_vi > run1/02_price_on_vi.json
```

Without WP-CLI: `php tsyc-site-snapshot.php --wp-load=/path/to/wp-load.php 02_price_on_vi > run1/02_price_on_vi.json`

| File | Do this first |
|---|---|
| `02_price_on_vi.json` | Enter regular price `111000` on the **VI** product only. Save |
| `03_price_on_en.json` | Enter regular price `222000` on the **EN** product only. Save |
| `04_stock_on_vi.json` | Enter stock quantity `5` on the **VI** product only. Save |
| `05_backorders_on_de.json` | Set backorders to "Do not allow" on the **DE** product only. Save |
| `06_published.json` | Set DE backorders back to "Allow", then publish all three |
| `07_order_on_en.json` | Place one order, quantity 1, of the **EN** product (a test payment method) |
| `08_order_on_de.json` | Place one order, quantity 1, of the **DE** product |
| `09_concurrent_orders.json` | Two browsers, same moment: one orders **VI**, one orders **EN**, quantity 1 each |
| `10_cleanup.json` | `wp eval-file tsyc-bridge-trio-driver.php cleanup VI_ID --user=YOUR_ADMIN_LOGIN`, cancel the test orders |

Then:

```
python site_blackbox.py evaluate run1
```

The second block of the report (Gate B) says what the other two copies did after one
was priced, stocked or ordered: `OBSERVED`, or `BLOCKED` for a step you left out. The
tool does not decide whether the behaviour is right; the shop owner and you do.

## Doing it by hand instead

If the bridge is not installed you can still make the trio yourself: a new VI draft
as in step 2 above, snapshot `00b_vi_created.json`, then Polylang's "+" for EN and DE,
each saved as a draft, then `01_trio_created.json`. This shows how the site's own path
behaves and is useful for part 2. For part 1 it is reported `BLOCKED_REAL_SITE_TEST`,
with what was observed, because it does not test the bridge.

`python site_blackbox.py diff A.json B.json` shows the raw difference between any two
snapshots.
