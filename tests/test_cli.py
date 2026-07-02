"""Tests for Unit ⑯ — CLI (switchboard/cli.py).

Uses click.testing.CliRunner to invoke commands and check output.

Coverage:
- switchboard wallet balance (with token / without token)
- switchboard wallet grant (with / without options)
- switchboard wallet revoke (valid / invalid key_id)
- switchboard escrow create / confirm / refund / status
- switchboard metrics
- switchboard tools (list registry)
- --help on every command group
- JSON output validity (all commands write parseable JSON)
- Non-zero exit on missing required options
- Smoke: the same underlying operations as MCP
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from switchboard.cli import cli
from switchboard.delegation import Delegation, SpendPolicy
from switchboard.treasury import Treasury
from switchboard.agent_wallet import AgentWallet


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
ETH  = "0x0000000000000000000000000000000000000000"
CHAIN_ID = 84532
PAYEE = "0xDeadBeef00000000000000000000000000000001"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

runner = CliRunner()


def invoke(*args, **kwargs):
    """Invoke the CLI and return the result. Raises on traceback."""
    return runner.invoke(cli, args, **kwargs)


def parse(result) -> dict | list:
    """Parse result.output as JSON."""
    return json.loads(result.output)


def _patched_wallet(balance: int = 0, token: str = USDC):
    """Build an AgentWallet with mock MPC + escrow, suitable for CLI tests."""
    treasury = Treasury()
    if balance > 0:
        treasury.credit(chain_id=CHAIN_ID, token=token, amount=balance)
    mpc = MagicMock()
    mpc.address.return_value = "0xWallet"
    mpc.sign_and_send.return_value = "0xTxHash"
    escrow = MagicMock()
    escrow.create_payment.return_value = "escrow-cli-001"
    escrow.release_payment.return_value = True
    escrow.request_refund.return_value = True
    return AgentWallet(mpc=mpc, treasury=treasury, escrow=escrow)


# ---------------------------------------------------------------------------
# Help / version
# ---------------------------------------------------------------------------

class TestHelp:
    def test_root_help(self):
        r = invoke("--help")
        assert r.exit_code == 0
        assert "wallet" in r.output.lower() or "Wallet" in r.output

    def test_wallet_help(self):
        r = invoke("wallet", "--help")
        assert r.exit_code == 0

    def test_wallet_balance_help(self):
        r = invoke("wallet", "balance", "--help")
        assert r.exit_code == 0
        assert "--chain-id" in r.output

    def test_wallet_grant_help(self):
        r = invoke("wallet", "grant", "--help")
        assert r.exit_code == 0
        assert "--agent-id" in r.output

    def test_wallet_revoke_help(self):
        r = invoke("wallet", "revoke", "--help")
        assert r.exit_code == 0

    def test_escrow_help(self):
        r = invoke("escrow", "--help")
        assert r.exit_code == 0

    def test_escrow_create_help(self):
        r = invoke("escrow", "create", "--help")
        assert r.exit_code == 0

    def test_escrow_confirm_help(self):
        r = invoke("escrow", "confirm", "--help")
        assert r.exit_code == 0

    def test_escrow_refund_help(self):
        r = invoke("escrow", "refund", "--help")
        assert r.exit_code == 0

    def test_escrow_status_help(self):
        r = invoke("escrow", "status", "--help")
        assert r.exit_code == 0

    def test_metrics_help(self):
        r = invoke("metrics", "--help")
        assert r.exit_code == 0

    def test_tools_help(self):
        r = invoke("tools", "--help")
        assert r.exit_code == 0

    def test_version(self):
        r = invoke("--version")
        assert r.exit_code == 0
        assert "0.1.0" in r.output


# ---------------------------------------------------------------------------
# wallet balance
# ---------------------------------------------------------------------------

class TestWalletBalance:
    def test_balance_single_token(self):
        wallet = _patched_wallet(balance=42_000_000, token=USDC)
        delegation = Delegation(wallet=wallet)
        import switchboard.cli as cli_mod
        original_wallet = cli_mod._wallet
        original_delegation = cli_mod._delegation
        cli_mod._wallet = wallet
        cli_mod._delegation = delegation
        try:
            r = invoke("wallet", "balance", "--chain-id", str(CHAIN_ID), "--token", USDC)
            assert r.exit_code == 0
            data = parse(r)
            assert data["balance"] == 42_000_000
            assert data["spendable"] == 42_000_000
            assert data["token"] == USDC
        finally:
            cli_mod._wallet = original_wallet
            cli_mod._delegation = original_delegation

    def test_balance_all_tokens(self):
        wallet = _patched_wallet(balance=100_000, token=USDC)
        delegation = Delegation(wallet=wallet)
        import switchboard.cli as cli_mod
        original_wallet = cli_mod._wallet
        original_delegation = cli_mod._delegation
        cli_mod._wallet = wallet
        cli_mod._delegation = delegation
        try:
            r = invoke("wallet", "balance", "--chain-id", str(CHAIN_ID))
            assert r.exit_code == 0
            data = parse(r)
            assert "balances" in data
            tokens = [b["token"] for b in data["balances"]]
            assert USDC in tokens
        finally:
            cli_mod._wallet = original_wallet
            cli_mod._delegation = original_delegation

    def test_balance_missing_chain_id_exits_nonzero(self):
        r = invoke("wallet", "balance")
        assert r.exit_code != 0

    def test_balance_output_is_json(self):
        wallet = _patched_wallet(balance=1_000, token=USDC)
        import switchboard.cli as cli_mod
        cli_mod._wallet = wallet
        cli_mod._delegation = Delegation(wallet=wallet)
        try:
            r = invoke("wallet", "balance", "--chain-id", str(CHAIN_ID), "--token", USDC)
            assert r.exit_code == 0
            json.loads(r.output)  # must not raise
        finally:
            cli_mod._wallet = None
            cli_mod._delegation = None


# ---------------------------------------------------------------------------
# wallet grant
# ---------------------------------------------------------------------------

class TestWalletGrant:
    def _grant(self, *extra_args):
        import switchboard.cli as cli_mod
        wallet = _patched_wallet()
        cli_mod._wallet = wallet
        cli_mod._delegation = Delegation(wallet=wallet)
        r = invoke("wallet", "grant", "--agent-id", "agent-test", *extra_args)
        cli_mod._wallet = None
        cli_mod._delegation = None
        return r

    def test_grant_returns_key_id(self):
        r = self._grant()
        assert r.exit_code == 0
        data = parse(r)
        assert "key_id" in data
        assert len(data["key_id"]) > 0

    def test_grant_returns_agent_id(self):
        r = self._grant()
        data = parse(r)
        assert data["agent_id"] == "agent-test"

    def test_grant_with_per_tx_cap(self):
        r = self._grant("--per-tx-cap", "500000")
        assert r.exit_code == 0
        data = parse(r)
        assert data["per_tx_cap"] == 500000

    def test_grant_with_token_allowlist(self):
        r = self._grant("--token", USDC)
        assert r.exit_code == 0
        data = parse(r)
        assert data["token_allowlist"] == [USDC]

    def test_grant_with_daily_cap(self):
        r = self._grant("--daily-cap", "10000000")
        data = parse(r)
        assert data["daily_cap"] == 10000000

    def test_grant_with_expires_in_hours(self):
        r = self._grant("--expires-in-hours", "2")
        assert r.exit_code == 0
        data = parse(r)
        assert "expires_at" in data

    def test_grant_no_agent_id_exits_nonzero(self):
        r = invoke("wallet", "grant")
        assert r.exit_code != 0

    def test_grant_output_is_json(self):
        r = self._grant()
        json.loads(r.output)

    def test_grant_null_allowlist_when_no_token(self):
        r = self._grant()
        data = parse(r)
        assert data["token_allowlist"] is None

    def test_grant_with_counterparty(self):
        r = self._grant("--counterparty", PAYEE)
        data = parse(r)
        assert data["allowed_counterparties"] == [PAYEE]


# ---------------------------------------------------------------------------
# wallet revoke
# ---------------------------------------------------------------------------

class TestWalletRevoke:
    def test_revoke_valid_key(self):
        import switchboard.cli as cli_mod
        wallet = _patched_wallet()
        delegation = Delegation(wallet=wallet)
        cli_mod._wallet = wallet
        cli_mod._delegation = delegation

        # Grant first
        r_grant = invoke("wallet", "grant", "--agent-id", "agent-r")
        key_id = parse(r_grant)["key_id"]

        r_revoke = invoke("wallet", "revoke", "--key-id", key_id)
        assert r_revoke.exit_code == 0
        data = parse(r_revoke)
        assert data["revoked"] is True
        assert data["key_id"] == key_id

        cli_mod._wallet = None
        cli_mod._delegation = None

    def test_revoke_unknown_key_exits_nonzero(self):
        import switchboard.cli as cli_mod
        wallet = _patched_wallet()
        cli_mod._wallet = wallet
        cli_mod._delegation = Delegation(wallet=wallet)
        r = invoke("wallet", "revoke", "--key-id", "nonexistent-key")
        assert r.exit_code != 0
        cli_mod._wallet = None
        cli_mod._delegation = None

    def test_revoke_missing_key_id_exits_nonzero(self):
        r = invoke("wallet", "revoke")
        assert r.exit_code != 0


# ---------------------------------------------------------------------------
# escrow create
# ---------------------------------------------------------------------------

class TestEscrowCreate:
    def _setup(self):
        import switchboard.cli as cli_mod
        wallet = _patched_wallet(balance=1_000_000_000, token=USDC)
        delegation = Delegation(wallet=wallet)
        policy = SpendPolicy(
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        key = delegation.grant(agent_id="agent-e", policy=policy)
        cli_mod._wallet = wallet
        cli_mod._delegation = delegation
        return key.key_id

    def _teardown(self):
        import switchboard.cli as cli_mod
        cli_mod._wallet = None
        cli_mod._delegation = None

    def test_create_escrow_returns_escrow_id(self):
        key_id = self._setup()
        try:
            r = invoke(
                "escrow", "create",
                "--session-key", key_id,
                "--chain-id", str(CHAIN_ID),
                "--token", USDC,
                "--amount", "100000",
                "--payee", PAYEE,
            )
            assert r.exit_code == 0
            data = parse(r)
            assert "escrow_id" in data
            assert data["status"] == "Locked"
        finally:
            self._teardown()

    def test_create_escrow_missing_payee_exits_nonzero(self):
        key_id = self._setup()
        try:
            r = invoke(
                "escrow", "create",
                "--session-key", key_id,
                "--chain-id", str(CHAIN_ID),
                "--token", USDC,
                "--amount", "100000",
            )
            assert r.exit_code != 0
        finally:
            self._teardown()

    def test_create_escrow_invalid_session_key_exits_nonzero(self):
        self._setup()
        try:
            r = invoke(
                "escrow", "create",
                "--session-key", "bad-key",
                "--chain-id", str(CHAIN_ID),
                "--token", USDC,
                "--amount", "100000",
                "--payee", PAYEE,
            )
            assert r.exit_code != 0
        finally:
            self._teardown()


# ---------------------------------------------------------------------------
# escrow confirm
# ---------------------------------------------------------------------------

class TestEscrowConfirm:
    def _setup(self):
        import switchboard.cli as cli_mod
        wallet = _patched_wallet()
        delegation = Delegation(wallet=wallet)
        policy = SpendPolicy(
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        key = delegation.grant(agent_id="agent-c", policy=policy)
        cli_mod._wallet = wallet
        cli_mod._delegation = delegation
        return key.key_id

    def _teardown(self):
        import switchboard.cli as cli_mod
        cli_mod._wallet = None
        cli_mod._delegation = None

    def test_confirm_returns_released_true(self):
        key_id = self._setup()
        try:
            r = invoke(
                "escrow", "confirm",
                "--session-key", key_id,
                "--escrow-id", "escrow-001",
            )
            assert r.exit_code == 0
            data = parse(r)
            assert data["released"] is True
        finally:
            self._teardown()

    def test_confirm_missing_escrow_id_exits_nonzero(self):
        key_id = self._setup()
        try:
            r = invoke("escrow", "confirm", "--session-key", key_id)
            assert r.exit_code != 0
        finally:
            self._teardown()


# ---------------------------------------------------------------------------
# escrow refund
# ---------------------------------------------------------------------------

class TestEscrowRefund:
    def _setup(self):
        import switchboard.cli as cli_mod
        wallet = _patched_wallet()
        delegation = Delegation(wallet=wallet)
        policy = SpendPolicy(
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        key = delegation.grant(agent_id="agent-rf", policy=policy)
        cli_mod._wallet = wallet
        cli_mod._delegation = delegation
        return key.key_id

    def _teardown(self):
        import switchboard.cli as cli_mod
        cli_mod._wallet = None
        cli_mod._delegation = None

    def test_refund_returns_refund_requested_true(self):
        key_id = self._setup()
        try:
            r = invoke(
                "escrow", "refund",
                "--session-key", key_id,
                "--escrow-id", "escrow-002",
                "--reason", "delivery failed",
            )
            assert r.exit_code == 0
            data = parse(r)
            assert data["refund_requested"] is True
        finally:
            self._teardown()

    def test_refund_missing_escrow_id_exits_nonzero(self):
        key_id = self._setup()
        try:
            r = invoke("escrow", "refund", "--session-key", key_id)
            assert r.exit_code != 0
        finally:
            self._teardown()


# ---------------------------------------------------------------------------
# escrow status
# ---------------------------------------------------------------------------

class TestEscrowStatus:
    def _setup(self):
        import switchboard.cli as cli_mod
        wallet = _patched_wallet()
        delegation = Delegation(wallet=wallet)
        policy = SpendPolicy(
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        key = delegation.grant(agent_id="agent-st", policy=policy)
        cli_mod._wallet = wallet
        cli_mod._delegation = delegation
        return key.key_id

    def _teardown(self):
        import switchboard.cli as cli_mod
        cli_mod._wallet = None
        cli_mod._delegation = None

    def test_status_returns_escrow_id(self):
        key_id = self._setup()
        try:
            r = invoke(
                "escrow", "status",
                "--session-key", key_id,
                "--escrow-id", "escrow-003",
            )
            assert r.exit_code == 0
            data = parse(r)
            assert "escrow_id" in data
            assert "status" in data
        finally:
            self._teardown()

    def test_status_missing_escrow_id_exits_nonzero(self):
        key_id = self._setup()
        try:
            r = invoke("escrow", "status", "--session-key", key_id)
            assert r.exit_code != 0
        finally:
            self._teardown()


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

class TestMetrics:
    def test_metrics_returns_json(self):
        r = invoke("metrics")
        assert r.exit_code == 0
        data = parse(r)
        assert "escrow" in data
        assert "wallet_ops" in data
        assert "fleet" in data

    def test_metrics_escrow_has_fill_rate(self):
        r = invoke("metrics")
        data = parse(r)
        # fill_rate is None when no events — that's correct
        assert "fill_rate" in data["escrow"]

    def test_metrics_escrow_has_total_count(self):
        r = invoke("metrics")
        data = parse(r)
        assert data["escrow"]["total_count"] == 0

    def test_metrics_fleet_has_active_wallet_count(self):
        r = invoke("metrics")
        data = parse(r)
        assert "active_wallet_count" in data["fleet"]

    def test_metrics_with_chain_id_flag(self):
        r = invoke("metrics", "--chain-id", "84532")
        assert r.exit_code == 0
        data = parse(r)
        assert "escrow" in data


# ---------------------------------------------------------------------------
# tools list
# ---------------------------------------------------------------------------

class TestToolsList:
    def test_tools_returns_list(self):
        r = invoke("tools")
        assert r.exit_code == 0
        data = parse(r)
        assert isinstance(data, list)

    def test_tools_has_all_required(self):
        r = invoke("tools")
        data = parse(r)
        names = {t["name"] for t in data}
        required = {
            "wallet_balance", "pay", "create_escrow", "confirm_payment",
            "request_refund", "policy_status", "escrow_metrics",
        }
        assert required.issubset(names)

    def test_tools_each_has_description(self):
        r = invoke("tools")
        data = parse(r)
        for t in data:
            assert t.get("description"), f"Tool {t['name']!r} missing description"

    def test_tools_each_has_op(self):
        r = invoke("tools")
        data = parse(r)
        for t in data:
            assert "op" in t

    def test_tools_output_is_json(self):
        r = invoke("tools")
        json.loads(r.output)   # must not raise


# ---------------------------------------------------------------------------
# CLI drives same core as MCP (smoke test)
# ---------------------------------------------------------------------------

class TestCliDrivesSameCoreAsMCP:
    def test_grant_and_then_balance_share_same_delegation(self):
        """Grant a key via CLI then use it in the MCP server — same delegation."""
        import switchboard.cli as cli_mod
        from switchboard.mcp_server import MCPServer

        wallet = _patched_wallet(balance=999_999, token=USDC)
        delegation = Delegation(wallet=wallet)
        cli_mod._wallet = wallet
        cli_mod._delegation = delegation

        # Grant key via CLI
        r = invoke("wallet", "grant", "--agent-id", "shared-agent")
        key_id = parse(r)["key_id"]

        # The same delegation is visible to the MCP server
        server = MCPServer(wallet=wallet, delegation=delegation)
        server.handle_message({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})

        import json as _json
        resp = server.handle_message({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "policy_status", "arguments": {"session_key": key_id}},
        })
        content = _json.loads(resp["result"]["content"][0]["text"])
        assert content["key_id"] == key_id
        assert content["agent_id"] == "shared-agent"

        cli_mod._wallet = None
        cli_mod._delegation = None
