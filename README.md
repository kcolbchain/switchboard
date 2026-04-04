# switchboard

> AI x Blockchain agent infrastructure — agent wallets, autonomous payments, cross-chain execution

**kcolbchain** — open-source blockchain tools and research since 2015.

## Status

Early development. Looking for contributors! See [open issues](https://github.com/kcolbchain/switchboard/issues) for ways to help.

## Quick Start

```bash
git clone https://github.com/kcolbchain/switchboard.git
cd switchboard
npm install
npm run build
npm test
```

## Nonce Manager

Reliable nonce management for agents sending concurrent blockchain transactions. Includes reorg detection and automatic transaction re-queuing.

### Features

- **Per-wallet nonce tracking** — mutex-based serialization prevents nonce collisions
- **Reorg detection** — block hash monitoring detects chain reorganizations
- **Transaction re-queuing** — invalidated transactions are automatically queued for retry
- **Configurable confirmation depth** — tune finality requirements per deployment
- **Event-driven** — callbacks for reorg detection, nonce allocation, and tx re-queue

### Usage

```typescript
import { NonceManager, ChainProvider } from '@switchboard/nonce-manager';

// Implement ChainProvider for your chain (ethers.js, viem, etc.)
const provider: ChainProvider = {
  getBlockNumber: () => ethersProvider.getBlockNumber(),
  getBlock: (n) => ethersProvider.getBlock(n),
  getTransactionCount: (addr) => ethersProvider.getTransactionCount(addr),
};

const manager = new NonceManager(provider, {
  confirmationDepth: 12,
  pollIntervalMs: 2000,
});

// Listen for events
manager.onReorg((event) => {
  console.log(`Reorg detected at block ${event.forkPoint}, depth: ${event.depth}`);
});

manager.onRequeue((event) => {
  console.log(`${event.transactions.length} txns re-queued for ${event.walletAddress}`);
});

// Start monitoring
await manager.start();

// Allocate nonces concurrently — safe for parallel use
const [nonce1, nonce2] = await Promise.all([
  manager.allocateNonce('0xWallet', 'tx-1', { to: '0xRecipient', value: 100 }),
  manager.allocateNonce('0xWallet', 'tx-2', { to: '0xRecipient', value: 200 }),
]);

// Confirm when mined
await manager.confirmTransaction('0xWallet', nonce1.nonce);

// After a reorg, retry re-queued transactions
const retried = await manager.retryRequeuedTransactions('0xWallet');
```

### Architecture

```
NonceManager
├── Per-wallet state (mutex + nonce counter + pending txns)
├── ReorgDetector (block hash polling + fork detection)
└── Event emitters (reorg, requeue, nonceAllocated, error)
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to get started. Issues tagged `good-first-issue` are great entry points.

## Links

- **Docs:** https://docs.kcolbchain.com/switchboard/
- **All projects:** https://docs.kcolbchain.com/
- **kcolbchain:** https://kcolbchain.com

## License

MIT

---

*Founded by [Abhishek Krishna](https://abhishekkrishna.com) • GitHub: [@abhicris](https://github.com/abhicris)*
