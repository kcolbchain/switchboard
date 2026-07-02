"""Session-key delegation + SpendPolicy enforcement (Unit ⑨).

``grant(agent_id, policy) -> SessionKey`` issues a scoped, revocable,
time-boxed session key.  ``Delegation.pay_with_key(key, request)`` enforces
every rule in the ``SpendPolicy`` *before* asking the ``AgentWallet`` to
co-sign, so a compromised agent is always bounded by its policy.

Policy enforcement order (fail-fast)
-------------------------------------
1. Revoked?        → ``PolicyViolation("revoked")``
2. Expired?        → ``PolicyViolation("expired")``
3. Token allowed?  → ``PolicyViolation("token not in allowlist")``
4. Counterparty?   → ``PolicyViolation("counterparty not allowed")``
5. per_tx_cap?     → ``PolicyViolation("per_tx_cap exceeded")``
6. daily_cap?      → ``PolicyViolation("daily_cap exceeded")``
7. Delegate to ``AgentWallet.pay()`` (may raise ``InsufficientBalance``).

Gas / spend-cap accounting
---------------------------
Per-tx cap and daily cap are tracked **in token units** (not gas units) using
``GasManager`` from ``switchboard.gas_manager`` — the rolling-window semantics
are identical; we simply repurpose the per-hour window as the per-tx gate
(a trivial check) and the per-day window as the 24-hour spend cap.

``per_tx_cap`` is enforced as a simple comparison before the GasManager call
so the error message can be specific.  The GasManager then tracks the daily
rolling total.

Module-level helpers
---------------------
``grant(agent_id, policy)`` and ``revoke(key)`` use a **process-wide default
``Delegation`` instance**.  This is a convenience; production code should
instantiate ``Delegation(wallet=...)`` explicitly.

Seam note
---------
``Delegation`` receives an ``AgentWallet`` at construction; the wallet holds
the ``EscrowClient`` seam.  The real escrow client wires in via
``AgentWallet(escrow=real_client)``.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from switchboard.gas_manager import GasManager, GasLimits, BudgetExhausted
from switchboard.agent_wallet import AgentWallet, PaymentRequest, PaymentReceipt


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PolicyViolation(RuntimeError):
    """Raised when a payment would violate the session key's SpendPolicy."""


# ---------------------------------------------------------------------------
# SpendPolicy
# ---------------------------------------------------------------------------


@dataclass
class SpendPolicy:
    """Rules constraining what a delegated agent may do.

    Parameters
    ----------
    token_allowlist:
        Tokens (EVM address strings) the agent may spend.
        ``None`` means no restriction (any token).
        ``[]`` (empty list) blocks all tokens.
    per_tx_cap:
        Maximum amount (in the token's base units) per single transaction.
        ``None`` means unlimited.
    daily_cap:
        Maximum cumulative spend (in the token's base units) in any rolling
        24-hour window, enforced via ``GasManager``.
        ``None`` means unlimited.
    expires_at:
        UTC datetime after which the session key is invalid.
    allowed_counterparties:
        EVM address strings of payees the agent may pay.
        ``None`` means no restriction (any payee).
        ``[]`` (empty list) blocks all payees.
    """

    expires_at: datetime
    token_allowlist: Optional[List[str]] = None
    per_tx_cap: Optional[int] = None
    daily_cap: Optional[int] = None
    allowed_counterparties: Optional[List[str]] = None


# ---------------------------------------------------------------------------
# SessionKey
# ---------------------------------------------------------------------------


@dataclass
class SessionKey:
    """An issued, revocable delegation credential.

    ``key_id`` is a random 32-hex-char string; treat it as opaque.
    ``is_active()`` returns False if the key has been explicitly revoked.
    Time-based expiry is checked by ``Delegation.pay_with_key()``, not here,
    so that revocation and expiry produce distinct error messages.
    """

    key_id: str
    agent_id: str
    policy: SpendPolicy
    _revoked: bool = field(default=False, init=False, repr=False, compare=False)

    def is_active(self) -> bool:
        """Return True unless explicitly revoked."""
        return not self._revoked

    def _mark_revoked(self) -> None:
        self._revoked = True


# ---------------------------------------------------------------------------
# Delegation — the enforcement layer
# ---------------------------------------------------------------------------


