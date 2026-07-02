"""Unit ⑫ — FleetBalancer.

Spreads signing work across N wallet addresses to avoid:
  - nonce contention (two concurrent txs from the same key),
  - single-key blast radius / rate limits.

Uses ``NonceManager`` to read pending-nonce counts per wallet.  The wallet
with the fewest pending nonces is chosen; ties broken by wallet list order.

Thread-safe: an internal lock prevents two concurrent callers from selecting
the same wallet when they start with equal nonce counts.
"""

from __future__ import annotations

import threading
from typing import List, Optional

from switchboard.nonce_manager import NonceManager


class FleetBalancer:
    """Selects the least-busy wallet from a fleet.

    Parameters
    ----------
    wallets:
        Ordered list of EVM wallet addresses in the fleet.  Must be non-empty.
    nonce_manager:
        Shared ``NonceManager`` instance tracking pending nonces per wallet.
    chain_id:
        EVM chain the fleet operates on.
    """

    def __init__(
        self,
        wallets: List[str],
        nonce_manager: NonceManager,
        chain_id: int,
    ) -> None:
        if not wallets:
            raise ValueError("FleetBalancer requires at least one wallet")
        self._wallets = list(wallets)
        self._nm = nonce_manager
        self._chain_id = chain_id
        self._lock = threading.Lock()

    def pick(self, chain_id: Optional[int] = None) -> str:
        """Return the wallet address with the fewest pending nonces.

        Tie-breaks by position in the wallet list (earlier = preferred).

        Parameters
        ----------
        chain_id:
            Override the chain_id used for nonce lookup.  Defaults to the
            chain_id supplied at construction.
        """
        cid = chain_id if chain_id is not None else self._chain_id

        with self._lock:
            # Compute pending nonce count for each wallet and pick the min.
            best: Optional[str] = None
            best_count: int = -1

            for wallet in self._wallets:
                pending = len(self._nm.get_pending_nonces(wallet, chain_id=cid))
                if best is None or pending < best_count:
                    best = wallet
                    best_count = pending

            # best is guaranteed non-None because wallets is non-empty.
            return best  # type: ignore[return-value]
