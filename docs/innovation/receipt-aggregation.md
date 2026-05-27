# Receipt Aggregation — N Micropayments, 1 On-Chain Settlement

**Status:** Innovation spec v0.1
**Author:** @Gaotax2006
**Tracks issue:** [#43](https://github.com/kcolbchain/switchboard/issues/43)

---

## 1. Problem

An agent that provides high-volume services (data labeling, API calls, compute time) may receive thousands of micropayments per day. Each micropayment costs:

- Gas: ~60k gas for createPayment + confirmPayment on the existing AgentEscrow.sol.
- Time: Each settlement requires at least one block confirmation.
- Overhead: For a $0.01 payment, the gas cost ($0.50-2.00 depending on chain) is 50-200x the payment value.

On-chain settlement at full granularity is economically infeasible for sub-dollar payments.

## 2. Approach: Merkle tree of receipts

Instead of settling each micropayment individually, the parties aggregate N receipts into a Merkle tree and submit only the root on-chain. Individual receipts are verified off-chain; the on-chain transaction settles the net balance.

### 2.1 Receipt format

```
{
  "receipt_id": "uuid",
  "payer": "0x...",
  "payee": "0x...",
  "amount_wei": "1234",
  "description": "API call #42",
  "timestamp": 1716800000,
  "signature": "base64..."   // payee's signature of the receipt content
}
```

### 2.2 Merkle tree

```
tree = MerkleTree()
for receipt in receipts:
    leaf = keccak256(encode(receipt))
    tree.add_leaf(leaf)

root = tree.get_root()  // the on-chain commitment
```

The tree uses standard binary Merkle tree with keccak256(left || right) as the internal node hash. The tree depth is ceil(log2(N)).

### 2.3 Off-chain exchange

The parties maintain a shared receipt list. After each micropayment:

1. Payee sends a signed receipt to the payer.
2. Payer verifies the receipt and acknowledges.
3. Both parties update their local Merkle tree.

When the batch is ready:

1. Payer computes the Merkle root.
2. Payer submits the root on-chain with the total value.

## 3. On-chain settlement

### 3.1 Batch settlement contract

```solidity
interface IBatchSettlement {
    struct Batch {
        bytes32 merkleRoot;
        uint256 totalAmount;
        uint256 receiptCount;
        uint256 createdAt;
        address payee;
        bool settled;
    }

    function submitBatch(
        bytes32 merkleRoot,
        uint256 totalAmount,
        uint256 receiptCount,
        address payee
    ) external payable;

    function settleBatch(bytes32 batchId) external;
    function challengeReceipt(bytes32 batchId, bytes calldata receipt, bytes32[] calldata merkleProof) external;
}
```

### 3.2 Flow

1. submitBatch: Payer sends totalAmount ETH and the Merkle root. Funds are locked.
2. Off-chain: Both parties verify the batch. If valid, payee signs an approval.
3. settleBatch: Payer (or payee) calls this with the payee's approval. Funds released.

### 3.3 Challenge period

If the payee detects an invalid receipt in the batch, they call challengeReceipt with the receipt and Merkle proof within a challenge window. The contract verifies:

1. The receipt's leaf hash exists at the claimed position in the tree.
2. The receipt content is invalid (e.g., wrong amount, wrong parties).

If the challenge succeeds, the batch is rejected and the payer can reclaim funds (minus a penalty). If the challenge fails (invalid proof), the challenger is penalized.

### 3.4 Tally-based settlement (simplified variant)

For trusted agent pairs, a simpler variant avoids the challenge mechanism:

1. Both parties maintain a running tally of net balance.
2. When the tally exceeds a threshold (e.g., $10), the debtor submits a single on-chain settlement.
3. The tally is tracked off-chain with signed receipts.

This is equivalent to a payment channel but does not require a channel factory contract.

## 4. Gas optimization analysis

### 4.1 Per-microtransaction cost breakdown

| Component | Individual escrow | Aggregated (100x) | Aggregated (1000x) |
|-----------|:-----------------:|:-----------------:|:------------------:|
| createPayment gas | 45k x 100 = 4.5M | 1 x 60k = 60k | 1 x 60k = 60k |
| confirmPayment gas | 30k x 100 = 3.0M | 1 x 40k = 40k | 1 x 40k = 40k |
| Off-chain verification | 0 | 100 x ~100 gas eq. | 1000 x ~100 gas eq. |
| **Total** | **7.5M** | **~100k + off-chain** | **~100k + off-chain** |

**Savings at 100x aggregation:** ~98.7% gas reduction.

### 4.2 Break-even point

At current Ethereum mainnet gas prices (50 gwei, $3000/ETH):

| Aggregation | Gas cost | USD cost | Break-even microtransaction value |
|-------------|:--------:|:--------:|:--------------------------------:|
| No aggregation | 7.5M gas | $112.50 | N/A (uneconomical for <$1) |
| 100x batch | 100k gas | $1.50 | $0.015 |
| 1000x batch | 100k gas | $1.50 | $0.0015 |

With 100x aggregation, sub-cent micropayments become economically viable on Ethereum mainnet. On L2 chains (Base, Arbitrum, Optimism) with gas costs 10-100x lower, even single micropayments become viable.

## 5. Security considerations

### 5.1 Merkle proof manipulation

An aggregator could submit a root that excludes some receipts the payee believes were included. The payee detects this during off-chain verification and refuses to sign.

### 5.2 Double-spending of receipts

Each receipt has a unique receipt_id. The payer tracks consumed receipt IDs off-chain. The on-chain batch contract enforces that a given receipt_id can appear in at most one batch (via a used-receipt bitmap).

### 5.3 Challenge mechanism cost

The challenge mechanism costs gas for the challenger. If the challenge is valid, the contract refunds the gas cost plus a bonus. If invalid, the challenger loses a bond.

### 5.4 Batch timeout

If the payer never calls settleBatch, the payee is left waiting. Mitigation: the batch contract includes a batchTimeout after which the payee can force-settle by providing M-of-N payee signatures.

### 5.5 Partial dishonesty

An aggregator could include one invalid receipt among 999 valid ones. The payee's off-chain verification catches this. The challenge mechanism costs gas but is cheaper than processing 1000 individual escrows.

## 6. Implementation sketch

### 6.1 Off-chain: Receipt Manager

```python
class ReceiptManager:
    def __init__(self):
        self.receipts: List[Receipt] = []
        self.tree: MerkleTree | None = None

    def add_receipt(self, receipt: Receipt, counterparty_sig: bytes):
        assert verify_signature(receipt, counterparty_sig)
        self.receipts.append(receipt)
        self.tree = MerkleTree([keccak256(encode(r)) for r in self.receipts])

    def get_batch(self) -> Batch:
        return Batch(
            merkle_root=self.tree.root,
            total_amount=sum(r.amount_wei for r in self.receipts),
            receipt_count=len(self.receipts),
            payee=self.receipts[0].payee,
        )

    def generate_proof(self, receipt: Receipt) -> List[bytes32]:
        return self.tree.get_proof(keccak256(encode(receipt)))
```

### 6.2 On-chain: BatchSettlement.sol

```solidity
contract BatchSettlement {
    mapping(bytes32 => Batch) public batches;
    mapping(bytes32 => bool) public usedReceipts;

    struct Batch {
        bytes32 merkleRoot;
        uint256 totalAmount;
        uint256 receiptCount;
        uint256 createdAt;
        address payer;
        address payable payee;
        bool settled;
    }

    function submitBatch(
        bytes32 merkleRoot,
        uint256 totalAmount,
        uint256 receiptCount,
        address payable payee
    ) external payable {
        require(msg.value == totalAmount, "value mismatch");
        bytes32 batchId = keccak256(abi.encode(msg.sender, payee, merkleRoot, block.timestamp));
        batches[batchId] = Batch(merkleRoot, totalAmount, receiptCount, block.number, msg.sender, payee, false);
        emit BatchSubmitted(batchId, merkleRoot, totalAmount, receiptCount, payee);
    }

    function settleBatch(bytes32 batchId) external {
        Batch storage b = batches[batchId];
        require(!b.settled, "already settled");
        require(msg.sender == b.payer || msg.sender == b.payee, "unauthorized");
        b.settled = true;
        (bool ok, ) = b.payee.call{value: b.totalAmount}("");
        require(ok, "transfer failed");
        emit BatchSettled(batchId);
    }
}
```

## 7. Variant: Tally-based continuous settlement

### 7.1 Mechanism

Instead of discrete batches, the parties maintain a running net tally:

```
tally = 0  # positive = payer owes payee

for each microtransaction:
    receipt = generate_receipt(amount)
    exchange_signatures(receipt)
    tally += amount
    if abs(tally) > THRESHOLD:
        settle_net(tally)
        tally = 0
```

### 7.2 Advantages

- No batching delay: each microtransaction is immediately credited.
- No challenge period: the tally is the source of truth.
- Lower off-chain bookkeeping.

### 7.3 Disadvantages

- Requires ongoing trust between parties (one party could refuse to settle).
- More complex dispute resolution if the tally diverges.

## 8. Open questions

- Merkle tree variant: Standard binary Merkle vs. sparse Merkle tree for sparse receipt sets?
- Gas optimization: Can we use calldata-optimized Merkle proofs?
- Multi-payee batches: Can a batch have multiple payees with a single root?
- Channel vs. batch tradeoff: How does this compare to state channels for the same use case?

## 9. Future work

- Cross-chain receipt aggregation: Aggregate receipts across multiple chains and settle on the cheapest chain.
- Receipt aggregation with privacy: Combine with private x402 (issue #44) for aggregated private payments.
- Automatic threshold adjustment: Agents adapt the batch threshold based on current gas prices and payment volume.
