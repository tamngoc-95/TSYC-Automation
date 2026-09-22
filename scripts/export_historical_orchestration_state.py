#!/usr/bin/env python3
"""
Read-only state exporter for historical automation orchestration.

Queries current candidate state and updates historical_state.json snapshot.
Does NOT mutate any business data.

Usage:
  python scripts/export_historical_orchestration_state.py --status
  python scripts/export_historical_orchestration_state.py --json
"""

import json
import sys
from datetime import datetime
from pathlib import Path

def export_state(output_format="status"):
    """
    Export current historical candidate state.
    
    Args:
        output_format: 'status' for summary, 'json' for raw JSON
    
    Returns:
        dict with state snapshot
    """
    repo_root = Path(__file__).parent.parent
    state_file = repo_root / "data" / "processed" / "orchestration" / "historical_state.json"
    
    # Initialize state structure
    state = {
        "schema_version": 1,
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "repository_commit": None,
        "historical_total": 0,
        "automatable_now": 0,
        "human_review": 0,
        "recovery_review": 0,
        "conflict": 0,
        "terminal": 0,
        "latest_execution": {
            "execution_id": None,
            "result": None,
            "candidate_codes": [],
            "attempted": 0,
            "progressed": 0,
            "unchanged": 0,
            "human_gated": 0,
            "failed": 0
        },
        "audit": {
            "status": None,
            "errors": None,
            "warnings": None
        },
        "preflight": {
            "status": None
        },
        "safety": {
            "price_writes": 0,
            "publish_actions": 0,
            "outside_allowlist_touched": 0,
            "non_historical_touched": 0
        },
        "next_recommended_action": None
    }
    
    # TODO: Query actual candidate counts from Supabase
    # For now, placeholder that can be populated by Claude Code
    # This is a read-only template—actual data comes from pipeline queries
    
    # Write state snapshot
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)
    
    if output_format == "json":
        print(json.dumps(state, indent=2))
    else:  # status
        print(f"State Export: {state['updated_at']}")
        print(f"  Historical total:     {state['historical_total']}")
        print(f"  Automatable now:      {state['automatable_now']}")
        print(f"  Human review:         {state['human_review']}")
        print(f"  Recovery review:      {state['recovery_review']}")
        print(f"  Conflict:             {state['conflict']}")
        print(f"  Terminal:             {state['terminal']}")
        print(f"  Latest execution:     {state['latest_execution']['execution_id']}")
        print(f"  Latest result:        {state['latest_execution']['result']}")
    
    return state

if __name__ == "__main__":
    format_arg = sys.argv[1] if len(sys.argv) > 1 else "--status"
    output_format = "json" if format_arg == "--json" else "status"
    export_state(output_format)
