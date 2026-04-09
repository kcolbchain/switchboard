import unittest
import threading
from typing import Dict, Any, List, Callable
from sortedcontainers import SortedSet

# Assuming nonce_manager.py is correctly importable from 'switchboard' package
from switchboard.nonce_manager import NonceManager, ChainClient

# --- Mock ChainClient for testing ---
class MockChainClientImpl:
    """
    A mock implementation of the ChainClient protocol for testing purposes.
    Allows simulating on-chain nonce changes.
    """
    def __init__(self, initial_onchain_nonces: Dict[str, int]):
        self._onchain_nonces = initial_onchain_nonces
        self._lock = threading.Lock()

    def get_current_onchain_nonce(self, address: str) -> int:
        """
        Returns the simulated current transaction count (nonce) for an address.
        """
        with self._lock:
            return self._onchain_nonces.get(address, 0)

    def set_onchain_nonce(self, address: str, nonce: int):
        """
        Simulates an external confirmation or a direct change in the blockchain's
        reported nonce for an address.
        """
        with self._lock:
            self._onchain_nonces[address] = nonce

class MockTransaction:
    """
    A simple mock transaction object to be associated with nonces and re-queued.
    """
    def __init__(self, nonce: int, content: str):
        self.nonce = nonce
        self.content = content
        self.re_queued_count = 0 # To track how many times it was re-queued

    def __repr__(self):
        return f"MockTransaction(nonce={self.nonce}, content='{self.content}', re_queued_count={self.re_queued_count})"
    
    def __eq__(self, other):
        if not isinstance(other, MockTransaction):
            return NotImplemented
        return self.nonce == other.nonce and self.content == other.content


