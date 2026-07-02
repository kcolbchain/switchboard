"""Tests for switchboard.access_policy — Unit ⑲.

Fairness + agent access policy engine, layered on SpendPolicy.

Coverage
--------
* Per-agent access tiers (explorer / standard / trusted) with different ceilings.
* Rate-fairness token-bucket: one agent cannot starve others under contention.
* Contract-compliance checks: refuse actions that would violate escrow terms.
* Decision fields: allow/deny + typed reason strings.
* Metric emission hook: every denial carries a WalletOpEvent-compatible payload.
* Thread safety: N concurrent agents each get a fair, bounded share.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone, timedelta
from typing import List
from unittest.mock import MagicMock

import pytest

from switchboard.access_policy import (
    AccessPolicy,
    AgentTier,
    Decision,
    TierConfig,
    TokenBucketConfig,
    WalletOpEvent,
    check,
)
from switchboard.delegation import SpendPolicy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ETH = "0x0000000000000000000000000000000000000000"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
PAYEE_A = "0xPayeeA"
PAYEE_B = "0xPayeeB"

FAR_FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)


def _policy(**kwargs) -> SpendPolicy:
    defaults = dict(expires_at=FAR_FUTURE, token_allowlist=[ETH, USDC])
    defaults.update(kwargs)
    return SpendPolicy(**defaults)


def _make_pay_action(amount: int = 100, token: str = ETH, payee: str = PAYEE_A) -> dict:
    return {"type": "pay", "amount": amount, "token": token, "payee": payee}


def _make_escrow_action(amount: int = 100, token: str = ETH, payee: str = PAYEE_A, escrow_state: str = "open") -> dict:
    return {"type": "escrow", "amount": amount, "token": token, "payee": payee, "escrow_state": escrow_state}


# ---------------------------------------------------------------------------
# 1. Basic allow path
# ---------------------------------------------------------------------------

class TestBasicAllow:
    def test_allow_returns_decision(self):
        policy = AccessPolicy()
        policy.register("agent-1", tier=AgentTier.STANDARD, spend_policy=_policy())
        d = policy.check("agent-1", _make_pay_action(amount=500))
        assert isinstance(d, Decision)
        assert d.allowed is True
        assert d.reason is None

    def test_decision_carries_agent_id(self):
        policy = AccessPolicy()
        policy.register("agent-1", tier=AgentTier.EXPLORER, spend_policy=_policy())
        d = policy.check("agent-1", _make_pay_action(amount=10))
        assert d.agent_id == "agent-1"

    def test_unknown_agent_defaults_to_explorer(self):
        """Unregistered agents fall back to EXPLORER tier — safe default."""
        policy = AccessPolicy()
        d = policy.check("unknown-agent", _make_pay_action(amount=10))
        assert d.allowed is True   # within explorer ceiling

    def test_module_level_check_convenience(self):
        """Module-level check() uses a process-wide AccessPolicy."""
        d = check("mod-agent", _make_pay_action(amount=1))
        assert isinstance(d, Decision)


# ---------------------------------------------------------------------------
# 2. Tier ceilings
# ---------------------------------------------------------------------------

class TestTierCeilings:
    """Tiers enforce per-transaction amount ceilings."""

    def test_explorer_ceiling_enforced(self):
        cfg = TierConfig(
            explorer=TokenBucketConfig(per_tx_cap=100, rate=10, capacity=100),
            standard=TokenBucketConfig(per_tx_cap=1000, rate=100, capacity=1000),
            trusted=TokenBucketConfig(per_tx_cap=10_000, rate=1000, capacity=10_000),
        )
        policy = AccessPolicy(tier_config=cfg)
        policy.register("explorer-1", tier=AgentTier.EXPLORER, spend_policy=_policy())

        # At ceiling — allowed
        d = policy.check("explorer-1", _make_pay_action(amount=100))
        assert d.allowed is True

        # Over ceiling — denied with tier_ceiling reason
        d = policy.check("explorer-1", _make_pay_action(amount=101))
        assert d.allowed is False
        assert d.reason == "tier_ceiling"

    def test_standard_ceiling_enforced(self):
        cfg = TierConfig(
            explorer=TokenBucketConfig(per_tx_cap=100, rate=10, capacity=100),
            standard=TokenBucketConfig(per_tx_cap=1000, rate=100, capacity=1000),
            trusted=TokenBucketConfig(per_tx_cap=10_000, rate=1000, capacity=10_000),
        )
        policy = AccessPolicy(tier_config=cfg)
        policy.register("std-1", tier=AgentTier.STANDARD, spend_policy=_policy())

        d = policy.check("std-1", _make_pay_action(amount=1001))
        assert d.allowed is False
        assert d.reason == "tier_ceiling"

    def test_trusted_ceiling_higher_than_standard(self):
        cfg = TierConfig(
            explorer=TokenBucketConfig(per_tx_cap=100, rate=10, capacity=100),
            standard=TokenBucketConfig(per_tx_cap=1000, rate=100, capacity=1000),
            trusted=TokenBucketConfig(per_tx_cap=10_000, rate=1000, capacity=10_000),
        )
        policy = AccessPolicy(tier_config=cfg)
        policy.register("trusted-1", tier=AgentTier.TRUSTED, spend_policy=_policy())

        # 5000 is within trusted but over standard
        d = policy.check("trusted-1", _make_pay_action(amount=5000))
        assert d.allowed is True

    def test_tier_upgrade_relaxes_ceiling(self):
        cfg = TierConfig(
            explorer=TokenBucketConfig(per_tx_cap=100, rate=10, capacity=100),
            standard=TokenBucketConfig(per_tx_cap=1000, rate=100, capacity=1000),
            trusted=TokenBucketConfig(per_tx_cap=10_000, rate=1000, capacity=10_000),
        )
        policy = AccessPolicy(tier_config=cfg)
        policy.register("agent-x", tier=AgentTier.EXPLORER, spend_policy=_policy())

        # Currently denied as explorer
        d = policy.check("agent-x", _make_pay_action(amount=500))
        assert d.allowed is False

        # Upgrade to standard
        policy.set_tier("agent-x", AgentTier.STANDARD)
        d = policy.check("agent-x", _make_pay_action(amount=500))
        assert d.allowed is True


# ---------------------------------------------------------------------------
# 3. Rate fairness (token-bucket)
# ---------------------------------------------------------------------------

class TestRateFairness:
    """One agent cannot starve others — token-bucket enforces bounded share."""

    def test_single_agent_exhausts_bucket_then_rate_limited(self):
        """After capacity exhaustion, further requests are rate_limited."""
        cfg = TierConfig(
            explorer=TokenBucketConfig(per_tx_cap=1_000_000, rate=0, capacity=3),
            standard=TokenBucketConfig(per_tx_cap=1_000_000, rate=0, capacity=3),
            trusted=TokenBucketConfig(per_tx_cap=1_000_000, rate=0, capacity=3),
        )
        policy = AccessPolicy(tier_config=cfg)
        policy.register("hog", tier=AgentTier.STANDARD, spend_policy=_policy())

        # Drain 3 tokens from the bucket
        results = [policy.check("hog", _make_pay_action(amount=1)) for _ in range(3)]
        assert all(d.allowed for d in results)

        # 4th request should be rate_limited
        d = policy.check("hog", _make_pay_action(amount=1))
        assert d.allowed is False
        assert d.reason == "rate_limited"

    def test_n_agents_each_get_bounded_share(self):
        """N agents contending: each gets at most capacity tokens, others not starved."""
        cap = 5
        cfg = TierConfig(
            explorer=TokenBucketConfig(per_tx_cap=1_000_000, rate=0, capacity=cap),
            standard=TokenBucketConfig(per_tx_cap=1_000_000, rate=0, capacity=cap),
            trusted=TokenBucketConfig(per_tx_cap=1_000_000, rate=0, capacity=cap),
        )
        policy = AccessPolicy(tier_config=cfg)
        n_agents = 4
        for i in range(n_agents):
            policy.register(f"agent-{i}", tier=AgentTier.STANDARD, spend_policy=_policy())

        allow_counts: dict[str, int] = {f"agent-{i}": 0 for i in range(n_agents)}
        deny_counts: dict[str, int] = {f"agent-{i}": 0 for i in range(n_agents)}

        # Each agent fires 10 requests
        for _ in range(10):
            for i in range(n_agents):
                d = policy.check(f"agent-{i}", _make_pay_action(amount=1))
                if d.allowed:
                    allow_counts[f"agent-{i}"] += 1
                else:
                    deny_counts[f"agent-{i}"] += 1

        # Each agent is bounded to exactly `cap` allows (bucket drained, rate=0)
        for i in range(n_agents):
            assert allow_counts[f"agent-{i}"] == cap, (
                f"agent-{i} allowed {allow_counts[f'agent-{i}']} times, expected {cap}"
            )

    def test_token_bucket_refills_over_time(self):
        """Token bucket refills at the configured rate."""
        fast_clock = [0.0]

        def clock():
            return fast_clock[0]

        # rate=1 token/second, capacity=2
        cfg = TierConfig(
            explorer=TokenBucketConfig(per_tx_cap=1_000_000, rate=1.0, capacity=2),
            standard=TokenBucketConfig(per_tx_cap=1_000_000, rate=1.0, capacity=2),
            trusted=TokenBucketConfig(per_tx_cap=1_000_000, rate=1.0, capacity=2),
        )
        policy = AccessPolicy(tier_config=cfg, clock=clock)
        policy.register("refill-agent", tier=AgentTier.STANDARD, spend_policy=_policy())

        # Drain bucket
        policy.check("refill-agent", _make_pay_action(amount=1))
        policy.check("refill-agent", _make_pay_action(amount=1))
        d = policy.check("refill-agent", _make_pay_action(amount=1))
        assert d.allowed is False

        # Advance time by 2 seconds → 2 tokens refilled
        fast_clock[0] = 2.0
        d = policy.check("refill-agent", _make_pay_action(amount=1))
        assert d.allowed is True

    def test_concurrent_agents_thread_safe(self):
        """N threads hitting check() concurrently — no races, bounded results."""
        cap = 10
        cfg = TierConfig(
            explorer=TokenBucketConfig(per_tx_cap=1_000_000, rate=0, capacity=cap),
            standard=TokenBucketConfig(per_tx_cap=1_000_000, rate=0, capacity=cap),
            trusted=TokenBucketConfig(per_tx_cap=1_000_000, rate=0, capacity=cap),
        )
        policy = AccessPolicy(tier_config=cfg)
        n_agents = 3
        attempts = 20
        for i in range(n_agents):
            policy.register(f"t-agent-{i}", tier=AgentTier.STANDARD, spend_policy=_policy())

        allows: List[int] = [0] * n_agents
        lock = threading.Lock()

        def run(idx: int):
            local_allows = 0
            for _ in range(attempts):
                d = policy.check(f"t-agent-{idx}", _make_pay_action(amount=1))
                if d.allowed:
                    local_allows += 1
            with lock:
                allows[idx] = local_allows

        threads = [threading.Thread(target=run, args=(i,)) for i in range(n_agents)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for i, count in enumerate(allows):
            assert count == cap, f"t-agent-{i} got {count} allows, expected {cap}"


# ---------------------------------------------------------------------------
# 4. Contract compliance checks
# ---------------------------------------------------------------------------

class TestContractCompliance:
    """Refuse actions that would violate escrow contract terms."""

    def test_escrow_in_terminal_state_refused(self):
        """Cannot interact with an escrow that's already closed/refunded."""
        policy = AccessPolicy()
        policy.register("comp-agent", tier=AgentTier.TRUSTED, spend_policy=_policy())

        action = _make_escrow_action(escrow_state="released")
        d = policy.check("comp-agent", action)
        assert d.allowed is False
        assert d.reason == "noncompliant"

    def test_escrow_refunded_state_refused(self):
        policy = AccessPolicy()
        policy.register("comp-agent", tier=AgentTier.TRUSTED, spend_policy=_policy())

        action = _make_escrow_action(escrow_state="refunded")
        d = policy.check("comp-agent", action)
        assert d.allowed is False
        assert d.reason == "noncompliant"

    def test_escrow_cancelled_state_refused(self):
        policy = AccessPolicy()
        policy.register("comp-agent", tier=AgentTier.TRUSTED, spend_policy=_policy())

        action = _make_escrow_action(escrow_state="cancelled")
        d = policy.check("comp-agent", action)
        assert d.allowed is False
        assert d.reason == "noncompliant"

    def test_escrow_open_state_allowed(self):
        """An escrow in 'open' state can be acted upon."""
        policy = AccessPolicy()
        policy.register("comp-agent", tier=AgentTier.TRUSTED, spend_policy=_policy())

        action = _make_escrow_action(escrow_state="open")
        d = policy.check("comp-agent", action)
        assert d.allowed is True

    def test_escrow_confirmed_state_allowed(self):
        """Confirmed escrow can be released."""
        policy = AccessPolicy()
        policy.register("comp-agent", tier=AgentTier.TRUSTED, spend_policy=_policy())

        action = _make_escrow_action(escrow_state="confirmed")
        d = policy.check("comp-agent", action)
        assert d.allowed is True

    def test_zero_amount_action_refused_as_noncompliant(self):
        """A zero-amount payment violates escrow minimum-amount constraints."""
        policy = AccessPolicy()
        policy.register("comp-agent", tier=AgentTier.TRUSTED, spend_policy=_policy())

        action = _make_pay_action(amount=0)
        d = policy.check("comp-agent", action)
        assert d.allowed is False
        assert d.reason == "noncompliant"

    def test_negative_amount_action_refused_as_noncompliant(self):
        policy = AccessPolicy()
        policy.register("comp-agent", tier=AgentTier.TRUSTED, spend_policy=_policy())

        action = _make_pay_action(amount=-50)
        d = policy.check("comp-agent", action)
        assert d.allowed is False
        assert d.reason == "noncompliant"


