"""
Tests for switchboard.metrics — escrow-fulfilment metrics + wallet ops.

Unit ⑳ of the agent-wallet-multitoken-settlement plan.

Record shapes consumed by these metrics are defined here and serve
as the canonical spec for what the backend must emit.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from switchboard.metrics import (
    # Record types (input shape)
    EscrowEvent,
    WalletOpEvent,
    EscrowState,
    # Computed metric containers
    EscrowMetrics,
    WalletOpsMetrics,
    FleetHealth,
    # Compute functions
    compute_escrow_metrics,
    compute_wallet_ops_metrics,
    compute_fleet_health,
    # Aggregate
    compute_all_metrics,
)


# ---------------------------------------------------------------------------
# Fixtures — canonical record shapes
# ---------------------------------------------------------------------------

def _ts(offset: float = 0.0) -> float:
    """Return a stable base timestamp plus offset (seconds)."""
    return 1_750_000_000.0 + offset


def make_escrow_event(
    request_id: str = "req-001",
    event_type: str = "Released",
    token: str = "ETH",
    amount: float = 1.0,
    created_at: float | None = None,
    resolved_at: float | None = None,
    payer: str = "0xPayer",
    payee: str = "0xPayee",
    chain_id: int = 1,
) -> EscrowEvent:
    """Build an EscrowEvent with sane defaults."""
    return EscrowEvent(
        request_id=request_id,
        event_type=event_type,
        token=token,
        amount=amount,
        created_at=created_at if created_at is not None else _ts(0),
        resolved_at=resolved_at,
        payer=payer,
        payee=payee,
        chain_id=chain_id,
    )


def make_wallet_op(
    op_type: str = "pay",
    token: str = "ETH",
    rail: str = "escrow",
    amount: float = 1.0,
    agent_id: str = "agent-1",
    wallet_id: str = "wallet-A",
    denied: bool = False,
    denial_reason: str | None = None,
    ts: float | None = None,
) -> WalletOpEvent:
    return WalletOpEvent(
        op_type=op_type,
        token=token,
        rail=rail,
        amount=amount,
        agent_id=agent_id,
        wallet_id=wallet_id,
        denied=denied,
        denial_reason=denial_reason,
        timestamp=ts if ts is not None else _ts(0),
    )


def make_escrow_state(
    request_id: str = "req-001",
    state: str = "Locked",
    token: str = "ETH",
    amount: float = 1.0,
    created_at: float | None = None,
    wallet_id: str = "wallet-A",
) -> EscrowState:
    return EscrowState(
        request_id=request_id,
        state=state,
        token=token,
        amount=amount,
        created_at=created_at if created_at is not None else _ts(-600),
        wallet_id=wallet_id,
    )


# ---------------------------------------------------------------------------
# EscrowEvent record shape tests
# ---------------------------------------------------------------------------

class TestEscrowEventShape:
    def test_has_required_fields(self):
        ev = make_escrow_event()
        assert ev.request_id == "req-001"
        assert ev.event_type == "Released"
        assert ev.token == "ETH"
        assert ev.amount == 1.0
        assert ev.created_at == _ts(0)
        assert ev.resolved_at is None
        assert ev.payer == "0xPayer"
        assert ev.payee == "0xPayee"
        assert ev.chain_id == 1

    def test_all_escrow_event_types_accepted(self):
        for et in ("Released", "Refunded", "Cancelled", "Timeout", "Challenged"):
            ev = make_escrow_event(event_type=et)
            assert ev.event_type == et

    def test_token_field_is_string(self):
        # ERC-20 address or "ETH"
        ev = make_escrow_event(token="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")
        assert ev.token.startswith("0x")


class TestWalletOpEventShape:
    def test_has_required_fields(self):
        op = make_wallet_op()
        assert op.op_type == "pay"
        assert op.token == "ETH"
        assert op.rail == "escrow"
        assert op.amount == 1.0
        assert op.agent_id == "agent-1"
        assert op.wallet_id == "wallet-A"
        assert op.denied is False
        assert op.denial_reason is None

    def test_denied_op_carries_reason(self):
        op = make_wallet_op(denied=True, denial_reason="daily_cap_exceeded")
        assert op.denied is True
        assert op.denial_reason == "daily_cap_exceeded"


class TestEscrowStateShape:
    def test_has_required_fields(self):
        st = make_escrow_state()
        assert st.request_id == "req-001"
        assert st.state in ("Locked", "Released", "Refunded", "Cancelled")
        assert st.token == "ETH"
        assert st.amount == 1.0
        assert st.wallet_id == "wallet-A"


# ---------------------------------------------------------------------------
# compute_escrow_metrics
# ---------------------------------------------------------------------------

class TestFillRate:
    def test_all_released_gives_100_pct(self):
        events = [
            make_escrow_event("r1", "Released", resolved_at=_ts(60)),
            make_escrow_event("r2", "Released", resolved_at=_ts(120)),
        ]
        m = compute_escrow_metrics(events)
        assert m.fill_rate == pytest.approx(1.0)

    def test_mixed_events(self):
        events = [
            make_escrow_event("r1", "Released", resolved_at=_ts(60)),
            make_escrow_event("r2", "Refunded", resolved_at=_ts(300)),
            make_escrow_event("r3", "Timeout",  resolved_at=_ts(400)),
            make_escrow_event("r4", "Released", resolved_at=_ts(100)),
        ]
        m = compute_escrow_metrics(events)
        # 2 Released out of 4 total resolved = 0.5
        assert m.fill_rate == pytest.approx(0.5)

    def test_empty_events_gives_none(self):
        m = compute_escrow_metrics([])
        assert m.fill_rate is None

    def test_no_resolved_events_gives_none(self):
        # Only Locked/pending events (no resolved_at)
        events = [make_escrow_event("r1", "Locked")]
        m = compute_escrow_metrics(events)
        assert m.fill_rate is None


class TestTimeToRelease:
    def test_computes_mean_seconds_for_released_events(self):
        events = [
            make_escrow_event("r1", "Released",
                              created_at=_ts(0), resolved_at=_ts(60)),
            make_escrow_event("r2", "Released",
                              created_at=_ts(0), resolved_at=_ts(120)),
        ]
        m = compute_escrow_metrics(events)
        assert m.avg_time_to_release_s == pytest.approx(90.0)

    def test_only_released_counted_for_time_to_release(self):
        events = [
            make_escrow_event("r1", "Released",
                              created_at=_ts(0), resolved_at=_ts(60)),
            make_escrow_event("r2", "Refunded",
                              created_at=_ts(0), resolved_at=_ts(600)),
        ]
        m = compute_escrow_metrics(events)
        assert m.avg_time_to_release_s == pytest.approx(60.0)

    def test_no_released_events_gives_none(self):
        events = [make_escrow_event("r1", "Timeout")]
        m = compute_escrow_metrics(events)
        assert m.avg_time_to_release_s is None


class TestTimeoutRate:
    def test_timeout_rate_pure(self):
        events = [
            make_escrow_event("r1", "Timeout"),
            make_escrow_event("r2", "Timeout"),
            make_escrow_event("r3", "Released", resolved_at=_ts(60)),
        ]
        m = compute_escrow_metrics(events)
        assert m.timeout_rate == pytest.approx(2 / 3)

    def test_no_timeouts_gives_zero(self):
        events = [make_escrow_event("r1", "Released", resolved_at=_ts(60))]
        m = compute_escrow_metrics(events)
        assert m.timeout_rate == pytest.approx(0.0)


class TestRefundRate:
    def test_refund_rate(self):
        events = [
            make_escrow_event("r1", "Refunded"),
            make_escrow_event("r2", "Released", resolved_at=_ts(60)),
            make_escrow_event("r3", "Released", resolved_at=_ts(60)),
        ]
        m = compute_escrow_metrics(events)
        assert m.refund_rate == pytest.approx(1 / 3)


class TestChallengeRate:
    def test_challenge_rate(self):
        events = [
            make_escrow_event("r1", "Challenged"),
            make_escrow_event("r2", "Challenged"),
            make_escrow_event("r3", "Released", resolved_at=_ts(60)),
            make_escrow_event("r4", "Released", resolved_at=_ts(60)),
            make_escrow_event("r5", "Released", resolved_at=_ts(60)),
        ]
        m = compute_escrow_metrics(events)
        assert m.challenge_rate == pytest.approx(2 / 5)

    def test_zero_challenge_rate(self):
        events = [make_escrow_event("r1", "Released", resolved_at=_ts(60))]
        m = compute_escrow_metrics(events)
        assert m.challenge_rate == pytest.approx(0.0)


class TestEscrowMetricsTotalCounts:
    def test_total_count(self):
        events = [
            make_escrow_event("r1", "Released", resolved_at=_ts(60)),
            make_escrow_event("r2", "Refunded"),
            make_escrow_event("r3", "Timeout"),
        ]
        m = compute_escrow_metrics(events)
        assert m.total_count == 3

    def test_released_count(self):
        events = [
            make_escrow_event("r1", "Released", resolved_at=_ts(60)),
            make_escrow_event("r2", "Released", resolved_at=_ts(90)),
            make_escrow_event("r3", "Refunded"),
        ]
        m = compute_escrow_metrics(events)
        assert m.released_count == 2

    def test_pending_count_from_states(self):
        states = [
            make_escrow_state("r1", "Locked"),
            make_escrow_state("r2", "Locked"),
            make_escrow_state("r3", "Released"),
        ]
        m = compute_escrow_metrics([], states=states)
        assert m.pending_count == 2


# ---------------------------------------------------------------------------
# compute_wallet_ops_metrics
# ---------------------------------------------------------------------------

class TestSpendByToken:
    def test_spend_by_token_sums_correctly(self):
        ops = [
            make_wallet_op(token="ETH",  amount=1.0),
            make_wallet_op(token="ETH",  amount=2.0),
            make_wallet_op(token="USDC", amount=100.0),
            make_wallet_op(token="LUX",  amount=500.0),
        ]
        m = compute_wallet_ops_metrics(ops)
        assert m.spend_by_token["ETH"]  == pytest.approx(3.0)
        assert m.spend_by_token["USDC"] == pytest.approx(100.0)
        assert m.spend_by_token["LUX"]  == pytest.approx(500.0)

    def test_denied_ops_excluded_from_spend(self):
        ops = [
            make_wallet_op(token="ETH", amount=5.0, denied=False),
            make_wallet_op(token="ETH", amount=99.0, denied=True),
        ]
        m = compute_wallet_ops_metrics(ops)
        assert m.spend_by_token["ETH"] == pytest.approx(5.0)


class TestSpendByRail:
    def test_spend_by_rail(self):
        ops = [
            make_wallet_op(rail="x402",   amount=0.01),
            make_wallet_op(rail="x402",   amount=0.02),
            make_wallet_op(rail="escrow", amount=1.0),
            make_wallet_op(rail="mpp",    amount=0.5),
        ]
        m = compute_wallet_ops_metrics(ops)
        assert m.spend_by_rail["x402"]   == pytest.approx(0.03)
        assert m.spend_by_rail["escrow"] == pytest.approx(1.0)
        assert m.spend_by_rail["mpp"]    == pytest.approx(0.5)


class TestPolicyDenials:
    def test_total_denial_count(self):
        ops = [
            make_wallet_op(denied=True,  denial_reason="daily_cap_exceeded"),
            make_wallet_op(denied=True,  denial_reason="token_not_allowed"),
            make_wallet_op(denied=False),
            make_wallet_op(denied=False),
        ]
        m = compute_wallet_ops_metrics(ops)
        assert m.policy_denial_count == 2

    def test_denials_by_reason(self):
        ops = [
            make_wallet_op(denied=True, denial_reason="daily_cap_exceeded"),
            make_wallet_op(denied=True, denial_reason="daily_cap_exceeded"),
            make_wallet_op(denied=True, denial_reason="token_not_allowed"),
        ]
        m = compute_wallet_ops_metrics(ops)
        assert m.denials_by_reason["daily_cap_exceeded"] == 2
        assert m.denials_by_reason["token_not_allowed"] == 1

    def test_zero_denials(self):
        ops = [make_wallet_op(denied=False) for _ in range(5)]
        m = compute_wallet_ops_metrics(ops)
        assert m.policy_denial_count == 0
        assert m.denials_by_reason == {}


class TestOpTotals:
    def test_total_ops_count(self):
        ops = [make_wallet_op() for _ in range(7)]
        m = compute_wallet_ops_metrics(ops)
        assert m.total_ops == 7

    def test_empty_ops(self):
        m = compute_wallet_ops_metrics([])
        assert m.total_ops == 0
        assert m.spend_by_token == {}
        assert m.spend_by_rail == {}
        assert m.policy_denial_count == 0


# ---------------------------------------------------------------------------
# compute_fleet_health
# ---------------------------------------------------------------------------

class TestFleetHealth:
    def test_healthy_wallets_reported(self):
        ops = [
            make_wallet_op(wallet_id="wallet-A"),
            make_wallet_op(wallet_id="wallet-A"),
            make_wallet_op(wallet_id="wallet-B"),
        ]
        h = compute_fleet_health(ops)
        assert "wallet-A" in h.wallet_op_counts
        assert h.wallet_op_counts["wallet-A"] == 2
        assert h.wallet_op_counts["wallet-B"] == 1

    def test_active_wallet_count(self):
        ops = [
            make_wallet_op(wallet_id="wallet-A"),
            make_wallet_op(wallet_id="wallet-B"),
            make_wallet_op(wallet_id="wallet-C"),
        ]
        h = compute_fleet_health(ops)
        assert h.active_wallet_count == 3

    def test_denial_rate_per_wallet(self):
        ops = [
            make_wallet_op(wallet_id="wallet-A", denied=True),
            make_wallet_op(wallet_id="wallet-A", denied=False),
            make_wallet_op(wallet_id="wallet-B", denied=False),
        ]
        h = compute_fleet_health(ops)
        assert h.denial_rate_by_wallet["wallet-A"] == pytest.approx(0.5)
        assert h.denial_rate_by_wallet["wallet-B"] == pytest.approx(0.0)

    def test_empty_ops_zero_active(self):
        h = compute_fleet_health([])
        assert h.active_wallet_count == 0
        assert h.wallet_op_counts == {}


# ---------------------------------------------------------------------------
# compute_all_metrics — aggregate
# ---------------------------------------------------------------------------

class TestComputeAllMetrics:
    def test_returns_all_three_components(self):
        escrow_events = [
            make_escrow_event("r1", "Released", resolved_at=_ts(60)),
        ]
        wallet_ops = [
            make_wallet_op(),
        ]
        result = compute_all_metrics(
            escrow_events=escrow_events,
            wallet_ops=wallet_ops,
        )
        assert hasattr(result, "escrow")
        assert hasattr(result, "wallet_ops")
        assert hasattr(result, "fleet")
        assert isinstance(result.escrow, EscrowMetrics)
        assert isinstance(result.wallet_ops, WalletOpsMetrics)
        assert isinstance(result.fleet, FleetHealth)

    def test_full_fixture_round_trip(self):
        """Realistic fixture: a mix of outcomes + multi-token ops."""
        escrow_events = [
            make_escrow_event("e1", "Released", token="ETH",
                              created_at=_ts(0), resolved_at=_ts(30)),
            make_escrow_event("e2", "Released", token="USDC",
                              created_at=_ts(0), resolved_at=_ts(90)),
            make_escrow_event("e3", "Refunded", token="LUX"),
            make_escrow_event("e4", "Timeout",  token="ZOO"),
            make_escrow_event("e5", "Challenged", token="ETH"),
        ]
        wallet_ops = [
            make_wallet_op(token="ETH",  rail="escrow",  amount=1.0,  wallet_id="W1"),
            make_wallet_op(token="USDC", rail="x402",    amount=50.0, wallet_id="W1"),
            make_wallet_op(token="LUX",  rail="escrow",  amount=200.0, wallet_id="W2"),
            make_wallet_op(token="ZOO",  rail="mpp",     amount=10.0, wallet_id="W2"),
            make_wallet_op(token="ETH",  rail="escrow",  amount=0.5,
                           denied=True, denial_reason="per_tx_cap_exceeded", wallet_id="W1"),
        ]

        result = compute_all_metrics(
            escrow_events=escrow_events,
            wallet_ops=wallet_ops,
        )

        # Escrow metrics
        em = result.escrow
        # 2 Released out of 5 total = 0.4
        assert em.fill_rate == pytest.approx(0.4)
        # avg time-to-release = (30+90)/2 = 60
        assert em.avg_time_to_release_s == pytest.approx(60.0)
        assert em.timeout_rate  == pytest.approx(1 / 5)
        assert em.refund_rate   == pytest.approx(1 / 5)
        assert em.challenge_rate == pytest.approx(1 / 5)
        assert em.total_count == 5

        # Wallet ops
        wm = result.wallet_ops
        assert wm.spend_by_token["ETH"]  == pytest.approx(1.0)   # 0.5 denied excluded
        assert wm.spend_by_token["USDC"] == pytest.approx(50.0)
        assert wm.spend_by_token["LUX"]  == pytest.approx(200.0)
        assert wm.spend_by_token["ZOO"]  == pytest.approx(10.0)
        assert wm.spend_by_rail["escrow"] == pytest.approx(201.0)
        assert wm.spend_by_rail["x402"]   == pytest.approx(50.0)
        assert wm.spend_by_rail["mpp"]    == pytest.approx(10.0)
        assert wm.policy_denial_count == 1
        assert wm.denials_by_reason["per_tx_cap_exceeded"] == 1

        # Fleet
        fh = result.fleet
        assert fh.active_wallet_count == 2
        assert fh.wallet_op_counts["W1"] == 3
        assert fh.wallet_op_counts["W2"] == 2
