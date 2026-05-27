# Private x402 — Pay an Agent Without Revealing the Amount

**Status:** Innovation spec v0.1
**Author:** @Gaotax2006
**Tracks issue:** [#44](https://github.com/kcolbchain/switchboard/issues/44)

---

## 1. Problem

The existing x402 payment flow reveals three things to any blockchain observer:

1. Who paid whom. Both payer and payee addresses are in cleartext on-chain.
2. How much. amount_wei is public in the escrow event.
3. That a payment happened at all. The event signature is distinctive.

For agent-to-agent payments, these leaks are acceptable for many use cases. However, some agent relationships require confidentiality:

- A reputation oracle paying agents for data: the per-agent amount reveals the pricing model.
- A DAO paying agents for governance work: the per-agent breakdown reveals influence patterns.
- Competitive bidding: an agent winning a contract doesn't want competitors to see the winning price.

## 2. Goals

1. Amount privacy: On-chain record shows a commitment, not the plaintext amount.
2. Payee privacy: The payee address is derived from a stealth key, not the agent's static address.
3. Transfer log confidentiality: The off-chain log is encrypted; only authorized parties can read it.
4. Verifiability: The payer and payee can independently verify the correct amount was escrowed and released.

## 3. Non-goals

- Hiding that a payment occurred (payments are pseudonymous, not anonymous).
- On-chain ZK verification of arbitrary conditions (outsourced to future work).
- Regulatory compliance (KYC/AML is out of scope for this spec).

## 4. Stealth addresses

### 4.1 Key derivation

Each agent has a static keypair (sk, pk) used for identity. For each payment, a one-time stealth address is derived:

```
// Payer side
ephemeral_key = random_scalar()
ephemeral_pub = ephemeral_key * G
shared_secret = keccak256(ephemeral_key * payee_pk)
stealth_address = payee_address + shared_secret[0..20]

// Payee side (recovery)
shared_secret = keccak256(payee_sk * ephemeral_pub)
stealth_address = payee_address + shared_secret[0..20]
```

### 4.2 On-chain footprint

```
event PrivatePaymentCreated(
    bytes32 indexed requestId,
    address stealthPayee,
    bytes ephemeralPub,
    bytes32 amountCommitment
);
```

The stealthPayee is the one-time address; the real payee address is never revealed on-chain. ephemeralPub allows the payee to derive the shared secret and recognize the payment. amountCommitment is a Pedersen commitment.

### 4.3 Scanning

The payee scans for PrivatePaymentCreated events, attempts to derive a stealth address from each ephemeralPub, and checks if it matches stealthPayee. Complexity: O(n) per scan, where n is the number of events since last scan. For a high-throughput agent, this can be optimized with a bloom filter.

## 5. Zero-knowledge amounts

### 5.1 Pedersen commitment

```
commitment = H(amount_wei * G + blinding_factor * H)
```

Where G and H are independent generators of an elliptic curve (e.g., secp256k1 with a NUMS H). The blinding_factor is a random scalar known only to the payer.

### 5.2 ZK range proof

To prevent the payee from being griefed by a zero-value commitment, the payer generates a range proof showing the committed amount is in a valid range (e.g., 1 wei to 1000 ETH):

```
range_proof = prove_in_range(commitment, amount_wei, 0, MAX_AMOUNT, blinding_factor)
```

### 5.3 Verification

The escrow contract stores amountCommitment but does not verify the range proof on-chain (gas cost is prohibitive). Instead:

1. The payee verifies the range proof off-chain.
2. On confirmPayment, both parties agree on the amount off-chain.
3. A dispute protocol (optional) would open the commitment on-chain.

### 5.4 Opening

When releasing funds, the payer reveals (amount_wei, blinding_factor) off-chain to the payee. The payee verifies:

```
commitment == H(amount_wei * G + blinding_factor * H)
```

## 6. Encrypted transfer logs

### 6.1 Log structure

Each private payment produces an encrypted log entry stored off-chain (IPFS or a DID-linked endpoint):

```
{
  "requestId": "uuid",
  "chainId": 1,
  "contractAddress": "0x...",
  "payer": "0x...",
  "payee": "0x...",
  "stealthAddress": "0x...",
  "amountWei": "1234000000000000000",
  "amountUsd": "3.45",
  "blindingFactor": "0xdeadbeef...",
  "timestamp": 1716800000,
  "description": "data labeling batch #42"
}
```

### 6.2 Encryption

The log is encrypted with a symmetric key derived from both parties:

```
encryption_key = keccak256(shared_secret || request_id)
encrypted_log = aes-256-gcm-encrypt(log_json, encryption_key)
```

The encrypted_log is stored on IPFS; the CID is emitted as an event field.

### 6.3 Access control

- The payer and payee can decrypt using their shared secret.
- Third parties cannot decrypt without the shared secret.
- For audit / dispute, either party can reveal the decryption key selectively.

## 7. Modified escrow contract

### 7.1 Additional interface

```solidity
interface IPrivateAgentEscrow {
    function createPrivatePayment(
        bytes32 requestId,
        address stealthPayee,
        bytes calldata ephemeralPub,
        bytes32 amountCommitment,
        bytes calldata encryptedLogCid
    ) external payable;

    function confirmPrivatePayment(
        bytes32 requestId,
        uint256 actualAmountWei,
        bytes32 blindingFactor
    ) external;
}
```

### 7.2 State machine differences

- createPrivatePayment locks msg.value but records amountCommitment instead of amount.
- confirmPrivatePayment validates that actualAmountWei + blindingFactor commit to the stored amountCommitment. If valid, releases actualAmountWei to the payee and the remainder (if any) back to the payer.
- Overpayment is refunded: if msg.value > committed_amount, the surplus is returned on confirmPrivatePayment.

### 7.3 Gas cost

The private variant is ~15-20% more expensive due to extra storage (commitment, ephemeral pub, encrypted log CID). For high-privacy payments this is acceptable; for routine micropayments the base AgentEscrow should be used.

## 8. Protocol flow

```
Payer                          Payee
  |                               |
  |  generate ephemeral key,      |
  |  compute stealth_address,     |
  |  build commitment             |
  |                               |
  |  createPrivatePayment{value}  |
  |------------------------------>| (event emitted)
  |                               |
  |  store encrypted log on IPFS  |
  |------------------------------>|
  |                               |
  |  payee scans events, derives  |
  |  shared secret, matches addr  |
  |                               |
  |  payee verifies range proof   |
  |  and decrypts log             |
  |                               |
  |  confirmPrivatePayment        |
  |------------------------------>| funds released
  |                               |
```

## 9. Security considerations

### 9.1 Replay of ephemeral keys

The payee MUST check that each ephemeralPub is unique to prevent replay of the same shared secret. The contract enforces requestId uniqueness at the mapper level.

### 9.2 Griefing via invalid commitments

A payer could commit to value X but lock a different amount. The payee discovers this during the off-chain verification and simply refuses to confirm. The payer's funds are stuck until the timeout + challenge period elapses.

### 9.3 Range proof verification

The off-chain range proof verification MUST be computationally sound to prevent the payer from committing a negative amount (which would allow overpayment extraction on confirm). Recommended: Bulletproofs via libsodium-wasm or a WASM-compiled dalek-bulletproofs.

### 9.4 Stealth address collision

Two payments to the same payee from different payers produce different stealth addresses (different ephemeral_key). The probability of collision is negligible (~2^-160).

## 10. Open questions

- Range proof library: Bulletproofs is gas-expensive on-chain. Off-chain verification removes this constraint. Which WASM-compatible library?
- Log storage: IPFS vs. Arweave vs. DID endpoint. IPFS is the default for v0.1.
- Key rotation: Agents should rotate static keys periodically. How does this affect stealth address derivation?
- ERC-4337 integration: Can the private escrow be used with account abstraction for gas sponsorship?

## 11. Future work

- On-chain range proof: Once L2 ZK precompiles are widespread, verify range proofs on-chain.
- Threshold payee: Split the shared secret among M-of-N payees for multi-agent payments.
- Stealth refund: Allow refunds to the payer's original address without revealing it.