# ---------------------------------------------------------------------------
# 5. SpendPolicy integration
# ---------------------------------------------------------------------------

class TestSpendPolicyIntegration:
    """AccessPolicy respects the underlying SpendPolicy rules."""

    def test_token_not_in_allowlist_denied(self):
        """If the action token isn't in the SpendPolicy allowlist, deny."""
        policy = AccessPolicy()
        sp = _policy(token_allowlist=[ETH])
        policy.register("sp-agent", tier=AgentTier.STANDARD, spend_policy=sp)

        d = policy.check("sp-agent", _make_pay_action(amount=10, token=USDC))
        assert d.allowed is False
        assert d.reason == "policy_violation"

    def test_expired_policy_denied(self):
        past = datetime(2000, 1, 1, tzinfo=timezone.utc)
        sp = _policy(expires_at=past)
        policy = AccessPolicy()
        policy.register("expired-agent", tier=AgentTier.STANDARD, spend_policy=sp)

        d = policy.check("expired-agent", _make_pay_action(amount=10))
        assert d.allowed is False
        assert d.reason == "policy_violation"

    def test_per_tx_cap_in_spend_policy_denied(self):
        """SpendPolicy.per_tx_cap is an additional ceiling below tier cap."""
        cfg = TierConfig(
            explorer=TokenBucketConfig(per_tx_cap=10_000, rate=100, capacity=1000),
            standard=TokenBucketConfig(per_tx_cap=10_000, rate=100, capacity=1000),
            trusted=TokenBucketConfig(per_tx_cap=10_000, rate=100, capacity=1000),
        )
        policy = AccessPolicy(tier_config=cfg)
        sp = _policy(per_tx_cap=50)
        policy.register("sp-cap-agent", tier=AgentTier.STANDARD, spend_policy=sp)

        d = policy.check("sp-cap-agent", _make_pay_action(amount=100))
        assert d.allowed is False
        # per_tx_cap from SpendPolicy is a policy_violation, not tier_ceiling
        assert d.reason == "policy_violation"


