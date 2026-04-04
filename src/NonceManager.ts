import {
  Transaction,
  NonceAllocation,
  NonceManagerConfig,
  ChainProvider,
  ReorgEvent,
  RequeueEvent,
  EventListener,
} from './types';
import { ReorgDetector } from './ReorgDetector';

/** Simple mutex for serializing async operations. */
class Mutex {
  private queue: Array<() => void> = [];
  private locked = false;

  async acquire(): Promise<void> {
    if (!this.locked) {
      this.locked = true;
      return;
    }
    return new Promise<void>((resolve) => {
      this.queue.push(resolve);
    });
  }

  release(): void {
    if (this.queue.length > 0) {
      const next = this.queue.shift()!;
      next();
    } else {
      this.locked = false;
    }
  }
}

/** Per-wallet nonce tracking state. */
interface WalletState {
  /** Whether the on-chain nonce has been fetched. */
  initialized: boolean;
  /** The next nonce to allocate. */
  nextNonce: number;
  /** Mutex protecting nonce allocation for this wallet. */
  mutex: Mutex;
  /** Pending (unconfirmed) transactions keyed by nonce. */
  pendingTransactions: Map<number, Transaction>;
  /** Base on-chain nonce (last confirmed). */
  onChainNonce: number;
}

/**
 * NonceManager tracks pending nonces per wallet address, uses mutexes for
 * concurrent nonce allocation, monitors block hashes via ReorgDetector to
 * detect chain reorganizations, and re-queues transactions whose nonces
 * were invalidated by reorgs.
 */
export class NonceManager {
  private wallets: Map<string, WalletState> = new Map();
  private provider: ChainProvider;
  private reorgDetector: ReorgDetector;
  private confirmationDepth: number;
  private maxRequeueRetries: number;
  private reorgListeners: EventListener<ReorgEvent>[] = [];
  private requeueListeners: EventListener<RequeueEvent>[] = [];
  private errorListeners: EventListener<Error>[] = [];
  private nonceAllocatedListeners: EventListener<NonceAllocation>[] = [];
  private requeuedTransactions: Map<string, Transaction[]> = new Map();
  private running = false;

  constructor(provider: ChainProvider, config: NonceManagerConfig = {}) {
    this.provider = provider;
    this.confirmationDepth = config.confirmationDepth ?? 12;
    this.maxRequeueRetries = config.maxRequeueRetries ?? 3;

    this.reorgDetector = new ReorgDetector(provider, config.pollIntervalMs ?? 2000);

    this.reorgDetector.onReorg((event) => this.handleReorg(event));
    this.reorgDetector.onError((err) => this.emitError(err));
  }

  // ── Event registration ──────────────────────────────────────────────

  /** Listen for reorg detection events. */
  onReorg(listener: EventListener<ReorgEvent>): void {
    this.reorgListeners.push(listener);
  }

  /** Listen for transaction re-queue events. */
  onRequeue(listener: EventListener<RequeueEvent>): void {
    this.requeueListeners.push(listener);
  }

  /** Listen for nonce allocation events. */
  onNonceAllocated(listener: EventListener<NonceAllocation>): void {
    this.nonceAllocatedListeners.push(listener);
  }

  /** Listen for errors. */
  onError(listener: EventListener<Error>): void {
    this.errorListeners.push(listener);
  }

  // ── Lifecycle ───────────────────────────────────────────────────────

  /** Start the nonce manager and block monitoring. */
  async start(): Promise<void> {
    if (this.running) return;
    this.running = true;
    await this.reorgDetector.start();
  }

  /** Stop the nonce manager and block monitoring. */
  stop(): void {
    this.running = false;
    this.reorgDetector.stop();
  }

  // ── Public API ──────────────────────────────────────────────────────

  /**
   * Allocate the next nonce for a wallet address.
   * Thread-safe: concurrent calls for the same wallet are serialized via mutex.
   */
  async allocateNonce(walletAddress: string, transactionId: string, txData?: unknown): Promise<NonceAllocation> {
    const state = this.getOrInitWalletSlot(walletAddress);

    await state.mutex.acquire();
    try {
      // If not yet initialized, fetch on-chain nonce while holding the lock
      if (!state.initialized) {
        const onChainNonce = await this.provider.getTransactionCount(walletAddress.toLowerCase());
        state.nextNonce = onChainNonce;
        state.onChainNonce = onChainNonce;
        state.initialized = true;
      }

      const nonce = state.nextNonce;
      state.nextNonce++;

      const tx: Transaction = {
        id: transactionId,
        walletAddress,
        nonce,
        data: txData,
        createdAt: Date.now(),
      };

      state.pendingTransactions.set(nonce, tx);

      const allocation: NonceAllocation = {
        nonce,
        walletAddress,
        transactionId,
      };

      this.emitNonceAllocated(allocation);
      return allocation;
    } finally {
      state.mutex.release();
    }
  }

