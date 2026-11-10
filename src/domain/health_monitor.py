"""
Read-only production health monitor: turn the existing read-only checks'
reports into stable findings, compare them with the accepted baseline and
the previous run, and decide which alerts are NEW.

Pure logic only. No database, WooCommerce, network or file access here;
scripts/health_check.py gathers the reports and owns every side effect
(which is limited to its own report/state files and an optional notifier).

Sources (each produced by an existing, authoritative read-only check):
  remote   scripts/audit_woo_remote_status.py  run_check().as_dict()
  mapping  scripts/audit_language_product_mapping.py  run()
  audit    scripts/audit_pipeline_state.py  stdout
  checks   scripts/preflight_pipeline.py  check_* (connectivity, recovery)

Alert policy:
  - a finding is alerted once, on the run where it first appears; it is not
    re-alerted while it stays open (state file) and never when the
    committed baseline accepts it;
  - systemic failures (configuration, connectivity, unreliable remote read,
    a check that could not run) are alerted on every run, because they mean
    the monitor cannot see production;
  - a tracked item (baseline "tracked") alerts whenever its finding set
    changes, in either direction (worse or resolved);
  - an accepted baseline entry that is no longer present alerts once
    (BASELINE_ENTRY_RESOLVED), so it gets pruned: while it stays listed, a
    later reappearance of the same issue would be accepted silently;
  - a gap since the last run, or no successful run within the window,
    alerts MISSED_CHECK / NO_RECENT_SUCCESS.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

HEALTH_MONITOR_VERSION = "1.1.0"
STATE_SCHEMA_VERSION = 1

# --- Alert categories ---------------------------------------------------
NEW_REMOTE_DRIFT = "NEW_REMOTE_DRIFT"
REMOTE_PRODUCT_REMOVED = "REMOTE_PRODUCT_REMOVED"
REMOVED_PRODUCT_RESTORED = "REMOVED_PRODUCT_RESTORED"
OWNERSHIP_CHANGE = "OWNERSHIP_CHANGE"
UNCERTAIN_CREATE = "UNCERTAIN_CREATE"
NEW_MAPPING_CONFLICT = "NEW_MAPPING_CONFLICT"
REMOTE_READ_INCONCLUSIVE = "REMOTE_READ_INCONCLUSIVE"
AUDIT_ERROR = "AUDIT_ERROR"
AUDIT_UNACCEPTED_WARNING = "AUDIT_UNACCEPTED_WARNING"
RECOVERY_REVIEW = "RECOVERY_REVIEW"
SYSTEMIC_API_FAILURE = "SYSTEMIC_API_FAILURE"
MISSED_CHECK = "MISSED_CHECK"
NO_RECENT_SUCCESS = "NO_RECENT_SUCCESS"
TRACKED_ITEM_CHANGED = "TRACKED_ITEM_CHANGED"
BASELINE_ENTRY_RESOLVED = "BASELINE_ENTRY_RESOLVED"
CHECK_CRASHED = "CHECK_CRASHED"

CRITICAL = "CRITICAL"
WARNING = "WARNING"

# Run outcomes.
RUN_OK = "OK"                          # every check ran; findings evaluated
RUN_DEGRADED = "DEGRADED"              # at least one systemic failure
RUN_SKIPPED_WRITER = "SKIPPED_WRITER_ACTIVE"  # a production writer held the lock

# woo_remote_status classifications (src/domain/woo_remote_status.py).
_REMOTE_CATEGORY = {
    "LOCAL_STATUS_STALE": NEW_REMOTE_DRIFT,
    "REMOTE_PUBLISHED": NEW_REMOTE_DRIFT,
    "REMOTE_TRASHED": REMOTE_PRODUCT_REMOVED,
    "REMOTE_REMOVED": REMOTE_PRODUCT_REMOVED,
    "OWNER_MODIFIED": OWNERSHIP_CHANGE,
    "IDENTITY_MISMATCH": OWNERSHIP_CHANGE,
    "TRANSLATION_LINKED_RECORD": NEW_MAPPING_CONFLICT,
    "UNCERTAIN_CREATE": UNCERTAIN_CREATE,
    "API_ACCESS_ERROR": REMOTE_READ_INCONCLUSIVE,
    "UNRESOLVED": REMOTE_READ_INCONCLUSIVE,
}
_REMOTE_IGNORED = {"CONSISTENT"}

# language_product_mapping finding codes with a more specific category;
# every other ERROR/WARNING code is a mapping conflict. INFO is ignored.
_MAPPING_CATEGORY = {
    "COPIED_OWNERSHIP_META": OWNERSHIP_CHANGE,
    "REMOTE_PRESENT_AFTER_REMOVAL": REMOVED_PRODUCT_RESTORED,
    "NO_WOO_ID": UNCERTAIN_CREATE,
}

_CRITICAL_CATEGORIES = {
    REMOVED_PRODUCT_RESTORED, OWNERSHIP_CHANGE, UNCERTAIN_CREATE, AUDIT_ERROR,
    SYSTEMIC_API_FAILURE, MISSED_CHECK, NO_RECENT_SUCCESS, TRACKED_ITEM_CHANGED, CHECK_CRASHED,
}

_AUDIT_ISSUE_LINE = re.compile(r"^\[\d+\]\s+(ERROR|WARNING)\s+\|\s+(\S+)\s*$")
_WOO_ID_IN_TEXT = re.compile(r"#(\d{1,10})\b")


@dataclass(frozen=True)
class Finding:
    key: str
    category: str
    source: str
    code: str
    product_code: str | None = None
    woo_id: int | None = None
    detail: str = ""
    systemic: bool = False

    @property
    def severity(self) -> str:
        return CRITICAL if self.category in _CRITICAL_CATEGORIES else WARNING

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["severity"] = self.severity
        return data


@dataclass(frozen=True)
class Alert:
    category: str
    severity: str
    key: str
    message: str
    product_code: str | None = None
    woo_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Evaluation:
    run_status: str
    findings: dict[str, Finding]
    alerts: list[Alert]
    new_keys: list[str]
    persisting_keys: list[str]
    resolved_keys: list[str]
    accepted_keys: list[str]
    stale_baseline_keys: list[str]
    tracked: dict[str, dict[str, Any]]
    next_state: dict[str, Any] = field(default_factory=dict)


def _woo_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _short(text: Any, limit: int = 160) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


# ---------------------------------------------------------------------
# Normalization: report -> findings
# ---------------------------------------------------------------------


def remote_findings(report: Mapping[str, Any]) -> list[Finding]:
    """Findings from audit_woo_remote_status run_check().as_dict()."""
    findings: list[Finding] = []
    if report.get("systemic_failure") or not report.get("bulk_read_ok", True):
        findings.append(systemic("WOO_REMOTE_STATE", f"Remote state unreliable: {report.get('systemic_message')}"))
    for row in report.get("results") or []:
        classification = str(row.get("classification") or "")
        if classification in _REMOTE_IGNORED:
            continue
        category = _REMOTE_CATEGORY.get(classification, NEW_REMOTE_DRIFT)  # unknown class: loud, not silent
        evidence = row.get("evidence") or {}
        if (category == OWNERSHIP_CHANGE and evidence.get("local_removal_marker")
                and str(row.get("remote_status") or "") not in ("", "NOT_FOUND", "trash")):
            category = REMOVED_PRODUCT_RESTORED
        woo_id = _woo_id(row.get("woocommerce_product_id"))
        product_code = row.get("product_code")
        findings.append(Finding(
            key=f"remote:{classification}:{product_code}:{woo_id or '-'}",
            category=category, source="remote", code=classification,
            product_code=product_code, woo_id=woo_id, detail=_short(row.get("reason")),
        ))
    return findings


def mapping_findings(report: Mapping[str, Any]) -> list[Finding]:
    """Findings from audit_language_product_mapping run(); INFO is ignored."""
    findings: list[Finding] = []
    for row in report.get("findings") or []:
        severity = str(row.get("severity") or "")
        if severity not in ("ERROR", "WARNING"):
            continue
        code = str(row.get("code") or "")
        detail = str(row.get("detail") or "")
        match = _WOO_ID_IN_TEXT.search(detail)
        product_code = row.get("product_code")
        language = row.get("language") or "-"
        findings.append(Finding(
            key=f"mapping:{code}:{product_code}:{language}",
            category=_MAPPING_CATEGORY.get(code, NEW_MAPPING_CONFLICT), source="mapping", code=code,
            product_code=product_code, woo_id=_woo_id(match.group(1)) if match else None, detail=_short(detail),
        ))
    return findings


def parse_audit_issues(stdout: str) -> list[tuple[str, str, str | None]]:
    """(severity, code, entity) for every issue audit_pipeline_state.py printed.
    The entity is the 'Entity:' line that follows the issue header."""
    issues: list[tuple[str, str, str | None]] = []
    lines = (stdout or "").splitlines()
    for index, line in enumerate(lines):
        match = _AUDIT_ISSUE_LINE.match(line.strip())
        if not match:
            continue
        entity = None
        if index + 1 < len(lines) and lines[index + 1].strip().startswith("Entity:"):
            entity = lines[index + 1].split(":", 1)[1].strip() or None
        issues.append((match.group(1), match.group(2), entity))
    return issues


def audit_findings(stdout: str, accepted_warning_codes: Iterable[str]) -> tuple[list[Finding], dict[str, int]]:
    """ERRORs and unaccepted WARNINGs become findings; accepted warnings are
    only counted (reported, never alerted)."""
    accepted = set(accepted_warning_codes)
    findings: list[Finding] = []
    accepted_counts: dict[str, int] = {}
    for severity, code, entity in parse_audit_issues(stdout):
        if severity == "WARNING" and code in accepted:
            accepted_counts[code] = accepted_counts.get(code, 0) + 1
            continue
        findings.append(Finding(
            key=f"audit:{severity}:{code}:{entity or '-'}",
            category=AUDIT_ERROR if severity == "ERROR" else AUDIT_UNACCEPTED_WARNING,
            source="audit", code=code, product_code=entity,
        ))
    return findings, accepted_counts


def recovery_finding(count: int) -> Finding | None:
    """One count-keyed finding: a change in the number of products needing
    recovery review is new; an unchanged count is not re-alerted."""
    if count == 0:
        return None
    label = "unknown" if count < 0 else str(count)
    return Finding(key=f"recovery:RECOVERY_REQUIRED:{label}", category=RECOVERY_REVIEW, source="recovery",
                   code="RECOVERY_REQUIRED", detail=f"{label} product(s) require recovery review.")


def systemic(check: str, detail: str) -> Finding:
    return Finding(key=f"systemic:{check}", category=SYSTEMIC_API_FAILURE, source="systemic",
                   code=check, detail=_short(detail, 240), systemic=True)


# ---------------------------------------------------------------------
# Time windows
# ---------------------------------------------------------------------


def parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def schedule_alerts(state: Mapping[str, Any], now: datetime, *, max_gap_hours: float,
                    success_window_hours: float) -> list[Alert]:
    """MISSED_CHECK when the previous run is older than the gap, and
    NO_RECENT_SUCCESS when no complete run happened inside the window.
    A first run (no state) alerts neither."""
    alerts: list[Alert] = []
    last_run = parse_ts(state.get("last_run_at"))
    last_success = parse_ts(state.get("last_success_at"))
    if last_run is not None and now - last_run > timedelta(hours=max_gap_hours):
        hours = (now - last_run).total_seconds() / 3600
        alerts.append(Alert(MISSED_CHECK, CRITICAL, "schedule:MISSED_CHECK",
                            f"Last health check ran {hours:.0f} h ago ({last_run.isoformat()}); expected every "
                            f"{max_gap_hours:g} h or less."))
    if last_run is not None and (last_success is None or now - last_success > timedelta(hours=success_window_hours)):
        since = last_success.isoformat() if last_success else "never"
        alerts.append(Alert(NO_RECENT_SUCCESS, CRITICAL, "schedule:NO_RECENT_SUCCESS",
                            f"No complete health check within {success_window_hours:g} h (last success: {since})."))
    return alerts


# ---------------------------------------------------------------------
# Baseline and tracked items
# ---------------------------------------------------------------------


def accepted_keys(baseline: Mapping[str, Any]) -> set[str]:
    return {str(item["key"]) for item in baseline.get("accepted") or [] if item.get("key")}


def _tracked_keys(item: Mapping[str, Any], findings: Mapping[str, Finding]) -> list[str]:
    codes = {str(c) for c in item.get("product_codes") or []}
    woo_ids = {int(w) for w in item.get("woo_ids") or []}
    return sorted(key for key, f in findings.items()
                  if (f.product_code in codes) or (f.woo_id is not None and f.woo_id in woo_ids))


def tracked_status(baseline: Mapping[str, Any], findings: Mapping[str, Finding],
                   previous: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], list[Alert]]:
    """Current finding set of every tracked item and an alert when it differs
    from the previous run (or, on a first run, from the baseline's expected set)."""
    status: dict[str, dict[str, Any]] = {}
    alerts: list[Alert] = []
    previous_tracked = previous.get("tracked") or {}
    for item in baseline.get("tracked") or []:
        item_id = str(item["id"])
        current = _tracked_keys(item, findings)
        if item_id in previous_tracked:
            before = sorted(previous_tracked[item_id].get("keys") or [])
        else:
            before = sorted(str(k) for k in item.get("expected_keys") or [])
        changed = current != before
        status[item_id] = {"keys": current, "changed": changed, "note": item.get("note"),
                           "woo_ids": list(item.get("woo_ids") or []),
                           "product_codes": list(item.get("product_codes") or [])}
        if changed:
            added = sorted(set(current) - set(before))
            removed = sorted(set(before) - set(current))
            parts = []
            if added:
                parts.append("new: " + ", ".join(added))
            if removed:
                parts.append("cleared: " + ", ".join(removed))
            alerts.append(Alert(TRACKED_ITEM_CHANGED, CRITICAL, f"tracked:{item_id}",
                                f"Tracked {item_id} changed ({'; '.join(parts)}). {item.get('note') or ''}".strip(),
                                woo_id=(item.get("woo_ids") or [None])[0]))
    return status, alerts


# ---------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------


def _alert_for(finding: Finding) -> Alert:
    target = finding.product_code or "-"
    woo = f" #{finding.woo_id}" if finding.woo_id else ""
    return Alert(finding.category, finding.severity, finding.key,
                 _short(f"{finding.code} {target}{woo}: {finding.detail}".strip(), 220),
                 product_code=finding.product_code, woo_id=finding.woo_id)


def evaluate(findings: Sequence[Finding], *, baseline: Mapping[str, Any], previous: Mapping[str, Any],
             now: datetime, run_status: str, max_gap_hours: float = 26,
             success_window_hours: float = 50, accepted_audit_counts: Mapping[str, int] | None = None
             ) -> Evaluation:
    """Decide the alerts of one run and the state to persist after it."""
    current: dict[str, Finding] = {}
    for finding in findings:
        current.setdefault(finding.key, finding)
    accepted = accepted_keys(baseline)
    previous_open = dict(previous.get("open") or {})
    ts = now.isoformat()

    alerts = schedule_alerts(previous, now, max_gap_hours=max_gap_hours, success_window_hours=success_window_hours)
    new_keys, persisting = [], []
    for key, finding in sorted(current.items()):
        if finding.systemic:
            alerts.append(_alert_for(finding))  # every run: the monitor is partly blind
            continue
        if key in accepted:
            continue
        if key in previous_open:
            persisting.append(key)
        else:
            new_keys.append(key)
            alerts.append(_alert_for(finding))

    # A skipped/degraded run did not see everything: keep previous open keys
    # it could not re-check, so they are not re-alerted as new next time.
    complete = run_status == RUN_OK
    resolved = sorted(k for k in previous_open if k not in current) if complete else []

    tracked, tracked_alerts = tracked_status(baseline, current, previous) if complete else (
        dict(previous.get("tracked") or {}), [])
    alerts.extend(tracked_alerts)

    # Only a complete run can tell that an accepted finding is gone.
    previously_stale = sorted(str(k) for k in previous.get("stale_baseline") or [])
    stale_baseline = sorted(accepted - set(current)) if complete else []
    if complete:
        for key in stale_baseline:
            if key not in previously_stale:
                alerts.append(Alert(
                    BASELINE_ENTRY_RESOLVED, WARNING, f"baseline:{key}",
                    _short(f"Accepted finding {key} is no longer present. Review and prune it from "
                           "config/health_check_baseline.json; while listed, a reappearance stays silent.", 260)))

    open_state: dict[str, Any] = {} if complete else {k: v for k, v in previous_open.items() if k not in current}
    for key, finding in current.items():
        if finding.systemic or key in accepted:
            continue
        first = (previous_open.get(key) or {}).get("first_seen") or ts
        open_state[key] = {"first_seen": first, "last_seen": ts, "category": finding.category}

    next_state: dict[str, Any] = {
        "schema_version": STATE_SCHEMA_VERSION,
        "monitor_version": HEALTH_MONITOR_VERSION,
        "last_run_at": ts,
        "last_run_status": run_status,
        "last_success_at": ts if complete else previous.get("last_success_at"),
        "last_audit_pass_at": previous.get("last_audit_pass_at"),
        "open": open_state,
        "tracked": tracked,
        "accepted_audit_counts": dict(accepted_audit_counts or previous.get("accepted_audit_counts") or {}),
        "stale_baseline": stale_baseline if complete else previously_stale,
    }
    if complete and not any(f.category == AUDIT_ERROR for f in current.values()):
        next_state["last_audit_pass_at"] = ts

    return Evaluation(run_status, current, alerts, new_keys, persisting, resolved,
                      sorted(accepted & set(current)), stale_baseline, tracked, next_state)


def format_message(alerts: Sequence[Alert], *, run_status: str, now: datetime, max_lines: int = 15) -> str:
    """One short, plain-text notification. Empty string: nothing to send."""
    if not alerts:
        return ""
    critical = sum(1 for a in alerts if a.severity == CRITICAL)
    head = (f"TSYC health {now:%Y-%m-%d %H:%M} UTC: {len(alerts)} alert(s), {critical} critical "
            f"(run {run_status}).")
    ordered = sorted(alerts, key=lambda a: (a.severity != CRITICAL, a.category, a.key))
    lines = [head] + [f"- [{a.category}] {a.message}" for a in ordered[:max_lines]]
    if len(ordered) > max_lines:
        lines.append(f"- … and {len(ordered) - max_lines} more (see latest_alerts.json).")
    return "\n".join(lines)


def redact(text: str, secrets: Iterable[str]) -> str:
    """Replace any credential value that leaked into a message."""
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "[REDACTED]")
    return text
