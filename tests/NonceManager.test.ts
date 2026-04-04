import { describe, it, expect, beforeEach, vi } from 'vitest';
import { NonceManager } from '../src/NonceManager';
import { ReorgDetector } from '../src/ReorgDetector';
import {
  Block,
  ChainProvider,
  ReorgEvent,
  RequeueEvent,
  NonceAllocation,
} from '../src/types';

// ── Mock chain provider ────────────────────────────────────────────────

class MockChainProvider implements ChainProvider {
  private blocks: Map<number, Block> = new Map();
  private nonces: Map<string, number> = new Map();
  private _blockNumber: number = 0;

  setBlockNumber(n: number): void {
    this._blockNumber = n;
  }

  addBlock(block: Block): void {
    this.blocks.set(block.number, block);
    if (block.number > this._blockNumber) {
      this._blockNumber = block.number;
    }
  }

  setNonce(address: string, nonce: number): void {
    this.nonces.set(address.toLowerCase(), nonce);
  }

  replaceBlock(blockNumber: number, newHash: string, newParentHash: string): void {
    this.blocks.set(blockNumber, {
      number: blockNumber,
      hash: newHash,
      parentHash: newParentHash,
    });
  }

  async getBlockNumber(): Promise<number> {
    return this._blockNumber;
  }

  async getBlock(blockNumber: number): Promise<Block | null> {
    return this.blocks.get(blockNumber) ?? null;
  }

  async getTransactionCount(address: string): Promise<number> {
    return this.nonces.get(address.toLowerCase()) ?? 0;
  }
}

function buildChain(provider: MockChainProvider, start: number, end: number, prefix: string = 'hash'): void {
  for (let i = start; i <= end; i++) {
    provider.addBlock({
      number: i,
      hash: `${prefix}-${i}`,
      parentHash: i > 0 ? `${prefix}-${i - 1}` : '0x0',
    });
  }
}

// ── Tests ──────────────────────────────────────────────────────────────