# ---------------------------------------------------------------------------
# 6. WalletOpEvent emission
# ---------------------------------------------------------------------------

class TestWalletOpEvents:
    """Denials emit WalletOpEvent with structured reason and agent metadata."""

    def test_denied_decision_has_wallet_op_event(self):
        cfg = TierConfig(
            explorer=TokenBucketConfig(per_tx_cap=100, rate=0, capacity=1),
            standard=TokenBucketConfig(per_tx_cap=100, rate=0, capacity=1),
            trusted=TokenBucketConfig(per_tx_cap=100, rate=0, capacity=1),
        )
        policy = AccessPolicy(tier_config=cfg)
        policy.register("ev-agent", tier=AgentTier.EXPLORER, spend_policy=_policy())

        # Drain bucket
        policy.check("ev-agent", _make_pay_action(amount=1))
        # Second call → denied
        d = policy.check("ev-agent", _make_pay_action(amount=1))
        assert d.allowed is False

        evt = d.event
        assert isinstance(evt, WalletOpEvent)
        assert evt.denied is True
        assert evt.denial_reason == d.reason
        assert evt.agent_id == "ev-agent"

    def test_allowed_decision_event_not_denied(self):
        policy = AccessPolicy()
        policy.register("ev-allow", tier=AgentTier.TRUSTED, spend_policy=_policy())

        d = policy.check("ev-allow", _make_pay_action(amount=10))
        assert d.allowed is True
        assert d.event.denied is False
        assert d.event.denial_reason is None

    def test_event_listener_receives_denied_events(self):
        """AccessPolicy accepts an event_listener callable called on each check."""
        events: List[WalletOpEvent] = []

        cfg = TierConfig(
            explorer=TokenBucketConfig(per_tx_cap=10, rate=0, capacity=1),
            standard=TokenBucketConfig(per_tx_cap=10, rate=0, capacity=1),
            trusted=TokenBucketConfig(per_tx_cap=10, rate=0, capacity=1),
        )
        policy = AccessPolicy(tier_config=cfg, event_listener=events.append)
        policy.register("listen-agent", tier=AgentTier.STANDARD, spend_policy=_policy())

        policy.check("listen-agent", _make_pay_action(amount=1))  # allow
        policy.check("listen-agent", _make_pay_action(amount=1))  # deny
        policy.check("listen-agent", _make_pay_action(amount=1))  # deny

        assert len(events) == 3
        denied = [e for e in events if e.denied]
        allowed = [e for e in events if not e.denied]
        assert len(denied) == 2
        assert len(allowed) == 1


# ---------------------------------------------------------------------------
# 7. Decision dataclass
# ---------------------------------------------------------------------------

class TestDecision:
    def test_allow_decision_fields(self):
        evt = WalletOpEvent(denied=False, denial_reason=None, agent_id="a")
        d = Decision(agent_id="a", allowed=True, reason=None, event=evt)
        assert d.agent_id == "a"
        assert d.allowed is True
        assert d.reason is None

    def test_deny_decision_reason_is_string(self):
        evt = WalletOpEvent(denied=True, denial_reason="tier_ceiling", agent_id="b")
        d = Decision(agent_id="b", allowed=False, reason="tier_ceiling", event=evt)
        assert d.reason == "tier_ceiling"

    def test_valid_reason_strings(self):
        """Allowed reason values are the typed literals defined in the spec."""
        valid_reasons = {"rate_limited", "tier_ceiling", "noncompliant", "policy_violation"}
        for r in valid_reasons:
            evt = WalletOpEvent(denied=True, denial_reason=r, agent_id="x")
            d = Decision(agent_id="x", allowed=False, reason=r, event=evt)
            assert d.reason in valid_reasons
