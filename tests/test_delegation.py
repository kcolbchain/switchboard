"""Tests for switchboard.delegation — Unit ⑨.

TDD: failing tests written first.

Covers:
- grant() returns a SessionKey.
- SpendPolicy fields: token_allowlist, per_tx_cap, daily_cap, expires_at,
  allowed_counterparties.
- Wallet enforces policy caps before co-signing (reuses gas_budget/gas_manager).
- Revocation blocks further signing.
- Expiry blocks signing.
- Token not in allowlist blocks signing.
- Counterparty not in allowed_counterparties blocks signing.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

from switchboard.delegation import (
    Delegation,
    SpendPolicy,
    SessionKey,
    PolicyViolation,
    grant,
    revoke,
)
from switchboard.agent_wallet import AgentWallet, PaymentRequest, EscrowClient
from switchboard.mpc_wallet import MPCWallet
from switchboard.treasury import Treasury


# ---------------------------------------------------------------------------
# Token / address constants
# ---------------------------------------------------------------------------

ETH  = "0x0000000000000000000000000000000000000000"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
DAI  = "0x6B175474E89094C44Da98b954EedeAC495271d0F"
LUX  = "0xLUX0000000000000000000000000000000000001"

PAYEE_A = "0xPayeeAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
PAYEE_B = "0xPayeeBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"

CHAIN_1 = 1


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _future(seconds: int = 3600) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _past(seconds: int = 1) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


def _funded_wallet() -> AgentWallet:
    mpc = MPCWallet(parties=3, threshold=2, chain_id=CHAIN_1)
    treasury = Treasury()
    treasury.credit(CHAIN_1, USDC, 10_000_000_000)   # 10,000 USDC
    treasury.credit(CHAIN_1, ETH, 10 * 10**18)
    treasury.credit(CHAIN_1, LUX, 100_000)
    mock_escrow: EscrowClient = MagicMock(spec=EscrowClient)
    mock_escrow.create_payment.return_value = "0xescrow_test"
    mock_escrow.release_payment.return_value = True
    return AgentWallet(mpc=mpc, treasury=treasury, escrow=mock_escrow)


def _req(
    token: str = USDC,
    amount: int = 100_000_000,
    payee: str = PAYEE_A,
) -> PaymentRequest:
    return PaymentRequest(
        chain_id=CHAIN_1,
        token=token,
        amount=amount,
        payee=payee,
    )


# ---------------------------------------------------------------------------
# grant() / SessionKey basics
# ---------------------------------------------------------------------------


def test_grant_returns_session_key():
    d = Delegation()
    policy = SpendPolicy(
        token_allowlist=[USDC],
        per_tx_cap=500_000_000,
        daily_cap=5_000_000_000,
        expires_at=_future(),
    )
    key = d.grant("agent-001", policy)
    assert isinstance(key, SessionKey)


def test_session_key_is_unique():
    d = Delegation()
    policy = SpendPolicy(token_allowlist=[USDC], expires_at=_future())
    k1 = d.grant("agent-001", policy)
    k2 = d.grant("agent-001", policy)
    assert k1.key_id != k2.key_id


def test_session_key_carries_policy():
    d = Delegation()
    policy = SpendPolicy(token_allowlist=[USDC], per_tx_cap=999, expires_at=_future())
    key = d.grant("agent-x", policy)
    assert key.policy is policy


# ---------------------------------------------------------------------------
# revoke() blocks further signing
# ---------------------------------------------------------------------------


def test_revoked_key_raises_on_pay():
    wallet = _funded_wallet()
    d = Delegation(wallet=wallet)
    policy = SpendPolicy(token_allowlist=[USDC], per_tx_cap=1_000_000_000, expires_at=_future())
    key = d.grant("agent-002", policy)

    d.revoke(key)

    req = _req(token=USDC, amount=100_000_000)
    with pytest.raises(PolicyViolation, match="revoked"):
        d.pay_with_key(key, req)


def test_revoked_key_is_no_longer_active():
    d = Delegation()
    policy = SpendPolicy(token_allowlist=[USDC], expires_at=_future())
    key = d.grant("agent-003", policy)
    assert d.is_active(key)
    d.revoke(key)
    assert not d.is_active(key)


def test_unknown_key_revoke_raises():
    d = Delegation()
    fake_key = SessionKey(key_id="nonexistent", agent_id="x", policy=SpendPolicy(expires_at=_future()))
    with pytest.raises((KeyError, PolicyViolation)):
        d.revoke(fake_key)


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def test_expired_key_raises_on_pay():
    wallet = _funded_wallet()
    d = Delegation(wallet=wallet)
    expired_policy = SpendPolicy(
        token_allowlist=[USDC],
        per_tx_cap=1_000_000_000,
        expires_at=_past(),    # already expired
    )
    key = d.grant("agent-004", expired_policy)

    req = _req(token=USDC, amount=100_000_000)
    with pytest.raises(PolicyViolation, match="expired"):
        d.pay_with_key(key, req)


def test_unexpired_key_succeeds():
    wallet = _funded_wallet()
    d = Delegation(wallet=wallet)
    policy = SpendPolicy(
        token_allowlist=[USDC],
        per_tx_cap=1_000_000_000,
        expires_at=_future(3600),
    )
    key = d.grant("agent-005", policy)
    req = _req(token=USDC, amount=100_000_000)
    receipt = d.pay_with_key(key, req)
    assert receipt is not None


# ---------------------------------------------------------------------------
# Token allowlist
# ---------------------------------------------------------------------------


def test_disallowed_token_raises():
    wallet = _funded_wallet()
    d = Delegation(wallet=wallet)
    policy = SpendPolicy(
        token_allowlist=[USDC],   # DAI not allowed
        per_tx_cap=1_000_000_000,
        expires_at=_future(),
    )
    key = d.grant("agent-006", policy)
    req = _req(token=DAI, amount=100_000_000, payee=PAYEE_A)
    with pytest.raises(PolicyViolation, match="token"):
        d.pay_with_key(key, req)


def test_allowed_token_succeeds():
    wallet = _funded_wallet()
    d = Delegation(wallet=wallet)
    policy = SpendPolicy(
        token_allowlist=[USDC, LUX],
        per_tx_cap=1_000_000_000,
        expires_at=_future(),
    )
    key = d.grant("agent-007", policy)
    req = _req(token=LUX, amount=1_000, payee=PAYEE_A)
    receipt = d.pay_with_key(key, req)
    assert receipt is not None


def test_empty_token_allowlist_blocks_all():
    wallet = _funded_wallet()
    d = Delegation(wallet=wallet)
    policy = SpendPolicy(token_allowlist=[], expires_at=_future())
    key = d.grant("agent-008", policy)
    req = _req(token=USDC)
    with pytest.raises(PolicyViolation, match="token"):
        d.pay_with_key(key, req)


def test_none_token_allowlist_allows_all():
    """A None allowlist means no restriction on token."""
    wallet = _funded_wallet()
    d = Delegation(wallet=wallet)
    policy = SpendPolicy(token_allowlist=None, per_tx_cap=1_000_000_000, expires_at=_future())
    key = d.grant("agent-009", policy)
    req = _req(token=DAI, amount=100_000_000)
    # Treasury doesn't have DAI, so we expect InsufficientBalance, NOT PolicyViolation
    from switchboard.treasury import InsufficientBalance
    with pytest.raises((InsufficientBalance, Exception)) as exc_info:
        d.pay_with_key(key, req)
    # Must NOT be a PolicyViolation for the token
    if isinstance(exc_info.value, PolicyViolation):
        assert "token" not in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# per_tx_cap enforcement (reuses gas_budget / gas_manager)
# ---------------------------------------------------------------------------


def test_per_tx_cap_exceeded_raises():
    wallet = _funded_wallet()
    d = Delegation(wallet=wallet)
    policy = SpendPolicy(
        token_allowlist=[USDC],
        per_tx_cap=50_000_000,     # 50 USDC cap per tx
        expires_at=_future(),
    )
    key = d.grant("agent-010", policy)
    req = _req(token=USDC, amount=100_000_000)   # 100 USDC > 50 USDC cap
    with pytest.raises(PolicyViolation, match="per_tx_cap"):
        d.pay_with_key(key, req)


def test_per_tx_cap_at_limit_succeeds():
    wallet = _funded_wallet()
    d = Delegation(wallet=wallet)
    policy = SpendPolicy(
        token_allowlist=[USDC],
        per_tx_cap=100_000_000,    # exactly 100 USDC
        expires_at=_future(),
    )
    key = d.grant("agent-011", policy)
    req = _req(token=USDC, amount=100_000_000)
    receipt = d.pay_with_key(key, req)
    assert receipt is not None


def test_per_tx_cap_none_means_unlimited():
    wallet = _funded_wallet()
    d = Delegation(wallet=wallet)
    policy = SpendPolicy(
        token_allowlist=[USDC],
        per_tx_cap=None,
        expires_at=_future(),
    )
    key = d.grant("agent-012", policy)
    req = _req(token=USDC, amount=9_000_000_000)   # very large amount (within treasury)
    receipt = d.pay_with_key(key, req)
    assert receipt is not None


# ---------------------------------------------------------------------------
# daily_cap enforcement
# ---------------------------------------------------------------------------


def test_daily_cap_exceeded_after_multiple_payments():
    wallet = _funded_wallet()
    d = Delegation(wallet=wallet)
    policy = SpendPolicy(
        token_allowlist=[USDC],
        per_tx_cap=200_000_000,    # 200 USDC per tx
        daily_cap=250_000_000,     # 250 USDC daily cap
        expires_at=_future(),
    )
    key = d.grant("agent-013", policy)

    # First payment: 200 USDC — OK
    req1 = _req(token=USDC, amount=200_000_000)
    d.pay_with_key(key, req1)

    # Second payment: 200 USDC — should exceed daily cap (200+200 > 250)
    req2 = _req(token=USDC, amount=200_000_000)
    with pytest.raises(PolicyViolation, match="daily_cap"):
        d.pay_with_key(key, req2)


def test_daily_cap_none_means_unlimited():
    wallet = _funded_wallet()
    d = Delegation(wallet=wallet)
    policy = SpendPolicy(
        token_allowlist=[USDC],
        per_tx_cap=1_000_000_000,
        daily_cap=None,
        expires_at=_future(),
    )
    key = d.grant("agent-014", policy)
    for _ in range(5):
        req = _req(token=USDC, amount=500_000_000)
        receipt = d.pay_with_key(key, req)
        assert receipt is not None


# ---------------------------------------------------------------------------
# allowed_counterparties enforcement
# ---------------------------------------------------------------------------


def test_disallowed_counterparty_raises():
    wallet = _funded_wallet()
    d = Delegation(wallet=wallet)
    policy = SpendPolicy(
        token_allowlist=[USDC],
        per_tx_cap=1_000_000_000,
        expires_at=_future(),
        allowed_counterparties=[PAYEE_A],   # PAYEE_B not allowed
    )
    key = d.grant("agent-015", policy)
    req = _req(payee=PAYEE_B)
    with pytest.raises(PolicyViolation, match="counterpart"):
        d.pay_with_key(key, req)


def test_allowed_counterparty_succeeds():
    wallet = _funded_wallet()
    d = Delegation(wallet=wallet)
    policy = SpendPolicy(
        token_allowlist=[USDC],
        per_tx_cap=1_000_000_000,
        expires_at=_future(),
        allowed_counterparties=[PAYEE_A, PAYEE_B],
    )
    key = d.grant("agent-016", policy)
    req = _req(payee=PAYEE_B)
    receipt = d.pay_with_key(key, req)
    assert receipt is not None


def test_none_allowed_counterparties_permits_any():
    """None = no counterparty restriction."""
    wallet = _funded_wallet()
    d = Delegation(wallet=wallet)
    policy = SpendPolicy(
        token_allowlist=[USDC],
        per_tx_cap=1_000_000_000,
        expires_at=_future(),
        allowed_counterparties=None,
    )
    key = d.grant("agent-017", policy)
    req = _req(payee=PAYEE_B)
    receipt = d.pay_with_key(key, req)
    assert receipt is not None


# ---------------------------------------------------------------------------
# module-level convenience helpers
# ---------------------------------------------------------------------------


def test_module_level_grant_revoke():
    """grant() / revoke() module-level functions create a default Delegation."""
    policy = SpendPolicy(token_allowlist=[USDC], expires_at=_future())
    key = grant("agent-018", policy)
    assert isinstance(key, SessionKey)
    revoke(key)
    assert not key.is_active()
