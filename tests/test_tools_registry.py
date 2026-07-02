"""Tests for the ⑰ tool registry (switchboard/tools.py).

TDD: these tests were written before the implementation and describe the
contract that MCP and CLI depend on.

Coverage:
- TOOL_DEFINITIONS is non-empty
- Each entry has the required fields with correct types
- get_tool() finds by name / returns None for unknown
- get_registry() returns a stable, complete list
- registry_as_json() round-trips back to the right names
- sync_registry_json() writes the "tools" key into registry.json
- AccessPolicy seam: AllowAllPolicy.check() always allows
- Decision dataclass is frozen/hashable
- Required tools are present: wallet_balance, pay, create_escrow,
  confirm_payment, request_refund, policy_status, escrow_metrics
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from switchboard.tools import (
    TOOL_DEFINITIONS,
    AllowAllPolicy,
    Decision,
    ToolDef,
    get_registry,
    get_tool,
    registry_as_json,
    sync_registry_json,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

REQUIRED_TOOL_NAMES = {
    "wallet_balance",
    "pay",
    "create_escrow",
    "confirm_payment",
    "request_refund",
    "policy_status",
    "escrow_metrics",
}


# ---------------------------------------------------------------------------
# Basic structure tests
# ---------------------------------------------------------------------------

class TestToolDefinitions:
    def test_registry_non_empty(self):
        assert len(TOOL_DEFINITIONS) >= 7, "Expected at least 7 tools"

    def test_all_required_tools_present(self):
        names = {t.name for t in TOOL_DEFINITIONS}
        missing = REQUIRED_TOOL_NAMES - names
        assert not missing, f"Missing required tools: {missing}"

    def test_each_tool_has_name(self):
        for t in TOOL_DEFINITIONS:
            assert isinstance(t.name, str) and t.name, f"Tool has blank name: {t}"

    def test_each_tool_has_description(self):
        for t in TOOL_DEFINITIONS:
            assert isinstance(t.description, str) and t.description, \
                f"Tool {t.name!r} has blank description"

    def test_each_tool_has_op(self):
        for t in TOOL_DEFINITIONS:
            assert isinstance(t.op, str) and t.op, \
                f"Tool {t.name!r} has blank op"

    def test_each_tool_schema_is_object_type(self):
        for t in TOOL_DEFINITIONS:
            assert isinstance(t.schema, dict), f"Tool {t.name!r} schema is not a dict"
            assert t.schema.get("type") == "object", \
                f"Tool {t.name!r} schema type is not 'object'"

    def test_each_tool_schema_has_properties(self):
        for t in TOOL_DEFINITIONS:
            assert "properties" in t.schema, \
                f"Tool {t.name!r} schema missing 'properties'"

    def test_each_tool_schema_has_required(self):
        for t in TOOL_DEFINITIONS:
            assert "required" in t.schema, \
                f"Tool {t.name!r} schema missing 'required' list"

    def test_each_tool_schema_required_is_list(self):
        for t in TOOL_DEFINITIONS:
            assert isinstance(t.schema["required"], list), \
                f"Tool {t.name!r} 'required' is not a list"

    def test_session_key_required_in_all_tools(self):
        """Every tool must require a session_key (gate by default)."""
        for t in TOOL_DEFINITIONS:
            assert "session_key" in t.schema["required"], \
                f"Tool {t.name!r} does not require 'session_key'"

    def test_policy_metadata_present(self):
        for t in TOOL_DEFINITIONS:
            assert isinstance(t.policy, dict), \
                f"Tool {t.name!r} policy is not a dict"
            assert "required_tier" in t.policy, \
                f"Tool {t.name!r} policy missing 'required_tier'"
            assert "rate_class" in t.policy, \
                f"Tool {t.name!r} policy missing 'rate_class'"

    def test_tool_def_is_frozen(self):
        t = TOOL_DEFINITIONS[0]
        with pytest.raises((AttributeError, TypeError)):
            t.name = "hacked"  # type: ignore[misc]

    def test_names_are_unique(self):
        names = [t.name for t in TOOL_DEFINITIONS]
        assert len(names) == len(set(names)), "Duplicate tool names found"


# ---------------------------------------------------------------------------
# get_registry() and get_tool()
# ---------------------------------------------------------------------------

class TestGetRegistry:
    def test_returns_list(self):
        reg = get_registry()
        assert isinstance(reg, list)

    def test_returns_all_tool_defs(self):
        reg = get_registry()
        assert len(reg) == len(TOOL_DEFINITIONS)

    def test_returns_a_copy(self):
        """Mutating the returned list must not affect TOOL_DEFINITIONS."""
        reg = get_registry()
        reg.clear()
        assert len(TOOL_DEFINITIONS) > 0


class TestGetTool:
    def test_finds_existing_tool(self):
        for name in REQUIRED_TOOL_NAMES:
            t = get_tool(name)
            assert t is not None, f"get_tool({name!r}) returned None"
            assert t.name == name

    def test_returns_none_for_unknown(self):
        assert get_tool("nonexistent_tool_xyz") is None

    def test_returns_tool_def_instance(self):
        t = get_tool("pay")
        assert isinstance(t, ToolDef)


# ---------------------------------------------------------------------------
# Specific tool schema correctness
# ---------------------------------------------------------------------------

class TestPayToolSchema:
    def test_pay_requires_chain_id(self):
        t = get_tool("pay")
        assert "chain_id" in t.schema["required"]

    def test_pay_requires_token(self):
        t = get_tool("pay")
        assert "token" in t.schema["required"]

    def test_pay_requires_amount(self):
        t = get_tool("pay")
        assert "amount" in t.schema["required"]

    def test_pay_requires_payee(self):
        t = get_tool("pay")
        assert "payee" in t.schema["required"]

    def test_pay_amount_is_integer_type(self):
        t = get_tool("pay")
        assert t.schema["properties"]["amount"]["type"] == "integer"


class TestWalletBalanceSchema:
    def test_wallet_balance_requires_chain_id(self):
        t = get_tool("wallet_balance")
        assert "chain_id" in t.schema["required"]

    def test_wallet_balance_token_optional(self):
        t = get_tool("wallet_balance")
        assert "token" not in t.schema["required"]
        assert "token" in t.schema["properties"]

    def test_chain_id_is_integer(self):
        t = get_tool("wallet_balance")
        assert t.schema["properties"]["chain_id"]["type"] == "integer"


class TestEscrowMetricsSchema:
    def test_escrow_metrics_has_optional_chain_id(self):
        t = get_tool("escrow_metrics")
        assert "chain_id" in t.schema["properties"]
        assert "chain_id" not in t.schema["required"]


# ---------------------------------------------------------------------------
# registry_as_json()
# ---------------------------------------------------------------------------

class TestRegistryAsJson:
    def test_returns_string(self):
        s = registry_as_json()
        assert isinstance(s, str)

    def test_parses_as_json(self):
        s = registry_as_json()
        data = json.loads(s)
        assert isinstance(data, list)

    def test_contains_all_tools(self):
        data = json.loads(registry_as_json())
        names = {entry["name"] for entry in data}
        assert REQUIRED_TOOL_NAMES.issubset(names)

    def test_each_entry_has_schema(self):
        data = json.loads(registry_as_json())
        for entry in data:
            assert "schema" in entry, f"Entry {entry['name']!r} missing 'schema'"

    def test_each_entry_has_op(self):
        data = json.loads(registry_as_json())
        for entry in data:
            assert "op" in entry, f"Entry {entry['name']!r} missing 'op'"

    def test_each_entry_has_policy(self):
        data = json.loads(registry_as_json())
        for entry in data:
            assert "policy" in entry, f"Entry {entry['name']!r} missing 'policy'"


# ---------------------------------------------------------------------------
# sync_registry_json()
# ---------------------------------------------------------------------------

class TestSyncRegistryJson:
    def test_writes_tools_key(self, tmp_path):
        registry_file = tmp_path / "registry.json"
        registry_file.write_text(json.dumps({
            "84532": {"name": "base-sepolia", "escrow": None, "usdc": "0xABC"},
        }))
        sync_registry_json(registry_path=registry_file)
        data = json.loads(registry_file.read_text())
        assert "tools" in data

    def test_preserves_existing_chains(self, tmp_path):
        registry_file = tmp_path / "registry.json"
        registry_file.write_text(json.dumps({"84532": {"name": "base-sepolia"}}))
        sync_registry_json(registry_path=registry_file)
        data = json.loads(registry_file.read_text())
        assert "84532" in data

    def test_tools_list_matches_registry(self, tmp_path):
        registry_file = tmp_path / "registry.json"
        registry_file.write_text(json.dumps({"84532": {}}))
        sync_registry_json(registry_path=registry_file)
        data = json.loads(registry_file.read_text())
        names = {t["name"] for t in data["tools"]}
        assert REQUIRED_TOOL_NAMES.issubset(names)

    def test_tools_entries_have_schema(self, tmp_path):
        registry_file = tmp_path / "registry.json"
        registry_file.write_text(json.dumps({"x": {}}))
        sync_registry_json(registry_path=registry_file)
        data = json.loads(registry_file.read_text())
        for t in data["tools"]:
            assert "schema" in t


# ---------------------------------------------------------------------------
# Access-policy seam tests
# ---------------------------------------------------------------------------

class TestAllowAllPolicy:
    def test_always_allows_any_action(self):
        p = AllowAllPolicy()
        d = p.check(agent_id="agent-1", action="pay")
        assert d.denied is False

    def test_always_allows_unknown_action(self):
        p = AllowAllPolicy()
        d = p.check(agent_id="agent-1", action="totally_unknown")
        assert d.denied is False

    def test_check_returns_decision(self):
        p = AllowAllPolicy()
        d = p.check(agent_id="agent-1", action="pay")
        assert isinstance(d, Decision)

    def test_decision_reason_is_none_when_allowed(self):
        p = AllowAllPolicy()
        d = p.check(agent_id="agent-1", action="pay")
        assert d.reason is None


class TestDecision:
    def test_denied_true(self):
        d = Decision(denied=True, reason="rate_limit_exceeded")
        assert d.denied is True
        assert d.reason == "rate_limit_exceeded"

    def test_denied_false(self):
        d = Decision(denied=False)
        assert d.denied is False
        assert d.reason is None

    def test_decision_is_frozen(self):
        d = Decision(denied=False)
        with pytest.raises((AttributeError, TypeError)):
            d.denied = True  # type: ignore[misc]
