"""Tests for the top-level Router.

The Router composes TokenSelector, RailSelector, FleetBalancer, and
Rebalancer into a single Router.route(request) -> Plan call.

It must:
  - Return a Plan with (token, rail, wallet) populated.
  - Emit a WalletOpEvent to the supplied metrics sink after routing.
  - Raise when no token can be selected (no solvent candidates).

All tests written BEFORE the implementation (TDD — RED first).
"""

import pytest
from unittest.mock import MagicMock, patch

from switchboard.treasury import Treasury
from switchboard.nonce_manager import NonceManager
from switchboard.metrics import WalletOpEvent
from switchboard.router import Router, Plan
from switchboard.router.token_selector import TokenSelector, TokenCandidate
from switchboard.router.rail_selector import RailSelector, RailConfig
from switchboard.router.fleet_balancer import FleetBalancer


CHAIN_ID = 1
ETH  = "0x0000000000000000000000000000000000000000"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
WALLET_A = "0x1111111111111111111111111111111111111111"


def make_treasury_with_usdc(amount=1_000_000):
    t = Treasury()
    t.credit(CHAIN_ID, USDC, amount)
    return t


def make_nonce_manager():
    chain_client = MagicMock()
    chain_client.get_current_onchain_nonce.return_value = 0
    return NonceManager(chain_client=chain_client)


def make_router(treasury=None, wallets=None, events=None, rail_config=None):
    treasury = treasury or make_treasury_with_usdc()
    wallets = wallets or [WALLET_A]
    nm = make_nonce_manager()
    token_sel = TokenSelector(treasury=treasury, chain_id=CHAIN_ID)
    rail_sel = RailSelector(config=rail_config or RailConfig())
    fleet = FleetBalancer(wallets=wallets, nonce_manager=nm, chain_id=CHAIN_ID)
    return Router(
        token_selector=token_sel,
        rail_selector=rail_sel,
        fleet_balancer=fleet,
        events=events,
    )


class TestRouterReturnsPlan:
    def test_route_returns_plan_with_token_rail_wallet(self):
        router = make_router()
        plan = router.route(
            chain_id=CHAIN_ID,
            amount=100,
            candidates=[TokenCandidate(token=USDC)],
        )
        assert isinstance(plan, Plan)
        assert plan.token == USDC
        assert plan.rail in ("x402", "escrow", "mpp")
        assert plan.wallet == WALLET_A


class TestRouterRailSelection:
    def test_micro_amount_selects_x402_rail(self):
        router = make_router()
        plan = router.route(
            chain_id=CHAIN_ID,
            amount=500,
            candidates=[TokenCandidate(token=USDC)],
        )
        assert plan.rail == "x402"

    def test_large_amount_selects_mpp_rail(self):
        # Use a tight config so 5_000_000 exceeds escrow_max (1_000_000).
        treasury = make_treasury_with_usdc(100_000_000)
        cfg = RailConfig(x402_max_amount=1_000, escrow_max_amount=1_000_000)
        router = make_router(treasury=treasury, rail_config=cfg)
        plan = router.route(
            chain_id=CHAIN_ID,
            amount=5_000_000,
            candidates=[TokenCandidate(token=USDC)],
        )
        assert plan.rail == "mpp"


class TestRouterEmitsWalletOpEvent:
    """Router must emit a WalletOpEvent per routed op (spec requirement)."""

    def test_successful_route_emits_non_denied_event(self):
        emitted = []
        router = make_router(events=emitted.append)
        router.route(
            chain_id=CHAIN_ID,
            amount=100,
            candidates=[TokenCandidate(token=USDC)],
            agent_id="agent-007",
        )
        assert len(emitted) == 1
        ev = emitted[0]
        assert isinstance(ev, WalletOpEvent)
        assert ev.op_type == "pay"
        assert ev.token == USDC
        assert ev.rail in ("x402", "escrow", "mpp")
        assert ev.amount == 100
        assert ev.agent_id == "agent-007"
        assert ev.wallet_id == WALLET_A
        assert ev.denied is False
        assert ev.denial_reason is None

    def test_failed_route_emits_denied_event(self):
        """When no token is solvent, emit a denied event and then raise."""
        empty_treasury = Treasury()  # no balance
        router = make_router(treasury=empty_treasury)
        emitted = []
        router._events = emitted.append  # swap out the event sink

        with pytest.raises(Exception):
            router.route(
                chain_id=CHAIN_ID,
                amount=100,
                candidates=[TokenCandidate(token=USDC)],
                agent_id="agent-404",
            )

        assert len(emitted) == 1
        ev = emitted[0]
        assert ev.denied is True
        assert ev.denial_reason is not None


class TestRouterNoSolventToken:
    def test_no_solvent_token_raises(self):
        empty_treasury = Treasury()
        router = make_router(treasury=empty_treasury)
        with pytest.raises(Exception, match="[Nn]o.*token|[Ii]nsufficient"):
            router.route(
                chain_id=CHAIN_ID,
                amount=100,
                candidates=[TokenCandidate(token=USDC)],
            )


class TestPlanDataclass:
    def test_plan_fields(self):
        p = Plan(token=USDC, rail="escrow", wallet=WALLET_A)
        assert p.token == USDC
        assert p.rail == "escrow"
        assert p.wallet == WALLET_A
