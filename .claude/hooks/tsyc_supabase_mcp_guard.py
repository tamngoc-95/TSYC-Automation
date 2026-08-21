#!/usr/bin/env python3
from __future__ import annotations
import json, re, sys

TARGET = "mcp__supabase-tsyc__execute_sql"
FORBIDDEN = {
    "insert","update","delete","merge","create","alter","drop","truncate",
    "grant","revoke","copy","vacuum","reindex","cluster","refresh","comment",
    "call","do","execute","prepare","deallocate","lock","set","reset","discard",
    "listen","notify","unlisten","security"
}
PATTERNS = (
    r"\bselect\s+.+?\binto\b",
    r"\bfor\s+(update|share)\b",
    r"\bset_config\s*\(",
    r"\bpg_advisory_(xact_)?lock\s*\(",
)

def emit(decision, reason):
    print(json.dumps({"hookSpecificOutput":{
        "hookEventName":"PreToolUse",
        "permissionDecision":decision,
        "permissionDecisionReason":reason
    }}))

def strip_sql(s):
    s = re.sub(r"/\*.*?\*/"," ",s,flags=re.S)
    s = re.sub(r"--[^\r\n]*"," ",s)
    s = re.sub(r"'(?:''|[^'])*'"," ",s,flags=re.S)
    s = re.sub(r'"(?:""|[^"])*"'," ",s,flags=re.S)
    return s

def main():
    try:
        event = json.loads(sys.stdin.read())
    except Exception:
        emit("deny","TSYC Supabase guard could not parse the tool request.")
        return
    if event.get("tool_name") != TARGET:
        return
    q = (event.get("tool_input") or {}).get("query")
    if not isinstance(q,str) or not q.strip():
        emit("deny","TSYC Supabase guard requires a non-empty SQL query.")
        return
    cleaned = strip_sql(q).strip()
    no_trailing = re.sub(r";\s*$","",cleaned)
    if ";" in no_trailing:
        emit("deny","TSYC blocks multi-statement execute_sql calls.")
        return
    norm = no_trailing.lower().strip()
    m = re.match(r"^([a-z_]+)",norm)
    first = m.group(1) if m else ""
    if first not in {"select","with","explain","show","values"}:
        emit("deny","TSYC permits execute_sql only for read/introspection SQL.")
        return
    words = set(re.findall(r"\b[a-z_]+\b",norm))
    bad = sorted(words & FORBIDDEN)
    if bad:
        emit("deny","TSYC blocked potentially mutating/privileged SQL keyword(s): "+", ".join(bad))
        return
    for p in PATTERNS:
        if re.search(p,norm,flags=re.S):
            emit("deny","TSYC blocked SQL with write/lock/session-changing behavior.")
            return
    emit("ask","TSYC Supabase read-only SQL gate: query passed local checks. Approve only if the displayed SQL is expected.")

if __name__ == "__main__":
    main()
