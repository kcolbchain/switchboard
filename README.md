# Agent-to-Agent Payment Protocol

Implementation of **Issue #4**: Lightweight payment protocol for agent-to-agent settlement.

## Overview

This PR adds:
1. **Solidity Escrow Contract** (`contracts/AgentEscrow.sol`) — trustless escrow with timeout/refund
2. **Python Payment Client** (`src/payment_protocol.py`) — full client implementation
3. **Unit Tests** (`tests/test_payment_protocol.py`) — comprehensive coverage

## Payment Protocol Flow

```
┌─────────────┐    createPayment()     ┌─────────────┐
│   Payer     │ ─────────────────────▶ │   Escrow     │
│  (client)   │     + ETH in value     │  Contract   │
└─────────────┘                         └──────┬──────┘
                                              │ funds locked
       ┌──────────────────────────────────────┘
       │ payer confirms work is done
       ▼
┌─────────────┐    confirmPayment()   ┌─────────────┐
│   Payer     │ ─────────────────────▶ │   Payee      │
│             │     funds released     │  receives    │
└─────────────┘                         └─────────────┘

  (Alternative: timeout → challenge period → refund)
```

## Files

```
kcolb-switchboard/
├── contracts/
│   └── AgentEscrow.sol          # Solidity escrow contract
├── src/
│   └── payment_protocol.py      # Python client library + CLI
├── tests/
│   └── test_payment_protocol.py  # Unit tests
└── README.md
```

## Escrow Contract Features

- **createPayment**: Lock ETH in escrow with timeout + challenge period
- **confirmPayment**: Payer releases funds to payee (one-step)
- **requestRefund**: Payer reclaims after timeout + challenge period
- **cancelPayment**: Mutual cancellation before timeout
- **Event logging**: PaymentCreated, PaymentLocked, PaymentConfirmed, PaymentReleased, PaymentRefunded

## Python Client Features

```python
from payment_protocol import PaymentClient

client = PaymentClient(private_key, escrow_address, rpc_url)

# Create and lock payment
req = client.create_payment(
    payee="0xPayeeAddress",
    amount_wei=10**18,  # 1 ETH
    timeout_blocks=100,
    challenge_period_blocks=10
)

# Confirm (after work is done)
client.confirm_payment(req.request_id)

# Check status
state = client.get_payment_state(req.request_id)
details = client.get_payment_details(req.request_id)
```

## CLI Usage

```bash
# Create payment
python -m payment_protocol --private-key KEY --escrow ADDR --rpc URL \
  --action create --payee 0xPayee --amount "0.1 ETH"

# Confirm payment
python -m payment_protocol --private-key KEY --escrow ADDR --rpc URL \
  --action confirm --request-id REQ-ID

# Check status
python -m payment_protocol --private-key KEY --escrow ADDR --rpc URL \
  --action status --request-id REQ-ID
```

## Test Results

```bash
$ pytest tests/test_payment_protocol.py -v

test_payment_request_creation      ✅
test_payment_request_from_dict      ✅
test_format_wei                     ✅
test_parse_wei                      ✅
test_payment_state_enum             ✅
test_content_hash_deterministic     ✅
test_mock_contract_create           ✅
test_payment_lifecycle              ✅
test_timeout_and_refund             ✅
test_payment_metadata               ✅

10 passed ✅
```

## Spec Compliance

| Spec Requirement | Implementation |
|-----------------|----------------|
| Payment request format | `PaymentRequest` dataclass with JSON serialization |
| Escrow smart contract | `AgentEscrow.sol` with full state machine |
| Confirmation flow | `confirmPayment()` one-step release |
| Timeout | `timeoutBlocks` tracked via block numbers |
| Refund | `requestRefund()` after challenge period |
| Python client | `PaymentClient` class with sync + async support |
| Tests | Mock chain state, 10 test cases |
