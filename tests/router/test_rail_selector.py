"""Tests for Unit ⑪ — RailSelector.

Strategy: pick the cheapest suitable rail for the payment amount.

Rails:
  x402   — micro-payments; cheapest, but only for amounts <= x402_max_amount.
  escrow — trustless on-chain; for amounts above x402_max_amount (up to escrow_max_amount).
  mpp    — multi-party payment; for amounts above escrow_max_amount or flagged trustless.

All tests written BEFORE the implementation (TDD — RED first).
"""

import pytest
from switchboard.router.rail_selector import RailSelector, RailConfig


# Default thresholds (in base units — think of as USDC micro-units or wei).
MICRO_MAX   = 1_000          # x402 up to 1 000 units
ESCROW_MAX  = 1_000_000      # escrow up to 1 000 000 units


def make_selector(micro_max=MICRO_MAX, escrow_max=ESCROW_MAX):
    return RailSelector(
        config=RailConfig(
            x402_max_amount=micro_max,
            escrow_max_amount=escrow_max,
        )
    )


class TestRailSelectorX402Threshold:
    """Amounts at or below x402_max_amount → x402 rail."""

    def test_micro_amount_uses_x402(self):
        sel = make_selector()
        assert sel.select(amount=1) == "x402"

    def test_at_micro_max_uses_x402(self):
        sel = make_selector()
        assert sel.select(amount=MICRO_MAX) == "x402"

    def test_just_above_micro_max_uses_escrow(self):
        sel = make_selector()
        assert sel.select(amount=MICRO_MAX + 1) == "escrow"


class TestRailSelectorEscrowThreshold:
    """Amounts between x402_max and escrow_max → escrow rail."""

    def test_mid_range_uses_escrow(self):
        sel = make_selector()
        assert sel.select(amount=50_000) == "escrow"

    def test_at_escrow_max_uses_escrow(self):
        sel = make_selector()
        assert sel.select(amount=ESCROW_MAX) == "escrow"

    def test_just_above_escrow_max_uses_mpp(self):
        sel = make_selector()
        assert sel.select(amount=ESCROW_MAX + 1) == "mpp"


class TestRailSelectorMPP:
    """Large amounts → mpp rail."""

    def test_large_amount_uses_mpp(self):
        sel = make_selector()
        assert sel.select(amount=10_000_000) == "mpp"

    def test_very_large_amount_uses_mpp(self):
        sel = make_selector()
        assert sel.select(amount=10 ** 18) == "mpp"


class TestRailSelectorForcedRail:
    """Caller can override the rail selection via ``force_rail``."""

    def test_force_escrow_on_micro_amount(self):
        sel = make_selector()
        assert sel.select(amount=1, force_rail="escrow") == "escrow"

    def test_force_mpp_on_micro_amount(self):
        sel = make_selector()
        assert sel.select(amount=1, force_rail="mpp") == "mpp"

    def test_force_x402_on_large_amount(self):
        sel = make_selector()
        assert sel.select(amount=10_000_000, force_rail="x402") == "x402"


class TestRailConfigDefaults:
    """RailConfig should have sensible defaults if not provided."""

    def test_default_config_exists(self):
        config = RailConfig()
        assert config.x402_max_amount > 0
        assert config.escrow_max_amount > config.x402_max_amount
