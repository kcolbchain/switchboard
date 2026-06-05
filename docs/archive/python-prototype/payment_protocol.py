"""
Agent-to-Agent Payment Protocol - Python Client

Implements the payment protocol for agent-to-agent settlement:
- Payment request format (RFC-style)
- Escrow smart contract interaction via Web3.py
- Confirmation flow with timeout/refund
- Async/concurrent payment management

Usage:
    client = PaymentClient(wallet_private_key, escrow_address, rpc_url)
    request_id = await client.create_payment(payee_address, amount_wei, timeout_blocks=100)
    await client.confirm_payment(request_id)  # after work is done
"""

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional, Dict, List, Callable
from decimal import Decimal

try:
    from web3 import Web3, AsyncWeb3
    from eth_account import Account
    HAS_WEB3 = True
except ImportError:
    HAS_WEB3 = False
    AsyncWeb3 = None
    Account = None


# ─── Payment Request Format ─────────────────────────────────────────────────

@dataclass
class PaymentRequest:
    """RFC-style payment request message"""
    version: str = "1.0"
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    payer: str = ""           # Ethereum address (checksummed)
    payee: str = ""           # Ethereum address (checksummed)
    amount_wei: int = 0       # Amount in wei
    amount_usd: Optional[Decimal] = None  # Optional USD equivalent
    currency: str = "ETH"     # ETH, USDT, USDC, etc.
    chain_id: int = 1         # Ethereum chain ID
    timeout_blocks: int = 100 # Blocks until payment expires
    challenge_period_blocks: int = 10  # Blocks payer waits before reclaim
    description: str = ""     # Human-readable description
    metadata: Dict = field(default_factory=dict)  # Arbitrary extra data
    created_at: float = field(default_factory=time.time)
    status: str = "pending"   # pending, locked, confirmed, released, refunded, cancelled

    def to_json(self) -> str:
        """Serialize to JSON for signing/transmission"""
        d = asdict(self)
        # Convert Decimal to string for JSON
        if self.amount_usd is not None:
            d['amount_usd'] = str(self.amount_usd)
        return json.dumps(d, sort_keys=True, separators=(',', ':'))

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.amount_usd is not None:
            d['amount_usd'] = str(self.amount_usd)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> 'PaymentRequest':
        d = dict(d)
        if d.get('amount_usd'):
            d['amount_usd'] = Decimal(d['amount_usd'])
        return cls(**d)

    def content_hash(self) -> str:
        """Calculate content-based hash for integrity check"""
        h = hashlib.sha256()
        h.update(self.to_json().encode('utf-8'))
        return "0x" + h.hexdigest()


# ─── Payment Protocol States ────────────────────────────────────────────────

class PaymentState(Enum):
    PENDING = "pending"
    LOCKED = "locked"       # Funds in escrow
    CONFIRMED = "confirmed"  # Payer approved
    RELEASED = "released"   # Payee received funds
    REFUNDED = "refunded"   # Payer reclaimed funds
    CANCELLED = "cancelled"
    EXPIRED = "expired"     # Timed out, awaiting refund window


# ─── Escrow ABI ─────────────────────────────────────────────────────────────

