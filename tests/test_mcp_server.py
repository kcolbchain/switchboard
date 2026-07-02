"""Tests for Unit ⑮ — MCP server (switchboard/mcp_server.py).

Each tool round-trips against a mocked/fresh wallet.
Policy-denied calls return structured errors.

TDD: tests were defined before the implementation and drive the contract.

Coverage:
- initialize handshake
- tools/list returns all 7 tools from registry
- tools/call: wallet_balance, pay, create_escrow, confirm_payment,
  request_refund, policy_status, escrow_metrics
- Policy enforcement: revoked key, expired key, policy violation
- Unknown tool → error
- Missing session_key → error
- Access-policy denial → error
- serve() I/O loop processes multiple messages
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from switchboard.agent_wallet import AgentWallet, PaymentRequest, PaymentReceipt
from switchboard.delegation import Delegation, SpendPolicy
from switchboard.mcp_server import MCPServer, _POLICY_DENIED, _SESSION_INVALID, _INVALID_PARAMS, _METHOD_NOT_FOUND
from switchboard.metrics import AllMetrics, EscrowMetrics, WalletOpsMetrics, FleetHealth
from switchboard.tools import AllowAllPolicy, Decision
from switchboard.treasury import Treasury


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ETH = "0x0000000000000000000000000000000000000000"
USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
CHAIN_ID = 84532
PAYEE = "0xDeadBeef00000000000000000000000000000001"


def make_wallet(balance: int = 0, token: str = USDC) -> AgentWallet:
    """Create an AgentWallet with optional pre-funded treasury."""
    treasury = Treasury()
    if balance > 0:
        treasury.credit(chain_id=CHAIN_ID, token=token, amount=balance)
    mpc = MagicMock()
    mpc.address.return_value = "0xWalletAddress"
    mpc.sign_and_send.return_value = "0xTxHash"
    escrow = MagicMock()
    escrow.create_payment.return_value = "escrow-001"
    escrow.release_payment.return_value = True
    escrow.request_refund.return_value = True
    return AgentWallet(mpc=mpc, treasury=treasury, escrow=escrow)


def make_server(
    balance: int = 1_000_000_000,
    token: str = USDC,
    access_policy=None,
    metrics_store=None,
) -> tuple[MCPServer, Delegation]:
    wallet = make_wallet(balance=balance, token=token)
    delegation = Delegation(wallet=wallet)
    server = MCPServer(
        wallet=wallet,
        delegation=delegation,
        access_policy=access_policy or AllowAllPolicy(),
        metrics_store=metrics_store,
    )
    return server, delegation


def active_policy(
    token_allowlist=None,
    per_tx_cap=None,
    daily_cap=None,
    allowed_counterparties=None,
    hours_valid: float = 24.0,
) -> SpendPolicy:
    return SpendPolicy(
        expires_at=datetime.now(timezone.utc) + timedelta(hours=hours_valid),
        token_allowlist=token_allowlist,
        per_tx_cap=per_tx_cap,
        daily_cap=daily_cap,
        allowed_counterparties=allowed_counterparties,
    )


def call(server: MCPServer, method: str, params: dict, req_id=1) -> dict:
    """Send a single message and return the parsed response."""
    msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    return server.handle_message(msg)


def tool_call(server: MCPServer, name: str, arguments: dict, req_id=2) -> dict:
    return call(server, "tools/call", {"name": name, "arguments": arguments}, req_id)


# ---------------------------------------------------------------------------
# Initialization handshake
# ---------------------------------------------------------------------------

class TestInitialize:
    def test_initialize_returns_protocol_version(self):
        server, _ = make_server()
        resp = call(server, "initialize", {})
        assert resp["result"]["protocolVersion"] == "2024-11-05"

    def test_initialize_returns_server_info(self):
        server, _ = make_server()
        resp = call(server, "initialize", {})
        assert "serverInfo" in resp["result"]
        assert resp["result"]["serverInfo"]["name"] == "switchboard-mcp"

    def test_initialize_returns_tools_capability(self):
        server, _ = make_server()
        resp = call(server, "initialize", {})
        assert "tools" in resp["result"]["capabilities"]

    def test_initialized_notification_returns_none(self):
        server, _ = make_server()
        # initialized is a notification (no id)
        msg = {"jsonrpc": "2.0", "method": "initialized"}
        resp = server.handle_message(msg)
        assert resp is None

    def test_tools_call_before_initialize_returns_error(self):
        wallet = make_wallet()
        delegation = Delegation(wallet=wallet)
        server = MCPServer(wallet=wallet, delegation=delegation)
        # Do NOT call initialize
        resp = tool_call(server, "wallet_balance", {"session_key": "x", "chain_id": 1})
        assert "error" in resp


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------

class TestToolsList:
    def _list(self, server):
        server.handle_message({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        return call(server, "tools/list", {})

    def test_returns_all_seven_tools(self):
        server, _ = make_server()
        resp = self._list(server)
        names = {t["name"] for t in resp["result"]["tools"]}
        expected = {
            "wallet_balance", "pay", "create_escrow", "confirm_payment",
            "request_refund", "policy_status", "escrow_metrics",
        }
        assert expected.issubset(names)

    def test_each_tool_has_input_schema(self):
        server, _ = make_server()
        resp = self._list(server)
        for t in resp["result"]["tools"]:
            assert "inputSchema" in t, f"Tool {t['name']!r} missing inputSchema"

    def test_each_tool_has_description(self):
        server, _ = make_server()
        resp = self._list(server)
        for t in resp["result"]["tools"]:
            assert t.get("description"), f"Tool {t['name']!r} missing description"

    def test_tools_list_before_initialize(self):
        """tools/list should work even before initialize (it's read-only)."""
        server, _ = make_server()
        resp = call(server, "tools/list", {})
        assert "result" in resp


# ---------------------------------------------------------------------------
# wallet_balance
# ---------------------------------------------------------------------------

class TestWalletBalance:
    def setup_method(self):
        self.server, self.delegation = make_server(balance=5_000_000_000, token=USDC)
        self.server.handle_message({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        policy = active_policy()
        self.key = self.delegation.grant(agent_id="agent-1", policy=policy)

    def test_balance_returns_balance_and_spendable(self):
        resp = tool_call(self.server, "wallet_balance", {
            "session_key": self.key.key_id,
            "chain_id": CHAIN_ID,
            "token": USDC,
        })
        content = json.loads(resp["result"]["content"][0]["text"])
        assert content["balance"] == 5_000_000_000
        assert content["spendable"] == 5_000_000_000

    def test_balance_all_tokens_on_chain(self):
        resp = tool_call(self.server, "wallet_balance", {
            "session_key": self.key.key_id,
            "chain_id": CHAIN_ID,
        })
        content = json.loads(resp["result"]["content"][0]["text"])
        assert "balances" in content
        tokens = {b["token"] for b in content["balances"]}
        assert USDC in tokens

    def test_missing_chain_id_returns_error(self):
        resp = tool_call(self.server, "wallet_balance", {
            "session_key": self.key.key_id,
        })
        assert "error" in resp

    def test_missing_session_key_returns_error(self):
        resp = tool_call(self.server, "wallet_balance", {
            "chain_id": CHAIN_ID,
        })
        assert "error" in resp

    def test_unknown_session_key_returns_session_invalid_error(self):
        resp = tool_call(self.server, "wallet_balance", {
            "session_key": "nonexistent-key-id",
            "chain_id": CHAIN_ID,
        })
        assert "error" in resp
        assert resp["error"]["code"] == _SESSION_INVALID


# ---------------------------------------------------------------------------
# pay
# ---------------------------------------------------------------------------

class TestPay:
    def setup_method(self):
        self.server, self.delegation = make_server(balance=1_000_000_000, token=USDC)
        self.server.handle_message({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        policy = active_policy(token_allowlist=[USDC])
        self.key = self.delegation.grant(agent_id="agent-1", policy=policy)

    def test_successful_pay_returns_receipt(self):
        resp = tool_call(self.server, "pay", {
            "session_key": self.key.key_id,
            "chain_id": CHAIN_ID,
            "token": USDC,
            "amount": 100_000_000,
            "payee": PAYEE,
        })
        assert "result" in resp
        content = json.loads(resp["result"]["content"][0]["text"])
        assert "tx_id" in content
        assert content["amount"] == 100_000_000

    def test_pay_with_wrong_token_raises_policy_violation(self):
        resp = tool_call(self.server, "pay", {
            "session_key": self.key.key_id,
            "chain_id": CHAIN_ID,
            "token": ETH,   # not in allowlist
            "amount": 1_000_000,
            "payee": PAYEE,
        })
        assert "error" in resp
        assert resp["error"]["code"] == _POLICY_DENIED

    def test_pay_missing_payee_returns_error(self):
        resp = tool_call(self.server, "pay", {
            "session_key": self.key.key_id,
            "chain_id": CHAIN_ID,
            "token": USDC,
            "amount": 100,
        })
        assert "error" in resp

    def test_pay_missing_amount_returns_error(self):
        resp = tool_call(self.server, "pay", {
            "session_key": self.key.key_id,
            "chain_id": CHAIN_ID,
            "token": USDC,
            "payee": PAYEE,
        })
        assert "error" in resp

    def test_pay_over_per_tx_cap_returns_policy_denied(self):
        server, delegation = make_server(balance=1_000_000_000, token=USDC)
        server.handle_message({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        policy = active_policy(token_allowlist=[USDC], per_tx_cap=500_000)
        key = delegation.grant(agent_id="agent-cap", policy=policy)
        resp = tool_call(server, "pay", {
            "session_key": key.key_id,
            "chain_id": CHAIN_ID,
            "token": USDC,
            "amount": 999_999_999,
            "payee": PAYEE,
        })
        assert "error" in resp
        assert resp["error"]["code"] == _POLICY_DENIED

    def test_revoked_key_pay_returns_policy_denied(self):
        policy = active_policy()
        key = self.delegation.grant(agent_id="agent-revoke", policy=policy)
        self.delegation.revoke(key)
        resp = tool_call(self.server, "pay", {
            "session_key": key.key_id,
            "chain_id": CHAIN_ID,
            "token": USDC,
            "amount": 100,
            "payee": PAYEE,
        })
        assert "error" in resp
        # revoked key has been removed → SESSION_INVALID
        assert resp["error"]["code"] in (_SESSION_INVALID, _POLICY_DENIED)

    def test_expired_key_pay_returns_policy_denied(self):
        server, delegation = make_server(balance=1_000_000_000, token=USDC)
        server.handle_message({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        expired_policy = SpendPolicy(
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        key = delegation.grant(agent_id="agent-expired", policy=expired_policy)
        resp = tool_call(server, "pay", {
            "session_key": key.key_id,
            "chain_id": CHAIN_ID,
            "token": USDC,
            "amount": 100,
            "payee": PAYEE,
        })
        assert "error" in resp
        assert resp["error"]["code"] == _POLICY_DENIED


# ---------------------------------------------------------------------------
# create_escrow
# ---------------------------------------------------------------------------

class TestCreateEscrow:
    def setup_method(self):
        self.server, self.delegation = make_server(balance=1_000_000_000, token=USDC)
        self.server.handle_message({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        policy = active_policy()
        self.key = self.delegation.grant(agent_id="agent-escrow", policy=policy)

    def test_create_escrow_returns_escrow_id(self):
        resp = tool_call(self.server, "create_escrow", {
            "session_key": self.key.key_id,
            "chain_id": CHAIN_ID,
            "token": USDC,
            "amount": 100_000_000,
            "payee": PAYEE,
        })
        assert "result" in resp
        content = json.loads(resp["result"]["content"][0]["text"])
        assert "escrow_id" in content
        assert content["status"] == "Locked"

    def test_create_escrow_missing_payee_returns_error(self):
        resp = tool_call(self.server, "create_escrow", {
            "session_key": self.key.key_id,
            "chain_id": CHAIN_ID,
            "token": USDC,
            "amount": 100_000_000,
        })
        assert "error" in resp


# ---------------------------------------------------------------------------
# confirm_payment
# ---------------------------------------------------------------------------

class TestConfirmPayment:
    def setup_method(self):
        self.server, self.delegation = make_server(balance=1_000_000_000, token=USDC)
        self.server.handle_message({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        policy = active_policy()
        self.key = self.delegation.grant(agent_id="agent-confirm", policy=policy)

    def test_confirm_payment_returns_released_true(self):
        resp = tool_call(self.server, "confirm_payment", {
            "session_key": self.key.key_id,
            "escrow_id": "escrow-001",
        })
        assert "result" in resp
        content = json.loads(resp["result"]["content"][0]["text"])
        assert content["released"] is True

    def test_confirm_payment_missing_escrow_id_returns_error(self):
        resp = tool_call(self.server, "confirm_payment", {
            "session_key": self.key.key_id,
        })
        assert "error" in resp


# ---------------------------------------------------------------------------
# request_refund
# ---------------------------------------------------------------------------

class TestRequestRefund:
    def setup_method(self):
        self.server, self.delegation = make_server()
        self.server.handle_message({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        policy = active_policy()
        self.key = self.delegation.grant(agent_id="agent-refund", policy=policy)

    def test_request_refund_succeeds(self):
        resp = tool_call(self.server, "request_refund", {
            "session_key": self.key.key_id,
            "escrow_id": "escrow-002",
        })
        assert "result" in resp
        content = json.loads(resp["result"]["content"][0]["text"])
        assert content["refund_requested"] is True

    def test_request_refund_missing_escrow_id_returns_error(self):
        resp = tool_call(self.server, "request_refund", {
            "session_key": self.key.key_id,
        })
        assert "error" in resp


# ---------------------------------------------------------------------------
# policy_status
# ---------------------------------------------------------------------------

class TestPolicyStatus:
    def setup_method(self):
        self.server, self.delegation = make_server()
        self.server.handle_message({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        policy = active_policy(token_allowlist=[USDC], per_tx_cap=1_000_000)
        self.key = self.delegation.grant(agent_id="agent-status", policy=policy)

    def test_policy_status_returns_key_id(self):
        resp = tool_call(self.server, "policy_status", {
            "session_key": self.key.key_id,
        })
        assert "result" in resp
        content = json.loads(resp["result"]["content"][0]["text"])
        assert content["key_id"] == self.key.key_id

    def test_policy_status_shows_active(self):
        resp = tool_call(self.server, "policy_status", {
            "session_key": self.key.key_id,
        })
        content = json.loads(resp["result"]["content"][0]["text"])
        assert content["active"] is True

    def test_policy_status_shows_token_allowlist(self):
        resp = tool_call(self.server, "policy_status", {
            "session_key": self.key.key_id,
        })
        content = json.loads(resp["result"]["content"][0]["text"])
        assert content["token_allowlist"] == [USDC]

    def test_policy_status_shows_per_tx_cap(self):
        resp = tool_call(self.server, "policy_status", {
            "session_key": self.key.key_id,
        })
        content = json.loads(resp["result"]["content"][0]["text"])
        assert content["per_tx_cap"] == 1_000_000

    def test_policy_status_shows_agent_id(self):
        resp = tool_call(self.server, "policy_status", {
            "session_key": self.key.key_id,
        })
        content = json.loads(resp["result"]["content"][0]["text"])
        assert content["agent_id"] == "agent-status"

    def test_policy_status_not_expired(self):
        resp = tool_call(self.server, "policy_status", {
            "session_key": self.key.key_id,
        })
        content = json.loads(resp["result"]["content"][0]["text"])
        assert content["expired"] is False


# ---------------------------------------------------------------------------
# escrow_metrics
# ---------------------------------------------------------------------------

class TestEscrowMetrics:
    def test_escrow_metrics_returns_fill_rate(self):
        metrics = AllMetrics(
            escrow=EscrowMetrics(
                total_count=10,
                released_count=8,
                fill_rate=0.8,
                timeout_rate=0.1,
                refund_rate=0.1,
                avg_time_to_release_s=120.0,
            ),
            wallet_ops=WalletOpsMetrics(),
            fleet=FleetHealth(),
        )
        server, delegation = make_server(metrics_store=metrics)
        server.handle_message({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        policy = active_policy()
        key = delegation.grant(agent_id="agent-metrics", policy=policy)

        resp = tool_call(server, "escrow_metrics", {
            "session_key": key.key_id,
        })
        assert "result" in resp
        content = json.loads(resp["result"]["content"][0]["text"])
        assert content["fill_rate"] == pytest.approx(0.8)
        assert content["total_count"] == 10

    def test_escrow_metrics_no_store_returns_nulls(self):
        server, delegation = make_server(metrics_store=None)
        server.handle_message({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        policy = active_policy()
        key = delegation.grant(agent_id="agent-m2", policy=policy)
        resp = tool_call(server, "escrow_metrics", {
            "session_key": key.key_id,
        })
        content = json.loads(resp["result"]["content"][0]["text"])
        assert content["fill_rate"] is None
        assert content["total_count"] == 0


# ---------------------------------------------------------------------------
# Unknown tool
# ---------------------------------------------------------------------------

class TestUnknownTool:
    def test_unknown_tool_returns_method_not_found(self):
        server, delegation = make_server()
        server.handle_message({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        policy = active_policy()
        key = delegation.grant(agent_id="agent-x", policy=policy)
        resp = tool_call(server, "totally_unknown_tool", {
            "session_key": key.key_id,
        })
        assert "error" in resp
        assert resp["error"]["code"] == _METHOD_NOT_FOUND


# ---------------------------------------------------------------------------
# Access-policy denial
# ---------------------------------------------------------------------------

class TestAccessPolicyDenial:
    def test_access_policy_denial_returns_policy_denied_error(self):
        class DenyAllPolicy:
            def check(self, agent_id: str, action: str) -> Decision:
                return Decision(denied=True, reason="tier_insufficient")

        server, delegation = make_server(access_policy=DenyAllPolicy())
        server.handle_message({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        policy = active_policy()
        key = delegation.grant(agent_id="agent-deny", policy=policy)

        resp = tool_call(server, "pay", {
            "session_key": key.key_id,
            "chain_id": CHAIN_ID,
            "token": USDC,
            "amount": 100,
            "payee": PAYEE,
        })
        assert "error" in resp
        assert resp["error"]["code"] == _POLICY_DENIED
        assert "tier_insufficient" in resp["error"]["message"]

    def test_wallet_balance_gated_by_policy(self):
        class DenyAll:
            def check(self, agent_id, action):
                return Decision(denied=True, reason="no_access")

        server, delegation = make_server(access_policy=DenyAll())
        server.handle_message({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        policy = active_policy()
        key = delegation.grant(agent_id="agent-deny2", policy=policy)
        resp = tool_call(server, "wallet_balance", {
            "session_key": key.key_id,
            "chain_id": CHAIN_ID,
        })
        assert "error" in resp
        assert resp["error"]["code"] == _POLICY_DENIED


# ---------------------------------------------------------------------------
# serve() I/O loop
# ---------------------------------------------------------------------------

class TestServeLoop:
    def test_serve_processes_multiple_messages(self):
        server, delegation = make_server()
        policy = active_policy()
        key = delegation.grant(agent_id="agent-loop", policy=policy)

        messages = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
            json.dumps({
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "policy_status", "arguments": {"session_key": key.key_id}},
            }),
        ]
        in_stream = io.StringIO("\n".join(messages) + "\n")
        out_stream = io.StringIO()
        server._in = in_stream
        server._out = out_stream

        server.serve()

        output = out_stream.getvalue().strip().splitlines()
        assert len(output) == 3
        responses = [json.loads(line) for line in output]
        assert responses[0]["id"] == 1
        assert responses[1]["id"] == 2
        assert responses[2]["id"] == 3

    def test_serve_handles_parse_error_gracefully(self):
        server, _ = make_server()
        in_stream = io.StringIO("not valid json\n")
        out_stream = io.StringIO()
        server._in = in_stream
        server._out = out_stream

        server.serve()

        output = out_stream.getvalue().strip()
        resp = json.loads(output)
        assert "error" in resp
        assert resp["error"]["code"] == -32700  # PARSE_ERROR

    def test_serve_handles_empty_lines(self):
        server, _ = make_server()
        in_stream = io.StringIO("\n\n\n")
        out_stream = io.StringIO()
        server._in = in_stream
        server._out = out_stream
        # Should not raise
        server.serve()
        assert out_stream.getvalue() == ""

    def test_ping_returns_empty_result(self):
        server, _ = make_server()
        resp = server.handle_message({"jsonrpc": "2.0", "id": 99, "method": "ping", "params": {}})
        assert resp["id"] == 99
        assert resp["result"] == {}
