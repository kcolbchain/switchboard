# Agent-to-Agent Payment Protocol

## Overview

A lightweight protocol enabling autonomous agents to settle payments between
each other using on-chain escrow.  The flow is trust-minimised: funds are
locked in a smart contract and released only when delivery is confirmed or
the deadline expires.

---

## 1  Payment Request Format

Every payment begins with a signed **Payment Request** JSON object:

```json
{
  "version": "1.0",
  "type": "payment_request",
  "from": "0xBuyerAddress",
  "to": "0xSellerAddress",
  "amount": "1000000000000000000",
  "token": "0x0000000000000000000000000000000000000000",
  "deadline": 1720000000,
  "nonce": 42,
  "metadata": {
    "service": "data-indexing",
    "description": "Index 10 000 blocks"
  },
  "signature": "0x..."
}
```

### Field Reference

| Field       | Type     | Description |
|-------------|----------|-------------|
| `version`   | string   | Protocol version (`"1.0"`). |
| `type`      | string   | Always `"payment_request"`. |
| `from`      | address  | Buyer / payer address. |
| `to`        | address  | Seller / payee address. |
| `amount`    | uint256  | Wei (or smallest token unit) to transfer. |
| `token`     | address  | ERC-20 token address.  `0x0` = native ETH. |
| `deadline`  | uint256  | Unix timestamp after which the buyer can reclaim. |
| `nonce`     | uint256  | Per-sender nonce to prevent replay attacks. |
| `metadata`  | object   | Optional free-form metadata for the service. |
| `signature` | bytes    | EIP-712 typed-data signature by `from`. |

### Signature Scheme (EIP-712)

```
Domain {
  name: "SwitchboardEscrow",
  version: "1",
  chainId: <chain>,
  verifyingContract: <escrow_address>
}

PaymentRequest {
  from: address,
  to: address,
  amount: uint256,
  token: address,
  deadline: uint256,
  nonce: uint256
}
```

---

## 2  Escrow Flow

```
 Buyer                    Escrow Contract               Seller
   |                            |                          |
   |--- createEscrow() ------->|                          |
   |    (deposit funds)         |                          |
   |                            |--- EscrowCreated ------->|
   |                            |                          |
   |                            |          (deliver work)  |
   |                            |                          |
   |--- releaseEscrow() ------>|                          |
   |                            |--- transfer funds ------>|
   |                            |--- EscrowReleased ------>|
   |                            |                          |
   *  OR after deadline:        |                          |
   |--- refundEscrow() ------->|                          |
   |<-- return funds -----------|                          |
   |                            |--- EscrowRefunded ------>|
```

### 2.1  States

| State       | Description |
|-------------|-------------|
| `Created`   | Escrow funded by buyer; seller can begin work. |
| `Released`  | Buyer confirmed delivery; funds sent to seller. |
| `Refunded`  | Deadline passed or dispute resolved; funds returned. |

### 2.2  Create

The buyer calls `createEscrow(to, token, amount, deadline, nonce)` and
attaches the ETH value (or pre-approves ERC-20 tokens).

### 2.3  Release

After the seller delivers the service, the buyer calls
`releaseEscrow(escrowId)`.  Funds are transferred to the seller immediately.

### 2.4  Refund (Timeout)

If `block.timestamp > deadline` and the escrow has not been released, **anyone**
may call `refundEscrow(escrowId)` to return funds to the buyer.  This ensures
buyers are never permanently locked out of their funds.

---

## 3  Multi-Signature Support

For high-value settlements, the protocol supports requiring **M-of-N**
confirmations before release:

1. The buyer creates the escrow with a list of `approvers` and a `threshold`.
2. Each approver calls `approveRelease(escrowId)`.
3. When the approval count reaches the threshold the funds are released.

This enables scenarios such as:
- Buyer + independent oracle must both confirm.
- DAO multi-sig acts as buyer.

---

## 4  Security Considerations

- **Reentrancy** – The escrow contract uses the checks-effects-interactions
  pattern and a reentrancy guard.
- **Replay protection** – Each escrow has a unique `(sender, nonce)` pair.
- **Timeout safety** – A hard deadline ensures funds are never permanently
  locked.
- **Token safety** – ERC-20 transfers use OpenZeppelin `SafeERC20`.

---

## 5  Python Client

See [`python/agent_payment.py`](../python/agent_payment.py) for a reference
client that implements request creation, signing, on-chain submission, status
monitoring, and claim/refund helpers.

---

## 6  Reference Implementations

| Component | Path |
|-----------|------|
| Solidity Escrow | `contracts/AgentEscrow.sol` |
| Python Client   | `python/agent_payment.py` |
| Solidity Tests  | `tests/test_agent_escrow.sol` |
| Python Tests    | `tests/test_agent_payment.py` |
