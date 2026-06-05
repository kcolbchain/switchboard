import threading
from dataclasses import dataclass
from sortedcontainers import SortedSet
from typing import Any, Callable, Dict, Optional, Protocol, Tuple


DEFAULT_CHAIN_ID = 0

SIGNATURE_ALG_ECDSA = 0x01
SIGNATURE_ALG_ML_DSA_65 = 0x02
SIGNATURE_ALG_HYBRID = 0x03

_SUPPORTED_SIGNATURE_ALGS = {
    SIGNATURE_ALG_ECDSA,
    SIGNATURE_ALG_ML_DSA_65,
    SIGNATURE_ALG_HYBRID,
}


class UnsupportedSignatureAlgorithm(ValueError):
    """Raised when a nonce is requested for an unknown signature algorithm tag."""


class HybridSignatureAlgorithmReserved(UnsupportedSignatureAlgorithm):
    """Raised when hybrid signatures are requested without the feature flag."""


class SignatureAlgorithmMismatch(ValueError):
    """Raised when a nonce would be re-signed under a different algorithm tag."""


@dataclass(frozen=True)
class NonceRecord:
    nonce: int
    signature_alg: int
    transaction: Optional[Any] = None


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
    Manages the local nonce state for a single wallet address on one chain.
    """

    def __init__(self, confirmed_nonce: int):
        # The next sequential nonce known to be valid by the manager.
        self.confirmed_nonce: int = confirmed_nonce

        # Stores nonces that have been acquired by the manager but not yet confirmed on-chain.
        # SortedSet ensures nonces are kept in order for easy processing and unique storage.
        self.pending_nonces: SortedSet[int] = SortedSet()

        # Maps a pending nonce to its associated transaction object.
        # This allows re-queuing of transactions if a reorg invalidates their nonces.
        self.pending_transactions: Dict[int, Any] = {}

        # Every pending nonce has a record carrying the signature algorithm used
        # to sign it. Existing in-memory records are migrated to ECDSA on read.
        self.pending_records: Dict[int, NonceRecord] = {}

        # Signature algorithms for transactions that were confirmed locally.
        # If a reorg rolls them back, the manager can prevent a different
        # algorithm from re-signing the same nonce by accident.
        self.confirmed_signature_algs: Dict[int, int] = {}

        # Signature algorithms for nonces that need rebroadcast after a reorg or
        # stale-pending cleanup.
        self.rebroadcast_signature_algs: Dict[int, int] = {}

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

    def __init__(
        self,
        chain_client: ChainClient,
        re_queue_callback: Optional[Callable[[Any], None]] = None,
        allow_hybrid: bool = False,
    ):
        """
        Initializes the NonceManager.

        Args:
            chain_client: An object conforming to the ChainClient protocol,
                          used to interact with the blockchain to get current on-chain nonces.
            re_queue_callback: An optional callback function to be invoked when
                               transactions need to be re-queued due to a reorg.
                               It should accept a single argument: the original transaction object.
            allow_hybrid: Feature flag for reserving hybrid signature-algorithm
                          nonces. Hybrid signing/verification is intentionally
                          not implemented here.
        """
        self._chain_client: ChainClient = chain_client
        self._wallet_states: Dict[Tuple[int, str], WalletState] = {}
        self._lock = threading.Lock()  # Protects access to _wallet_states for thread safety
        self._re_queue_callback = re_queue_callback
        self._allow_hybrid = allow_hybrid

    def _wallet_key(self, chain_id: int, address: str) -> Tuple[int, str]:
        return (chain_id, address)

    def _get_wallet_state(self, address: str, chain_id: int = DEFAULT_CHAIN_ID) -> WalletState:
        """
        Retrieves or initializes the WalletState for a given address.
        This method must be called under the `_lock` to ensure thread safety.
        """
        key = self._wallet_key(chain_id, address)
        if key not in self._wallet_states:
            # For a new wallet, fetch its current on-chain nonce to initialize.
            onchain_nonce = self._chain_client.get_current_onchain_nonce(address)
            self._wallet_states[key] = WalletState(onchain_nonce)
        return self._wallet_states[key]

    def _validate_signature_alg(self, signature_alg: int) -> None:
        if signature_alg not in _SUPPORTED_SIGNATURE_ALGS:
            raise UnsupportedSignatureAlgorithm(
                f"UNSUPPORTED_SIGNATURE_ALG: unknown signature_alg 0x{signature_alg:02x}"
            )

        if signature_alg == SIGNATURE_ALG_HYBRID and not self._allow_hybrid:
            raise HybridSignatureAlgorithmReserved(
                "HYBRID_SIGNATURE_ALG_RESERVED: enable allow_hybrid to reserve hybrid nonces"
            )

    def _ensure_pending_records(self, state: WalletState) -> None:
        for nonce in list(state.pending_nonces):
            if nonce not in state.pending_records:
                state.pending_records[nonce] = NonceRecord(
                    nonce=nonce,
                    signature_alg=SIGNATURE_ALG_ECDSA,
                    transaction=state.pending_transactions.get(nonce),
                )

    def _drop_pending_nonce(self, state: WalletState, nonce: int) -> Optional[NonceRecord]:
        record = state.pending_records.pop(nonce, None)
        if nonce in state.pending_nonces:
            state.pending_nonces.remove(nonce)
        state.pending_transactions.pop(nonce, None)
        return record

    def _assert_rebroadcast_alg(
        self,
        state: WalletState,
        nonce: int,
        signature_alg: int,
        force_rebroadcast_alg: bool,
    ) -> None:
        original_alg = state.rebroadcast_signature_algs.get(nonce)
        if original_alg is None:
            return

        if original_alg != signature_alg and not force_rebroadcast_alg:
            raise SignatureAlgorithmMismatch(
                "SIGNATURE_ALG_MISMATCH: nonce "
                f"{nonce} was previously signed with 0x{original_alg:02x}, "
                f"refusing to rebroadcast with 0x{signature_alg:02x}"
            )

    def _sync_with_onchain_nonce(self, state: WalletState, address: str):
        """
        Internal method to synchronize the local wallet state with the actual on-chain nonce.
        This helps in resolving situations where transactions were confirmed externally
        or where a reorg was resolved and new transactions got into blocks.
        This method must be called under the `_lock`.
        """
        onchain_nonce = self._chain_client.get_current_onchain_nonce(address)
        self._ensure_pending_records(state)

        if onchain_nonce > state.confirmed_nonce:
            # The on-chain nonce is higher than our locally confirmed nonce.
            # Pending nonces strictly less than onchain_nonce are confirmed; pending nonces
            # equal to onchain_nonce are stale and should be re-issued on the next acquire.
            nonces_to_remove = SortedSet(n for n in state.pending_nonces if n <= onchain_nonce)
            for n in nonces_to_remove:
                record = self._drop_pending_nonce(state, n)
                if record is None:
                    continue

                if n < onchain_nonce:
                    state.confirmed_signature_algs[n] = record.signature_alg
                else:
                    state.rebroadcast_signature_algs[n] = record.signature_alg

            # Out-of-order confirmations the chain has now subsumed are no longer interesting.
            state.out_of_order_confirmations = {
                n for n in state.out_of_order_confirmations if n > onchain_nonce
            }

            # Update our locally tracked confirmed_nonce to reflect the latest on-chain state.
            state.confirmed_nonce = onchain_nonce

    def _reserve_next_nonce(
        self,
        state: WalletState,
        signature_alg: int,
        transaction: Optional[Any],
        force_rebroadcast_alg: bool,
    ) -> int:
        self._ensure_pending_records(state)

        # Determine the next available nonce: the lowest nonce at or above
        # `confirmed_nonce` that is not already pending. Walking up from
        # `confirmed_nonce` (rather than taking ``max(pending) + 1``) means a
        # nonce freed by `release_nonce` or a gap left by an out-of-order
        # confirmation is reused before the sequence is extended. Ethereum
        # requires gapless nonces, so leaving a hole would stall every
        # higher-nonce pending tx until the gap is filled.
        next_nonce = state.confirmed_nonce
        for pending in state.pending_nonces.irange(minimum=next_nonce):
            if pending != next_nonce:
                break
            next_nonce += 1

        self._assert_rebroadcast_alg(
            state,
            next_nonce,
            signature_alg,
            force_rebroadcast_alg,
        )

        # Add the chosen nonce to the set of pending nonces.
        state.pending_nonces.add(next_nonce)
        state.pending_records[next_nonce] = NonceRecord(
            nonce=next_nonce,
            signature_alg=signature_alg,
            transaction=transaction,
        )
        state.rebroadcast_signature_algs.pop(next_nonce, None)
        if transaction is not None:
            state.pending_transactions[next_nonce] = transaction
        return next_nonce

    def next_nonce(
        self,
        chain_id: int,
        address: str,
        alg: int,
        transaction: Optional[Any] = None,
        *,
        force_rebroadcast_alg: bool = False,
    ) -> int:
        """
        Atomically reserves the next available nonce for (chain_id, address)
        under the supplied signature algorithm tag.

        If a prior reorg or stale-pending cleanup requires this nonce to be
        rebroadcast, the algorithm must match unless `force_rebroadcast_alg` is
        explicitly set by the operator.
        """
        self._validate_signature_alg(alg)

        with self._lock:
            state = self._get_wallet_state(address, chain_id)
            self._sync_with_onchain_nonce(state, address)
            return self._reserve_next_nonce(
                state,
                alg,
                transaction,
                force_rebroadcast_alg,
            )

    def acquire_nonce(
        self,
        address: str,
        transaction: Optional[Any] = None,
        signature_alg: int = SIGNATURE_ALG_ECDSA,
        *,
        chain_id: int = DEFAULT_CHAIN_ID,
        force_rebroadcast_alg: bool = False,
    ) -> int:
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
        return self.next_nonce(
            chain_id,
            address,
            signature_alg,
            transaction,
            force_rebroadcast_alg=force_rebroadcast_alg,
        )

    def release_nonce(self, address: str, nonce: int, *, chain_id: int = DEFAULT_CHAIN_ID):
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
            state = self._get_wallet_state(address, chain_id)
            self._ensure_pending_records(state)
            if nonce in state.pending_nonces:
                self._drop_pending_nonce(state, nonce)
            # Optionally, log a warning if the nonce was not found in pending_nonces.

    def confirm_nonce(self, address: str, nonce: int, *, chain_id: int = DEFAULT_CHAIN_ID):
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
            state = self._get_wallet_state(address, chain_id)
            self._ensure_pending_records(state)

            # If the nonce is currently pending, drop it from pending tracking.
            if nonce in state.pending_nonces:
                record = self._drop_pending_nonce(state, nonce)
                if record is not None:
                    state.confirmed_signature_algs[nonce] = record.signature_alg

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

    def on_reorg(self, address: str, reverted_to_nonce: int, *, chain_id: int = DEFAULT_CHAIN_ID):
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
            state = self._get_wallet_state(address, chain_id)
            self._ensure_pending_records(state)

            # If the reorg depth implies that our `confirmed_nonce` is no longer valid,
            # revert it to the `reverted_to_nonce` supplied by the reorg detector.
            if state.confirmed_nonce > reverted_to_nonce:
                for nonce, signature_alg in list(state.confirmed_signature_algs.items()):
                    if nonce >= reverted_to_nonce:
                        state.rebroadcast_signature_algs[nonce] = signature_alg
                        del state.confirmed_signature_algs[nonce]
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
                record = self._drop_pending_nonce(state, nonce)
                if record is not None:
                    state.rebroadcast_signature_algs[nonce] = record.signature_alg

            # If a `re_queue_callback` was provided, invoke it for all identified reverted transactions.
            if self._re_queue_callback and reverted_txns:
                for tx in reverted_txns:
                    self._re_queue_callback(tx)

    def get_pending_nonces(self, address: str, *, chain_id: int = DEFAULT_CHAIN_ID) -> SortedSet[int]:
        """
        Returns a copy of the set of nonces currently marked as pending for an address.
        """
        with self._lock:
            return SortedSet(self._get_wallet_state(address, chain_id).pending_nonces)

    def get_pending_nonce_records(
        self,
        address: str,
        *,
        chain_id: int = DEFAULT_CHAIN_ID,
    ) -> Dict[int, NonceRecord]:
        """
        Returns pending nonce records keyed by nonce.
        """
        with self._lock:
            state = self._get_wallet_state(address, chain_id)
            self._ensure_pending_records(state)
            return dict(state.pending_records)

    def get_confirmed_nonce(self, address: str, *, chain_id: int = DEFAULT_CHAIN_ID) -> int:
        """
        Returns the highest sequentially confirmed nonce for an address known to the manager.
        """
        with self._lock:
            return self._get_wallet_state(address, chain_id).confirmed_nonce

    def get_total_pending_transactions(self, address: str, *, chain_id: int = DEFAULT_CHAIN_ID) -> int:
        """
        Returns the count of transactions currently pending (acquired but not confirmed)
        for a specific address.
        """
        with self._lock:
            return len(self._get_wallet_state(address, chain_id).pending_transactions)
