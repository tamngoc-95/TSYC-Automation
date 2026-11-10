# Scheduled read-only health monitoring

`scripts/health_check.py` (logic: `src/domain/health_monitor.py`) runs the existing
authoritative read-only checks once a day and alerts only on what is **new**.

**Status (2026-11-10): implemented and validated (monitor 1.1.0), not enabled.** No
scheduled task is registered (checked: no `\TSYC\` task exists). Enabling needs the owner
steps in "Enable" below.

Live read-only validation, 2026-11-10 (`--allow-production-credentials --no-state-update`):
run `OK`, 22 findings, all 22 accepted, 0 new, 0 alerts; all four tracked items unchanged.

## What it checks

| # | Check | Reused from | Alert category |
|---|---|---|---|
| 1 | WooCommerce remote product status | `audit_woo_remote_status.run_check` (GET only) | `NEW_REMOTE_DRIFT` |
| 2 | Local/remote drift | same report | `NEW_REMOTE_DRIFT` |
| 3 | Translation mapping conflicts | `audit_language_product_mapping.run` (GET only) | `NEW_MAPPING_CONFLICT` |
| 4 | Owner-modified / ownership | `OWNER_MODIFIED`, `IDENTITY_MISMATCH`, `COPIED_OWNERSHIP_META` | `OWNERSHIP_CHANGE` |
| 5 | Removed-product protection | new `REMOTE_TRASHED`/`REMOTE_REMOVED`; a removed product live again | `REMOTE_PRODUCT_REMOVED`, `REMOVED_PRODUCT_RESTORED` |
| 6 | Uncertain Woo creates | `UNCERTAIN_CREATE`, mapping `NO_WOO_ID`, preflight recovery health | `UNCERTAIN_CREATE`, `RECOVERY_REVIEW` |
| 7 | API connectivity | preflight `check_supabase_read`, `check_woo_read` | `SYSTEMIC_API_FAILURE` |
| 8 | Last successful audit | state file; `audit_pipeline_state.py` errors / unaccepted warnings | `AUDIT_ERROR`, `AUDIT_UNACCEPTED_WARNING`, `MISSED_CHECK`, `NO_RECENT_SUCCESS` |

It never writes to Supabase or WooCommerce, never takes the production writer lock, and
changes no price, stock, status or translation link. While another production writer
holds the lock the run is `SKIPPED_WRITER_ACTIVE`, because reads in the middle of a write
show transient drift. It writes only its own files:

| File (gitignored) | Content |
|---|---|
| `data/output/health/health_<ts>.json`, `latest_report.json` | Full report: checks, findings, tracked items, timestamps |
| `data/output/health/latest_alerts.txt` | The notification text of the last run |
| `data/output/health/alerts.jsonl` | Every alert ever raised (append-only), including watchdog and crash records |
| `data/output/health/last_crash.txt` | Redacted traceback of the last unexpected failure (exit 4) |
| `data/processed/health/health_state.json` | Open findings, `last_run_at`, `last_success_at`, `last_audit_pass_at`, stale baseline keys |
| `data/processed/health/health_check.lock` | Present only while a check runs; a second concurrent check is refused (exit 3). Not the production writer lock. A lock left by a dead process is taken over |

## Alert policy (no repeated noise)

- A finding alerts **once**, on the run where it first appears. It is not re-alerted while
  it stays open. If it resolves and later reappears, it alerts again.
- `config/health_check_baseline.json` (committed, reviewed) lists **accepted** findings with
  a reason. These are never alerted. An entry that is no longer present raises
  `BASELINE_ENTRY_RESOLVED` **once** (warning) and is listed under `baseline_no_longer_present`:
  prune it in a reviewed commit, because while it stays listed a reappearance of the same issue
  would be accepted silently. Only a complete (`OK`) run can report an entry as gone.
- **Tracked items** alert whenever their finding set changes, in either direction:
  - `CAN-0033` (#4858, copied ownership meta)
  - `WOO-4829` (#4829, CAN-0034, copied ownership meta)
  - `WOO-3772` (#3772, CAN-0010, restored after `REMOTE_TRASHED`, owner decision open)
  - `CAN-0031` (FAILED create without a Woo ID; recovery belongs to `recover_historical_woo_create.py`)
- Systemic failures (connectivity, unreliable remote read, a check that could not run) alert
  on **every** run, because they mean the monitor cannot see production. A degraded run
  neither resolves nor forgets findings it could not re-check.
- `MISSED_CHECK`: the previous run was more than 26 h ago. `NO_RECENT_SUCCESS`: no complete
  run within 50 h. The credential-free `--watchdog` raises the same alerts if the daily check
  stops running altogether, once per stale run. With `--notify` it reads **only**
  `TSYC_HEALTH_WEBHOOK_URL` (plus the store and database URLs, to refuse posting to them) from
  `.env.health`, never a key.
- `CHECK_CRASHED`: an unexpected exception is recorded (redacted) in `last_crash.txt` and
  `alerts.jsonl`, notified when `--notify` and credentials were loaded, and exits 4. The state
  file is not advanced, so the next run and the watchdog also report the gap.

Example notification:

```
TSYC health 2026-11-09 07:00 UTC: 2 alert(s), 1 critical (run OK).
- [REMOVED_PRODUCT_RESTORED] OWNER_MODIFIED TSYC-…-CAN-0043 #4663: Recorded REMOTE_TRASHED, but the same Woo ID is live again (status 'publish').
- [NEW_MAPPING_CONFLICT] UNMAPPED_SKU_COPY TSYC-…-CAN-0009 #5320: #5320 (publish) carries the SKU but is not mapped. …
```

## Credentials (least privilege)

Scheduled runs read credentials **only** from `.env.health` in the repository root
(gitignored by `.env.*`). The check refuses (exit 3) when the file or a key is missing, and
refuses `--allow-production-credentials` together with `--scheduled`. Values are never
printed, written to a report or put in the task definition. Any value that leaks into an
error text is redacted.

```
SUPABASE_URL=...
SUPABASE_KEY=...                  # read-only key; see below
WOOCOMMERCE_URL=https://tiemsachyeucon.com
WOOCOMMERCE_CONSUMER_KEY=ck_...   # a NEW key with permission "Read"
WOOCOMMERCE_CONSUMER_SECRET=cs_...
WOOCOMMERCE_API_VERSION=wc/v3
TSYC_HEALTH_WEBHOOK_URL=https://...   # optional; notification destination
```

- **WooCommerce:** WooCommerce → Settings → Advanced → REST API → Add key, permission
  **Read**, assigned to a user who is not an administrator if possible. The check cannot
  prove the permission remotely; the owner confirms it when creating the key.
- **Supabase:** the production `SUPABASE_KEY` can write. A read-only key needs a role with
  SELECT-only grants on the tables read (`internal_products`, `woocommerce_product_syncs`,
  `product_candidates`, `woocommerce_language_products`, and the tables
  `audit_pipeline_state.py` reads). That is a schema change, so it is **owner-applied**
  (MCP is read-only here). Until then the owner may knowingly place the existing key in
  `.env.health`. That is a recorded exception, not least privilege.
- `audit_pipeline_state.py` runs as a subprocess with the `.env.health` values in its
  environment. Its own `load_dotenv()` does not override them.

## Notification destination

`--notify` sends **one** HTTPS POST of `{"text": "<message>"}` to `TSYC_HEALTH_WEBHOOK_URL`
(Slack/Teams/Discord-style incoming webhooks accept this shape). It is never retried. It is
refused when the URL is not HTTPS or points at the store or the database host. Without a
configured URL the alert stays in `latest_alerts.txt` and `alerts.jsonl`
(`notification: NOT_CONFIGURED`).

## Run it

```
.venv/Scripts/python.exe scripts/health_check.py --allow-production-credentials --no-state-update   # interactive validation
.venv/Scripts/python.exe scripts/health_check.py --scheduled --notify                               # what the task runs
.venv/Scripts/python.exe scripts/health_check.py --watchdog --notify                                # credential-free
```

Exit codes: 0 healthy, 1 new alert(s), 2 degraded (systemic failure), 3 refused (configuration,
authorization, or another check already running), 4 crashed.

## Scheduler behaviour

| Concern | Behaviour |
|---|---|
| Cadence | Check daily 07:00, watchdog daily 19:00 (local time; `-CheckTime` / `-WatchdogTime`) |
| Executable / working directory | `.venv\Scripts\python.exe`, working directory = repository root |
| Credentials | None in the task definition; `.env.health` read at run time |
| Logs | The task captures no console output. Everything is in the files above; Task Scheduler's "Last Run Result" shows the exit code |
| Machine off / asleep | `StartWhenAvailable`: a missed run starts when the PC is back. With `Interactive` logon the task runs only while the user is logged on; `S4U` runs logged off |
| Offline | Connectivity checks fail: run `DEGRADED` (exit 2), systemic alert; the notification POST also fails and is not retried. The next online run and the watchdog report it (`NO_RECENT_SUCCESS`) |
| Previous run still active | Task Scheduler `MultipleInstances IgnoreNew`; a manual run during a scheduled one is refused by `health_check.lock` |
| Hung run | `ExecutionTimeLimit` 1 h; the audit subprocess has a 15 min timeout; HTTP reads have timeouts |
| Production writer active | Run `SKIPPED_WRITER_ACTIVE`: not a success, so a writer that holds the lock for days surfaces as `NO_RECENT_SUCCESS` |

Known limitations: the console window flashes briefly at run time with `Interactive` logon;
the WooCommerce key's "Read" permission cannot be proven remotely; `wc/v3/system_status`
requires a user with `manage_woocommerce` (shop manager or administrator).

## Enable (owner-authorized; not done)

1. Create the read-only WooCommerce key and choose the Supabase key (above). Write `.env.health`.
2. Choose the notification destination and put its URL in `.env.health`.
3. Validate: `health_check.py --scheduled --notify` must exit 0 or 1, report
   `credentials: HEALTH_READONLY`, and a test alert must arrive.
4. Dry run of the task definition (registers nothing):
   `powershell -ExecutionPolicy Bypass -File tools\scheduler\register_health_tasks.ps1 -Notify`
5. Register, after the owner authorizes it:
   `… register_health_tasks.ps1 -Register -Notify` (add `-LogonType S4U` to run while logged
   off). This creates `\TSYC\TSYC Health Check` (daily 07:00) and `\TSYC\TSYC Health Watchdog`
   (daily 19:00), with `StartWhenAvailable` so a missed run starts when the PC is back on.
6. Unregister: `Unregister-ScheduledTask -TaskPath \TSYC\ -TaskName "TSYC Health Check"` (and the watchdog).

The PC must be on, or wake, for a local task. If that is not reliable, the same command can
run on any always-on Windows host with this repository and `.venv`. A cloud routine is not
used, because it would need the store and database credentials outside this machine.
