import threading
from sortedcontainers import SortedSet
from typing import Dict, Any, Optional, Callable, Protocol

class ChainClient(Protocol):
    """
    Protocol for a blockchain client that provides nonce data.
    A concrete implementation would interact with a specific blockchain (e.g., Ethereum RPC).
    """
    def get_current_onchain_nonce(self, address: str) -> int:
        """
        Fetches the current transaction count (nonce) for an address on the blockchain.
        This represents the nonce of the next transaction to be sent from the address
        that would be considered valid by the chain.
        """
        ...

class WalletState:
    """
    Manages the local nonce state for a single wallet address.
    """
    def __init__(self, confirmed_nonce: int):
        # The highest sequentially confirmed nonce known to the manager.
        self.confirmed_nonce: int = confirmed_nonce

        # Stores nonces that have been acquired by the manager but not yet confirmed on-chain.
        # SortedSet ensures nonces are kept in order for easy processing and unique storage.
        self.pending_nonces: SortedSet[int] = SortedSet()

        # Maps a pending nonce to its associated transaction object.
        # This allows re-queuing of transactions if a reorg invalidates their nonces.
        self.pending_transactions: Dict[int, Any] = {}

        # Nonces confirmed out-of-order (ahead of confirmed_nonce). They roll into
        # confirmed_nonce when the gap fills.
        self.out_of_order_confirmations: set = set()