describe('NonceManager', () => {
  let provider: MockChainProvider;
  let manager: NonceManager;

  beforeEach(() => {
    provider = new MockChainProvider();
    buildChain(provider, 0, 10);
    provider.setNonce('0xwallet1', 0);
    provider.setNonce('0xwallet2', 5);

    manager = new NonceManager(provider, {
      confirmationDepth: 3,
      pollIntervalMs: 100_000, // Large to avoid auto-poll; we poll manually
    });
  });

  // ── Nonce allocation ────────────────────────────────────────────────

  describe('nonce allocation', () => {
    it('allocates sequential nonces starting from on-chain nonce', async () => {
      const a1 = await manager.allocateNonce('0xWallet1', 'tx-1');
      const a2 = await manager.allocateNonce('0xWallet1', 'tx-2');
      const a3 = await manager.allocateNonce('0xWallet1', 'tx-3');

      expect(a1.nonce).toBe(0);
      expect(a2.nonce).toBe(1);
      expect(a3.nonce).toBe(2);
      expect(a1.walletAddress).toBe('0xWallet1');
    });

    it('starts from the on-chain nonce for a wallet with existing txns', async () => {
      const a1 = await manager.allocateNonce('0xWallet2', 'tx-1');
      expect(a1.nonce).toBe(5);
    });

    it('tracks pending transactions after allocation', async () => {
      await manager.allocateNonce('0xWallet1', 'tx-1', { to: '0xdead' });
      await manager.allocateNonce('0xWallet1', 'tx-2', { to: '0xbeef' });

      const pending = manager.getPendingTransactions('0xWallet1');
      expect(pending).toHaveLength(2);
      expect(pending[0].nonce).toBe(0);
      expect(pending[1].nonce).toBe(1);
      expect(pending[0].data).toEqual({ to: '0xdead' });
    });

    it('emits nonceAllocated events', async () => {
      const events: NonceAllocation[] = [];
      manager.onNonceAllocated((e) => events.push(e));

      await manager.allocateNonce('0xWallet1', 'tx-1');
      await manager.allocateNonce('0xWallet1', 'tx-2');

      expect(events).toHaveLength(2);
      expect(events[0].nonce).toBe(0);
      expect(events[1].nonce).toBe(1);
    });
  });

  // ── Concurrent nonce allocation ─────────────────────────────────────

  describe('concurrent nonce allocation', () => {
    it('serializes concurrent allocations for the same wallet', async () => {
      // Fire off multiple nonce requests concurrently
      const results = await Promise.all([
        manager.allocateNonce('0xWallet1', 'tx-a'),
        manager.allocateNonce('0xWallet1', 'tx-b'),
        manager.allocateNonce('0xWallet1', 'tx-c'),
        manager.allocateNonce('0xWallet1', 'tx-d'),
        manager.allocateNonce('0xWallet1', 'tx-e'),
      ]);

      const nonces = results.map((r) => r.nonce).sort((a, b) => a - b);
      expect(nonces).toEqual([0, 1, 2, 3, 4]);

      // All nonces should be unique
      const unique = new Set(nonces);
      expect(unique.size).toBe(5);
    });

    it('handles concurrent allocations across different wallets independently', async () => {
      const [a1, a2, b1, b2] = await Promise.all([
        manager.allocateNonce('0xWallet1', 'w1-tx1'),
        manager.allocateNonce('0xWallet1', 'w1-tx2'),
        manager.allocateNonce('0xWallet2', 'w2-tx1'),
        manager.allocateNonce('0xWallet2', 'w2-tx2'),
      ]);

      // Wallet1 starts at nonce 0
      const w1Nonces = [a1.nonce, a2.nonce].sort((a, b) => a - b);
      expect(w1Nonces).toEqual([0, 1]);

      // Wallet2 starts at nonce 5
      const w2Nonces = [b1.nonce, b2.nonce].sort((a, b) => a - b);
      expect(w2Nonces).toEqual([5, 6]);
    });

    it('produces no duplicate nonces under heavy concurrency', async () => {
      const promises: Promise<NonceAllocation>[] = [];
      for (let i = 0; i < 50; i++) {
        promises.push(manager.allocateNonce('0xWallet1', `tx-${i}`));
      }

      const results = await Promise.all(promises);
      const nonces = results.map((r) => r.nonce);
      const unique = new Set(nonces);

      expect(unique.size).toBe(50);
      expect(Math.min(...nonces)).toBe(0);
      expect(Math.max(...nonces)).toBe(49);
    });
  });

  // ── Transaction confirmation ────────────────────────────────────────

  describe('transaction confirmation', () => {
    it('removes confirmed transactions from pending', async () => {
      await manager.allocateNonce('0xWallet1', 'tx-1');
      await manager.allocateNonce('0xWallet1', 'tx-2');

      expect(manager.getPendingTransactions('0xWallet1')).toHaveLength(2);

      await manager.confirmTransaction('0xWallet1', 0);
      expect(manager.getPendingTransactions('0xWallet1')).toHaveLength(1);
      expect(manager.getPendingTransactions('0xWallet1')[0].nonce).toBe(1);
    });
  });

  // ── Multiple wallets ────────────────────────────────────────────────

  describe('multiple wallets', () => {
    it('maintains independent nonce sequences per wallet', async () => {
      const w1a = await manager.allocateNonce('0xWallet1', 'w1-1');
      const w2a = await manager.allocateNonce('0xWallet2', 'w2-1');
      const w1b = await manager.allocateNonce('0xWallet1', 'w1-2');
      const w2b = await manager.allocateNonce('0xWallet2', 'w2-2');

      expect(w1a.nonce).toBe(0);
      expect(w1b.nonce).toBe(1);
      expect(w2a.nonce).toBe(5);
      expect(w2b.nonce).toBe(6);
    });

    it('tracks pending transactions separately per wallet', async () => {
      await manager.allocateNonce('0xWallet1', 'w1-1');
      await manager.allocateNonce('0xWallet1', 'w1-2');
      await manager.allocateNonce('0xWallet2', 'w2-1');

      expect(manager.getPendingTransactions('0xWallet1')).toHaveLength(2);
      expect(manager.getPendingTransactions('0xWallet2')).toHaveLength(1);
    });

    it('getNextNonce returns correct value per wallet', async () => {
      await manager.allocateNonce('0xWallet1', 'w1-1');
      await manager.allocateNonce('0xWallet2', 'w2-1');

      expect(manager.getNextNonce('0xWallet1')).toBe(1);
      expect(manager.getNextNonce('0xWallet2')).toBe(6);
    });
  });

  // ── Wallet nonce reset ──────────────────────────────────────────────

  describe('wallet nonce reset', () => {
    it('resets wallet nonce from on-chain state', async () => {
      await manager.allocateNonce('0xWallet1', 'tx-1');
      await manager.allocateNonce('0xWallet1', 'tx-2');

      provider.setNonce('0xwallet1', 10);
      await manager.resetWalletNonce('0xWallet1');

      expect(manager.getNextNonce('0xWallet1')).toBe(10);
      expect(manager.getPendingTransactions('0xWallet1')).toHaveLength(0);
    });
  });
});

