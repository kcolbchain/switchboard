"""Treasury — balance tracking per (chain_id, token) for the Agent Wallet.

Unit ⑧ of the agent-wallet-multitoken-settlement spec.

Tracks how much of each token the wallet holds on each chain, and distinguishes
the *spendable* portion (total balance minus a configurable reserve).  The Router
queries this module before every payment; credits and debits happen atomically
under a lock.

The ``token`` parameter is always a checksummed EVM address string.
``address(0)`` (``0x0000...0000``) is the canonical sentinel for native ETH.

Featured partner tokens — LUX, ZOO, and other kcolbchain partners — are first-
class entries in the balance map; no special-casing is needed.

Usage::

    from switchboard.treasury import Treasury, InsufficientBalance

    t = Treasury()
    t.credit(chain_id=1, token=USDC, amount=500_000_000)
    t.debit(chain_id=1, token=USDC, amount=100_000_000)
    spendable = t.spendable(1, USDC)
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Dict, Tuple


class InsufficientBalance(RuntimeError):
    """Raised when a debit would take the balance below zero."""


class Treasury:
    """Per-(chain_id, token) balance store with reserve support.

    Thread-safe: all mutations and reads are protected by a single lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (chain_id, token) -> balance
        self._balances: Dict[Tuple[int, str], int] = defaultdict(int)
        # (chain_id, token) -> reserve (minimum held back from spendable)
        self._reserves: Dict[Tuple[int, str], int] = defaultdict(int)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def balance(self, chain_id: int, token: str) -> int:
        """Return the total (gross) balance for ``(chain_id, token)``."""
        with self._lock:
            return self._balances[(chain_id, token)]

    def spendable(self, chain_id: int, token: str) -> int:
        """Return the spendable balance: ``balance - reserve``, clamped to 0."""
        with self._lock:
            bal = self._balances[(chain_id, token)]
            res = self._reserves[(chain_id, token)]
            return max(0, bal - res)

    def balances(self, chain_id: int) -> Dict[str, int]:
        """Return a snapshot ``{token: balance}`` for every token on ``chain_id``."""
        with self._lock:
            return {
                token: amt
                for (cid, token), amt in self._balances.items()
                if cid == chain_id and amt > 0
            }

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def credit(self, chain_id: int, token: str, amount: int) -> None:
        """Increase the balance for ``(chain_id, token)`` by ``amount``."""
        if amount < 0:
            raise ValueError(f"credit amount must be non-negative, got {amount}")
        with self._lock:
            self._balances[(chain_id, token)] += amount

    def debit(self, chain_id: int, token: str, amount: int) -> None:
        """Decrease the balance for ``(chain_id, token)`` by ``amount``.

        Raises :class:`InsufficientBalance` if the result would be negative.
        """
        if amount < 0:
            raise ValueError(f"debit amount must be non-negative, got {amount}")
        with self._lock:
            current = self._balances[(chain_id, token)]
            if current < amount:
                raise InsufficientBalance(
                    f"Insufficient balance on chain {chain_id} token {token}: "
                    f"have {current}, need {amount}"
                )
            self._balances[(chain_id, token)] = current - amount

    def set_reserve(self, chain_id: int, token: str, reserve: int) -> None:
        """Set the minimum reserve for ``(chain_id, token)``.

        The reserve is not withdrawable via :meth:`debit`; it is only a floor
        used by :meth:`spendable`.  The operator is responsible for ensuring
        the reserve makes sense relative to the current balance.
        """
        if reserve < 0:
            raise ValueError(f"reserve must be non-negative, got {reserve}")
        with self._lock:
            self._reserves[(chain_id, token)] = reserve