ESCROW_ABI = [
    {
        "inputs": [],
        "name": "chainId",
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "requestId", "type": "string"},
            {"name": "payee", "type": "address"},
            {"name": "timeoutBlocks", "type": "uint256"},
            {"name": "challengePeriod", "type": "uint256"}
        ],
        "name": "createPayment",
        "outputs": [{"type": "bool"}],
        "stateMutability": "payable",
        "type": "function"
    },
    {
        "inputs": [{"name": "requestId", "type": "string"}],
        "name": "confirmPayment",
        "outputs": [{"type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"name": "requestId", "type": "string"}],
        "name": "requestRefund",
        "outputs": [{"type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"name": "requestId", "type": "string"}],
        "name": "cancelPayment",
        "outputs": [{"type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"name": "requestId", "type": "string"}],
        "name": "getPayment",
        "outputs": [
            {
                "components": [
                    {"name": "payer", "type": "address"},
                    {"name": "payee", "type": "address"},
                    {"name": "amount", "type": "uint256"},
                    {"name": "timeoutBlocks", "type": "uint256"},
                    {"name": "challengePeriod", "type": "uint256"},
                    {"name": "state", "type": "uint8"},
                    {"name": "requestId", "type": "string"},
                    {"name": "createdAt", "type": "uint256"}
                ],
                "type": "tuple",
                "name": "",
                "internalType": "struct AgentEscrow.Payment"
            }
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"name": "requestId", "type": "string"}],
        "name": "isState",
        "outputs": [{"type": "bool"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"name": "requestId", "type": "string"}],
        "name": "isExpired",
        "outputs": [{"type": "bool"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"name": "agent", "type": "address"}],
        "name": "registerAgent",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "anonymous": False,
        "inputs": [
            {"name": "requestId", "type": "string", "indexed": True},
            {"name": "payer", "type": "address", "indexed": True},
            {"name": "payee", "type": "address", "indexed": True},
            {"name": "amount", "type": "uint256"}
        ],
        "name": "PaymentCreated",
        "type": "event"
    },
    {
        "anonymous": False,
        "inputs": [
            {"name": "requestId", "type": "string", "indexed": True},
            {"name": "payee", "type": "address", "indexed": True},
            {"name": "amount", "type": "uint256"}
        ],
        "name": "PaymentReleased",
        "type": "event"
    },
    {
        "anonymous": False,
        "inputs": [
            {"name": "requestId", "type": "string", "indexed": True},
            {"name": "payer", "type": "address", "indexed": True},
            {"name": "amount", "type": "uint256"}
        ],
        "name": "PaymentRefunded",
        "type": "event"
    }
]


# ─── Payment Client ─────────────────────────────────────────────────────────

class PaymentClient:
    """
    Main client for the agent-to-agent payment protocol.
    
    Handles:
    - Wallet management
    - Escrow contract interaction
    - Payment state tracking
    - Event monitoring
    - Timeout/refund flows
    """

    STATE_MAP = {0: "Created", 1: "Locked", 2: "Confirmed", 3: "Released", 4: "Refunded", 5: "Cancelled"}

    def __init__(
        self,
        private_key: str,
        escrow_address: str,
        rpc_url: str,
        chain_id: int = 1,
        confirmations: int = 2,
        gas_buffer_wei: int = 50000
    ):
        if not HAS_WEB3:
            raise ImportError("web3.py is required: pip install web3 eth-account")
        
        self.account = Account.from_key(private_key)
        self.wallet_address = self.account.address
        self.escrow_address = Web3.to_checksum_address(escrow_address)
        self.chain_id = chain_id
        self.confirmations = confirmations
        self.gas_buffer_wei = gas_buffer_wei

        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.contract = self.w3.eth.contract(
            address=self.escrow_address,
            abi=ESCROW_ABI
        )

        # Track pending payments locally
        self.pending_payments: Dict[str, PaymentRequest] = {}
        self._nonce_cache: Dict[str, int] = {}

    # ─── Wallet Operations ─────────────────────────────────────────────────

    def get_nonce(self, force_refresh: bool = False) -> int:
        """Get next nonce for wallet, with caching for concurrent txns"""
        if force_refresh or self.wallet_address not in self._nonce_cache:
            self._nonce_cache[self.wallet_address] = self.w3.eth.get_transaction_count(self.wallet_address)
        else:
            self._nonce_cache[self.wallet_address] += 1
        return self._nonce_cache[self.wallet_address]

    def get_gas_price(self) -> int:
        return self.w3.eth.gas_price

    def sign_and_send(self, tx: dict) -> str:
        """Sign transaction with wallet and send"""
        nonce = tx.get('nonce', self.get_nonce())
        tx['nonce'] = nonce
        tx['gas'] = int(tx.get('gas', 300000) * 1.2)
        tx['gasPrice'] = tx.get('gasPrice', self.get_gas_price())
        tx['chainId'] = self.chain_id

        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex()

    def wait_for_confirmations(self, tx_hash: str, confirmations: int = None) -> dict:
        """Wait for transaction to be confirmed"""
        confirmations = confirmations or self.confirmations
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        if receipt.status == 0:
            raise RuntimeError(f"Transaction {tx_hash} failed")
        return receipt

    # ─── Payment Operations ────────────────────────────────────────────────

    def create_payment(
        self,
        payee: str,
        amount_wei: int,
        timeout_blocks: int = 100,
        challenge_period_blocks: int = 10,
        request_id: str = None,
        description: str = "",
        metadata: dict = None
    ) -> PaymentRequest:
        """
        Create a payment request and lock funds in escrow.
        
        Steps:
        1. Build PaymentRequest object
        2. Build createPayment() transaction
        3. Sign and send with value=amount
        4. Wait for confirmation
        5. Store in local tracking
        """
        request_id = request_id or str(uuid.uuid4())
        payee_checksum = Web3.to_checksum_address(payee)

        # Build on-chain transaction
        tx = self.contract.functions.createPayment(
            request_id,
            payee_checksum,
            timeout_blocks,
            challenge_period_blocks
        ).build_transaction({
            'from': self.wallet_address,
            'value': amount_wei
        })

        # Sign and send
        tx_hash = self.sign_and_send(tx)
        receipt = self.wait_for_confirmations(tx_hash)

        # Build payment request object
        payment_req = PaymentRequest(
            request_id=request_id,
            payer=self.wallet_address,
            payee=payee_checksum,
            amount_wei=amount_wei,
            timeout_blocks=timeout_blocks,
            challenge_period_blocks=challenge_period_blocks,
            description=description,
            metadata=metadata or {},
            status="locked"
        )
        self.pending_payments[request_id] = payment_req
        return payment_req

    def confirm_payment(self, request_id: str) -> bool:
        """
        Confirm a payment - releases funds from escrow to payee.
        Called by payer AFTER work is verified/done.
        """
        tx = self.contract.functions.confirmPayment(request_id).build_transaction({
            'from': self.wallet_address
        })
        tx_hash = self.sign_and_send(tx)
        self.wait_for_confirmations(tx_hash)

        if request_id in self.pending_payments:
            self.pending_payments[request_id].status = "confirmed"
        return True

    def request_refund(self, request_id: str) -> bool:
        """
        Request refund after timeout + challenge period has passed.
        Can only be called by original payer.
        """
        # Check if refund is available
        payment_info = self.contract.functions.getPayment(request_id).call()
        state_num = payment_info[5]
        created_at = payment_info[7]

        current_block = self.w3.eth.block_number
        timeout_blocks = payment_info[3]
        challenge_period = payment_info[4]

        if current_block < created_at + timeout_blocks + challenge_period:
            raise RuntimeError(
                f"Challenge period not over. "
                f"Available at block {created_at + timeout_blocks + challenge_period}, "
                f"current: {current_block}"
            )

        tx = self.contract.functions.requestRefund(request_id).build_transaction({
            'from': self.wallet_address
        })
        tx_hash = self.sign_and_send(tx)
        self.wait_for_confirmations(tx_hash)

        if request_id in self.pending_payments:
            self.pending_payments[request_id].status = "refunded"
        return True

    def cancel_payment(self, request_id: str) -> bool:
        """
        Cancel a payment by mutual agreement.
        Can only be called by payer when state is Locked.
        """
        tx = self.contract.functions.cancelPayment(request_id).build_transaction({
            'from': self.wallet_address
        })
        tx_hash = self.sign_and_send(tx)
        self.wait_for_confirmations(tx_hash)

        if request_id in self.pending_payments:
            self.pending_payments[request_id].status = "cancelled"
        return True

    # ─── State Queries ─────────────────────────────────────────────────────

    def get_payment_state(self, request_id: str) -> str:
        """Query on-chain payment state"""
        try:
            payment_info = self.contract.functions.getPayment(request_id).call()
            return self.STATE_MAP.get(payment_info[5], f"Unknown({payment_info[5]})")
        except Exception:
            return "NotFound"

    def is_expired(self, request_id: str) -> bool:
        """Check if payment has passed its timeout block"""
        return self.contract.functions.isExpired(request_id).call()

    def get_payment_details(self, request_id: str) -> dict:
        """Get full payment details from escrow"""
        info = self.contract.functions.getPayment(request_id).call()
        return {
            "payer": info[0],
            "payee": info[1],
            "amount_wei": info[2],
            "timeout_blocks": info[3],
            "challenge_period": info[4],
            "state": self.STATE_MAP.get(info[5], str(info[5])),
            "request_id": info[6],
            "created_at_block": info[7]
        }

    def get_balance(self) -> int:
        """Get wallet ETH balance"""
        return self.w3.eth.get_balance(self.wallet_address)

    def get_escrow_balance(self, request_id: str) -> int:
        """Get amount locked in a specific escrow"""
        info = self.get_payment_details(request_id)
        return info["amount_wei"]

    # ─── Event Monitoring ──────────────────────────────────────────────────

    def watch_payment(self, request_id: str, callback: Callable[[dict], None], poll_interval: int = 5):
        """
        Watch a payment and call callback on state changes.
        Example: client.watch_payment("req-123", lambda e: print(f"Payment {e['event']}!"))
        """
        last_state = self.get_payment_state(request_id)
        while True:
            current_state = self.get_payment_state(request_id)
            if current_state != last_state:
                last_state = current_state
                callback({"request_id": request_id, "event": current_state})
            time.sleep(poll_interval)


# ─── Async Version ──────────────────────────────────────────────────────────

class AsyncPaymentClient(PaymentClient):
    """Async version using AsyncWeb3"""

    async def create_payment_async(self, payee: str, amount_wei: int, **kwargs) -> PaymentRequest:
        """Async version of create_payment"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.create_payment, payee, amount_wei, kwargs)

    async def confirm_payment_async(self, request_id: str) -> bool:
        """Async version of confirm_payment"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.confirm_payment, request_id)

    async def wait_for_confirmations_async(self, tx_hash: str) -> dict:
        """Async wait for transaction confirmation"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: self.wait_for_confirmations(tx_hash)
        )


# ─── Convenience Functions ─────────────────────────────────────────────────

def format_wei(wei: int, currency: str = "ETH") -> str:
    """Format wei as human-readable currency amount"""
    eth = Decimal(wei) / Decimal(10**18)
    if currency == "ETH":
        return f"{eth:.6f} ETH"
    elif currency in ("USDT", "USDC"):
        return f"{eth:.2f} {currency}"
    return f"{eth:.6f}"


def parse_wei(amount: str) -> int:
    """Parse human-readable amount to wei. e.g. "0.5 ETH" → wei"""
    parts = amount.strip().split()
    if len(parts) == 2:
        num_str, currency = parts
    else:
        num_str = parts[0]
        currency = "ETH"
    
    multiplier = {
        "ETH": 10**18,
        "wei": 1,
        "KETH": 10**21,
    }.get(currency.upper(), 10**18)
    
    return int(Decimal(num_str) * Decimal(multiplier))


# ─── CLI Usage ─────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Agent-to-Agent Payment Client")
    parser.add_argument("--private-key", required=True, help="Wallet private key")
    parser.add_argument("--escrow", required=True, help="Escrow contract address")
    parser.add_argument("--rpc", required=True, help="RPC URL")
    parser.add_argument("--chain-id", type=int, default=1, help="Chain ID")
    parser.add_argument("--action", required=True, choices=["create", "confirm", "refund", "cancel", "status"])
    parser.add_argument("--request-id", help="Payment request ID")
    parser.add_argument("--payee", help="Payee address (for create)")
    parser.add_argument("--amount", help="Amount in ETH (for create), e.g. '0.1 ETH'")
    parser.add_argument("--timeout", type=int, default=100, help="Timeout in blocks")
    parser.add_argument("--challenge", type=int, default=10, help="Challenge period in blocks")

    args = parser.parse_args()

    client = PaymentClient(args.private_key, args.escrow, args.rpc, args.chain_id)

    if args.action == "create":
        amount_wei = parse_wei(args.amount)
        payee = args.payee
        req = client.create_payment(
            payee, amount_wei,
            timeout_blocks=args.timeout,
            challenge_period_blocks=args.challenge
        )
        print(f"Created payment: {req.request_id}")
        print(f"Amount: {format_wei(req.amount_wei)}")
        print(f"Payer: {req.payer}")
        print(f"Payee: {req.payee}")

    elif args.action == "confirm":
        client.confirm_payment(args.request_id)
        print(f"Payment {args.request_id} confirmed and released")

    elif args.action == "refund":
        client.request_refund(args.request_id)
        print(f"Payment {args.request_id} refunded")

    elif args.action == "cancel":
        client.cancel_payment(args.request_id)
        print(f"Payment {args.request_id} cancelled")

    elif args.action == "status":
        state = client.get_payment_state(args.request_id)
        details = client.get_payment_details(args.request_id)
        print(f"State: {state}")
        print(f"Details: {json.dumps(details, indent=2)}")


if __name__ == "__main__":
    main()
