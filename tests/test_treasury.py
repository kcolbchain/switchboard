"""Tests for switchboard.treasury — Unit ⑧ (Treasury portion).

TDD: these tests are written first and must be run to confirm they fail before
implementation exists, then pass after implementation is complete.
"""

from __future__ import annotations

import pytest

from switchboard.treasury import Treasury, InsufficientBalance


# Token addresses used in tests
ETH = "0x0000000000000000000000000000000000000000"   # native ETH sentinel
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
DAI  = "0x6B175474E89094C44Da98b954EedeAC495271d0F"
LUX  = "0xLUX0000000000000000000000000000000000001"   # kcolbchain partner token
ZOO  = "0xZOO0000000000000000000000000000000000002"   # kcolbchain partner token

CHAIN_1 = 1
CHAIN_137 = 137  # Polygon


# ---------------------------------------------------------------------------
# Construction / empty state
# ---------------------------------------------------------------------------


def test_empty_treasury_balance_is_zero():
    t = Treasury()
    assert t.balance(CHAIN_1, ETH) == 0


def test_balance_isolated_across_chains():
    t = Treasury()
    t.credit(CHAIN_1, USDC, 1_000)
    assert t.balance(CHAIN_137, USDC) == 0


def test_balance_isolated_across_tokens():
    t = Treasury()
    t.credit(CHAIN_1, USDC, 500)
    assert t.balance(CHAIN_1, DAI) == 0


# ---------------------------------------------------------------------------
# credit / debit
# ---------------------------------------------------------------------------


def test_credit_increases_balance():
    t = Treasury()
    t.credit(CHAIN_1, ETH, 10 ** 18)
    assert t.balance(CHAIN_1, ETH) == 10 ** 18


def test_credit_is_additive():
    t = Treasury()
    t.credit(CHAIN_1, USDC, 100)
    t.credit(CHAIN_1, USDC, 200)
    assert t.balance(CHAIN_1, USDC) == 300


def test_debit_reduces_balance():
    t = Treasury()
    t.credit(CHAIN_1, USDC, 500)
    t.debit(CHAIN_1, USDC, 200)
    assert t.balance(CHAIN_1, USDC) == 300


def test_debit_to_zero_is_allowed():
    t = Treasury()
    t.credit(CHAIN_1, ETH, 100)
    t.debit(CHAIN_1, ETH, 100)
    assert t.balance(CHAIN_1, ETH) == 0


def test_debit_below_zero_raises():
    t = Treasury()
    t.credit(CHAIN_1, ETH, 50)
    with pytest.raises(InsufficientBalance):
        t.debit(CHAIN_1, ETH, 51)


def test_debit_on_empty_raises():
    t = Treasury()
    with pytest.raises(InsufficientBalance):
        t.debit(CHAIN_1, USDC, 1)


def test_credit_negative_raises():
    t = Treasury()
    with pytest.raises(ValueError):
        t.credit(CHAIN_1, USDC, -1)


def test_debit_negative_raises():
    t = Treasury()
    with pytest.raises(ValueError):
        t.debit(CHAIN_1, USDC, -1)


# ---------------------------------------------------------------------------
# spendable (respects reserves)
# ---------------------------------------------------------------------------


def test_spendable_equals_balance_with_no_reserve():
    t = Treasury()
    t.credit(CHAIN_1, USDC, 1_000)
    assert t.spendable(CHAIN_1, USDC) == 1_000


def test_spendable_respects_reserve():
    t = Treasury()
    t.credit(CHAIN_1, USDC, 1_000)
    t.set_reserve(CHAIN_1, USDC, 200)
    assert t.spendable(CHAIN_1, USDC) == 800


def test_spendable_never_negative():
    """Reserve > balance → spendable == 0, not negative."""
    t = Treasury()
    t.credit(CHAIN_1, ETH, 100)
    t.set_reserve(CHAIN_1, ETH, 500)
    assert t.spendable(CHAIN_1, ETH) == 0


def test_reserve_default_is_zero():
    t = Treasury()
    t.credit(CHAIN_1, ETH, 99)
    assert t.spendable(CHAIN_1, ETH) == 99


# ---------------------------------------------------------------------------
# Multi-token / multi-chain snapshot
# ---------------------------------------------------------------------------


def test_balances_snapshot_returns_all_tokens():
    t = Treasury()
    t.credit(CHAIN_1, ETH, 10 ** 18)
    t.credit(CHAIN_1, USDC, 500_000_000)
    t.credit(CHAIN_1, LUX, 1_000)
    snap = t.balances(CHAIN_1)
    assert snap[ETH] == 10 ** 18
    assert snap[USDC] == 500_000_000
    assert snap[LUX] == 1_000


def test_balances_snapshot_excludes_other_chains():
    t = Treasury()
    t.credit(CHAIN_1, USDC, 100)
    t.credit(CHAIN_137, ZOO, 200)
    snap = t.balances(CHAIN_1)
    assert ZOO not in snap
    snap_poly = t.balances(CHAIN_137)
    assert USDC not in snap_poly


def test_partner_tokens_lux_zoo_tracked():
    """Partner tokens LUX and ZOO are first-class; no special-casing needed."""
    t = Treasury()
    t.credit(CHAIN_1, LUX, 9_000)
    t.credit(CHAIN_1, ZOO, 4_200)
    assert t.balance(CHAIN_1, LUX) == 9_000
    assert t.balance(CHAIN_1, ZOO) == 4_200