describe('ReorgDetector', () => {
  let provider: MockChainProvider;
  let detector: ReorgDetector;

  beforeEach(() => {
    provider = new MockChainProvider();
    buildChain(provider, 0, 10);
  });

  it('tracks block hashes on initial sync', async () => {
    detector = new ReorgDetector(provider, 100_000);
    await detector.start();
    detector.stop();

    expect(detector.getLatestBlock()).toBe(10);
    expect(detector.getBlockHash(5)).toBe('hash-5');
    expect(detector.getBlockHash(10)).toBe('hash-10');
  });

  it('detects a reorg when block hashes change', async () => {
    detector = new ReorgDetector(provider, 100_000);
    await detector.start();
    detector.stop();

    const reorgEvents: ReorgEvent[] = [];
    detector.onReorg((e) => reorgEvents.push(e));

    // Simulate a 2-block reorg: blocks 9 and 10 get different hashes
    provider.replaceBlock(9, 'fork-9', 'hash-8');
    provider.replaceBlock(10, 'fork-10', 'fork-9');
    // Chain also grows by 1
    provider.addBlock({ number: 11, hash: 'fork-11', parentHash: 'fork-10' });

    await detector.poll();

    expect(reorgEvents).toHaveLength(1);
    expect(reorgEvents[0].depth).toBe(2);
    expect(reorgEvents[0].forkPoint).toBe(9);
    expect(reorgEvents[0].oldHashes).toContain('hash-9');
    expect(reorgEvents[0].oldHashes).toContain('hash-10');
    expect(reorgEvents[0].newHashes).toContain('fork-9');
    expect(reorgEvents[0].newHashes).toContain('fork-10');
  });

  it('does not emit reorg when chain grows normally', async () => {
    detector = new ReorgDetector(provider, 100_000);
    await detector.start();
    detector.stop();

    const reorgEvents: ReorgEvent[] = [];
    detector.onReorg((e) => reorgEvents.push(e));

    // Normal chain growth
    provider.addBlock({ number: 11, hash: 'hash-11', parentHash: 'hash-10' });
    provider.addBlock({ number: 12, hash: 'hash-12', parentHash: 'hash-11' });

    await detector.poll();

    expect(reorgEvents).toHaveLength(0);
    expect(detector.getLatestBlock()).toBe(12);
  });

  it('reports errors via onError', async () => {
    const failingProvider: ChainProvider = {
      getBlockNumber: async () => { throw new Error('RPC down'); },
      getBlock: async () => null,
      getTransactionCount: async () => 0,
    };

    detector = new ReorgDetector(failingProvider, 100_000);
    const errors: Error[] = [];
    detector.onError((e) => errors.push(e));

    await expect(detector.start()).rejects.toThrow('RPC down');
  });
});