class Delegation:
    """Issues session keys and enforces SpendPolicy on every payment.

    Parameters
    ----------
    wallet:
        The ``AgentWallet`` this delegation layer wraps.  If None, a fresh
        wallet with no pre-funded treasury is used (useful in tests that
        only check policy enforcement, not actual payment execution).
    """

    def __init__(self, wallet: Optional[AgentWallet] = None) -> None:
        self._wallet: AgentWallet = wallet if wallet is not None else AgentWallet()
        self._lock = threading.Lock()
        # key_id -> SessionKey
        self._keys: Dict[str, SessionKey] = {}
        # key_id -> GasManager (one per session; tracks daily spend)
        self._gas_managers: Dict[str, GasManager] = {}

    # ------------------------------------------------------------------
    # grant / revoke
    # ------------------------------------------------------------------

    def grant(self, agent_id: str, policy: SpendPolicy) -> SessionKey:
        """Issue a new session key for ``agent_id`` bound to ``policy``."""
        key_id = secrets.token_hex(16)
        key = SessionKey(key_id=key_id, agent_id=agent_id, policy=policy)

        # Build a GasManager with the daily_cap as the rolling-day limit.
        # per_tx_cap is enforced as a direct comparison; only daily_cap feeds
        # the GasManager so we get accurate rolling-window semantics.
        limits = GasLimits(
            per_hour=None,   # not used at session-key level
            per_day=policy.daily_cap,
        )
        manager = GasManager(default_limits=limits)

        with self._lock:
            self._keys[key_id] = key
            self._gas_managers[key_id] = manager

        return key

    def revoke(self, key: SessionKey) -> None:
        """Revoke ``key`` so it can no longer authorize payments."""
        with self._lock:
            if key.key_id not in self._keys:
                raise KeyError(f"SessionKey {key.key_id!r} is not registered with this Delegation")
            key._mark_revoked()
            del self._keys[key.key_id]
            del self._gas_managers[key.key_id]

    def is_active(self, key: SessionKey) -> bool:
        """Return True if ``key`` is currently active (not revoked)."""
        with self._lock:
            return key.key_id in self._keys and key.is_active()

    # ------------------------------------------------------------------
    # pay_with_key — enforcement + delegation to wallet
    # ------------------------------------------------------------------

    def pay_with_key(self, key: SessionKey, request: PaymentRequest) -> PaymentReceipt:
        """Enforce SpendPolicy then delegate to the AgentWallet.

        Raises
        ------
        PolicyViolation
            If any policy rule is violated.
        InsufficientBalance
            If the treasury cannot cover the request (from AgentWallet).
        """
        policy = key.policy

        # 1. Revocation check
        with self._lock:
            if not key.is_active() or key.key_id not in self._keys:
                raise PolicyViolation(f"Session key {key.key_id!r} has been revoked")
            gas_manager = self._gas_managers[key.key_id]

        # 2. Expiry check
        now_utc = datetime.now(timezone.utc)
        expires = policy.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if now_utc >= expires:
            raise PolicyViolation(
                f"Session key {key.key_id!r} has expired (expired at {policy.expires_at})"
            )

        # 3. Token allowlist
        if policy.token_allowlist is not None:
            if request.token not in policy.token_allowlist:
                raise PolicyViolation(
                    f"token {request.token!r} is not in the session key's token allowlist"
                )

        # 4. Counterparty allowlist
        if policy.allowed_counterparties is not None:
            if request.payee not in policy.allowed_counterparties:
                raise PolicyViolation(
                    f"counterparty {request.payee!r} is not in the session key's "
                    "allowed_counterparties"
                )

        # 5. per_tx_cap
        if policy.per_tx_cap is not None and request.amount > policy.per_tx_cap:
            raise PolicyViolation(
                f"per_tx_cap exceeded: amount {request.amount} > cap {policy.per_tx_cap}"
            )

        # 6. daily_cap (via GasManager rolling window)
        if policy.daily_cap is not None:
            if not gas_manager.can_spend("session", request.amount):
                raise PolicyViolation(
                    f"daily_cap exceeded: adding {request.amount} would exceed "
                    f"daily cap {policy.daily_cap}"
                )

        # All checks passed — delegate to AgentWallet.
        receipt = self._wallet.pay(request)

        # Record the spend in the GasManager *after* a successful payment.
        if policy.daily_cap is not None:
            gas_manager.record("session", request.amount)

        return receipt


# ---------------------------------------------------------------------------
# Module-level process-wide default Delegation (convenience helpers)
# ---------------------------------------------------------------------------

_default_delegation: Optional[Delegation] = None
_default_lock = threading.Lock()


def _get_default() -> Delegation:
    global _default_delegation
    with _default_lock:
        if _default_delegation is None:
            _default_delegation = Delegation()
        return _default_delegation


def grant(agent_id: str, policy: SpendPolicy) -> SessionKey:
    """Issue a session key using the process-wide default ``Delegation``."""
    return _get_default().grant(agent_id, policy)


def revoke(key: SessionKey) -> None:
    """Revoke a session key previously issued by the process-wide default ``Delegation``."""
    _get_default().revoke(key)
