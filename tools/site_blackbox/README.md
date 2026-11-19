# TSYC black-box test package (Gate A on a copy of the site)

For the website developer of Tiệm Sách Yêu Con.

**The question it answers:** when TSYC creates a new, unpriced pre-order book as
three linked Polylang drafts (VI, then EN and DE), does anything on the real
site's setup (theme, child theme, plugins, custom code) change the VI draft or
any other product as a side effect?

**What it needs from you:** about 15 minutes on an isolated copy of the site.
**No source code is read or sent.** The package takes a read-only snapshot of
the product catalog before and after, and compares them.

---

## 1. Rules (please read first)

- **Only on an isolated copy.** Never on `tiemsachyeucon.com`. The package
  refuses to write anything unless it can establish isolation (section 3). If it
  cannot, it stops before the first write. It does not guess.
- The copy should carry the **same theme, child theme, plugins and versions** as
  the live site. Otherwise the result says nothing about the live site.
- **The copy must not reach the outside world:** no mail, no payments, no
  webhooks, no sync back to production, no stock or marketplace connectors.
- The test data is synthetic: one book named `TSYC blackbox test book <time>`,
  SKU `TSYC-BBX-<time>`. No customer, order or payment is created.
- Nothing reads source code, customers, orders, users, passwords or API keys.
- Nothing is retried. If something stops, that stop is the result.

## 2. Supported environment

| Component | Needed | Tested by TSYC |
|---|---|---|
| WordPress | the live site's version | 7.1.4 |
| WooCommerce | the live site's version | 11.2.1 |
| Polylang (free) | the live site's version | 3.9.1 |
| PHP | 7.4 or newer | 8.3 |
| Database | MySQL / MariaDB (or SQLite) | MariaDB 11.4, SQLite |
| WP-CLI | 2.x, run on the copy's server | 2.12.0 |
| Python | 3.9 or newer, standard library only | 3.12 |

Linux, macOS and Windows work. Python must run where WP-CLI runs (or pass a
`--wp` command that reaches it, for example over `ssh`).

## 3. Isolation: what the package checks (fail closed)

`tsyc-isolation.php` is read-only. All four checks must pass, or nothing is
written:

| Check | Passes when |
|---|---|
| I1 `HOST_NOT_PRODUCTION` | The home and site URL, both as WordPress uses them and as stored in the database, are not `tiemsachyeucon.com` / `www.tiemsachyeucon.com` |
| I2 `ENVIRONMENT_TYPE` | `WP_ENVIRONMENT_TYPE` is `staging` or `local` |
| I3 `OUTBOUND_HTTP_BLOCKED` | `WP_HTTP_BLOCK_EXTERNAL` is `true`, and `WP_ACCESSIBLE_HOSTS` (if set) contains no `*` and no production host |
| I4 `OPERATOR_ATTESTATION` | `TSYC_BLACKBOX_ISOLATED_COPY` is set to **this copy's own host name**. It records that you confirm mail, payments and outbound sync are off. A config copied to another host no longer matches |

The harness also checks that the installed bridge is exactly the
`tsyc-polylang-bridge.php` file from this package (SHA-256; line endings do not
matter). The driver repeats the isolation check itself before every write.

What it cannot check: mail sent directly by SMTP plugins, server cron jobs, or
anything outside WordPress's own HTTP functions. Those are what I4 asks you to
confirm.

## 4. Setup on the copy

1. Make a **backup or snapshot of the copy** (database and files), so it can be
   restored whatever happens.
2. Check that the files arrived intact: `sha256sum -c SHA256SUMS.txt` (or compare
   with `MANIFEST.json`).
3. Copy `tsyc-polylang-bridge.php` from this folder into the copy's
   `wp-content/mu-plugins/` folder (create the folder if needed). Do not edit it.
