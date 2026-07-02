"""
switchboard.metrics — escrow-fulfilment metrics + wallet-ops health.

Unit ⑳ of the agent-wallet-multitoken-settlement plan.

Consumes structured event/state records (defined as dataclasses here);
computes fill rate, time-to-release, timeout rate, refund rate, challenge
rate, spend by token/rail, policy denials, and fleet health.

Designed to be driven by an event-polling loop (the dashboard panel calls
``compute_all_metrics`` on a timer), but is pure / side-effect-free so it
is fully testable against fixtures.

Input record shapes
-------------------
EscrowEvent
    One settled or terminal escrow event emitted when a payment
    leaves the ``Locked`` state.  Mirrors the events emitted by
    ``AgentEscrow.sol`` (PaymentReleased / PaymentRefunded /
    PaymentCancelled) plus inferred Timeout / Challenged events.

WalletOpEvent
    One wallet operation attempted by an agent — a pay, policy-check,
    rebalance, etc.  Includes rail, token, amount, and whether the
    wallet co-signed or denied the request.

EscrowState
    Current on-chain snapshot of an escrow (for pending-count
    reporting, independent of event history).

Usage::

    events = polling_layer.fetch_escrow_events(since=last_ts)
    ops    = polling_layer.fetch_wallet_ops(since=last_ts)
    states = polling_layer.fetch_open_escrows()

    result = compute_all_metrics(
        escrow_events=events,
        wallet_ops=ops,
        escrow_states=states,
    )
    dashboard.render(result)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Input record types (the "backend must emit" contract)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EscrowEvent:
    """One terminal escrow event.

    Fields
    ------
    request_id : str
        Off-chain payment-request ID (matches the contract ``requestId``).
    event_type : str
        One of: ``Released``, ``Refunded``, ``Cancelled``,
        ``Timeout``, ``Challenged``.
    token : str
        Token symbol (``"ETH"``) or ERC-20 contract address.
    amount : float
        Amount in base units (wei/token-decimals at backend discretion;
        consistent within a dataset).
    created_at : float
        Unix timestamp (seconds) when the escrow was created on-chain.
    resolved_at : float | None
        Unix timestamp when the event was emitted.  ``None`` for events
        that are inferred but not yet block-confirmed.
    payer : str
        Payer address (``0x…``).
    payee : str
        Payee address (``0x…``).
    chain_id : int
        EVM chain id the escrow lives on.
    """

    request_id: str
    event_type: str
    token: str
    amount: float
    created_at: float
    resolved_at: Optional[float]
    payer: str
    payee: str
    chain_id: int


@dataclass(frozen=True)
class WalletOpEvent:
    """One wallet operation attempted by an agent.

    Fields
    ------
    op_type : str
        Operation kind: ``pay``, ``rebalance``, ``policy_check``, etc.
    token : str
        Token used / proposed.
    rail : str
        Settlement rail: ``x402``, ``escrow``, ``mpp``.
    amount : float
        Amount proposed (even if denied).
    agent_id : str
        Logical agent identifier.
    wallet_id : str
        Physical wallet (MPC share / fleet member) that handled the op.
    denied : bool
        Whether the wallet co-signing was refused.
    denial_reason : str | None
        Machine-readable policy rule that caused the denial, e.g.
        ``"daily_cap_exceeded"``, ``"token_not_allowed"``,
        ``"counterparty_not_allowed"``, ``"per_tx_cap_exceeded"``.
    timestamp : float
        Unix timestamp when the op was attempted.
    """

    op_type: str
    token: str
    rail: str
    amount: float
    agent_id: str
    wallet_id: str
    denied: bool
    denial_reason: Optional[str]
    timestamp: float


@dataclass(frozen=True)
class EscrowState:
    """Current on-chain snapshot of one escrow (for pending count).

    Fields
    ------
    request_id : str
    state : str
        Contract state: ``Locked``, ``Released``, ``Refunded``,
        ``Cancelled``.
    token : str
    amount : float
    created_at : float
    wallet_id : str
        The agent-wallet wallet that initiated/owns this escrow.
    """

    request_id: str
    state: str
    token: str
    amount: float
    created_at: float
    wallet_id: str


# ---------------------------------------------------------------------------
# Output metric containers
# ---------------------------------------------------------------------------


@dataclass
class EscrowMetrics:
    """Computed escrow-fulfilment metrics over a set of events.

    Rates are fractions in ``[0, 1]`` (e.g. 0.95 = 95 %).
    ``None`` means "not enough data to compute."
    """

    total_count: int = 0
    released_count: int = 0
    refunded_count: int = 0
    timeout_count: int = 0
    cancelled_count: int = 0
    challenged_count: int = 0
    pending_count: int = 0

    # Derived rates (None when denominator is 0 / no data)
    fill_rate: Optional[float] = None
    timeout_rate: Optional[float] = None
    refund_rate: Optional[float] = None
    challenge_rate: Optional[float] = None

    # Time-to-release (seconds, mean over Released events only)
    avg_time_to_release_s: Optional[float] = None


@dataclass
class WalletOpsMetrics:
    """Wallet operation totals and policy-denial breakdown."""

    total_ops: int = 0

    # Spend by token/rail — only non-denied ops counted
    spend_by_token: Dict[str, float] = field(default_factory=dict)
    spend_by_rail:  Dict[str, float] = field(default_factory=dict)

    # Policy denials
    policy_denial_count: int = 0
    denials_by_reason: Dict[str, int] = field(default_factory=dict)


@dataclass
class FleetHealth:
    """Per-wallet activity and denial summary."""

    active_wallet_count: int = 0
    wallet_op_counts: Dict[str, int] = field(default_factory=dict)
    denial_rate_by_wallet: Dict[str, float] = field(default_factory=dict)


@dataclass
class AllMetrics:
    """Aggregated result from ``compute_all_metrics``."""

    escrow:     EscrowMetrics
    wallet_ops: WalletOpsMetrics
    fleet:      FleetHealth


# ---------------------------------------------------------------------------
# Compute functions
# ---------------------------------------------------------------------------

_RESOLVED_TYPES = frozenset({"Released", "Refunded", "Cancelled", "Timeout", "Challenged"})
_TERMINAL_TYPES = _RESOLVED_TYPES  # alias for clarity


def compute_escrow_metrics(
    events: List[EscrowEvent],
    states: List[EscrowState] | None = None,
) -> EscrowMetrics:
    """Compute escrow-fulfilment metrics from event records.

    Parameters
    ----------
    events:
        List of ``EscrowEvent`` records (terminal events).
    states:
        Optional list of current ``EscrowState`` snapshots; used only
        for ``pending_count``.
    """
    m = EscrowMetrics()

    # Count by type — only terminal events count toward rates
    released_times: list[float] = []
    terminal_count = 0
    for ev in events:
        m.total_count += 1
        if ev.event_type == "Released":
            terminal_count += 1
            m.released_count += 1
            if ev.resolved_at is not None and ev.created_at is not None:
                released_times.append(ev.resolved_at - ev.created_at)
        elif ev.event_type == "Refunded":
            terminal_count += 1
            m.refunded_count += 1
        elif ev.event_type == "Timeout":
            terminal_count += 1
            m.timeout_count += 1
        elif ev.event_type == "Cancelled":
            terminal_count += 1
            m.cancelled_count += 1
        elif ev.event_type == "Challenged":
            terminal_count += 1
            m.challenged_count += 1
        # Other event types (e.g. "Locked") are counted in total_count
        # but do not contribute to rate denominators.

    # Rates (denominator = terminal_count; None when 0)
    n = terminal_count
    if n > 0:
        m.fill_rate      = m.released_count  / n
        m.timeout_rate   = m.timeout_count   / n
        m.refund_rate    = m.refunded_count  / n
        m.challenge_rate = m.challenged_count / n
    # else all remain None

    # Average time-to-release
    if released_times:
        m.avg_time_to_release_s = sum(released_times) / len(released_times)

    # Pending count from states snapshot
    if states:
        m.pending_count = sum(1 for s in states if s.state == "Locked")

    return m


def compute_wallet_ops_metrics(ops: List[WalletOpEvent]) -> WalletOpsMetrics:
    """Compute wallet-operation spend and denial metrics."""
    m = WalletOpsMetrics()
    m.total_ops = len(ops)

    spend_token: dict[str, float] = defaultdict(float)
    spend_rail:  dict[str, float] = defaultdict(float)
    denials:     dict[str, int]   = defaultdict(int)

    for op in ops:
        if op.denied:
            m.policy_denial_count += 1
            if op.denial_reason:
                denials[op.denial_reason] += 1
        else:
            spend_token[op.token] += op.amount
            spend_rail[op.rail]   += op.amount

    m.spend_by_token  = dict(spend_token)
    m.spend_by_rail   = dict(spend_rail)
    m.denials_by_reason = dict(denials)
    return m


def compute_fleet_health(ops: List[WalletOpEvent]) -> FleetHealth:
    """Compute per-wallet health indicators from op records."""
    h = FleetHealth()

    total_by_wallet:  dict[str, int] = defaultdict(int)
    denied_by_wallet: dict[str, int] = defaultdict(int)

    for op in ops:
        total_by_wallet[op.wallet_id]  += 1
        if op.denied:
            denied_by_wallet[op.wallet_id] += 1

    h.wallet_op_counts   = dict(total_by_wallet)
    h.active_wallet_count = len(total_by_wallet)
    h.denial_rate_by_wallet = {
        wid: denied_by_wallet.get(wid, 0) / total
        for wid, total in total_by_wallet.items()
    }
    return h


def compute_all_metrics(
    escrow_events: List[EscrowEvent],
    wallet_ops: List[WalletOpEvent],
    escrow_states: List[EscrowState] | None = None,
) -> AllMetrics:
    """Compute all three metric groups and return an ``AllMetrics`` bundle."""
    return AllMetrics(
        escrow=compute_escrow_metrics(escrow_events, states=escrow_states),
        wallet_ops=compute_wallet_ops_metrics(wallet_ops),
        fleet=compute_fleet_health(wallet_ops),
    )
