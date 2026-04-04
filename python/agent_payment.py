"""
Agent-to-Agent Payment Client
==============================

Reference Python client for the Switchboard agent payment protocol.
Uses web3.py to interact with the AgentEscrow smart contract.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, List, Optional

from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3
from web3.contract import Contract
from web3.types import TxReceipt

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ETH_ADDRESS = "0x0000000000000000000000000000000000000000"

EIP712_DOMAIN = {
    "name": "SwitchboardEscrow",
    "version": "1",
}

PAYMENT_REQUEST_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "PaymentRequest": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "amount", "type": "uint256"},
        {"name": "token", "type": "address"},
        {"name": "deadline", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
    ],
}

# Minimal ABI covering the functions we call
ESCROW_ABI: List[Dict[str, Any]] = [
    {
        "name": "createEscrow",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {"name": "seller", "type": "address"},
            {"name": "token", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "deadline", "type": "uint256"},
            {"name": "nonce", "type": "uint256"},
            {"name": "approvers", "type": "address[]"},
            {"name": "threshold", "type": "uint256"},
        ],
        "outputs": [{"name": "escrowId", "type": "uint256"}],
    },
    {
        "name": "releaseEscrow",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "escrowId", "type": "uint256"}],
        "outputs": [],
    },
    {
        "name": "refundEscrow",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "escrowId", "type": "uint256"}],
        "outputs": [],
    },
    {
        "name": "approveRelease",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "escrowId", "type": "uint256"}],
        "outputs": [],
    },
    {
        "name": "getEscrow",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "escrowId", "type": "uint256"}],
        "outputs": [
            {"name": "buyer", "type": "address"},
            {"name": "seller", "type": "address"},
            {"name": "token", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "deadline", "type": "uint256"},
            {"name": "nonce", "type": "uint256"},
            {"name": "status", "type": "uint8"},
            {"name": "approvalCount", "type": "uint256"},
            {"name": "threshold", "type": "uint256"},
        ],
    },
    {
        "name": "nextEscrowId",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "EscrowCreated",
        "type": "event",
        "inputs": [
            {"name": "escrowId", "type": "uint256", "indexed": True},
            {"name": "buyer", "type": "address", "indexed": True},
            {"name": "seller", "type": "address", "indexed": True},
            {"name": "token", "type": "address", "indexed": False},
            {"name": "amount", "type": "uint256", "indexed": False},
            {"name": "deadline", "type": "uint256", "indexed": False},
            {"name": "nonce", "type": "uint256", "indexed": False},
        ],
    },
    {
        "name": "EscrowReleased",
        "type": "event",
        "inputs": [
            {"name": "escrowId", "type": "uint256", "indexed": True},
        ],
    },
    {
        "name": "EscrowRefunded",
        "type": "event",
        "inputs": [
            {"name": "escrowId", "type": "uint256", "indexed": True},
        ],
    },
]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class EscrowStatus(IntEnum):
    CREATED = 0
    RELEASED = 1
    REFUNDED = 2


@dataclass
class PaymentRequest:
    """Represents a signed payment request."""

    sender: str
    receiver: str
    amount: int
    token: str = ETH_ADDRESS
    deadline: int = 0
    nonce: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    signature: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": "1.0",
            "type": "payment_request",
            "from": self.sender,
            "to": self.receiver,
            "amount": str(self.amount),
            "token": self.token,
            "deadline": self.deadline,
            "nonce": self.nonce,
            "metadata": self.metadata,
            "signature": self.signature or "",
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


@dataclass
class EscrowInfo:
    """Snapshot of an on-chain escrow record."""

    escrow_id: int
    buyer: str
    seller: str
    token: str
    amount: int
    deadline: int
    nonce: int
    status: EscrowStatus
    approval_count: int
    threshold: int


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class AgentPaymentClient:
    """High-level client for the agent payment protocol."""

    def __init__(
        self,
        web3: Web3,
        escrow_address: str,
        private_key: str,
        abi: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.w3 = web3
        self.private_key = private_key
        self.account = Account.from_key(private_key)
        self.address: str = self.account.address
        self.contract: Contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(escrow_address),
            abi=abi or ESCROW_ABI,
        )

    # -- helpers -------------------------------------------------------------

    def _send_tx(self, tx: Dict[str, Any]) -> TxReceipt:
        """Sign and send a transaction, wait for receipt."""
        tx["from"] = self.address
        tx["nonce"] = self.w3.eth.get_transaction_count(self.address)
        tx["gas"] = self.w3.eth.estimate_gas(tx)
        tx["gasPrice"] = self.w3.eth.gas_price
        signed = self.w3.eth.account.sign_transaction(tx, self.private_key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return self.w3.eth.wait_for_transaction_receipt(tx_hash)

    # -- request creation & signing ------------------------------------------

    def create_payment_request(
        self,
        receiver: str,
        amount: int,
        token: str = ETH_ADDRESS,
        deadline: int = 0,
        nonce: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PaymentRequest:
        """Build an unsigned payment request."""
        if deadline == 0:
            deadline = int(time.time()) + 3600  # 1 hour default
        return PaymentRequest(
            sender=self.address,
            receiver=receiver,
            amount=amount,
            token=token,
            deadline=deadline,
            nonce=nonce,
            metadata=metadata or {},
        )

    def sign_request(self, request: PaymentRequest) -> PaymentRequest:
        """Sign a PaymentRequest using EIP-712 typed data."""
        chain_id = self.w3.eth.chain_id
        domain_data = {
            **EIP712_DOMAIN,
            "chainId": chain_id,
            "verifyingContract": self.contract.address,
        }
        message_data = {
            "from": request.sender,
            "to": request.receiver,
            "amount": request.amount,
            "token": request.token,
            "deadline": request.deadline,
            "nonce": request.nonce,
        }

        full_message = {
            "types": PAYMENT_REQUEST_TYPES,
            "primaryType": "PaymentRequest",
            "domain": domain_data,
            "message": message_data,
        }

        signed = Account.sign_message(
            encode_typed_data(full_message),
            self.private_key,
        )
        request.signature = signed.signature.hex()
        return request

    # -- on-chain interactions -----------------------------------------------

    def submit_escrow(
        self,
        request: PaymentRequest,
        approvers: Optional[List[str]] = None,
        threshold: int = 0,
    ) -> int:
        """Create an on-chain escrow from a PaymentRequest. Returns escrow id."""
        approvers = approvers or []
        is_eth = request.token == ETH_ADDRESS

        tx = self.contract.functions.createEscrow(
            Web3.to_checksum_address(request.receiver),
            Web3.to_checksum_address(request.token),
            request.amount,
            request.deadline,
            request.nonce,
            [Web3.to_checksum_address(a) for a in approvers],
            threshold,
        ).build_transaction(
            {"value": request.amount if is_eth else 0}
        )

        receipt = self._send_tx(tx)

        # Parse EscrowCreated event
        logs = self.contract.events.EscrowCreated().process_receipt(receipt)
        if logs:
            return logs[0]["args"]["escrowId"]

        raise RuntimeError("EscrowCreated event not found in receipt")

    def get_escrow(self, escrow_id: int) -> EscrowInfo:
        """Fetch current state of an escrow."""
        result = self.contract.functions.getEscrow(escrow_id).call()
        return EscrowInfo(
            escrow_id=escrow_id,
            buyer=result[0],
            seller=result[1],
            token=result[2],
            amount=result[3],
            deadline=result[4],
            nonce=result[5],
            status=EscrowStatus(result[6]),
            approval_count=result[7],
            threshold=result[8],
        )

    def release(self, escrow_id: int) -> TxReceipt:
        """Release funds to the seller (buyer only)."""
        tx = self.contract.functions.releaseEscrow(
            escrow_id,
        ).build_transaction({"value": 0})
        return self._send_tx(tx)

    def refund(self, escrow_id: int) -> TxReceipt:
        """Refund funds to the buyer (after deadline)."""
        tx = self.contract.functions.refundEscrow(
            escrow_id,
        ).build_transaction({"value": 0})
        return self._send_tx(tx)

    def approve(self, escrow_id: int) -> TxReceipt:
        """Submit a multi-sig approval."""
        tx = self.contract.functions.approveRelease(
            escrow_id,
        ).build_transaction({"value": 0})
        return self._send_tx(tx)

    def monitor(
        self,
        escrow_id: int,
        poll_interval: int = 5,
        timeout: int = 300,
    ) -> EscrowStatus:
        """Poll escrow status until it leaves Created state or timeout."""
        start = time.time()
        while time.time() - start < timeout:
            info = self.get_escrow(escrow_id)
            if info.status != EscrowStatus.CREATED:
                return info.status
            time.sleep(poll_interval)
        raise TimeoutError(
            f"Escrow {escrow_id} still in Created state after {timeout}s"
        )
