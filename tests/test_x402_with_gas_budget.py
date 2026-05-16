"""Integration: X402Middleware driven by GasBudgetTracker via WalletBoundBudget.

Exercises the compat shim that lets the new multi-wallet tracker satisfy the
middleware's older ``can_send_transaction`` / ``record_gas_usage`` contract.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from switchboard.gas_budget import GasBudgetTracker, GasLimits, WalletBoundBudget
from switchboard.x402_middleware import PaymentOffer, X402Middleware


PAYER = "0xPAYER"


def _make_middleware(bound: WalletBoundBudget, *, max_payment_wei: int) -> X402Middleware:
    client = MagicMock()
    client.wallet_address = PAYER
    return X402Middleware(
        payment_client=client,
        gas_tracker=bound,
        max_payment_wei=max_payment_wei,
    )


def _offer(amount_wei: int) -> PaymentOffer:
    return PaymentOffer(
        amount_wei=amount_wei,
        currency="ETH",
        recipient="0xRECIPIENT",
        chain_id=1,
    )


class TestX402WithGasBudgetTracker:
    def test_bind_wallet_returns_wallet_bound_budget(self):
        budget = GasBudgetTracker(default_limits=GasLimits(per_hour=10**18))
        bound = budget.bind_wallet(PAYER)
        assert isinstance(bound, WalletBoundBudget)
        assert bound.wallet == PAYER
        assert bound.tracker is budget

    def test_small_offer_is_accepted(self):
        budget = GasBudgetTracker(default_limits=GasLimits(per_hour=10**18))
        bound = budget.bind_wallet(PAYER)
        mw = _make_middleware(bound, max_payment_wei=10**18)

        offer = _offer(amount_wei=10**12)  # well under the per-hour ceiling
        mw._validate_offer(offer)  # should not raise

    def test_huge_offer_is_rejected_via_can_send_transaction(self):
        # Hourly limit deliberately smaller than the offer amount so the
        # *gas budget* (not the max_payment_wei cap) is what blocks the spend.
        budget = GasBudgetTracker(default_limits=GasLimits(per_hour=10**6))
        bound = budget.bind_wallet(PAYER)
        mw = _make_middleware(bound, max_payment_wei=10**30)

        offer = _offer(amount_wei=10**18)
        with pytest.raises(ValueError, match="gas budget"):
            mw._validate_offer(offer)

    def test_record_gas_usage_updates_underlying_tracker(self):
        budget = GasBudgetTracker(default_limits=GasLimits(per_hour=10**6))
        bound = budget.bind_wallet(PAYER)

        assert bound.can_send_transaction(500_000) is True
        bound.record_gas_usage(500_000)

        # 500k of 1M consumed; another 500k still fits, 600k does not.
        assert bound.can_send_transaction(500_000) is True
        assert bound.can_send_transaction(600_000) is False
        assert budget.status(PAYER).spent_last_hour == 500_000

    def test_wallets_remain_isolated_through_shim(self):
        budget = GasBudgetTracker(default_limits=GasLimits(per_hour=1_000))
        a = budget.bind_wallet("0xA")
        b = budget.bind_wallet("0xB")

        a.record_gas_usage(1_000)
        assert a.is_paused() is True
        assert b.is_paused() is False
        assert b.can_send_transaction(999) is True