class NonceManager:
    """
    Manages nonces for multiple wallet addresses, tracking pending and confirmed
    transactions and providing reorg protection.

    It ensures nonces are always valid and correctly ordered, even when
    concurrent transactions are being sent or chain reorganizations occur.
    """
    def __init__(self, chain_client: ChainClient, re_queue_callback: Optional[Callable[[Any], None]] = None):
        """
        Initializes the NonceManager.

        Args:
            chain_client: An object conforming to the ChainClient protocol,
                          used to interact with the blockchain to get current on-chain nonces.
            re_queue_callback: An optional callback function to be invoked when
                               transactions need to be re-queued due to a reorg.
                               It should accept a single argument: the original transaction object.
        """
        self._chain_client: ChainClient = chain_client
        self._wallet_states: Dict[str, WalletState] = {}
        self._lock = threading.Lock() # Protects access to _wallet_states for thread safety
        self._re_queue_callback = re_queue_callback

    def _get_wallet_state(self, address: str) -> WalletState:
        """
        Retrieves or initializes the WalletState for a given address.
        This method must be called under the `_lock` to ensure thread safety.
        """
        if address not in self._wallet_states:
            # For a new wallet, fetch its current on-chain nonce to initialize.
            onchain_nonce = self._chain_client.get_current_onchain_nonce(address)
            self._wallet_states[address] = WalletState(onchain_nonce)
        return self._wallet_states[address]

    def _sync_with_onchain_nonce(self, state: WalletState, address: str):
        """
        Internal method to synchronize the local wallet state with the actual on-chain nonce.
        This helps in resolving situations where transactions were confirmed externally
        or where a reorg was resolved and new transactions got into blocks.
        This method must be called under the `_lock`.
        """
        onchain_nonce = self._chain_client.get_current_onchain_nonce(address)

        if onchain_nonce > state.confirmed_nonce:
            # The on-chain nonce is higher than our locally confirmed nonce.
            # Pending nonces strictly less than onchain_nonce are confirmed; pending nonces
            # equal to onchain_nonce are stale (the chain advanced past them without including
            # our tx) and should be re-issued on the next acquire.
            nonces_to_remove = SortedSet(n for n in state.pending_nonces if n <= onchain_nonce)
            for n in nonces_to_remove:
                state.pending_nonces.remove(n)
                if n in state.pending_transactions:
                    del state.pending_transactions[n]

            # Out-of-order confirmations the chain has now subsumed are no longer interesting.
            state.out_of_order_confirmations = {
                n for n in state.out_of_order_confirmations if n > onchain_nonce
            }

            # Update our locally tracked confirmed_nonce to reflect the latest on-chain state.
            state.confirmed_nonce = onchain_nonce

    def acquire_nonce(self, address: str, transaction: Optional[Any] = None) -> int:
        """
        Acquires the next available nonce for a given wallet address.
        The acquired nonce is marked as 'pending' and associated with a transaction.

        Args:
            address: The blockchain wallet address for which to acquire a nonce.
            transaction: An optional transaction object to associate with this nonce.
                         This object will be passed to the `re_queue_callback` if a
                         reorg invalidates this nonce.

        Returns:
            The integer value of the acquired nonce.
        """
        with self._lock:
            state = self._get_wallet_state(address)
            
            # First, ensure our local state is synchronized with the latest on-chain nonce.
            self._sync_with_onchain_nonce(state, address)

            # Determine the next available nonce.
            # If there are any pending nonces, the next one is the highest pending + 1.
            # Otherwise, it's the current `confirmed_nonce` (which should be the next expected nonce).
            next_nonce = state.confirmed_nonce
            if state.pending_nonces:
                next_nonce = max(state.pending_nonces) + 1
            
            # Add the chosen nonce to the set of pending nonces.
            state.pending_nonces.add(next_nonce)
            if transaction is not None:
                state.pending_transactions[next_nonce] = transaction
            return next_nonce

    def release_nonce(self, address: str, nonce: int):
        """
        Releases a previously acquired nonce, making it available again.
        This is typically used if a transaction using this nonce failed locally
        before being broadcast or was dropped from the mempool.
        This method does NOT update the `confirmed_nonce` as it doesn't imply
        any chain confirmation.

        Args:
            address: The wallet address.
            nonce: The nonce to release.
        """
        with self._lock:
            state = self._get_wallet_state(address)
            if nonce in state.pending_nonces:
                state.pending_nonces.remove(nonce)
                if nonce in state.pending_transactions:
                    del state.pending_transactions[nonce]
            # Optionally, log a warning if the nonce was not found in pending_nonces.

    def confirm_nonce(self, address: str, nonce: int):
        """
        Marks a nonce as successfully confirmed on the blockchain (i.e., the transaction
        using it has been mined into a block).

        Confirmations may arrive out of order. Out-of-order confirmations are stashed
        and rolled into `confirmed_nonce` once the preceding nonces confirm.

        Args:
            address: The wallet address.
            nonce: The nonce to confirm.
        """
        with self._lock:
            state = self._get_wallet_state(address)

            # If the nonce is currently pending, drop it from pending tracking.
            if nonce in state.pending_nonces:
                state.pending_nonces.remove(nonce)
                if nonce in state.pending_transactions:
                    del state.pending_transactions[nonce]

            if nonce < state.confirmed_nonce:
                # Already counted (e.g., via prior sync).
                return

            if nonce == state.confirmed_nonce:
                # Advance by one, then roll forward through any stashed out-of-order
                # confirmations that are now contiguous.
                state.confirmed_nonce += 1
                while state.confirmed_nonce in state.out_of_order_confirmations:
                    state.out_of_order_confirmations.discard(state.confirmed_nonce)
                    state.confirmed_nonce += 1
            else:
                # nonce > confirmed_nonce: out-of-order. Stash for later roll-forward.
                state.out_of_order_confirmations.add(nonce)

    def on_reorg(self, address: str, reverted_to_nonce: int):
        """
        Handles a chain reorganization event for a specific wallet.
        This method should be called by an external chain monitor component
        when a reorg is detected.

        It adjusts the `confirmed_nonce` for the affected wallet if the reorg
        depth requires it and invalidates/re-queues any pending transactions
        whose nonces are no longer valid due to the reorg.

        Args:
            address: The wallet address affected by the reorg.
            reverted_to_nonce: The highest nonce that is considered confirmed
                               and valid at the common ancestor block after the reorg.
                               All transactions with nonces equal to or greater than
                               `reverted_to_nonce` are considered potentially invalid.
        """
        with self._lock:
            state = self._get_wallet_state(address)

            # If the reorg depth implies that our `confirmed_nonce` is no longer valid,
            # revert it to the `reverted_to_nonce` supplied by the reorg detector.
            if state.confirmed_nonce > reverted_to_nonce:
                state.confirmed_nonce = reverted_to_nonce

            # Drop any stashed out-of-order confirmations the reorg has invalidated.
            state.out_of_order_confirmations = {
                n for n in state.out_of_order_confirmations if n < reverted_to_nonce
            }

            reverted_txns = []
            nonces_to_remove = SortedSet()

            # Identify all pending nonces that are equal to or greater than `reverted_to_nonce`.
            # These nonces are now invalid and their associated transactions need to be re-queued.
            for nonce in state.pending_nonces:
                if nonce >= reverted_to_nonce:
                    nonces_to_remove.add(nonce)
                    if nonce in state.pending_transactions:
                        reverted_txns.append(state.pending_transactions[nonce])

            # Remove identified invalid nonces and their associated transactions from our local state.
            for nonce in nonces_to_remove:
                state.pending_nonces.remove(nonce)
                del state.pending_transactions[nonce]

            # If a `re_queue_callback` was provided, invoke it for all identified reverted transactions.
            if self._re_queue_callback and reverted_txns:
                for tx in reverted_txns:
                    self._re_queue_callback(tx)

    def get_pending_nonces(self, address: str) -> SortedSet[int]:
        """
        Returns a copy of the set of nonces currently marked as pending for an address.
        """
        with self._lock:
            return SortedSet(self._get_wallet_state(address).pending_nonces)

    def get_confirmed_nonce(self, address: str) -> int:
        """
        Returns the highest sequentially confirmed nonce for an address known to the manager.
        """
        with self._lock:
            return self._get_wallet_state(address).confirmed_nonce

    def get_total_pending_transactions(self, address: str) -> int:
        """
        Returns the count of transactions currently pending (acquired but not confirmed)
        for a specific address.
        """
        with self._lock:
            return len(self._get_wallet_state(address).pending_transactions)

