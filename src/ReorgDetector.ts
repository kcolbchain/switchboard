import {
  Block,
  ChainProvider,
  ReorgEvent,
  EventListener,
} from './types';

/**
 * Monitors the blockchain for reorganizations by tracking block hashes
 * at each height and comparing them when new blocks arrive.
 */
export class ReorgDetector {
  private provider: ChainProvider;
  private pollIntervalMs: number;
  private blockHistory: Map<number, string> = new Map();
  private latestBlock: number = 0;
  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private running: boolean = false;
  private listeners: EventListener<ReorgEvent>[] = [];
  private errorListeners: EventListener<Error>[] = [];
  private maxHistorySize: number;

  constructor(provider: ChainProvider, pollIntervalMs: number = 2000, maxHistorySize: number = 256) {
    this.provider = provider;
    this.pollIntervalMs = pollIntervalMs;
    this.maxHistorySize = maxHistorySize;
  }

  /** Register a listener for reorg events. */
  onReorg(listener: EventListener<ReorgEvent>): void {
    this.listeners.push(listener);
  }

  /** Register a listener for errors. */
  onError(listener: EventListener<Error>): void {
    this.errorListeners.push(listener);
  }

  /** Start polling for new blocks. */
  async start(): Promise<void> {
    if (this.running) return;
    this.running = true;

    // Initialize with the current block
    await this.syncToHead();

    this.pollTimer = setInterval(() => {
      this.poll().catch((err) => this.emitError(err));
    }, this.pollIntervalMs);
  }

  /** Stop polling. */
  stop(): void {
    this.running = false;
    if (this.pollTimer) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  }

  /** Manually trigger a poll cycle (useful for testing). */
  async poll(): Promise<void> {
    const currentBlockNumber = await this.provider.getBlockNumber();
    if (currentBlockNumber <= this.latestBlock) return;

    // Check existing blocks for reorgs (hash mismatches)
    const reorgDepth = await this.detectReorg(currentBlockNumber);

    if (reorgDepth > 0) {
      const forkPoint = this.latestBlock - reorgDepth + 1;
      const oldHashes: string[] = [];
      const newHashes: string[] = [];

      for (let i = forkPoint; i <= this.latestBlock; i++) {
        const oldHash = this.blockHistory.get(i);
        if (oldHash) oldHashes.push(oldHash);

        const newBlock = await this.provider.getBlock(i);
        if (newBlock) {
          newHashes.push(newBlock.hash);
          this.blockHistory.set(i, newBlock.hash);
        }
      }

      const event: ReorgEvent = {
        forkPoint,
        depth: reorgDepth,
        oldHashes,
        newHashes,
      };

      this.emitReorg(event);
    }

    // Record new blocks
    for (let i = this.latestBlock + 1; i <= currentBlockNumber; i++) {
      const block = await this.provider.getBlock(i);
      if (block) {
        this.blockHistory.set(i, block.hash);
      }
    }

    this.latestBlock = currentBlockNumber;
    this.pruneHistory();
  }

  /** Get the current latest tracked block number. */
  getLatestBlock(): number {
    return this.latestBlock;
  }

  /** Get the stored hash for a block number. */
  getBlockHash(blockNumber: number): string | undefined {
    return this.blockHistory.get(blockNumber);
  }

  private async syncToHead(): Promise<void> {
    const currentBlockNumber = await this.provider.getBlockNumber();
    const startBlock = Math.max(0, currentBlockNumber - this.maxHistorySize + 1);

    for (let i = startBlock; i <= currentBlockNumber; i++) {
      const block = await this.provider.getBlock(i);
      if (block) {
        this.blockHistory.set(i, block.hash);
      }
    }

    this.latestBlock = currentBlockNumber;
  }

  private async detectReorg(currentBlockNumber: number): Promise<number> {
    let reorgDepth = 0;

    // Walk backwards from the latest known block checking for hash changes
    const checkFrom = Math.min(this.latestBlock, currentBlockNumber);
    for (let i = checkFrom; i >= Math.max(0, checkFrom - this.maxHistorySize); i--) {
      const storedHash = this.blockHistory.get(i);
      if (!storedHash) break;

      const block = await this.provider.getBlock(i);
      if (!block) break;

      if (block.hash !== storedHash) {
        reorgDepth++;
      } else {
        break;
      }
    }

    return reorgDepth;
  }

  private pruneHistory(): void {
    const minBlock = this.latestBlock - this.maxHistorySize;
    for (const blockNumber of this.blockHistory.keys()) {
      if (blockNumber < minBlock) {
        this.blockHistory.delete(blockNumber);
      }
    }
  }

  private emitReorg(event: ReorgEvent): void {
    for (const listener of this.listeners) {
      try {
        listener(event);
      } catch {
        // Listener errors should not crash the detector
      }
    }
  }

  private emitError(err: unknown): void {
    const error = err instanceof Error ? err : new Error(String(err));
    for (const listener of this.errorListeners) {
      try {
        listener(error);
      } catch {
        // Swallow listener errors
      }
    }
  }
}
