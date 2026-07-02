"""Tests for Unit ⑬ — Rebalancer.

Strategy: compute *intended* swap actions to move treasury allocations toward
a target ratio.  The Rebalancer emits ``SwapIntent`` objects — it does NOT
execute swaps (that is the adapter's job).

All tests written BEFORE the implementation (TDD — RED first).
"""

import pytest
from switchboard.treasury import Treasury
from switchboard.router.rebalancer import Rebalancer, RebalanceTarget, SwapIntent


CHAIN_ID = 1
ETH  = "0x0000000000000000000000000000000000000000"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
LUX  = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
ZOO  = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def make_treasury(**balances):
    t = Treasury()
    for token, amount in balances.items():
        t.credit(CHAIN_ID, token, amount)
    return t


# ---------------------------------------------------------------------------
# Unit ⑬ Tests
# ---------------------------------------------------------------------------

class TestRebalancerNothingToDo:
    """When treasury already matches targets, emit no swaps."""

    def test_perfectly_balanced_produces_no_intents(self):
        treasury = make_treasury(**{USDC: 600, ETH: 400})
        targets = [
            RebalanceTarget(token=USDC, target_pct=60.0),
            RebalanceTarget(token=ETH,  target_pct=40.0),
        ]
        rebalancer = Rebalancer(treasury=treasury, chain_id=CHAIN_ID)
        intents = rebalancer.rebalance_targets(targets=targets)
        assert intents == []


class TestRebalancerSingleTokenOverweight:
    """One overweight token → one swap intent to sell the surplus."""

    def test_usdc_overweight_emits_swap_to_eth(self):
        # 800 USDC, 200 ETH → target 60/40 → ideal 600/400 → sell 200 USDC
        treasury = make_treasury(**{USDC: 800, ETH: 200})
        targets = [
            RebalanceTarget(token=USDC, target_pct=60.0),
            RebalanceTarget(token=ETH,  target_pct=40.0),
        ]
        rebalancer = Rebalancer(treasury=treasury, chain_id=CHAIN_ID)
        intents = rebalancer.rebalance_targets(targets=targets)

        # Should have at least one intent moving USDC → ETH
        assert len(intents) >= 1
        sell_intent = next((i for i in intents if i.from_token == USDC), None)
        assert sell_intent is not None
        assert sell_intent.to_token == ETH
        assert sell_intent.amount > 0


class TestRebalancerThresholdFiltering:
    """Swaps below a threshold percentage should not be emitted (avoid tiny swaps)."""

    def test_small_imbalance_below_threshold_produces_no_intent(self):
        # 505 USDC, 495 ETH → target 50/50 → only 5 off; threshold=2% of 1000 = 20
        treasury = make_treasury(**{USDC: 505, ETH: 495})
        targets = [
            RebalanceTarget(token=USDC, target_pct=50.0),
            RebalanceTarget(token=ETH,  target_pct=50.0),
        ]
        rebalancer = Rebalancer(
            treasury=treasury, chain_id=CHAIN_ID, min_rebalance_pct=2.0
        )
        intents = rebalancer.rebalance_targets(targets=targets)
        assert intents == []

    def test_larger_imbalance_above_threshold_produces_intent(self):
        # 700 USDC, 300 ETH → target 50/50 → 200 off; threshold=2% of 1000 = 20
        treasury = make_treasury(**{USDC: 700, ETH: 300})
        targets = [
            RebalanceTarget(token=USDC, target_pct=50.0),
            RebalanceTarget(token=ETH,  target_pct=50.0),
        ]
        rebalancer = Rebalancer(
            treasury=treasury, chain_id=CHAIN_ID, min_rebalance_pct=2.0
        )
        intents = rebalancer.rebalance_targets(targets=targets)
        assert len(intents) >= 1


class TestRebalancerPartnerTokens:
    """LUX and ZOO work as first-class allocation targets."""

    def test_lux_zoo_underweight_both_emit_buy_intents(self):
        # Hold: 100% USDC, 0% LUX, 0% ZOO → targets: 80% USDC, 10% LUX, 10% ZOO
        treasury = make_treasury(**{USDC: 1000})
        targets = [
            RebalanceTarget(token=USDC, target_pct=80.0),
            RebalanceTarget(token=LUX,  target_pct=10.0),
            RebalanceTarget(token=ZOO,  target_pct=10.0),
        ]
        rebalancer = Rebalancer(treasury=treasury, chain_id=CHAIN_ID)
        intents = rebalancer.rebalance_targets(targets=targets)

        to_tokens = {i.to_token for i in intents}
        assert LUX in to_tokens
        assert ZOO in to_tokens


class TestRebalancerInvalidTargets:
    """Target percentages must sum to 100 (±float tolerance)."""

    def test_targets_not_summing_to_100_raises(self):
        treasury = make_treasury(**{USDC: 1000})
        targets = [
            RebalanceTarget(token=USDC, target_pct=60.0),
            RebalanceTarget(token=ETH,  target_pct=20.0),  # total = 80%
        ]
        rebalancer = Rebalancer(treasury=treasury, chain_id=CHAIN_ID)
        with pytest.raises(ValueError, match="100"):
            rebalancer.rebalance_targets(targets=targets)


class TestSwapIntentDataclass:
    def test_swap_intent_fields(self):
        intent = SwapIntent(
            from_token=USDC,
            to_token=ETH,
            amount=100,
            chain_id=1,
        )
        assert intent.from_token == USDC
        assert intent.to_token == ETH
        assert intent.amount == 100
        assert intent.chain_id == 1


class TestRebalanceTargetDataclass:
    def test_rebalance_target_fields(self):
        t = RebalanceTarget(token=USDC, target_pct=60.0)
        assert t.token == USDC
        assert t.target_pct == 60.0
