"""Tests for Unit ⑩ — TokenSelector.

Strategy: pick the source token to spend based on:
  1. balance (must have enough spendable)
  2. fee (prefer lower fee)
  3. expected slippage (prefer lower slippage)

All tests written BEFORE the implementation (TDD — RED first).
"""

import pytest
from unittest.mock import MagicMock

from switchboard.treasury import Treasury
from switchboard.router.token_selector import TokenSelector, TokenCandidate


CHAIN_ID = 1
ETH   = "0x0000000000000000000000000000000000000000"
USDC  = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDT  = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
LUX   = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
ZOO   = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_treasury(**balances):
    """Return a Treasury pre-loaded with (chain=1, token=amount) balances."""
    t = Treasury()
    for token, amount in balances.items():
        t.credit(CHAIN_ID, token, amount)
    return t


# ---------------------------------------------------------------------------
# Unit ⑩ Tests — each tests one distinct behaviour
# ---------------------------------------------------------------------------

class TestTokenSelectorPicksOnlySolvencyTokens:
    """Tokens with insufficient balance must not be returned."""

    def test_single_token_insufficient_balance_returns_none(self):
        treasury = make_treasury(**{ETH: 50})
        selector = TokenSelector(treasury=treasury, chain_id=CHAIN_ID)
        result = selector.select(amount=100, candidates=[TokenCandidate(token=ETH)])
        assert result is None

    def test_single_token_exact_balance_is_selected(self):
        treasury = make_treasury(**{USDC: 100})
        selector = TokenSelector(treasury=treasury, chain_id=CHAIN_ID)
        result = selector.select(amount=100, candidates=[TokenCandidate(token=USDC)])
        assert result is not None
        assert result.token == USDC

    def test_multiple_tokens_only_solvent_ones_returned(self):
        treasury = make_treasury(**{ETH: 5, USDC: 200})
        selector = TokenSelector(treasury=treasury, chain_id=CHAIN_ID)
        result = selector.select(
            amount=100,
            candidates=[
                TokenCandidate(token=ETH),    # insufficient
                TokenCandidate(token=USDC),   # sufficient
            ],
        )
        assert result is not None
        assert result.token == USDC


class TestTokenSelectorPreferLowerFee:
    """Among solvent tokens, pick the one with lower fee_bps."""

    def test_lower_fee_token_preferred(self):
        treasury = make_treasury(**{USDC: 1000, USDT: 1000})
        selector = TokenSelector(treasury=treasury, chain_id=CHAIN_ID)
        result = selector.select(
            amount=100,
            candidates=[
                TokenCandidate(token=USDC, fee_bps=30),
                TokenCandidate(token=USDT, fee_bps=5),
            ],
        )
        assert result is not None
        assert result.token == USDT   # lower fee wins

    def test_zero_fee_beats_any_positive_fee(self):
        treasury = make_treasury(**{ETH: 1000, USDC: 1000})
        selector = TokenSelector(treasury=treasury, chain_id=CHAIN_ID)
        result = selector.select(
            amount=100,
            candidates=[
                TokenCandidate(token=ETH,  fee_bps=0),
                TokenCandidate(token=USDC, fee_bps=10),
            ],
        )
        assert result.token == ETH


class TestTokenSelectorPreferLowerSlippage:
    """When fees are tied, pick the token with lower expected_slippage_bps."""

    def test_lower_slippage_token_preferred_on_fee_tie(self):
        treasury = make_treasury(**{USDC: 1000, LUX: 1000})
        selector = TokenSelector(treasury=treasury, chain_id=CHAIN_ID)
        result = selector.select(
            amount=100,
            candidates=[
                TokenCandidate(token=USDC, fee_bps=10, expected_slippage_bps=50),
                TokenCandidate(token=LUX,  fee_bps=10, expected_slippage_bps=20),
            ],
        )
        assert result.token == LUX


class TestTokenSelectorPartnerTokensWorkNaturally:
    """LUX and ZOO (partner tokens) need no special-casing — balance drives selection."""

    def test_lux_selected_when_highest_balance_and_lowest_fee(self):
        treasury = make_treasury(**{LUX: 5000, ZOO: 5000, USDC: 100})
        selector = TokenSelector(treasury=treasury, chain_id=CHAIN_ID)
        result = selector.select(
            amount=100,
            candidates=[
                TokenCandidate(token=USDC, fee_bps=5,  expected_slippage_bps=10),
                TokenCandidate(token=LUX,  fee_bps=2,  expected_slippage_bps=5),
                TokenCandidate(token=ZOO,  fee_bps=2,  expected_slippage_bps=8),
            ],
        )
        assert result.token == LUX

    def test_zoo_selected_when_lux_insufficient(self):
        treasury = make_treasury(**{LUX: 10, ZOO: 500})  # LUX balance < amount
        selector = TokenSelector(treasury=treasury, chain_id=CHAIN_ID)
        result = selector.select(
            amount=100,
            candidates=[
                TokenCandidate(token=LUX, fee_bps=0),
                TokenCandidate(token=ZOO, fee_bps=5),
            ],
        )
        assert result.token == ZOO


class TestTokenSelectorEmptyCandidates:
    def test_empty_candidates_returns_none(self):
        treasury = make_treasury(**{ETH: 1000})
        selector = TokenSelector(treasury=treasury, chain_id=CHAIN_ID)
        result = selector.select(amount=100, candidates=[])
        assert result is None


class TestTokenCandidateDefaults:
    """TokenCandidate should default fee_bps and expected_slippage_bps to 0."""

    def test_defaults_are_zero(self):
        c = TokenCandidate(token=USDC)
        assert c.fee_bps == 0
        assert c.expected_slippage_bps == 0
