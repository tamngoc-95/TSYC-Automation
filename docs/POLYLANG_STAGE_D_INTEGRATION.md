# Stage D: real Flatsome Child and bulk-link source in the lab

Prepared 2026-11-08. **Not run: the developer source has not been supplied**
(`data/raw/developer_source/` does not exist). Production multilingual write routes stay off.
Nothing here touches production.

## Evidence status before Stage D

Summarised from `POLYLANG_BRIDGE.md` sections 5.1, 5.2, 6 and 8. Nothing in this table is new
evidence, and **Stage D has not passed**.

| Class | What it covers | Evidence | Status |
|---|---|---|---|
| Verified bridge behaviour, real local WordPress | Bridge 1.1.0 create, rollback, group lock, sibling snapshot and side-effect stop, duplicate SKU, published-source refusal, native "+" and hreflang (H01–H13) | Hardening pilot at WC 11.2.1 / Polylang 3.9.1: 13 `PASS_REAL_LOCAL`, 0 `FAIL` on MariaDB; on SQLite one H09 stall, then 0 `FAIL` (section 5.2) | Lab only. Native stack, no theme. Not installed anywhere real |
| Synthetic hooks | Y01–Y08: TSYC's **guesses** at what the theme might do | `PASS_SYNTHETIC_HOOK` 8 | Shows the guards react **if** the store behaves that way. Nothing about the real theme |
| Flatsome Child, bulk-link, Duplicate Post RB | Real hooks, metadata copy, SKU copy, interference (B11–B16) | None. `BLOCKED_MISSING_SOURCE` 6 | **Unverified** |
| Price, stock, backorder sync | Direction and authority between VI/EN/DE; cross-language orders | Natively, each language is independent (lab). The store's behaviour is unknown | **Unverified**. Section 6 risks stay open; 11.2.1 / 3.9.1 changed which core hooks a price/stock/order save fires |
| Developer / owner | Source zips, parent theme licence, Duplicate Post RB package, price/stock authority | Not supplied | **Blocking**, see below |

## Why B11–B16 are blocked

| Test | What it needs | Harness behaviour today |
|---|---|---|
| B11 price/stock/backorder sync | `flatsome-child` active (+ Flatsome parent) | `observe_or_block`: observes when `flatsome-child` is the active theme |
| B12 parallel cross-language orders | same | same |
| B13 bridge create with real hooks (Stage D gate) | same | same |
| B14 Flatsome Child metadata sync | same | **Always `BLOCKED_MISSING_SOURCE`**, even with the theme active: no observe path exists yet |
| B15 bulk-link interference | `pll-bulk-link-translations*` active | observes when a plugin whose name starts with `pll-bulk-link` is active |
| B16 Duplicate Post RB copy + SKU edit | Duplicate Post RB 1.6.8 + `flatsome-child` | **Always `BLOCKED_MISSING_SOURCE`**: no observe path exists yet |

So criterion 5 in `POLYLANG_BRIDGE.md` section 10 ("B11–B16 report
`OBSERVED_REAL_SOURCE`") **cannot be met by B14 and B16 until the harness gets observe
paths for them**. They should be written once the source is read, against what the hook
inventory shows the theme actually copies. They should not be guessed now.

Blockers, in order:
1. **Developer:** zip of `flatsome-child/` and both `pll-bulk-link-translations*` folders,
   and how the existing EN/DE copies were made.
2. **Developer/owner:** the Flatsome parent theme under their licence, and the Duplicate Post RB
   1.6.8 package used on the store.
3. **Engineering (after 1):** B14 and B16 observe paths in `tools/wp_lab/run_hardening_pilot.py`.

## Exact commands (when the source arrives)

Sources stay outside Git, under `data/raw/developer_source/` (gitignored):

```
# 1. Static inventory: nothing executed, zips read in memory
.venv/Scripts/python.exe scripts/inventory_php_hooks.py --source data/raw/developer_source/flatsome-child.zip --source data/raw/developer_source/pll-bulk-link-translations.zip --source data/raw/developer_source/pll-bulk-link-translations-v2.zip
#    exit 0/1 = inventoried (read HIGH rows / suspected secrets first); 3 = no PHP read

# 2. Fresh MariaDB lab at production versions (WC 11.2.1, Polylang 3.9.1)
.venv/Scripts/python.exe tools/wp_lab/provision_lab.py --db mariadb --woocommerce-version 11.2.1 --polylang-version 3.9.1

# 3. Install the real code into the lab (loopback lab only; outbound HTTP is blocked there)
$LAB = "$env:LOCALAPPDATA\TSYC\wp-lab-mariadb"
function labwp { & "$LAB\php\php.exe" -c "$LAB\php\php.ini" "$LAB\wp-cli\wp-cli.phar" "--path=$LAB\site" @args }
#    MariaDB must be running (the pilots start and stop it; start it the same way, provision_lab.start_mariadb)
labwp theme install data/raw/developer_source/flatsome.zip
labwp theme install data/raw/developer_source/flatsome-child.zip --activate
labwp plugin install data/raw/developer_source/pll-bulk-link-translations.zip data/raw/developer_source/pll-bulk-link-translations-v2.zip data/raw/developer_source/duplicate-post-rb-1.6.8.zip --activate
labwp theme list --status=active; labwp plugin list --status=active

# 4. Both pilots with the real code active
.venv/Scripts/python.exe tools/wp_lab/run_local_pilot.py --lab-dir "$LAB"
.venv/Scripts/python.exe tools/wp_lab/run_hardening_pilot.py --lab-dir "$LAB"
#    run_hardening_pilot.py defaults to the SQLite lab; --lab-dir is required for MariaDB.
#    Reports: data/output/polylang_local/<run>/, data/output/polylang_hardening/<run>/
```

## Acceptance criteria for this stage

All must hold. Each B-result is evidence for a person to read, never a pass by itself.

1. The inventory reports `INVENTORIED`. Every HIGH/MEDIUM row and unresolved relevant callback has been read, and `POLYLANG_BRIDGE.md` section 6 states the real fields, direction and triggers for price, stock, backorders, SKU and `_tsyc_*`. Any suspected secret has gone to the developer, without being pasted anywhere.
2. `health` in the hardening report shows the production versions (WC 11.2.1, Polylang 3.9.1), with `flatsome-child` as the active theme and both bulk-link plugins active.
3. B11, B12, B13 and B15 report `OBSERVED_REAL_SOURCE`, and B14 and B16 do too once their observe paths exist. Each has been read by a person.
4. Both pilots report **0 `FAIL`** with the real code active. Any changed expectation is explained in `POLYLANG_BRIDGE.md`.
5. B13: a bridge create and a rollback leave every sibling unchanged, including on a later reread for deferred or `shutdown` work. Any `tsyc_sibling_side_effect` is explained and accepted in writing by the owner and developer. **The guard is not loosened to pass.**
6. If the theme copies SKUs, the wp-admin save of a copy with a raw-copied SKU has been tested.
7. Owner and developer have recorded price and stock authority, the status of the bulk-link plugins, and who would install the bridge read routes.

Passing Stage D allows Stage E design only. Staging writes need section 10 C–E and a separate owner authorization. Production is Stage G.
