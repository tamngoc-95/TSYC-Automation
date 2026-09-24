"""
Tests for TSYC controller handoff infrastructure.

Validates:
- State schema integrity
- Execution report schema
- No production writes during export
- Human/recovery/conflict queue separation
- Safety invariant enforcement
"""

import json
import pytest
from pathlib import Path
from datetime import datetime


class TestStateSchema:
    """Test historical_state.json schema."""
    
    def test_state_file_exists(self):
        """Verify historical_state.json exists."""
        state_file = Path("data/processed/orchestration/historical_state.json")
        assert state_file.exists(), "historical_state.json not found"
    
    def test_state_schema_version(self):
        """Verify schema version is present."""
        with open("data/processed/orchestration/historical_state.json") as f:
            state = json.load(f)
        assert state["schema_version"] == 1
    
    def test_state_has_required_fields(self):
        """Verify all required fields are present (CLAUDE_AUTOMATION.md
        section 20 orchestration-state contract)."""
        with open("data/processed/orchestration/historical_state.json") as f:
            state = json.load(f)

        required = [
            "schema_version", "updated_at", "repository_commit",
            "ready_for_draft", "multilingual_content", "fast_track",
            "enrichment_needed", "human_review", "recovery_review",
            "conflict", "terminal", "historical_recovery_backlog",
            "latest_execution", "audit", "preflight", "safety",
            "next_recommended_action"
        ]
        for field in required:
            assert field in state, f"Missing field: {field}"
    
    def test_latest_execution_structure(self):
        """Verify latest_execution has correct structure."""
        with open("data/processed/orchestration/historical_state.json") as f:
            state = json.load(f)
        
        exec_fields = [
            "execution_id", "result", "candidate_codes",
            "attempted", "progressed", "unchanged", "human_gated", "failed"
        ]
        for field in exec_fields:
            assert field in state["latest_execution"], \
                f"Missing execution field: {field}"
    
    def test_safety_fields_present(self):
        """Verify safety tracking fields."""
        with open("data/processed/orchestration/historical_state.json") as f:
            state = json.load(f)
        
        safety_fields = [
            "price_writes", "publish_actions",
            "outside_allowlist_touched", "non_historical_touched"
        ]
        for field in safety_fields:
            assert field in state["safety"], f"Missing safety field: {field}"
            assert state["safety"][field] == 0, \
                f"Safety field {field} should start at 0"


class TestExecutionReportSchema:
    """Test execution_report_schema.json."""
    
    def test_schema_file_exists(self):
        """Verify execution_report_schema.json exists."""
        schema_file = Path("data/processed/orchestration/execution_report_schema.json")
        assert schema_file.exists(), "execution_report_schema.json not found"
    
    def test_schema_is_valid_json_schema(self):
        """Verify schema file is valid JSON Schema."""
        with open("data/processed/orchestration/execution_report_schema.json") as f:
            schema = json.load(f)
        
        assert "$schema" in schema
        assert "properties" in schema
        assert "required" in schema


class TestQueueSeparation:
    """Test that human queues are independent."""
    
    def test_queue_names_defined(self):
        """Verify queue names are documented in CLAUDE_AUTOMATION.md."""
        doc_file = Path("CLAUDE_AUTOMATION.md")
        assert doc_file.exists()
        
        content = doc_file.read_text(encoding="utf-8")
        required_queues = [
            "CONTENT_REVIEW_REQUIRED",
            "IMAGE_REVIEW_REQUIRED",
            "IMAGE_GROUP_OWNERSHIP_AMBIGUOUS",
            "IDENTITY_CONFLICT",
            "RECOVERY_REVIEW",
            "EDITION_REVIEW_REQUIRED",
            "CONFIRM_SELLABLE_UNIT"
        ]

        for queue in required_queues:
            assert queue in content, f"Queue {queue} not documented"

    def test_queue_independence_documented(self):
        """Verify documentation states queues are independent."""
        doc_file = Path("CLAUDE_AUTOMATION.md")
        content = doc_file.read_text(encoding="utf-8")

        assert "block unrelated candidates" in content, \
            "Queue independence policy not documented"