# --- Unit Tests for NonceManager ---
class TestNonceManager(unittest.TestCase):
    def setUp(self):
        """
        Set up shared resources for each test case.
        Initializes the mock chain client and NonceManager.
        """
        self.wallet_address_1 = "0xAgentWallet1"
        self.wallet_address_2 = "0xAgentWallet2"
        self.initial_nonces = {
            self.wallet_address_1: 0,
            self.wallet_address_2: 5, # Simulate an agent that already has some txns confirmed
        }
        self.mock_chain_client = MockChainClientImpl(self.initial_nonces.copy())
        
        # List to capture transactions passed to the re_queue_callback
        self.re_queued_txns: List[MockTransaction] = []

        # Define the re-queue callback function
        def re_queue_callback(tx: MockTransaction):
            tx.re_queued_count += 1
            self.re_queued_txns.append(tx)

        self.nonce_manager = NonceManager(self.mock_chain_client, re_queue_callback)

    def test_initial_state_and_acquire_nonce(self):
        """
        Tests the initial state of wallets and basic nonce acquisition.
        """
        # Wallet 1: Starts with 0 on-chain nonce
        self.assertEqual(self.nonce_manager.get_confirmed_nonce(self.wallet_address_1), 0)
        self.assertEqual(self.nonce_manager.get_pending_nonces(self.wallet_address_1), SortedSet())

        # Acquire first nonce for Wallet 1 (should be 0)
        tx1_0 = MockTransaction(0, "tx_0_w1")
        nonce0 = self.nonce_manager.acquire_nonce(self.wallet_address_1, tx1_0)
        self.assertEqual(nonce0, 0)
        self.assertEqual(self.nonce_manager.get_confirmed_nonce(self.wallet_address_1), 0)
        self.assertEqual(self.nonce_manager.get_pending_nonces(self.wallet_address_1), SortedSet([0]))
        self.assertEqual(self.nonce_manager.get_total_pending_transactions(self.wallet_address_1), 1)

        # Acquire second nonce for Wallet 1 (should be 1)
        tx1_1 = MockTransaction(1, "tx_1_w1")
        nonce1 = self.nonce_manager.acquire_nonce(self.wallet_address_1, tx1_1)
        self.assertEqual(nonce1, 1)
        self.assertEqual(self.nonce_manager.get_confirmed_nonce(self.wallet_address_1), 0)
        self.assertEqual(self.nonce_manager.get_pending_nonces(self.wallet_address_1), SortedSet([0, 1]))
        self.assertEqual(self.nonce_manager.get_total_pending_transactions(self.wallet_address_1), 2)

        # Wallet 2: Starts with 5 on-chain nonce
        self.assertEqual(self.nonce_manager.get_confirmed_nonce(self.wallet_address_2), 5)
        self.assertEqual(self.nonce_manager.get_pending_nonces(self.wallet_address_2), SortedSet())

        # Acquire first nonce for Wallet 2 (should be 5)
        tx2_5 = MockTransaction(5, "tx_5_w2")
        nonce5 = self.nonce_manager.acquire_nonce(self.wallet_address_2, tx2_5)
        self.assertEqual(nonce5, 5)
        self.assertEqual(self.nonce_manager.get_confirmed_nonce(self.wallet_address_2), 5)
        self.assertEqual(self.nonce_manager.get_pending_nonces(self.wallet_address_2), SortedSet([5]))
        self.assertEqual(self.nonce_manager.get_total_pending_transactions(self.wallet_address_2), 1)

    def test_confirm_nonce_sequential(self):
        """
        Tests nonce confirmation when transactions are mined in sequential order.
        """
        # Acquire some nonces for Wallet 1
        tx_w1_0 = MockTransaction(0, "w1_0")
        tx_w1_1 = MockTransaction(1, "w1_1")
        tx_w1_2 = MockTransaction(2, "w1_2")
        self.nonce_manager.acquire_nonce(self.wallet_address_1, tx_w1_0) # nonce 0
        self.nonce_manager.acquire_nonce(self.wallet_address_1, tx_w1_1) # nonce 1
        self.nonce_manager.acquire_nonce(self.wallet_address_1, tx_w1_2) # nonce 2

        self.assertEqual(self.nonce_manager.get_confirmed_nonce(self.wallet_address_1), 0)
        self.assertEqual(self.nonce_manager.get_pending_nonces(self.wallet_address_1), SortedSet([0, 1, 2]))

        # Confirm nonce 0
        self.nonce_manager.confirm_nonce(self.wallet_address_1, 0)
        self.assertEqual(self.nonce_manager.get_confirmed_nonce(self.wallet_address_1), 1)
        self.assertEqual(self.nonce_manager.get_pending_nonces(self.wallet_address_1), SortedSet([1, 2]))

        # Confirm nonce 1
        self.nonce_manager.confirm_nonce(self.wallet_address_1, 1)
        self.assertEqual(self.nonce_manager.get_confirmed_nonce(self.wallet_address_1), 2)
        self.assertEqual(self.nonce_manager.get_pending_nonces(self.wallet_address_1), SortedSet([2]))

        # Confirm nonce 2
        self.nonce_manager.confirm_nonce(self.wallet_address_1, 2)
        self.assertEqual(self.nonce_manager.get_confirmed_nonce(self.wallet_address_1), 3)
        self.assertEqual(self.nonce_manager.get_pending_nonces(self.wallet_address_1), SortedSet())
        self.assertEqual(self.nonce_manager.get_total_pending_transactions(self.wallet_address_1), 0)

    def test_confirm_nonce_out_of_order_or_gap(self):
        """
        Tests nonce confirmation when transactions are mined out of sequential order.
        """
        # Acquire nonces 0, 1, 2
        tx_w1_0 = MockTransaction(0, "w1_0")
        tx_w1_1 = MockTransaction(1, "w1_1")
        tx_w1_2 = MockTransaction(2, "w1_2")
        self.nonce_manager.acquire_nonce(self.wallet_address_1, tx_w1_0)
        self.nonce_manager.acquire_nonce(self.wallet_address_1, tx_w1_1)
        self.nonce_manager.acquire_nonce(self.wallet_address_1, tx_w1_2)
        self.assertEqual(self.nonce_manager.get_confirmed_nonce(self.wallet_address_1), 0)
        self.assertEqual(self.nonce_manager.get_pending_nonces(self.wallet_address_1), SortedSet([0, 1, 2]))

        # Confirm nonce 2 directly (out of order). Confirmed_nonce should NOT advance past 0
        # because nonces 0 and 1 are still pending.
        self.nonce_manager.confirm_nonce(self.wallet_address_1, 2)
        self.assertEqual(self.nonce_manager.get_confirmed_nonce(self.wallet_address_1), 0)
        self.assertEqual(self.nonce_manager.get_pending_nonces(self.wallet_address_1), SortedSet([0, 1]))
        self.assertEqual(self.nonce_manager.get_total_pending_transactions(self.wallet_address_1), 2)

        # Confirm nonce 0. This will advance confirmed_nonce to 1.
        self.nonce_manager.confirm_nonce(self.wallet_address_1, 0)
        self.assertEqual(self.nonce_manager.get_confirmed_nonce(self.wallet_address_1), 1)
        self.assertEqual(self.nonce_manager.get_pending_nonces(self.wallet_address_1), SortedSet([1]))
        self.assertEqual(self.nonce_manager.get_total_pending_transactions(self.wallet_address_1), 1)

        # Confirm nonce 1. This will advance confirmed_nonce to 3 (because nonce 2 was already handled).
        self.nonce_manager.confirm_nonce(self.wallet_address_1, 1)
        self.assertEqual(self.nonce_manager.get_confirmed_nonce(self.wallet_address_1), 3)
        self.assertEqual(self.nonce_manager.get_pending_nonces(self.wallet_address_1), SortedSet())
        self.assertEqual(self.nonce_manager.get_total_pending_transactions(self.wallet_address_1), 0)

    def test_release_nonce(self):
        """
        Tests releasing a pending nonce, e.g., if a transaction is dropped.
        """
        tx_w1_0 = MockTransaction(0, "w1_0")
        tx_w1_1 = MockTransaction(1, "w1_1")
        self.nonce_manager.acquire_nonce(self.wallet_address_1, tx_w1_0)
        self.nonce_manager.acquire_nonce(self.wallet_address_1, tx_w1_1)
        self.assertEqual(self.nonce_manager.get_pending_nonces(self.wallet_address_1), SortedSet([0, 1]))
        self.assertEqual(self.nonce_manager.get_total_pending_transactions(self.wallet_address_1), 2)

        # Release nonce 1
        self.nonce_manager.release_nonce(self.wallet_address_1, 1)
        self.assertEqual(self.nonce_manager.get_pending_nonces(self.wallet_address_1), SortedSet([0]))
        self.assertEqual(self.nonce_manager.get_total_pending_transactions(self.wallet_address_1), 1)
        
        # Confirm nonce 0
        self.nonce_manager.confirm_nonce(self.wallet_address_1, 0)
        self.assertEqual(self.nonce_manager.get_confirmed_nonce(self.wallet_address_1), 1)
        self.assertEqual(self.nonce_manager.get_pending_nonces(self.wallet_address_1), SortedSet())

        # Attempt to release a nonce that was never pending or already confirmed, should have no effect
        self.nonce_manager.release_nonce(self.wallet_address_1, 5)
        self.assertEqual(self.nonce_manager.get_pending_nonces(self.wallet_address_1), SortedSet())

    def test_sync_with_onchain_nonce_external_confirmation(self):
        """
        Tests synchronization with the on-chain nonce when external transactions
        or unknown confirmations have advanced the chain state.
        """
        # Initial state: confirmed_nonce = 0, pending = {}
        # Acquire nonces 0, 1, 2 locally
        tx_w1_0 = MockTransaction(0, "w1_0")
        tx_w1_1 = MockTransaction(1, "w1_1")
        tx_w1_2 = MockTransaction(2, "w1_2")
        self.nonce_manager.acquire_nonce(self.wallet_address_1, tx_w1_0)
        self.nonce_manager.acquire_nonce(self.wallet_address_1, tx_w1_1)
        self.nonce_manager.acquire_nonce(self.wallet_address_1, tx_w1_2)
        self.assertEqual(self.nonce_manager.get_confirmed_nonce(self.wallet_address_1), 0)
        self.assertEqual(self.nonce_manager.get_pending_nonces(self.wallet_address_1), SortedSet([0, 1, 2]))
        self.assertEqual(self.nonce_manager.get_total_pending_transactions(self.wallet_address_1), 3)

        # Simulate external confirmation of nonces 0 and 1, so the chain's next nonce is 2.
        self.mock_chain_client.set_onchain_nonce(self.wallet_address_1, 2) 
        
        # When `acquire_nonce` is called again, it will trigger `_sync_with_onchain_nonce`.
        tx_w1_new = MockTransaction(2, "w1_new") # New transaction trying to acquire a nonce
        acquired_nonce = self.nonce_manager.acquire_nonce(self.wallet_address_1, tx_w1_new) 
        
        # Expect the manager to have synced: confirmed_nonce moves to 2.
        # Pending nonces < 2 (i.e., 0 and 1) are removed.
        # The new transaction acquires nonce 2.
        self.assertEqual(acquired_nonce, 2)
        self.assertEqual(self.nonce_manager.get_confirmed_nonce(self.wallet_address_1), 2)
        self.assertEqual(self.nonce_manager.get_pending_nonces(self.wallet_address_1), SortedSet([2]))
        self.assertEqual(self.nonce_manager.get_total_pending_transactions(self.wallet_address_1), 1)

        # Verify that the new transaction has overwritten the old one for nonce 2 in pending_transactions
        state = self.nonce_manager._get_wallet_state(self.wallet_address_1)
        self.assertIn(2, state.pending_transactions)
        self.assertEqual(state.pending_transactions[2].content, "w1_new")

    def test_on_reorg(self):
        """
        Tests the `on_reorg` mechanism, ensuring nonces are reverted and transactions re-queued.
        """
        # Acquire nonces 0, 1, 2, 3
        txs = {}
        for i in range(4):
            tx = MockTransaction(i, f"w1_{i}")
            self.nonce_manager.acquire_nonce(self.wallet_address_1, tx)
            txs[i] = tx
        
        self.assertEqual(self.nonce_manager.get_confirmed_nonce(self.wallet_address_1), 0)
        self.assertEqual(self.nonce_manager.get_pending_nonces(self.wallet_address_1), SortedSet([0, 1, 2, 3]))
        self.assertEqual(self.nonce_manager.get_total_pending_transactions(self.wallet_address_1), 4)

        # Confirm nonce 0 and 1
        self.nonce_manager.confirm_nonce(self.wallet_address_1, 0)
        self.nonce_manager.confirm_nonce(self.wallet_address_1, 1)
        self.assertEqual(self.nonce_manager.get_confirmed_nonce(self.wallet_address_1), 2)
        self.assertEqual(self.nonce_manager.get_pending_nonces(self.wallet_address_1), SortedSet([2, 3]))
        self.assertEqual(self.nonce_manager.get_total_pending_transactions(self.wallet_address_1), 2)
        self.assertEqual(len(self.re_queued_txns), 0)

        # Simulate a reorg where the chain reverts back to nonce 1 as the common ancestor's nonce.
        # This means transactions with nonce 1 and higher are potentially invalid.
        # Our local `confirmed_nonce` is 2, so it will be reverted to 1.
        self.nonce_manager.on_reorg(self.wallet_address_1, 1)

        self.assertEqual(self.nonce_manager.get_confirmed_nonce(self.wallet_address_1), 1)
        self.assertEqual(self.nonce_manager.get_pending_nonces(self.wallet_address_1), SortedSet()) # All pending (2, 3) are now gone
        self.assertEqual(self.nonce_manager.get_total_pending_transactions(self.wallet_address_1), 0)

        # Check that the affected transactions were re-queued
        self.assertEqual(len(self.re_queued_txns), 2)
        re_queued_nonces = {tx.nonce for tx in self.re_queued_txns}
        self.assertIn(2, re_queued_nonces)
        self.assertIn(3, re_queued_nonces)
        self.assertEqual(txs[2].re_queued_count, 1)
        self.assertEqual(txs[3].re_queued_count, 1)

        # Acquire a new nonce after the reorg; it should now correctly pick up from the reverted state.
        new_tx = MockTransaction(1, "w1_new_after_reorg")
        new_nonce = self.nonce_manager.acquire_nonce(self.wallet_address_1, new_tx)
        self.assertEqual(new_nonce, 1) # Should now acquire nonce 1 again
        self.assertEqual(self.nonce_manager.get_pending_nonces(self.wallet_address_1), SortedSet([1]))
        self.assertEqual(self.nonce_manager.get_confirmed_nonce(self.wallet_address_1), 1)

    def test_on_reorg_deeper_than_pending(self):
        """
        Tests reorg handling when the reorg depth affects previously confirmed nonces.
        """
        # Acquire nonces 0, 1, 2
        txs = {}
        for i in range(3):
            tx = MockTransaction(i, f"w1_{i}")
            self.nonce_manager.acquire_nonce(self.wallet_address_1, tx)
            txs[i] = tx
        
        # Confirm nonces 0 and 1
        self.nonce_manager.confirm_nonce(self.wallet_address_1, 0)
        self.nonce_manager.confirm_nonce(self.wallet_address_1, 1)
        # Current state: confirmed_nonce = 2, pending = {2}

        # Simulate a deep reorg to common ancestor nonce 0.
        # This means confirmed_nonce (2) should revert to 0.
        # Pending nonce 2 should also be invalidated.
        self.nonce_manager.on_reorg(self.wallet_address_1, 0)
        
        self.assertEqual(self.nonce_manager.get_confirmed_nonce(self.wallet_address_1), 0)
        self.assertEqual(self.nonce_manager.get_pending_nonces(self.wallet_address_1), SortedSet())
        self.assertEqual(len(self.re_queued_txns), 1) # Only txs[2] was pending and >= 0
        self.assertEqual(self.re_queued_txns[0].nonce, 2)

    def test_on_reorg_no_effect_on_confirmed(self):
        """
        Tests reorg handling where the common ancestor nonce is equal to the current
        confirmed_nonce, only affecting pending transactions.
        """
        # Acquire nonces 0, 1
        txs = {}
        for i in range(2):
            tx = MockTransaction(i, f"w1_{i}")
            self.nonce_manager.acquire_nonce(self.wallet_address_1, tx)
            txs[i] = tx
        
        # Confirm nonce 0
        self.nonce_manager.confirm_nonce(self.wallet_address_1, 0)
        # Current state: confirmed_nonce = 1, pending = {1}

        # Simulate reorg to common ancestor nonce 1.
        # `confirmed_nonce` (1) matches `reverted_to_nonce` (1), so confirmed_nonce doesn't change.
        # Only pending nonces >= 1 (i.e., pending nonce 1) are affected.
        self.nonce_manager.on_reorg(self.wallet_address_1, 1)

        self.assertEqual(self.nonce_manager.get_confirmed_nonce(self.wallet_address_1), 1)
        self.assertEqual(self.nonce_manager.get_pending_nonces(self.wallet_address_1), SortedSet()) # Pending 1 removed
        self.assertEqual(len(self.re_queued_txns), 1)
        self.assertEqual(self.re_queued_txns[0].nonce, 1)

    def test_concurrent_access(self):
        """
        Tests thread safety of `acquire_nonce` under concurrent access.
        """
        num_threads = 5
        num_tx_per_thread = 10
        wallet = self.wallet_address_1

        # Simulate initial external confirmation to set a starting point.
        # `acquire_nonce` will sync with this, setting confirmed_nonce to 10.
        self.mock_chain_client.set_onchain_nonce(wallet, 10)
        
        def agent_task():
            for _ in range(num_tx_per_thread):
                tx = MockTransaction(0, "concurrent_tx") # Nonce will be assigned by manager
                self.nonce_manager.acquire_nonce(wallet, tx)

        threads = []
        for _ in range(num_threads):
            t = threading.Thread(target=agent_task)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # After all `acquire_nonce` calls, check the total number of pending nonces
        pending = self.nonce_manager.get_pending_nonces(wallet)
        self.assertEqual(len(pending), num_threads * num_tx_per_thread)

        # All acquired nonces should be unique and sequential, starting from the synced confirmed_nonce (10).
        expected_start_nonce = 10 
        expected_nonces = SortedSet(range(expected_start_nonce, expected_start_nonce + num_threads * num_tx_per_thread))
        self.assertEqual(pending, expected_nonces)

        # Simulate all transactions being confirmed in order to finalize state
        for i in range(expected_start_nonce, expected_start_nonce + num_threads * num_tx_per_thread):
            self.nonce_manager.confirm_nonce(wallet, i)
        
        self.assertEqual(self.nonce_manager.get_confirmed_nonce(wallet), expected_start_nonce + num_threads * num_tx_per_thread)
        self.assertEqual(self.nonce_manager.get_pending_nonces(wallet), SortedSet())
        self.assertEqual(self.nonce_manager.get_total_pending_transactions(wallet), 0)


if __name__ == '__main__':
    unittest.main()

