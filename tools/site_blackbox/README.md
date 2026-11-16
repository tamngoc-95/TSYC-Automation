# TSYC black-box test package

For the website developer. It shows how the real site behaves when three linked
language copies of one new pre-order book are created, priced, stocked and
ordered, **without anyone handing over theme or plugin source code**.

It works by taking a read-only snapshot of the product catalog before and after
each step and comparing them.

## Rules

- **Run it only on an isolated copy of the site.** Never on `tiemsachyeucon.com`.
  The steps create, price, publish and order test products. The comparison tool
  refuses snapshots taken on the production host, and the driver refuses to run there.
- The copy must not send mail, take payments, or sync anything back to production.
- Nothing here reads source code, customer data, orders, users or keys.
- Snapshots contain product titles, SKUs, prices, stock, image IDs, translation
  links and SHA-256 hashes of the descriptions (not the descriptions). Look through
  one before you send it.

## Files

| File | What it does |
|---|---|
| `tsyc-site-snapshot.php` | **Read-only.** Prints one JSON snapshot of every product |
| `site_blackbox.py` | Compares snapshots and writes the report. Python 3.9+, standard library only, no network |
| `tsyc-bridge-trio-driver.php` | Optional, **writes test products**: creates the test trio through the TSYC bridge. Needs the bridge installed on the copy with writes enabled |

## Taking a snapshot

With WP-CLI, in the WordPress folder of the copy:

```
wp eval-file tsyc-site-snapshot.php 00_baseline > run1/00_baseline.json
```

Without WP-CLI:

```
php tsyc-site-snapshot.php --wp-load=/path/to/wp-load.php 00_baseline > run1/00_baseline.json
```

Use exactly the file names in the table below, all in one folder. After each step,
**wait two minutes before the snapshot**, so scheduled or delayed jobs have run.

## The steps

Use a made-up book. Do every step in wp-admin as you normally would, except step 01
in variant A.

| File | Do this first |
|---|---|
| `00_baseline.json` | Nothing |
| `01_trio_created.json` | Create the trio: see below |
| `02_price_on_vi.json` | Enter regular price `111000` on the **VI** product only. Save |
| `03_price_on_en.json` | Enter regular price `222000` on the **EN** product only. Save |
| `04_stock_on_vi.json` | Enter stock quantity `5` on the **VI** product only. Save |
| `05_backorders_on_de.json` | Set backorders to "Do not allow" on the **DE** product only. Save |
| `06_published.json` | Set DE backorders back to "Allow", then publish all three |
| `07_order_on_en.json` | Place one order, quantity 1, of the **EN** product (a test payment method) |
| `08_order_on_de.json` | Place one order, quantity 1, of the **DE** product |
| `09_concurrent_orders.json` | Two browsers, same moment: one orders **VI**, one orders **EN**, quantity 1 each |
| `10_cleanup.json` | Move the three test products to the trash, cancel the test orders |

Any step can be left out. The checks that need it are then reported `BLOCKED`,
never passed.

### Step 01: creating the trio

**Variant A, through the TSYC bridge** (this is the one that answers "is TSYC's
automation safe on this site?"). Needs `tsyc-polylang-bridge.php` in `mu-plugins`
on the copy, and in the copy's `wp-config.php`:

```php
define('WP_ENVIRONMENT_TYPE', 'staging');
define('TSYC_BRIDGE_ENABLE_WRITES', true);
define('TSYC_BRIDGE_WRITE_TOKEN', 'any-random-text-of-32-or-more-characters');
```

Then:

```
wp eval-file tsyc-bridge-trio-driver.php create --user=YOUR_ADMIN_LOGIN
```

It prints the three product IDs. If the bridge stops with
`tsyc_sibling_side_effect`, **that is the result**: do not retry, take the
snapshot, and send the output. It means something on the site changed another
product while the copy was being linked.

**Variant B, by hand** (shows how the site's own path behaves; useful for the
price and stock steps even if the bridge is not installed): add a new VI product
as a draft with "Manage stock" on, quantity `0`, backorders "Allow", **no price**,
then use Polylang's "+" for EN and for DE and save each as a draft.

## The report

```
python site_blackbox.py evaluate run1
```

It prints two blocks and writes `run1/blackbox_report.json`. Please send that file
and the snapshot folder.

- **Gate A on this copy**: the three new products are linked to each other, are
  unpriced pre-order drafts, and **no other product changed at any step**. `PASS` or
  `FAIL`. For a hand-made trio (variant B) these lines are `BLOCKED`, with what was
  observed, because that does not test the bridge.
- **Gate B**: what happened to the other two copies when one was priced, stocked or
  ordered. Always `OBSERVED` or `BLOCKED`. The tool does not decide whether the
  behaviour is right; the shop owner and you do.

`python site_blackbox.py diff A.json B.json` shows the raw difference between any
two snapshots.

Exit codes of `evaluate`: `0` evaluated with no Gate A failure, `1` a Gate A check
failed, `2` a snapshot file is unusable, `4` the run is not valid (production host,
mixed hosts, missing baseline, or the trio could not be identified).
