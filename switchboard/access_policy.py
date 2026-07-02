"""Fairness + agent access policy engine — Unit ⑲.

Layered on :class:`~switchboard.delegation.SpendPolicy`, this module is the
"how the wallet is transacted" rulebook that every MCP server and Router call
must pass before acting.

Architecture
------------
Three independent concern layers, evaluated in order::

    1. Contract compliance  — refuse actions that violate escrow invariants.
    2. SpendPolicy          — delegation rules (token allowlist, expiry, per-tx cap).
    3. Tier ceiling         — per-agent tier (explorer/standard/trusted) amount cap.
    4. Rate fairness        — token-bucket so no single agent starves others.

Denial short-circuits at the first violated layer.

Public API
----------
``check(agent_id, action) -> Decision``
    The single entry-point.  ``action`` is a plain dict with at minimum a
    ``"type"`` key (``"pay"`` or ``"escrow"``) and an ``"amount"`` key.
    Escrow actions additionally carry an ``"escrow_state"`` key.

``Decision``
    Dataclass with ``allowed: bool``, ``reason: str | None``, ``agent_id: str``,
    and ``event: WalletOpEvent`` for metric emission.

``WalletOpEvent``
    Lightweight metric payload: ``denied, denial_reason, agent_id``.

Reason strings (typed literals)
--------------------------------
``"noncompliant"``    — action would violate escrow contract terms.
``"policy_violation"``— action violates the agent's SpendPolicy.
``"tier_ceiling"``    — action amount exceeds the agent's tier per-tx cap.
``"rate_limited"``    — token-bucket exhausted; agent is contending too hard.

Token-bucket algorithm
----------------------
Each (agent_id, tier) pair gets an independent bucket.  Burst capacity is
``TokenBucketConfig.capacity`` operations; the bucket refills at
``TokenBucketConfig.rate`` tokens/second.  At rate=0 the bucket is strictly
capacity-limited (no refill) — useful for tests and quota-style limits.

This is a **per-agent** bucket, so one agent cannot drain capacity from
another; fairness is achieved by the fact that every agent's bucket is
independent and bounded.

Defaults
--------
Three default tiers (adjustable via ``TierConfig``):

+-----------+-----------+-------+-----------+
| Tier      | per_tx_cap| rate  | capacity  |
+===========+===========+=======+===========+
| explorer  |    1 000  |    1  |    10     |
+-----------+-----------+-------+-----------+
| standard  |   10 000  |   10  |    50     |
+-----------+-----------+-------+-----------+
| trusted   |  100 000  |  100  |   200     |
+-----------+-----------+-------+-----------+

(Amounts in token base units; rate in tokens/second.)

Thread safety
-------------
All mutable state is protected by a single ``threading.Lock``.  The module-level
``check()`` helper uses a ``threading.local``-backed process-wide instance.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Callable, Dict, List, Literal, Optional


# ---------------------------------------------------------------------------
# Re-export SpendPolicy so callers have a single import surface.
# ---------------------------------------------------------------------------

from switchboard.delegation import SpendPolicy  # noqa: F401 — re-exported


# ---------------------------------------------------------------------------
# Enums + config dataclasses
# ---------------------------------------------------------------------------


class AgentTier(Enum):
    """Access tier assigned to each registered agent."""
    EXPLORER = auto()
    STANDARD = auto()
    TRUSTED = auto()


@dataclass(frozen=True)
class TokenBucketConfig:
    """Configuration for one tier's token-bucket rate-limiter.

    Parameters
    ----------
    per_tx_cap:
        Maximum ``amount`` (token base units) in a single action.
    rate:
        Refill rate in bucket tokens per second.  0 = no refill (quota mode).
    capacity:
        Maximum number of bucket tokens (burst ceiling).
    """
    per_tx_cap: int
    rate: float          # tokens / second
    capacity: float      # max bucket tokens


@dataclass(frozen=True)
class TierConfig:
    """Per-tier bucket + ceiling configuration.

    Pass a custom ``TierConfig`` to ``AccessPolicy`` to override defaults.
    """
    explorer: TokenBucketConfig
    standard: TokenBucketConfig
    trusted: TokenBucketConfig

    def for_tier(self, tier: AgentTier) -> TokenBucketConfig:
        if tier is AgentTier.EXPLORER:
            return self.explorer
        if tier is AgentTier.STANDARD:
            return self.standard
        return self.trusted


# Sensible production defaults.
_DEFAULT_TIER_CONFIG = TierConfig(
    explorer=TokenBucketConfig(per_tx_cap=1_000,    rate=1.0,   capacity=10),
    standard=TokenBucketConfig(per_tx_cap=10_000,   rate=10.0,  capacity=50),
    trusted=TokenBucketConfig(per_tx_cap=100_000,   rate=100.0, capacity=200),
)


# ---------------------------------------------------------------------------
# WalletOpEvent — metric payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WalletOpEvent:
    """Lightweight event emitted for every ``check()`` call.

    Designed so a metrics backend (or the ``⑳`` dashboard) can subscribe and
    track denial rates, tier distribution, and denial reasons without coupling
    to ``AccessPolicy`` internals.

    Parameters
    ----------
    denied:
        ``True`` if the action was denied.
    denial_reason:
        One of ``"noncompliant"``, ``"policy_violation"``, ``"tier_ceiling"``,
        ``"rate_limited"``; or ``None`` if allowed.
    agent_id:
        The agent that attempted the action.
    """
    denied: bool
    denial_reason: Optional[str]
    agent_id: str


# ---------------------------------------------------------------------------
# Decision — the return value of check()
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """Result of ``AccessPolicy.check()``.

    Parameters
    ----------
    agent_id:
        Echoed from the call — useful for logging / routing.
    allowed:
        ``True`` iff the action may proceed.
    reason:
        ``None`` when allowed; one of ``"noncompliant"``, ``"policy_violation"``,
        ``"tier_ceiling"``, ``"rate_limited"`` when denied.
    event:
        Metric payload always present — pass to an event bus or discard.
    """
    agent_id: str
    allowed: bool
    reason: Optional[str]
    event: WalletOpEvent


# ---------------------------------------------------------------------------
# Internal per-agent bucket state
# ---------------------------------------------------------------------------


@dataclass
class _BucketState:
    tokens: float
    last_refill: float   # monotonic timestamp


# ---------------------------------------------------------------------------
# AccessPolicy
# ---------------------------------------------------------------------------


class AccessPolicy:
    """Per-agent access-tier + rate-fairness + contract-compliance gate.

    Parameters
    ----------
    tier_config:
        Override per-tier ceilings and bucket parameters.
        Defaults to ``_DEFAULT_TIER_CONFIG``.
    clock:
        Injectable ``() -> float`` returning the current monotonic time in
        seconds.  Defaults to ``time.monotonic``.  Pass a controllable clock
        in tests.
    event_listener:
        Optional ``(WalletOpEvent) -> None`` callback invoked after each
        ``check()`` call — use to feed a metrics backend.
    """

    def __init__(
        self,
        tier_config: Optional[TierConfig] = None,
        clock: Callable[[], float] = time.monotonic,
        event_listener: Optional[Callable[[WalletOpEvent], None]] = None,
    ) -> None:
        self._tier_config: TierConfig = tier_config or _DEFAULT_TIER_CONFIG
        self._clock = clock
        self._event_listener = event_listener
        self._lock = threading.Lock()

        # agent_id -> (tier, SpendPolicy | None)
        self._agents: Dict[str, tuple[AgentTier, Optional[SpendPolicy]]] = {}
        # agent_id -> _BucketState (one per agent, isolated)
        self._buckets: Dict[str, _BucketState] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        agent_id: str,
        tier: AgentTier,
        spend_policy: Optional[SpendPolicy] = None,
    ) -> None:
        """Register an agent with a tier and optional SpendPolicy.

        Safe to call multiple times; subsequent calls update the tier and
        policy while preserving the existing bucket state.
        """
        with self._lock:
            self._agents[agent_id] = (tier, spend_policy)
            if agent_id not in self._buckets:
                cfg = self._tier_config.for_tier(tier)
                self._buckets[agent_id] = _BucketState(
                    tokens=cfg.capacity,
                    last_refill=self._clock(),
                )

    def set_tier(self, agent_id: str, tier: AgentTier) -> None:
        """Upgrade or downgrade an agent's tier.

        Bucket capacity is reset to the new tier's capacity.
        """
        with self._lock:
            _, policy = self._agents.get(agent_id, (AgentTier.EXPLORER, None))
            self._agents[agent_id] = (tier, policy)
            cfg = self._tier_config.for_tier(tier)
            self._buckets[agent_id] = _BucketState(
                tokens=cfg.capacity,
                last_refill=self._clock(),
            )

    # ------------------------------------------------------------------
    # check() — the single public gate
    # ------------------------------------------------------------------

    def check(self, agent_id: str, action: dict) -> Decision:
        """Evaluate ``action`` for ``agent_id`` and return a ``Decision``.

        Parameters
        ----------
        agent_id:
            The agent attempting the action.
        action:
            A dict with at minimum:

            - ``"type"``   — ``"pay"`` or ``"escrow"``
            - ``"amount"`` — token base units (int)

            For escrow actions, also include:

            - ``"escrow_state"`` — one of ``"open"``, ``"confirmed"``,
              ``"released"``, ``"refunded"``, ``"cancelled"``

        Returns
        -------
        Decision
            Always returned (never raises).  Check ``decision.allowed``.
        """
        with self._lock:
            tier, spend_policy = self._agents.get(
                agent_id, (AgentTier.EXPLORER, None)
            )
            # Ensure bucket exists for unregistered agents.
            if agent_id not in self._buckets:
                cfg = self._tier_config.for_tier(tier)
                self._buckets[agent_id] = _BucketState(
                    tokens=cfg.capacity,
                    last_refill=self._clock(),
                )

            # --- Layer 1: contract compliance ---------------------------------
            compliance_reason = self._check_compliance(action)
            if compliance_reason is not None:
                return self._deny(agent_id, compliance_reason)

            # --- Layer 2: SpendPolicy -----------------------------------------
            if spend_policy is not None:
                policy_reason = self._check_spend_policy(spend_policy, action)
                if policy_reason is not None:
                    return self._deny(agent_id, policy_reason)

            # --- Layer 3: tier ceiling ----------------------------------------
            cfg = self._tier_config.for_tier(tier)
            amount = action.get("amount", 0)
            if amount > cfg.per_tx_cap:
                return self._deny(agent_id, "tier_ceiling")

            # --- Layer 4: token-bucket rate fairness --------------------------
            bucket = self._buckets[agent_id]
            self._refill_bucket(bucket, cfg)
            if bucket.tokens < 1.0:
                return self._deny(agent_id, "rate_limited")

            bucket.tokens -= 1.0
            return self._allow(agent_id)

    # ------------------------------------------------------------------
    # Internal checkers
    # ------------------------------------------------------------------

    def _check_compliance(self, action: dict) -> Optional[str]:
        """Return a denial reason string or None if compliant."""
        amount = action.get("amount", 0)

        # Non-positive amounts are always non-compliant.
        if amount <= 0:
            return "noncompliant"

        # Escrow-specific: refuse interactions with terminal-state escrows.
        if action.get("type") == "escrow":
            terminal_states = {"released", "refunded", "cancelled"}
            if action.get("escrow_state", "open") in terminal_states:
                return "noncompliant"

        return None

    def _check_spend_policy(self, policy: SpendPolicy, action: dict) -> Optional[str]:
        """Return ``"policy_violation"`` if any SpendPolicy rule is violated."""
        now_utc = datetime.now(timezone.utc)
        expires = policy.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if now_utc >= expires:
            return "policy_violation"

        token = action.get("token")
        if policy.token_allowlist is not None and token not in policy.token_allowlist:
            return "policy_violation"

        if policy.allowed_counterparties is not None:
            payee = action.get("payee")
            if payee not in policy.allowed_counterparties:
                return "policy_violation"

        amount = action.get("amount", 0)
        if policy.per_tx_cap is not None and amount > policy.per_tx_cap:
            return "policy_violation"

        return None

    def _refill_bucket(self, bucket: _BucketState, cfg: TokenBucketConfig) -> None:
        """Add tokens earned since last refill, capped at capacity."""
        now = self._clock()
        elapsed = now - bucket.last_refill
        if elapsed > 0 and cfg.rate > 0:
            earned = elapsed * cfg.rate
            bucket.tokens = min(cfg.capacity, bucket.tokens + earned)
        bucket.last_refill = now

    # ------------------------------------------------------------------
    # Decision builders
    # ------------------------------------------------------------------

    def _deny(self, agent_id: str, reason: str) -> Decision:
        evt = WalletOpEvent(denied=True, denial_reason=reason, agent_id=agent_id)
        d = Decision(agent_id=agent_id, allowed=False, reason=reason, event=evt)
        if self._event_listener is not None:
            self._event_listener(evt)
        return d

    def _allow(self, agent_id: str) -> Decision:
        evt = WalletOpEvent(denied=False, denial_reason=None, agent_id=agent_id)
        d = Decision(agent_id=agent_id, allowed=True, reason=None, event=evt)
        if self._event_listener is not None:
            self._event_listener(evt)
        return d


# ---------------------------------------------------------------------------
# Module-level process-wide default (convenience helper)
# ---------------------------------------------------------------------------

_default_policy: Optional[AccessPolicy] = None
_default_policy_lock = threading.Lock()


def _get_default_policy() -> AccessPolicy:
    global _default_policy
    with _default_policy_lock:
        if _default_policy is None:
            _default_policy = AccessPolicy()
        return _default_policy


def check(agent_id: str, action: dict) -> Decision:
    """Check ``action`` for ``agent_id`` using the process-wide ``AccessPolicy``.

    Convenience wrapper for the MCP server and Router — they can call this
    without instantiating an ``AccessPolicy``.  For production code that needs
    custom tiers or event listeners, instantiate ``AccessPolicy`` directly.
    """
    return _get_default_policy().check(agent_id, action)
