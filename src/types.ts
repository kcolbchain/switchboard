/** Represents a block on the chain. */
export interface Block {
  number: number;
  hash: string;
  parentHash: string;
}

/** Represents a queued or pending transaction. */
export interface Transaction {
  id: string;
  walletAddress: string;
  nonce: number;
  data: unknown;
  createdAt: number;
}

/** Result of a nonce allocation. */
export interface NonceAllocation {
  nonce: number;
  walletAddress: string;
  transactionId: string;
}

/** Info emitted when a reorg is detected. */
export interface ReorgEvent {
  /** The block number where the fork was detected. */
  forkPoint: number;
  /** How many blocks were reorganized. */
  depth: number;
  /** Old block hashes that are no longer canonical. */
  oldHashes: string[];
  /** New block hashes on the canonical chain. */
  newHashes: string[];
}

/** Info emitted when transactions are re-queued after a reorg. */
export interface RequeueEvent {
  walletAddress: string;
  transactions: Transaction[];
  reason: 'reorg';
}

/** Configuration for the NonceManager. */
export interface NonceManagerConfig {
  /** Number of block confirmations required before a nonce is considered final. Default: 12. */
  confirmationDepth?: number;
  /** Interval in ms to poll for new blocks. Default: 2000. */
  pollIntervalMs?: number;
  /** Maximum retries for re-queued transactions. Default: 3. */
  maxRequeueRetries?: number;
}

/** Provider interface for interacting with the chain. */
export interface ChainProvider {
  /** Get the latest block number. */
  getBlockNumber(): Promise<number>;
  /** Get a block by its number. */
  getBlock(blockNumber: number): Promise<Block | null>;
  /** Get the current on-chain nonce (transaction count) for an address. */
  getTransactionCount(address: string): Promise<number>;
}

/** Event types emitted by the nonce manager system. */
export type NonceManagerEventType = 'reorg' | 'requeue' | 'nonceAllocated' | 'error';

/** Listener callback type. */
export type EventListener<T> = (event: T) => void;
