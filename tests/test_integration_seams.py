"""Integration tests for the agent-wallet / multi-token-settlement wiring pass.

Each ``class`` here corresponds to one reconciled *seam* between units that were
built in parallel.  These are integration tests: they assert the units compose
into one working system, not the internal behavior of any single unit (that is
covered by the per-unit suites).

Seams
-----
1. Canonical ``PaymentRequest`` — ``AgentWallet`` uses ``src.payment_protocol``'s
   ``PaymentRequest`` (with ``settlement_token``), not a private copy.
2. Single ``WalletOpEvent`` — ``access_policy`` emits ``metrics.WalletOpEvent`` so
   denials flow to the ⑳ dashboard.
3. ``AccessPolicy`` satisfies the MCP ``AccessPolicy`` Protocol and the real
   engine gates MCP calls.
4. ``Router`` sits in the ``AgentWallet.pay`` path: it picks (token, rail,
   wallet), consults ``access_policy``, and emits a ``WalletOpEvent``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest


# Token addresses used across the seams
ETH = "0x0000000000000000000000000000000000000000"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
CHAIN_1 = 1
PAYEE = "0xPayee000000000000000000000000000000000001"


# ===========================================================================
# Seam 1 — Canonical PaymentRequest
# ===========================================================================


class TestSeam1CanonicalPaymentRequest:
    def test_agent_wallet_reexports_canonical_payment_request(self):
        """``switchboard.agent_wallet.PaymentRequest`` IS the canonical protocol type."""
        from switchboard.agent_wallet import PaymentRequest as WalletPR
        from src.payment_protocol import PaymentRequest as CanonicalPR

        assert WalletPR is CanonicalPR

    def test_canonical_request_carries_settlement_token_field(self):
        from switchboard.agent_wallet import PaymentRequest

        req = PaymentRequest(chain_id=CHAIN_1, token=USDC, amount_wei=100, payee=PAYEE)
        # settlement_token is the v1.2 negotiation result field.
        assert hasattr(req, "settlement_token")
        assert req.settlement_token is None

    def test_amount_alias_reads_amount_wei(self):
        from switchboard.agent_wallet import PaymentRequest

        req = PaymentRequest(chain_id=CHAIN_1, token=USDC, amount_wei=42, payee=PAYEE)
        assert req.amount == 42
        assert req.amount_wei == 42

    def test_delegation_shares_the_same_request_type(self):
        """Delegation imports PaymentRequest transitively; must be the canonical one."""
        from switchboard.delegation import PaymentRequest as DelegationPR
        from src.payment_protocol import PaymentRequest as CanonicalPR

        assert DelegationPR is CanonicalPR

    def test_pay_end_to_end_with_canonical_request(self):
        from switchboard.agent_wallet import AgentWallet, PaymentRequest, EscrowClient
        from switchboard.mpc_wallet import MPCWallet
        from switchboard.treasury import Treasury

        treasury = Treasury()
        treasury.credit(CHAIN_1, USDC, 1_000_000)
        escrow = MagicMock(spec=EscrowClient)
        escrow.create_payment.return_value = "0xescrow_seam1"
        escrow.release_payment.return_value = True
        wallet = AgentWallet(mpc=MPCWallet(), treasury=treasury, escrow=escrow)

        req = PaymentRequest(chain_id=CHAIN_1, token=USDC, amount_wei=250_000, payee=PAYEE)
        receipt = wallet.pay(req)

        assert receipt.token == USDC
        assert receipt.amount == 250_000
        assert receipt.escrow_id == "0xescrow_seam1"
        assert treasury.balance(CHAIN_1, USDC) == 750_000

    def test_token_field_off_the_v1_wire_and_hash(self):
        """The multi-token ``token`` field must not perturb the frozen wire/hash."""
        from switchboard.agent_wallet import PaymentRequest

        bare = PaymentRequest(request_id="w", payer="0xA", payee="0xB", amount_wei=10**18)
        with_tok = PaymentRequest(
            request_id="w", payer="0xA", payee="0xB", amount_wei=10**18, token=USDC
        )
        # token at default ("") is omitted from the wire entirely
        assert "token" not in bare.to_json()
        # a set token never changes the content hash (it is a wallet-side selection)
        assert bare.content_hash() == with_tok.content_hash()


# ===========================================================================
# Seam 2 — Single WalletOpEvent
# ===========================================================================


class TestSeam2SingleWalletOpEvent:
    def test_access_policy_reexports_metrics_event(self):
        """access_policy.WalletOpEvent IS metrics.WalletOpEvent — one canonical type."""
        from switchboard.access_policy import WalletOpEvent as AP_Event
        from switchboard.metrics import WalletOpEvent as Metrics_Event

        assert AP_Event is Metrics_Event

    def test_denial_event_has_full_metrics_shape(self):
        """A denial from AccessPolicy emits an event with every dashboard field."""
        from switchboard.access_policy import (
            AccessPolicy,
            AgentTier,
            TierConfig,
            TokenBucketConfig,
        )
        from switchboard.delegation import SpendPolicy

        # Force a tier-ceiling denial with amount over the cap.
        cfg = TierConfig(
            explorer=TokenBucketConfig(per_tx_cap=10, rate=0, capacity=5),
            standard=TokenBucketConfig(per_tx_cap=10, rate=0, capacity=5),
            trusted=TokenBucketConfig(per_tx_cap=10, rate=0, capacity=5),
        )
        policy = AccessPolicy(tier_config=cfg)
        sp = SpendPolicy(expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc))
        policy.register("agent-x", tier=AgentTier.STANDARD, spend_policy=sp)

        d = policy.check("agent-x", {"type": "pay", "amount": 9999, "token": USDC})
        assert d.allowed is False
        evt = d.event
        # Every canonical field must be present and populated from the action.
        for fld in (
            "op_type", "token", "rail", "amount", "agent_id",
            "wallet_id", "denied", "denial_reason", "timestamp",
        ):
            assert hasattr(evt, fld), f"missing metrics field {fld!r}"
        assert evt.op_type == "pay"
        assert evt.token == USDC
        assert evt.amount == 9999.0
        assert evt.agent_id == "agent-x"
        assert evt.denied is True
        assert evt.denial_reason == "tier_ceiling"

    def test_denials_flow_into_dashboard_metrics(self):
        """Emitted denial events feed compute_wallet_ops_metrics unchanged."""
        from switchboard.access_policy import (
            AccessPolicy,
            AgentTier,
            TierConfig,
            TokenBucketConfig,
        )
        from switchboard.delegation import SpendPolicy
        from switchboard.metrics import compute_wallet_ops_metrics

        collected = []
        cfg = TierConfig(
            explorer=TokenBucketConfig(per_tx_cap=100, rate=0, capacity=1),
            standard=TokenBucketConfig(per_tx_cap=100, rate=0, capacity=1),
            trusted=TokenBucketConfig(per_tx_cap=100, rate=0, capacity=1),
        )
        policy = AccessPolicy(tier_config=cfg, event_listener=collected.append)
        sp = SpendPolicy(expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc))
        policy.register("dash-agent", tier=AgentTier.STANDARD, spend_policy=sp)

        policy.check("dash-agent", {"type": "pay", "amount": 10, "token": USDC})  # allow
        policy.check("dash-agent", {"type": "pay", "amount": 10, "token": USDC})  # deny (bucket)

        # The dashboard's own compute function consumes them directly.
        m = compute_wallet_ops_metrics(collected)
        assert m.total_ops == 2
        assert m.policy_denial_count == 1
        assert m.denials_by_reason.get("rate_limited") == 1


# ===========================================================================
# Seam 3 — AccessPolicy satisfies the MCP Protocol + real engine wired in
# ===========================================================================


def _mcp_bits(balance: int = 1_000_000_000, token: str = USDC):
    """Build (wallet, delegation) for MCP tests with a mocked escrow/mpc."""
    from switchboard.agent_wallet import AgentWallet
    from switchboard.delegation import Delegation
    from switchboard.treasury import Treasury

    treasury = Treasury()
    treasury.credit(chain_id=CHAIN_1, token=token, amount=balance)
    mpc = MagicMock()
    mpc.address.return_value = "0xWalletAddress"
    mpc.sign_and_send.return_value = "0xTxHash"
    escrow = MagicMock()
    escrow.create_payment.return_value = "escrow-seam3"
    escrow.release_payment.return_value = True
    wallet = AgentWallet(mpc=mpc, treasury=treasury, escrow=escrow)
    return wallet, Delegation(wallet=wallet)


def _init(server) -> None:
    server.handle_message({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})


class TestSeam3AccessPolicyProtocol:
    def test_decision_exposes_denied_and_reason(self):
        """access_policy.Decision satisfies the MCP Protocol: .denied + .reason."""
        from switchboard.access_policy import AccessPolicy, AgentTier
        from switchboard.delegation import SpendPolicy

        engine = AccessPolicy()
        engine.register(
            "a", tier=AgentTier.STANDARD,
            spend_policy=SpendPolicy(expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc)),
        )
        d = engine.check("a", "pay")  # MCP form: op-name string
        assert hasattr(d, "denied") and hasattr(d, "reason")
        assert d.denied is False
        assert d.reason is None

    def test_check_accepts_op_name_string(self):
        """The op-name-only form must not false-deny (no amount => no zero-amount trip)."""
        from switchboard.access_policy import AccessPolicy, AgentTier
        from switchboard.delegation import SpendPolicy

        engine = AccessPolicy()
        engine.register(
            "b", tier=AgentTier.STANDARD,
            spend_policy=SpendPolicy(
                expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
                token_allowlist=[USDC],  # would trip if token=None were checked
            ),
        )
        d = engine.check("b", "create_escrow")
        assert d.denied is False

    def test_real_engine_denies_op_through_mcp(self):
        """A denied op is refused THROUGH the MCP server by the real engine."""
        from switchboard.access_policy import AccessPolicy, AgentTier
        from switchboard.delegation import SpendPolicy
        from switchboard.mcp_server import MCPServer, _POLICY_DENIED

        wallet, delegation = _mcp_bits()
        # Valid, live session key so we reach the access-policy gate.
        live = SpendPolicy(expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
        key = delegation.grant("agent-denied", live)

        # Register the SAME agent in the real engine with an EXPIRED policy so
        # the op-name gate denies with policy_violation before dispatch.
        engine = AccessPolicy()
        engine.register(
            "agent-denied", tier=AgentTier.STANDARD,
            spend_policy=SpendPolicy(expires_at=datetime(2000, 1, 1, tzinfo=timezone.utc)),
        )
        server = MCPServer(wallet=wallet, delegation=delegation, access_policy=engine)
        _init(server)

        resp = server.handle_message({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "pay", "arguments": {
                "session_key": key.key_id, "chain_id": CHAIN_1,
                "token": USDC, "amount": 100, "payee": PAYEE,
            }},
        })
        assert "error" in resp
        assert resp["error"]["code"] == _POLICY_DENIED
        assert "policy_violation" in resp["error"]["message"]

    def test_real_engine_allows_op_through_mcp(self):
        """A compliant agent passes the real engine and the pay executes."""
        from switchboard.access_policy import AccessPolicy, AgentTier
        from switchboard.delegation import SpendPolicy
        from switchboard.mcp_server import MCPServer

        wallet, delegation = _mcp_bits()
        live = SpendPolicy(expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
        key = delegation.grant("agent-ok", live)

        engine = AccessPolicy()
        engine.register(
            "agent-ok", tier=AgentTier.TRUSTED,
            spend_policy=SpendPolicy(expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc)),
        )
        server = MCPServer(wallet=wallet, delegation=delegation, access_policy=engine)
        _init(server)

        resp = server.handle_message({
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "pay", "arguments": {
                "session_key": key.key_id, "chain_id": CHAIN_1,
                "token": USDC, "amount": 100, "payee": PAYEE,
            }},
        })
        assert "result" in resp, resp
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["tx_id"] == "0xTxHash"


# ===========================================================================
# Seam 4 — Router in the pay path
# ===========================================================================


def _wallet_with_router(events=None, access_policy=None, extra_tokens=None):
    """Build an AgentWallet whose pay() runs through a real Router.

    Returns (wallet, treasury, escrow_mock).
    """
    from switchboard.agent_wallet import AgentWallet, EscrowClient
    from switchboard.nonce_manager import NonceManager
    from switchboard.router import Router
    from switchboard.router.token_selector import TokenSelector
    from switchboard.router.rail_selector import RailSelector
    from switchboard.router.fleet_balancer import FleetBalancer
    from switchboard.treasury import Treasury

    treasury = Treasury()
    treasury.credit(CHAIN_1, USDC, 1_000_000_000)
    for tok, bal in (extra_tokens or {}).items():
        treasury.credit(CHAIN_1, tok, bal)

    chain_client = MagicMock()
    chain_client.get_current_onchain_nonce.return_value = 0
    router = Router(
        token_selector=TokenSelector(treasury=treasury, chain_id=CHAIN_1),
        rail_selector=RailSelector(),
        fleet_balancer=FleetBalancer(
            wallets=["0xWalletA", "0xWalletB"],
            nonce_manager=NonceManager(chain_client=chain_client),
            chain_id=CHAIN_1,
        ),
        events=events,
    )
    escrow = MagicMock(spec=EscrowClient)
    escrow.create_payment.return_value = "0xescrow_seam4"
    escrow.release_payment.return_value = True

    mpc = MagicMock()
    mpc.address.return_value = "0xRoot"
    mpc.sign_and_send.return_value = "0xTx4"

    wallet = AgentWallet(
        mpc=mpc, treasury=treasury, escrow=escrow,
        router=router, access_policy=access_policy,
    )
    return wallet, treasury, escrow, mpc


class TestSeam4RouterInPayPath:
    def test_pay_routes_and_emits_wallet_op_event(self):
        from switchboard.agent_wallet import PaymentRequest
        from switchboard.metrics import WalletOpEvent

        events: list = []
        wallet, _, escrow, _ = _wallet_with_router(events=events.append)

        # 100 USDC -> escrow rail (above x402 micro threshold), a fleet wallet.
        req = PaymentRequest(chain_id=CHAIN_1, token=USDC, amount_wei=200_000, payee=PAYEE)
        receipt = wallet.pay(req, agent_id="router-agent")

        # Router picked rail + a signing wallet, recorded on the receipt.
        assert receipt.token == USDC
        assert receipt.rail == "escrow"
        assert receipt.wallet in ("0xWalletA", "0xWalletB")
        assert receipt.escrow_id == "0xescrow_seam4"

        # Exactly one routing WalletOpEvent, canonical shape, correct agent.
        routed = [e for e in events if isinstance(e, WalletOpEvent)]
        assert len(routed) == 1
        assert routed[0].agent_id == "router-agent"
        assert routed[0].rail == "escrow"
        assert routed[0].denied is False

    def test_pay_consults_access_policy_before_signing_and_denies(self):
        """A denied access-policy decision blocks the pay before MPC signs."""
        from dataclasses import dataclass

        from switchboard.agent_wallet import PaymentRequest, AccessDenied

        @dataclass
        class _Decision:
            denied: bool
            reason: object

        class DenyEngine:
            def __init__(self):
                self.called_with = None

            def check(self, agent_id, action):
                self.called_with = (agent_id, action)
                return _Decision(denied=True, reason="tier_ceiling")

        engine = DenyEngine()
        wallet, treasury, escrow, mpc = _wallet_with_router(access_policy=engine)
        before = treasury.balance(CHAIN_1, USDC)

        req = PaymentRequest(chain_id=CHAIN_1, token=USDC, amount_wei=200_000, payee=PAYEE)
        with pytest.raises(AccessDenied) as ei:
            wallet.pay(req, agent_id="blocked-agent")

        assert ei.value.reason == "tier_ceiling"
        # Access check ran with the agent + a pay action; nothing signed/debited.
        assert engine.called_with[0] == "blocked-agent"
        assert engine.called_with[1]["type"] == "pay"
        mpc.sign_and_send.assert_not_called()
        escrow.create_payment.assert_not_called()
        assert treasury.balance(CHAIN_1, USDC) == before

    def test_pay_allowed_by_access_policy_then_routes(self):
        """An allowed decision lets the routed pay proceed to a receipt."""
        from switchboard.agent_wallet import PaymentRequest

        class AllowEngine:
            def check(self, agent_id, action):
                from dataclasses import make_dataclass
                D = make_dataclass("D", [("denied", bool), ("reason", object)])
                return D(False, None)

        wallet, _, escrow, mpc = _wallet_with_router(access_policy=AllowEngine())
        req = PaymentRequest(chain_id=CHAIN_1, token=USDC, amount_wei=200_000, payee=PAYEE)
        receipt = wallet.pay(req, agent_id="ok-agent")
        assert receipt.rail == "escrow"
        mpc.sign_and_send.assert_called_once()

    def test_no_router_keeps_direct_path(self):
        """Without a Router, pay() behaves exactly as before (no rail/wallet)."""
        from switchboard.agent_wallet import AgentWallet, PaymentRequest, EscrowClient
        from switchboard.mpc_wallet import MPCWallet
        from switchboard.treasury import Treasury

        treasury = Treasury()
        treasury.credit(CHAIN_1, USDC, 1_000_000)
        escrow = MagicMock(spec=EscrowClient)
        escrow.create_payment.return_value = "0xdirect"
        escrow.release_payment.return_value = True
        wallet = AgentWallet(mpc=MPCWallet(), treasury=treasury, escrow=escrow)

        req = PaymentRequest(chain_id=CHAIN_1, token=USDC, amount_wei=500, payee=PAYEE)
        receipt = wallet.pay(req)
        assert receipt.rail is None
        assert receipt.wallet is None
        assert receipt.token == USDC
