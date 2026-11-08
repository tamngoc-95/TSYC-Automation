# PHP hook inventory

`scripts/inventory_php_hooks.py` lists every WordPress hook in the developer's PHP
source, so that hook behaviour is read from the code and not assumed. **The developer's
source has not been supplied, so nothing about the store's hooks is known yet.**

## Run it

Put the zips or folders outside the repository, or under `data/raw/developer_source/`
(gitignored, and the default source):

```
.venv/Scripts/python.exe scripts/inventory_php_hooks.py --source flatsome-child.zip --source pll-bulk-link-translations.zip --source pll-bulk-link-translations-v2.zip
```

Each run writes `hook_inventory.json` and `hook_inventory.md` to
`data/output/hook_inventory/<run>/`.

| Exit | Meaning |
|---|---|
| 0 | Inventoried; no HIGH row and no suspected secret |
| 1 | Inventoried; read the HIGH rows or suspected secrets first |
| 2 | Refused: bad zip, or a source or report path inside a tracked folder |
| 3 | `BLOCKED_MISSING_SOURCE`: no PHP was read. This is never a clean result |

## What each row records

| Field | Content |
|---|---|
| `hook`, `kind`, `hook_is_dynamic` | Name, action or filter. A built name keeps its known parts: `woocommerce_{}_product` |
| `callback`, `callback_resolved` | Function, method or closure. What cannot be read is reported as unresolved, and a method found only by its name says `(by name)` |
| `priority`, `accepted_args` | As registered |
| `file`, `line`, `callback_file`, `callback_line`, `registered_in` | Where it is registered and where the code is |
| `reads`, `writes`, `fields_mentioned` | Product fields, following calls between the source's own functions. `_stock` is reported as `stock_quantity`, and so on |
| `dynamic_meta_writes`, `raw_sql_write` | Writes whose key is a variable; raw SQL |
| `trigger_conditions`, `guards` | Its `if` conditions, plus autosave, revision, capability, language and recursion checks |
| `sibling_apis`, `direction_guess` | Whether it can reach other translations, and a guess at the direction |
| `fires`, `fires_custom` | Hooks its calls fire |
| `fires_on_bridge_create`, `fires_on_bridge_rollback`, `measured_on_source_or_sibling` | From the events measured on the lab (`tests/fixtures/php_hooks/measured_hook_events.json`) |
| `risk`, `risk_reasons` | HIGH, MEDIUM, LOW or NONE: the reading order |

The report also lists recursion cycles, conflicts (the same field on the same hook,
fields written from several hooks, a callback registered twice) and removed hooks.

## What it does not prove

- **Every row is a lead.** `direction_guess` is a heuristic over call names. A
  recursion guard is reported as "seen", never as working. Only the lab run with the real
  code can prove behaviour (`POLYLANG_BRIDGE.md`, Stage D).
- The measured events come from plain WordPress, WooCommerce and Polylang. They show what
  fires without the theme, and the theme can fire more.
- A callback chosen at runtime, code in an unreadable file and writes in a scheduled job
  have to be followed by a person.

## Safety

- **Nothing is executed.** It has its own lexer and never calls PHP, so no plugin or
  theme code can run.
- **Zips are read in memory, never extracted.** Traversal paths, nested archives, encrypted
  members and symlinks are skipped and listed. File and total sizes are capped.
- **Config files are not read:** `wp-config*.php`, `.env*`, `*.sql`, `*.pem`, `*.key`,
  `auth.json` and similar.
- **Values are not copied.** A hook name or meta key appears only if it is
  identifier-shaped. Other strings in a condition print as `'...'`. A suspected credential
  is reported as file, line and kind only.
- **Git:** a source or report path inside a tracked folder is refused.

## Checked against real code

The parser ran on the lab's public Polylang and WooCommerce source, compared with an
independent regex count of call sites:

| Source | PHP files | Tool | Regex | Difference |
|---|---|---|---|---|
| Polylang 3.8.4 | 242 | 445 | 445 | None |
| WooCommerce 10.7.0 | 3,581 | 2,477 | 2,486 | Method definitions named `add_action`, and `Note::add_action` calls that are not hooks, both excluded correctly |

The comparison found one real miss: `self::add_action(...)` wrappers were dropped. They are
now reported with `indirect: true`, so counts rise slightly on code that uses them.

## After the source arrives

1. Run the inventory. Tell the developer about any suspected secret, without pasting it.
2. A person reads every HIGH and MEDIUM row and every unresolved relevant callback, and
   records the real fields, direction and triggers in `POLYLANG_BRIDGE.md` section 6.
3. Install the source in the MariaDB lab and rerun both pilots. B11–B16 stop reporting
   `BLOCKED_MISSING_SOURCE` and report `OBSERVED_REAL_SOURCE`, for a person to read.
4. Apply the staging acceptance criteria (`POLYLANG_BRIDGE.md` section 10).