4. Add to the copy's `wp-config.php`, above `/* That's all, stop editing! */`:

   ```php
   define('WP_ENVIRONMENT_TYPE', 'staging');
   define('WP_HTTP_BLOCK_EXTERNAL', true);
   define('TSYC_BLACKBOX_ISOLATED_COPY', 'copy.example.com');   // this copy's own host name, exactly
   define('TSYC_BRIDGE_ENABLE_WRITES', true);
   define('TSYC_BRIDGE_WRITE_TOKEN', 'PASTE-A-NEW-RANDOM-VALUE');  // 32+ characters, used only on this copy
   ```

   Make a new random token for the copy, for example:
   `php -r "echo bin2hex(random_bytes(24)), PHP_EOL;"`. Do not reuse any
   production secret. The token never leaves the copy.
5. Pick a WordPress login on the copy that may edit products (an administrator).
6. Check isolation (read-only, writes nothing):

   ```
   wp eval-file tsyc-isolation.php --path=/path/to/the/copy
   ```

   It prints one JSON line. `"isolated":true` and exit code `0` mean you can go
   on. Exit code `5` means not isolated: the failing check says why.

## 5. Run it (one command)

From this folder, with a **new, empty folder name** for each run:

```
python3 site_blackbox.py run-gate-a run1 --user ADMIN_LOGIN --wp "wp --path=/path/to/the/copy"
```

(On Windows: `python` instead of `python3`.) Options: `--wait 120` seconds
before the after-snapshot, so delayed jobs can run (default 120);
`--timeout 300` seconds allowed per WP-CLI call (default 300). A whole run makes
at most 7 WP-CLI calls, so it ends within about `7 × timeout + wait`.

What it does, in order:

1. Isolation check (`00_isolation.json`). Stops here, with nothing written, unless all four checks pass and the bridge file matches.
2. Snapshot of the whole catalog (`00_baseline.json`), which must confirm isolation again.
3. One synthetic VI draft: stock managed, quantity 0, backorders allowed, **no price**, status draft (`00b_vi_created.json`).
4. An EN and a DE draft through the TSYC bridge, linked to it with Polylang.
5. Waits, then a snapshot (`01_trio_created.json`).
6. Moves the three test products to the trash (`10_cleanup.json`).
7. Judges, prints the result and writes `run1/blackbox_report.json` and `run1/summary.txt`.

## 6. Reading the result

```
TSYC black-box Gate A on a site copy: PASS
vi_draft: all UNCHANGED
every_other_product: all UNCHANGED
```

For the VI draft and for every other product, it compares price (regular, sale,
current), stock (managed, quantity, status, total sales), backorders, SKU,
images (main, gallery), status and type, Polylang language and translation
links, owner-maintained fields (title, slug, description hashes, categories,
attributes, up-sells, cross-sells, weight) and "saved again" (a save that
changed no field still shows). It also checks that the three new products are
exactly one VI, one EN and one DE draft, all linked to each other, unpriced,
stock 0, backorders allowed.

| Result | Exit code | Meaning |
|---|---|---|
| `PASS` | 0 | Three linked, unpriced pre-order drafts, and nothing else changed |
| `FAIL` | 1 | Something else changed, or the drafts are not as intended. `summary.txt` names the products and fields |
| unusable snapshot | 2 | A snapshot file is damaged or truncated |
| `BLOCKED_REAL_SITE_TEST` | 4 | No valid test: not isolated, bridge missing or different, a timeout, a step missing, or the bridge refused. **Never a pass** |

If the bridge stops with `tsyc_sibling_side_effect`, **that is the finding**: something on
the site changed another product while the copies were linked. The report names what.

## 7. Send the results back

```
python3 site_blackbox.py pack run1
```

This writes `run1_results.zip`, with:

- `blackbox_report.json`, `summary.txt`: the judgement and its evidence
- `run_log.jsonl`: each WP-CLI call, its exit code, duration and error output (no login, no token)
- `00_isolation.json`, `driver_*.txt`, `failure.json` if there was one
- the snapshots, with the **titles and slugs of every real product replaced by
  hashes** (the same text gives the same hash, so the comparison still works).
  Prices, stock levels and SKUs stay, because they are what is being compared.

Please open the zip and look through it before sending. Send it to the shop
owner. Nothing in it is a customer, order, user or key.

## 8. Cleanup after the test

1. The run already moved its three test products to the trash. Empty them from
   the trash if you like (products whose SKU starts with `TSYC-BBX-`).
2. Remove `wp-content/mu-plugins/tsyc-polylang-bridge.php` from the copy.
3. Remove the five `define(...)` lines from the copy's `wp-config.php` (or put
   `TSYC_BRIDGE_ENABLE_WRITES` to `false`).
4. Delete the `run1` folder once the zip has been sent.

## 9. If something fails

- **It stopped before writing** (exit 4, `failure.json` says `"nothing_written": true`):
  nothing to undo. Fix what the message names (usually a check from section 3)
  and run again with a new folder name.
- **It stopped after the VI draft was created** (a timeout, an error): `failure.json`
  gives the exact cleanup command, for example

  ```
  wp eval-file tsyc-bridge-trio-driver.php cleanup 1234 --user=ADMIN_LOGIN --path=/path/to/the/copy
  ```

  It only trashes products this package made (SKU prefix `TSYC-BBX-` and the
  package's own book ID). Do **not** re-run the same command blindly: after a
  timeout the earlier call may still have finished. Look at the products first.
- **The result is `FAIL`**: leave the copy as it is if you can, and send the zip.
  Then restore the copy from the backup of step 4.1 if you need it clean.
- **Anything unexpected**: restore the copy from its backup. The package never
  touches production, so there is nothing to undo there.

## Files

| File | What it does |
|---|---|
| `tsyc-isolation.php` | **Read-only.** The four isolation checks and the bridge file hash |
| `tsyc-site-snapshot.php` | **Read-only.** One JSON snapshot of every product (plus theme and plugin names and versions) |
| `tsyc-bridge-trio-driver.php` | **Writes test products:** the synthetic VI draft, then EN and DE through the bridge. Refuses unless isolated |
| `tsyc-polylang-bridge.php` | The TSYC bridge (must-use plugin) that creates and links the EN/DE drafts |
| `site_blackbox.py` | Runs everything in one command, compares, reports, packs. Standard library only; it opens no network connection and starts nothing but your WP-CLI command |
| `MANIFEST.json`, `SHA256SUMS.txt` | Version, source commit and file hashes |

---

## Optional, not requested now: price, stock and orders (Gate B)

Not needed for the current pilot (unpriced drafts only). Do this only if the
shop owner asks. It shows how the site syncs the copies once they are priced
and sold, and it needs a test payment method that cannot take real money.

Run part 1 with `--keep`, then do each step in wp-admin. After each step wait
two minutes, then snapshot **into the same folder** with exactly this name:

```
wp eval-file tsyc-site-snapshot.php 02_price_on_vi > run1/02_price_on_vi.json
```

| File | Do this first |
|---|---|
| `02_price_on_vi.json` | Regular price `111000` on the **VI** product only. Save |
| `03_price_on_en.json` | Regular price `222000` on the **EN** product only. Save |
| `04_stock_on_vi.json` | Stock quantity `5` on the **VI** product only. Save |
| `05_backorders_on_de.json` | Backorders "Do not allow" on the **DE** product only. Save |
| `06_published.json` | DE backorders back to "Allow", then publish all three |
| `07_order_on_en.json` | One test order, quantity 1, of the **EN** product |
| `08_order_on_de.json` | One test order, quantity 1, of the **DE** product |
| `09_concurrent_orders.json` | Two browsers at the same moment: VI and EN, quantity 1 each |
| `10_cleanup.json` | `wp eval-file tsyc-bridge-trio-driver.php cleanup VI_ID --user=ADMIN_LOGIN`, cancel the test orders |

Then `python3 site_blackbox.py evaluate run1` and `pack run1`. The Gate B block
records what the other two copies did (`OBSERVED`, or `BLOCKED` for a step left
out). The tool does not decide whether that behaviour is right.

`python3 site_blackbox.py diff A.json B.json` shows the raw difference between
any two snapshots.
