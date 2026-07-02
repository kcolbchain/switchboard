"""Tests for switchboard.agent_wallet — Unit ⑧ (AgentWallet portion).

TDD: these tests are written first and must be run to confirm they fail before
implementation exists, then pass after implementation is complete.

The on-chain escrow client is NOT available in this worktree.  We test against
the ``EscrowClient`` Protocol via a mock — the real client wires in later.
See the EscrowClient seam defined in switchboard/agent_wallet.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from switchboard.agent_wallet import (
    AgentWallet,
    PaymentRequest,
    PaymentReceipt,
    EscrowClient,   # the Protocol / interface seam
    WalletError,
)
from switchboard.mpc_wallet import MPCWallet
from switchboard.treasury import Treasury, InsufficientBalance


# Token addresses
ETH  = "0x0000000000000000000000000000000000000000"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
CHAIN_1 = 1


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def make_wallet() -> tuple[AgentWallet, Treasury, MagicMock]:
    """Return an AgentWallet with a pre-funded Treasury and a mock EscrowClient."""
    mpc = MPCWallet(parties=3, threshold=2, chain_id=CHAIN_1)
    treasury = Treasury()
    treasury.credit(CHAIN_1, USDC, 1_000_000_000)   # 1,000 USDC (6 decimals)
    treasury.credit(CHAIN_1, ETH, 2 * 10**18)

    mock_escrow: EscrowClient = MagicMock(spec=EscrowClient)
    mock_escrow.create_payment.return_value = "0xescrow_id_abc"
    mock_escrow.release_payment.return_value = True

    wallet = AgentWallet(mpc=mpc, treasury=treasury, escrow=mock_escrow)
    return wallet, treasury, mock_escrow


def make_request(
    chain_id: int = CHAIN_1,
    token: str = USDC,
    amount: int = 100_000_000,   # 100 USDC
    payee: str = "0xPayee000000000000000000000000000000000001",
) -> PaymentRequest:
    return PaymentRequest(
        chain_id=chain_id,
        token=token,
        amount_wei=amount,
        payee=payee,
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_agent_wallet_exposes_mpc_address():
    mpc = MPCWallet()
    wallet = AgentWallet(mpc=mpc)
    assert wallet.address() == mpc.address()


def test_agent_wallet_has_treasury():
    mpc = MPCWallet()
    treasury = Treasury()
    wallet = AgentWallet(mpc=mpc, treasury=treasury)
    assert wallet.treasury is treasury


# ---------------------------------------------------------------------------
# Treasury delegation: balance / spendable forwarded
# ---------------------------------------------------------------------------


def test_wallet_balance_delegates_to_treasury():
    wallet, treasury, _ = make_wallet()
    assert wallet.balance(CHAIN_1, USDC) == treasury.balance(CHAIN_1, USDC)


def test_wallet_spendable_delegates_to_treasury():
    wallet, treasury, _ = make_wallet()
    treasury.set_reserve(CHAIN_1, USDC, 50_000_000)
    assert wallet.spendable(CHAIN_1, USDC) == treasury.spendable(CHAIN_1, USDC)


# ---------------------------------------------------------------------------
# pay() — happy path
# ---------------------------------------------------------------------------


def test_pay_returns_receipt():
    wallet, treasury, _ = make_wallet()
    req = make_request()
    receipt = wallet.pay(req)
    assert isinstance(receipt, PaymentReceipt)


def test_pay_receipt_has_tx_id():
    wallet, treasury, _ = make_wallet()
    req = make_request()
    receipt = wallet.pay(req)
    assert receipt.tx_id is not None
    assert len(receipt.tx_id) > 0


def test_pay_receipt_records_token_and_amount():
    wallet, treasury, _ = make_wallet()
    req = make_request(token=USDC, amount=50_000_000)
    receipt = wallet.pay(req)
    assert receipt.token == USDC
    assert receipt.amount == 50_000_000


def test_pay_debits_treasury():
    wallet, treasury, _ = make_wallet()
    before = treasury.balance(CHAIN_1, USDC)
    req = make_request(amount=100_000_000)
    wallet.pay(req)
    assert treasury.balance(CHAIN_1, USDC) == before - 100_000_000


def test_pay_invokes_escrow_create():
    wallet, treasury, mock_escrow = make_wallet()
    req = make_request()
    wallet.pay(req)
    mock_escrow.create_payment.assert_called_once()


# ---------------------------------------------------------------------------
# pay() — error cases
# ---------------------------------------------------------------------------


def test_pay_raises_on_insufficient_balance():
    mpc = MPCWallet()
    treasury = Treasury()
    treasury.credit(CHAIN_1, USDC, 10)  # only 10 units
    mock_escrow: EscrowClient = MagicMock(spec=EscrowClient)
    wallet = AgentWallet(mpc=mpc, treasury=treasury, escrow=mock_escrow)

    req = make_request(amount=1_000_000)   # asks for 1,000,000
    with pytest.raises((InsufficientBalance, WalletError)):
        wallet.pay(req)


def test_pay_raises_on_zero_amount():
    wallet, _, _ = make_wallet()
    req = make_request(amount=0)
    with pytest.raises((ValueError, WalletError)):
        wallet.pay(req)


# ---------------------------------------------------------------------------
# EscrowClient Protocol conformance — mock satisfies the interface
# ---------------------------------------------------------------------------


def test_mock_escrow_satisfies_protocol():
    """The mock must satisfy the EscrowClient protocol; isinstance check via runtime_checkable."""
    from switchboard.agent_wallet import EscrowClient as EC
    mock_escrow = MagicMock(spec=EC)
    # structural check: mock has the required methods
    assert callable(getattr(mock_escrow, "create_payment", None))
    assert callable(getattr(mock_escrow, "release_payment", None))