describe('NonceManager + ReorgDetector integration', () => {
  let provider: MockChainProvider;
  let manager: NonceManager;

  beforeEach(() => {
    provider = new MockChainProvider();
    buildChain(provider, 0, 10);
    provider.setNonce('0xwallet1', 0);
    provider.setNonce('0xwallet2', 5);

    manager = new NonceManager(provider, {
      confirmationDepth: 3,
      pollIntervalMs: 100_000,
    });
  });

  it('re-queues pending transactions on reorg', async () => {
    // Start and allocate some nonces
    await manager.start();
    manager.stop(); // stop auto-polling

    const a1 = await manager.allocateNonce('0xWallet1', 'tx-1', { value: 100 });
    const a2 = await manager.allocateNonce('0xWallet1', 'tx-2', { value: 200 });

    expect(a1.nonce).toBe(0);
    expect(a2.nonce).toBe(1);
    expect(manager.getPendingTransactions('0xWallet1')).toHaveLength(2);

    // Track events
    const reorgEvents: ReorgEvent[] = [];
    const requeueEvents: RequeueEvent[] = [];
    manager.onReorg((e) => reorgEvents.push(e));
    manager.onRequeue((e) => requeueEvents.push(e));

    // Simulate a 2-block reorg
    provider.replaceBlock(9, 'fork-9', 'hash-8');
    provider.replaceBlock(10, 'fork-10', 'fork-9');
    provider.addBlock({ number: 11, hash: 'fork-11', parentHash: 'fork-10' });

    await manager.poll();

    // Reorg should be detected
    expect(reorgEvents).toHaveLength(1);
    expect(reorgEvents[0].depth).toBe(2);

    // Transactions should be re-queued
    expect(requeueEvents).toHaveLength(1);
    expect(requeueEvents[0].walletAddress).toBe('0xwallet1');
    expect(requeueEvents[0].transactions).toHaveLength(2);
    expect(requeueEvents[0].reason).toBe('reorg');

    // Pending should be cleared
    expect(manager.getPendingTransactions('0xWallet1')).toHaveLength(0);

    // Re-queued transactions are available
    const requeued = manager.getRequeuedTransactions('0xWallet1');
    expect(requeued).toHaveLength(2);
    expect(requeued[0].data).toEqual({ value: 100 });
  });

  it('allows retrying re-queued transactions with fresh nonces', async () => {
    await manager.start();
    manager.stop();

    await manager.allocateNonce('0xWallet1', 'tx-1', { value: 100 });
    await manager.allocateNonce('0xWallet1', 'tx-2', { value: 200 });

    // Trigger reorg
    provider.replaceBlock(9, 'fork-9', 'hash-8');
    provider.replaceBlock(10, 'fork-10', 'fork-9');
    provider.addBlock({ number: 11, hash: 'fork-11', parentHash: 'fork-10' });
    await manager.poll();

    // Retry the re-queued transactions
    const retried = await manager.retryRequeuedTransactions('0xWallet1');

    expect(retried).toHaveLength(2);
    expect(retried[0].nonce).toBe(0); // Re-allocated from on-chain nonce
    expect(retried[1].nonce).toBe(1);

    // Re-queue list should be empty now
    expect(manager.getRequeuedTransactions('0xWallet1')).toHaveLength(0);

    // New pending transactions should exist
    expect(manager.getPendingTransactions('0xWallet1')).toHaveLength(2);
  });

  it('handles reorg with multiple wallets independently', async () => {
    await manager.start();
    manager.stop();

    await manager.allocateNonce('0xWallet1', 'w1-tx-1');
    await manager.allocateNonce('0xWallet1', 'w1-tx-2');
    await manager.allocateNonce('0xWallet2', 'w2-tx-1');

    const requeueEvents: RequeueEvent[] = [];
    manager.onRequeue((e) => requeueEvents.push(e));

    // Trigger reorg
    provider.replaceBlock(10, 'fork-10', 'hash-9');
    provider.addBlock({ number: 11, hash: 'fork-11', parentHash: 'fork-10' });
    await manager.poll();

    // Both wallets should have their transactions re-queued
    expect(requeueEvents).toHaveLength(2);

    const w1Event = requeueEvents.find((e) => e.walletAddress === '0xwallet1');
    const w2Event = requeueEvents.find((e) => e.walletAddress === '0xwallet2');

    expect(w1Event?.transactions).toHaveLength(2);
    expect(w2Event?.transactions).toHaveLength(1);
  });

  it('nonce resets properly after reorg so new allocations are correct', async () => {
    await manager.start();
    manager.stop();

    await manager.allocateNonce('0xWallet1', 'tx-1');
    await manager.allocateNonce('0xWallet1', 'tx-2');
    expect(manager.getNextNonce('0xWallet1')).toBe(2);

    // Trigger reorg
    provider.replaceBlock(10, 'fork-10', 'hash-9');
    provider.addBlock({ number: 11, hash: 'fork-11', parentHash: 'fork-10' });
    await manager.poll();

    // Next nonce should reset to on-chain nonce (0)
    expect(manager.getNextNonce('0xWallet1')).toBe(0);

    // Allocating new nonces starts from 0 again
    const fresh = await manager.allocateNonce('0xWallet1', 'tx-fresh');
    expect(fresh.nonce).toBe(0);
  });
});
