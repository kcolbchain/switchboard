# Multi-Chain Switchboard: Settlement Model

**Status:** Design v0.1
**Tracks issue:** [#59](https://github.com/kcolbchain/switchboard/issues/59)

---

## 1. Goal

Extend the switchboard agent-to-agent payment protocol to three non-EVM chains with incompatible settlement primitives:

| Chain    | Native asset | VM         | Settlement model          |
| -------- | ------------ | ---------- | ------------------------- |
| TRON     | TRX          | TVM (EVM-adapted) | TRC-20 + Energy bandwidth |
| Avalanche| AVAX         | EVM (subnet-aware) | C-Chain native + P/X chain teleporters |
| LUX      | LUX          | EVM + PQ precompiles | Native escrow + PQ signature verification |

Each chain introduces a different verification and finality profile. The switchboard adapter abstracts these differences behind a uniform interface so agent-to-agent payment code does not need per-chain branches.

## 2. Cross-chain message verification

### 2.1 Model: light-client relay vs. notary

| Approach       | Trust model             | Gas cost       | Latency                |
| -------------- | ----------------------- | -------------- | ---------------------- |
| Light-client   | Trustless (validate consensus) | High (header + proof) | Blocks + finality delay |
| Notary / Oracle| Trusted third party     | Low (signature) | ~confirmation time     |
| ZK-relay       | Trustless (validity proof) | Medium (verify proof) | Proof generation delay |

**Recommendation:** Use a notary model for v1 with ZK-relay as the long-term target. Rationale:

- TRON and Avalanche light clients are not production-ready for cross-chain messaging at this granularity.
- LUX has native ZK precompiles; a Groth16 relay proof is viable on LUX today.
- Notary can be upgraded to ZK-relay in-place by replacing the adapter's contract endpoint.

### 2.2 Message envelope

Every cross-chain payment message carries:

```
envelope {
  source_chain:      string     // CAIP-2 (e.g. "eip155:1", "tron:mainnet")
  destination_chain: string
  request_id:        string     // UUIDv4, opaque to settlement
  payload:           bytes      // serialized PaymentRequest / PaymentProof
  source_block:      uint64     // block number on source chain
  source_tx:         bytes32    // tx hash on source chain
  attestation:       bytes      // notary signature or ZK proof
  attestation_type:  uint8      // 0x01 = notary (ECDSA), 0x02 = ZK (Groth16)
}
```

The destination adapter verifies:

1. `attestation_type` matches a registered verifier.
2. `attestation` is valid against the source chain state (notary sig or ZK proof).
3. `request_id` is not already consumed on destination.

### 2.3 TRON verification

TRON uses a delegated-proof-of-stake consensus with 27 super representatives. Finality is ~19 blocks (~57 seconds).

**Verification path:**

1. A switchboard notary node monitors TRON for PaymentCreated events on the AgentEscrow-TRON contract.
2. Notary waits for 19 TRON block confirmations (irreversibility).
3. Notary signs `(source_tx, source_block, request_id, amount, payee)` with its ECDSA key.
4. Destination chain validates the notary signature against a known notary set.

**TRON-specific considerations:**

- TRC-20 tokens require bandwidth/energy for the transfer; the notary MUST include the fee estimate in the envelope.
- TRON's event system exposes logs via gRPC, not JSON-RPC. The adapter needs a gRPC-to-HTTP bridge.
- TRON addresses are base58; the switchboard wire format MUST normalize to lowercase hex for internal processing.

### 2.4 Avalanche verification

Avalanche uses snowman consensus with sub-second finality (~1-2 seconds) on the C-Chain.

**Verification path:**

1. Switchboard monitors C-Chain for events (standard EVM logs).
2. Notary confirms after 5 C-Chain blocks (conservative margin for stale reads).
3. For Avalanche P-Chain or X-Chain payments: teleport the AVAX to C-Chain first via the Avalanche Bridge (native), then process as C-Chain escrow.

**Avalanche-specific considerations:**

- C-Chain is EVM-compatible; the existing AgentEscrow.sol deploys with zero changes.
- Subnet-to-subnet transfers require Avalanche Teleporter. The adapter MUST route payments through Teleporter to reach non-C-Chain subnets.
- P-Chain staking / validation rewards are out of scope for switchboard v1.

### 2.5 LUX verification

LUX is an EVM-compatible chain with PQ precompiles and native ZK verification.

**Verification path:**

1. Events monitored via standard JSON-RPC (EVM compatible).
2. Notary is optional: LUX's PQ precompiles allow the destination contract to verify a ZK proof of the source chain state directly.
3. The ZK proof covers the same fields as the notary attestation but is trustless.

**LUX-specific considerations:**

- `luxfi/threshold` precompile for BLS signature aggregation; can reduce ZK proof cost.
- LUX native escrow (AgentEscrowPQ.sol) supports PQ signatures natively at the contract level.
- The adapter SHOULD prefer PQ-KEM (ML-KEM) for the cross-chain message encryption between agents.

## 3. Settlement finality

### 3.1 Finality table

| Chain    | Consensus          | Finality | Confirmations | Reorg risk |
| -------- | ------------------ | -------- | ------------- | ---------- |
| TRON     | DPoS               | ~57s (19 blocks) | 19       | Low (SR-controlled) |
| Avalanche| Snowman            | ~1-2s (sub-second) | 5      | Very low (probabilistic) |
| LUX      | EVM + PQ staking   | ~12s (1 epoch) | 2            | Very low (PQ bonded) |

### 3.2 Settlement guarantees

The switchboard adapter MUST NOT release the payee's funds until:

1. The source chain has reached its canonical finality threshold.
2. The attestation (notary or ZK) has been verified on the destination chain.
3. A challenge window (configurable per chain pair) has elapsed.

**Configurable settlement parameters:**

```python
# config/settlement.json
{
  "tron:c-chain": {
    "confirmations": 19,
    "challenge_window_blocks": 50,
    "notary_threshold": 3,
    "attestation_type": "notary-ecdsa"
  },
  "avalanche:c-chain": {
    "confirmations": 5,
    "challenge_window_blocks": 20,
    "notary_threshold": 3,
    "attestation_type": "notary-ecdsa"
  },
  "lux:mainnet": {
    "confirmations": 2,
    "challenge_window_blocks": 10,
    "notary_threshold": 0,
    "attestation_type": "zk-groth16"
  }
}
```

### 3.3 Failed settlement handling

If the attestation fails to verify:

1. The adapter retries up to 3 times with exponential backoff (1 min, 5 min, 30 min).
2. After exhaustion, the payment is marked SETTLEMENT_FAILED and a bond is slashed (if notary-based).
3. The payer can reclaim funds on the source chain via the existing refund path (requestRefund after timeout + challenge).

## 4. Adapter pattern

### 4.1 Interface

```python
class ChainAdapter(ABC):
    """Abstract base for a multi-chain settlement adapter."""

    @abstractmethod
    def watch_payments(self, from_block: int) -> List[PaymentEvent]:
        """Poll the chain for new PaymentCreated events."""

    @abstractmethod
    def verify_settlement(self, envelope: CrossChainEnvelope) -> bool:
        """Verify an attestation from another chain."""

    @abstractmethod
    def submit_settlement(self, envelope: CrossChainEnvelope) -> str:
        """Submit a verified settlement to the destination chain; returns tx hash."""

    @abstractmethod
    def finality_blocks(self) -> int:
        """Number of confirmations to wait for finality."""

    @abstractmethod
    def estimate_fee(self, envelope: CrossChainEnvelope) -> int:
        """Estimate the fee in wei (or equivalent) for this settlement."""
```

### 4.2 Adapter registry

Adapters are registered at startup from a config file or environment variable:

```python
ADAPTER_REGISTRY = {
    "tron:mainnet":   TronAdapter,
    "avalanche:c-chain": AvalancheAdapter,
    "lux:mainnet":    LuxAdapter,
}
```

Each adapter instance is initialized with chain-specific RPC endpoints, notary public keys, and gas parameters.

### 4.3 Adapter: TRON

TronAdapter wraps the TronGrid API (or a local full node via gRPC):

- Event polling: wallet/geteventresult via HTTP.
- TX broadcast: wallet/broadcasttransaction via HTTP.
- Address format: base58 to hex conversion in the adapter layer.
- Fee estimation: TRON energy calculator (bandwidth + energy for TRC-20 transfers).

### 4.4 Adapter: Avalanche

AvalancheAdapter uses standard Ethereum JSON-RPC on the C-Chain:

- Same as existing src/payment_protocol.py chain monitoring.
- Teleporter integration for P/X-chain payments via the Avalanche Warp Messaging (AWM) interface.
- Subnet discovery: each subnet has its own RPC URL and chain ID; mapped in chain_registry.json.

### 4.5 Adapter: LUX

LuxAdapter extends the EVM adapter with:

- PQ precompile calls for signature verification.
- ZK proof verification via eth_call to the LUX Verifier contract.
- Native escrow contract (AgentEscrowPQ.sol) interaction for PQ-secured settlements.

## 5. Security considerations

### 5.1 Notary compromise

If a notary's key is compromised, the attacker can forge attestations for any chain the notary covers.

**Mitigations:**

- Threshold notary: M-of-N signatures required (configured per chain pair).
- Notary bonding: each notary posts a bond that is slashed on fraudulent attestation.
- Fraud proof window: a challenge period during which any observer can submit a fraud proof and claim the bond.

### 5.2 Replay attacks

A valid attestation for a given (source_tx, request_id) pair MUST NOT be replayable on multiple destination chains or multiple times on the same chain.

**Mitigations:**

- The envelope includes destination_chain in the signed data.
- The destination contract tracks consumed (source_tx, request_id) pairs.
- The notary signs a unique nonce per attestation.

### 5.3 Cross-chain finality mismatch

If the source chain has a reorg after the notary attests, the attestation references a now-invalid block.

**Mitigations:**

- The notary waits for finality_blocks before attesting.
- On reorg detection, the notary broadcasts a revocation for the affected attestation.
- The destination chain's challenge window allows revocation processing.

### 5.4 Gas oracle manipulation

An attacker could manipulate the gas price oracle to grief settlement submissions.

**Mitigations:**

- Use a decentralized gas oracle (e.g., Chainlink on Avalanche).
- The adapter tracks historical gas prices and rejects outliers > 3 standard deviations from the 24-hour mean.

## 6. Future work

- **ZK-relay for all chains**: replace the notary model with Groth16 proofs for TRON and Avalanche once ZK proving is efficient enough for non-LUX chains.
- **CAIP-2 chain identifiers**: migrate from string chain names to CAIP-2 format.
- **Cross-chain refund**: allow a payer on chain A to trigger a refund on chain B without re-depositing.
- **Dynamic notary set**: on-chain governance for adding/removing notaries.