  /**
   * Confirm a transaction, removing it from the pending set.
   * Should be called when a transaction is mined and has enough confirmations.
   */
  async confirmTransaction(walletAddress: string, nonce: number): Promise<void> {
    const state = this.wallets.get(walletAddress.toLowerCase());
    if (!state) return;

    state.pendingTransactions.delete(nonce);
    if (nonce >= state.onChainNonce) {
      state.onChainNonce = nonce + 1;
    }
  }

  /** Get the list of pending transactions for a wallet. */
  getPendingTransactions(walletAddress: string): Transaction[] {
    const state = this.wallets.get(walletAddress.toLowerCase());
    if (!state) return [];
    return Array.from(state.pendingTransactions.values()).sort((a, b) => a.nonce - b.nonce);
  }

  /** Get the next nonce that would be allocated for a wallet. */
  getNextNonce(walletAddress: string): number | undefined {
    const state = this.wallets.get(walletAddress.toLowerCase());
    return state?.nextNonce;
  }

  /** Get re-queued transactions waiting to be retried for a wallet. */
  getRequeuedTransactions(walletAddress: string): Transaction[] {
    return this.requeuedTransactions.get(walletAddress.toLowerCase()) ?? [];
  }

  /**
   * Re-send re-queued transactions by allocating fresh nonces.
   * Returns new allocations for the re-queued transactions.
   */
  async retryRequeuedTransactions(walletAddress: string): Promise<NonceAllocation[]> {
    const key = walletAddress.toLowerCase();
    const queued = this.requeuedTransactions.get(key);
    if (!queued || queued.length === 0) return [];

    this.requeuedTransactions.delete(key);

    const allocations: NonceAllocation[] = [];
    for (const tx of queued) {
      const alloc = await this.allocateNonce(walletAddress, tx.id, tx.data);
      allocations.push(alloc);
    }
    return allocations;
  }

  /** Get the underlying ReorgDetector for advanced use. */
  getReorgDetector(): ReorgDetector {
    return this.reorgDetector;
  }

  /** Manually trigger a poll cycle (useful for testing). */
  async poll(): Promise<void> {
    await this.reorgDetector.poll();
  }

  /** Reset the nonce for a wallet from on-chain state. */
  async resetWalletNonce(walletAddress: string): Promise<void> {
    const key = walletAddress.toLowerCase();
    const onChainNonce = await this.provider.getTransactionCount(key);

    const state = this.wallets.get(key);
    if (state) {
      await state.mutex.acquire();
      try {
        state.nextNonce = onChainNonce;
        state.onChainNonce = onChainNonce;
        state.initialized = true;
        state.pendingTransactions.clear();
      } finally {
        state.mutex.release();
      }
    }
  }

  // ── Internal ────────────────────────────────────────────────────────

  /**
   * Synchronously get or create the wallet slot (without fetching on-chain state).
   * The actual on-chain nonce is fetched lazily inside the mutex lock.
   */
  private getOrInitWalletSlot(walletAddress: string): WalletState {
    const key = walletAddress.toLowerCase();
    let state = this.wallets.get(key);

    if (!state) {
      state = {
        initialized: false,
        nextNonce: 0,
        mutex: new Mutex(),
        pendingTransactions: new Map(),
        onChainNonce: 0,
      };
      this.wallets.set(key, state);
    }

    return state;
  }

  private handleReorg(event: ReorgEvent): void {
    // Forward the reorg event to listeners
    for (const listener of this.reorgListeners) {
      try {
        listener(event);
      } catch {
        // Swallow listener errors
      }
    }

    // Invalidate pending transactions at or after the fork point
    for (const [walletKey, state] of this.wallets.entries()) {
      const invalidated: Transaction[] = [];

      for (const [nonce, tx] of state.pendingTransactions.entries()) {
        // Transactions submitted around or after the fork point may be invalid
        if (nonce >= state.onChainNonce) {
          invalidated.push(tx);
          state.pendingTransactions.delete(nonce);
        }
      }

      if (invalidated.length > 0) {
        // Reset the next nonce to the on-chain nonce
        state.nextNonce = state.onChainNonce;

        // Store for re-queuing
        const existing = this.requeuedTransactions.get(walletKey) ?? [];
        this.requeuedTransactions.set(walletKey, [...existing, ...invalidated]);

        const requeueEvent: RequeueEvent = {
          walletAddress: walletKey,
          transactions: invalidated,
          reason: 'reorg',
        };

        for (const listener of this.requeueListeners) {
          try {
            listener(requeueEvent);
          } catch {
            // Swallow listener errors
          }
        }
      }
    }
  }

  private emitNonceAllocated(allocation: NonceAllocation): void {
    for (const listener of this.nonceAllocatedListeners) {
      try {
        listener(allocation);
      } catch {
        // Swallow listener errors
      }
    }
  }

  private emitError(err: Error): void {
    for (const listener of this.errorListeners) {
      try {
        listener(err);
      } catch {
        // Swallow listener errors
      }
    }
  }
}