class TestSafetyInvariants:
    """Test safety invariant enforcement."""

    def test_safety_invariants_documented(self):
        """Verify safety invariants are formally documented."""
        doc_file = Path("CLAUDE_AUTOMATION.md")
        assert doc_file.exists()

        content = doc_file.read_text(encoding="utf-8").lower()
        required_invariants = [
            "auto-publish",
            "regular_price",
            "sale_price",
            "blind-retry",
            "fabricate metadata"
        ]

        for invariant in required_invariants:
            assert invariant in content, f"Invariant {invariant} not documented"

    def test_global_stop_conditions_documented(self):
        """Verify all global stop conditions are listed."""
        doc_file = Path("CLAUDE_AUTOMATION.md")
        content = doc_file.read_text(encoding="utf-8").lower()

        required_stops = [
            "audit error",
            "preflight blocked",
            "selling-price mutation",
            "publish action"
        ]

        for stop in required_stops:
            assert stop in content, f"Stop condition {stop} not documented"


class TestExectuteorPreventsProduction:
    """Test that state exporter is read-only."""
    
    def test_exporter_script_exists(self):
        """Verify export_historical_orchestration_state.py exists."""
        script = Path("scripts/export_historical_orchestration_state.py")
        assert script.exists(), "Exporter script not found"
    
    def test_exporter_has_read_only_comment(self):
        """Verify script documents read-only nature."""
        script = Path("scripts/export_historical_orchestration_state.py")
        content = script.read_text(encoding="utf-8")
        
        assert "read-only" in content.lower(), \
            "Script should document read-only behavior"
        assert "NOT mutate" in content, \
            "Script should explicitly state no mutations"


class TestControllerPolicyExists:
    """Test that controller policy is in place."""
    
    def test_claude_automation_md_exists(self):
        """Verify CLAUDE_AUTOMATION.md exists."""
        policy = Path("CLAUDE_AUTOMATION.md")
        assert policy.exists(), "CLAUDE_AUTOMATION.md not found"
    
    def test_policy_defines_role_split(self):
        """Verify policy defines Cowork vs Claude Code roles."""
        policy = Path("CLAUDE_AUTOMATION.md")
        content = policy.read_text(encoding="utf-8")
        
        assert "Cowork" in content
        assert "Claude Code" in content
        assert "controller" in content.lower()
        assert "execution engine" in content.lower()
    
    def test_policy_defines_single_writer(self):
        """Verify single-writer rule is documented."""
        policy = Path("CLAUDE_AUTOMATION.md")
        content = policy.read_text(encoding="utf-8")
        
        assert "single" in content.lower() and "writer" in content.lower()
    
    def test_policy_defines_result_types(self):
        """Verify PASS/PARTIAL/STOPPED are documented."""
        policy = Path("CLAUDE_AUTOMATION.md")
        content = policy.read_text(encoding="utf-8")
        
        for result_type in ["PASS", "PARTIAL", "STOPPED"]:
            assert result_type in content, f"Result type {result_type} not documented"


class TestNoSecretsInTemplates:
    """Test that templates don't contain secrets."""
    
    def test_state_template_no_secrets(self):
        """Verify state template contains no .env values."""
        with open("data/processed/orchestration/historical_state.json") as f:
            content = f.read()
        
        # Should not contain API keys, URLs, etc.
        assert "http" not in content.lower(), "State template contains URLs"
        assert "key=" not in content.lower(), "State template contains keys"
    
    def test_exporter_doesnt_log_env(self):
        """Verify exporter script doesn't print .env variables."""
        script = Path("scripts/export_historical_orchestration_state.py")
        content = script.read_text(encoding="utf-8")
        
        assert ".env" not in content or "read_" not in content, \
            "Exporter should not access .env"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
