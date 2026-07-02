"""Tests for Unit ⑫ — FleetBalancer.

Strategy: spread spend/nonce across N wallets to avoid:
  - nonce contention (two concurrent txs from the same key),
  - single-key blast radius.

Uses NonceManager to track pending nonces per wallet address.

All tests written BEFORE the implementation (TDD — RED first).
"""

import pytest
import threading
from unittest.mock import MagicMock

from switchboard.nonce_manager import NonceManager, SIGNATURE_ALG_ECDSA
from switchboard.router.fleet_balancer import FleetBalancer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WALLETS = [
    "0x1111111111111111111111111111111111111111",
    "0x2222222222222222222222222222222222222222",
    "0x3333333333333333333333333333333333333333",
]


def make_nonce_manager(start_nonce: int = 0) -> NonceManager:
    """Return a NonceManager backed by a mock chain client."""
    chain_client = MagicMock()
    chain_client.get_current_onchain_nonce.return_value = start_nonce
    return NonceManager(chain_client=chain_client)


def make_balancer(wallets=WALLETS, **kwargs):
    nm = make_nonce_manager()
    return FleetBalancer(wallets=wallets, nonce_manager=nm, chain_id=1, **kwargs)


# ---------------------------------------------------------------------------
# Unit ⑫ Tests
# ---------------------------------------------------------------------------

class TestFleetBalancerPicksLeastBusyWallet:
    """Wallet with fewest pending nonces is preferred."""

    def test_single_wallet_always_returned(self):
        balancer = make_balancer(wallets=[WALLETS[0]])
        chosen = balancer.pick(chain_id=1)
        assert chosen == WALLETS[0]

    def test_first_pick_can_be_any_wallet(self):
        balancer = make_balancer()
        chosen = balancer.pick(chain_id=1)
        assert chosen in WALLETS

    def test_after_one_pick_second_pick_is_different(self):
        """After acquiring a nonce for wallet[0], the next pick avoids it."""
        nm = make_nonce_manager()
        balancer = FleetBalancer(wallets=WALLETS[:2], nonce_manager=nm, chain_id=1)

        first = balancer.pick(chain_id=1)
        # Simulate nonce acquired for `first` — the balancer tracks this via nonce_manager
        nm.acquire_nonce(first, chain_id=1)
        second = balancer.pick(chain_id=1)

        # second wallet should have 0 pending nonces and therefore be preferred
        other = [w for w in WALLETS[:2] if w != first][0]
        assert second == other


class TestFleetBalancerNonceDistribution:
    """After many picks (with nonce acquisition), load is spread."""

    def test_picks_distributed_across_all_wallets(self):
        nm = make_nonce_manager()
        balancer = FleetBalancer(wallets=WALLETS, nonce_manager=nm, chain_id=1)

        chosen_counts = {w: 0 for w in WALLETS}
        for _ in range(9):  # 3 rounds × 3 wallets
            w = balancer.pick(chain_id=1)
            nm.acquire_nonce(w, chain_id=1)
            chosen_counts[w] += 1

        # Every wallet should have been chosen at least twice in 9 rounds
        for w, count in chosen_counts.items():
            assert count >= 2, f"Wallet {w} only picked {count} times"


class TestFleetBalancerConcurrency:
    """Concurrent picks must not hand the same wallet to two threads simultaneously
    when all wallets are equally loaded (the balancer should rotate)."""

    def test_concurrent_picks_use_different_wallets(self):
        nm = make_nonce_manager()
        two_wallets = WALLETS[:2]
        balancer = FleetBalancer(wallets=two_wallets, nonce_manager=nm, chain_id=1)

        results = []
        lock = threading.Lock()

        def do_pick():
            w = balancer.pick(chain_id=1)
            nm.acquire_nonce(w, chain_id=1)
            with lock:
                results.append(w)

        threads = [threading.Thread(target=do_pick) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 2
        # Both wallets should be used (round-robin / least-pending)
        assert set(results) == set(two_wallets)


class TestFleetBalancerEmptyFleet:
    def test_empty_fleet_raises(self):
        nm = make_nonce_manager()
        with pytest.raises(ValueError, match="at least one wallet"):
            FleetBalancer(wallets=[], nonce_manager=nm, chain_id=1)
