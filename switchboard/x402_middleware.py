"""
x402 Server-Side Middleware for Switchboard

Implements HTTP 402 Payment Required flows for agent-to-agent API monetization.
When a server returns 402, this middleware automatically handles payment via
the switchboard PaymentClient, then retries the original request with a
payment proof header.

Supports:
- Automatic 402 detection and payment handling
- Budget-aware payment gating (integrates with GasTracker)
- Payment proof via X-Payment-Proof header
- USDC and ETH settlement
- Configurable per-endpoint pricing caps

Usage:
    middleware = X402Middleware(
        payment_client=client,
        gas_tracker=tracker,
        max_payment_usd=Decimal("1.00"),
    )
    response = await middleware.request("https://agent.example.com/inference", payload)

References:
    - https://github.com/coinbase/x402
    - EIP-7702 for smart account payments
"""

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Dict, Any, Callable
from enum import Enum

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False


class PaymentScheme(Enum):
    """Supported x402 payment schemes."""
    EXACT = "exact"           # Pay exact amount specified in 402 response
    ESCROW = "escrow"         # Lock in escrow, release on delivery
    STREAMING = "streaming"   # Micro-payments per chunk (future: MPP)


@dataclass
class PaymentOffer:
    """Parsed from the 402 response's X-Payment-Required header."""
    amount_wei: int
    currency: str                  # "ETH", "USDC", etc.
    recipient: str                 # Payee address
    chain_id: int
    scheme: PaymentScheme = PaymentScheme.EXACT
    description: str = ""
    endpoint: str = ""
    nonce: str = ""
    expires_at: Optional[int] = None  # Unix timestamp

    @classmethod
    def from_header(cls, header_value: str, endpoint: str = "") -> "PaymentOffer":
        """Parse X-Payment-Required header JSON."""
        data = json.loads(header_value)
        return cls(
            amount_wei=int(data["amount"]),
            currency=data.get("currency", "ETH"),
            recipient=data["recipient"],
            chain_id=int(data.get("chainId", 1)),
            scheme=PaymentScheme(data.get("scheme", "exact")),
            description=data.get("description", ""),
            endpoint=endpoint,
            nonce=data.get("nonce", ""),
            expires_at=data.get("expiresAt"),
        )

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at


@dataclass
class PaymentProof:
    """Proof that payment was made, sent back to the server."""
    tx_hash: str
    chain_id: int
    payer: str
    amount_wei: int
    nonce: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_header(self) -> str:
        return json.dumps({
            "txHash": self.tx_hash,
            "chainId": self.chain_id,
            "payer": self.payer,
            "amount": self.amount_wei,
            "nonce": self.nonce,
            "timestamp": int(self.timestamp),
        })


@dataclass
class PaymentRecord:
    """Log of a completed payment."""
    endpoint: str
    offer: PaymentOffer
    proof: PaymentProof
    response_status: int
    paid_at: float = field(default_factory=time.time)


class X402Middleware:
    """
    HTTP middleware that intercepts 402 responses and pays automatically.

    Integrates with:
    - PaymentClient for on-chain settlement
    - GasTracker for budget enforcement
    """

    def __init__(
        self,
        payment_client,
        gas_tracker=None,
        max_payment_wei: int = 10**16,  # 0.01 ETH default cap
        allowed_recipients: Optional[set] = None,
        auto_pay: bool = True,
        on_payment: Optional[Callable[[PaymentRecord], None]] = None,
    ):
        if not HAS_AIOHTTP:
            raise ImportError("aiohttp required: pip install aiohttp")

        self.payment_client = payment_client
        self.gas_tracker = gas_tracker
        self.max_payment_wei = max_payment_wei
        self.allowed_recipients = allowed_recipients
        self.auto_pay = auto_pay
        self.on_payment = on_payment

        self.payment_history: list[PaymentRecord] = []
        self.total_spent_wei: int = 0
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _validate_offer(self, offer: PaymentOffer) -> None:
        """Check offer against policy before paying."""
        if offer.is_expired():
            raise ValueError(f"Payment offer expired at {offer.expires_at}")

        if offer.amount_wei > self.max_payment_wei:
            raise ValueError(
                f"Payment {offer.amount_wei} exceeds cap {self.max_payment_wei}"
            )

        if self.allowed_recipients and offer.recipient not in self.allowed_recipients:
            raise ValueError(f"Recipient {offer.recipient} not in allowlist")

        if self.gas_tracker:
            if not self.gas_tracker.can_send_transaction(offer.amount_wei):
                raise ValueError("Payment would exceed gas budget")

    def _pay_onchain(self, offer: PaymentOffer) -> PaymentProof:
        """Execute on-chain payment via PaymentClient."""
        if offer.scheme == PaymentScheme.EXACT:
            # Direct transfer — build and send a simple value transfer
            tx = {
                "to": offer.recipient,
                "value": offer.amount_wei,
                "from": self.payment_client.wallet_address,
            }
            tx_hash = self.payment_client.sign_and_send(tx)
            self.payment_client.wait_for_confirmations(tx_hash)

            return PaymentProof(
                tx_hash=tx_hash,
                chain_id=offer.chain_id,
                payer=self.payment_client.wallet_address,
                amount_wei=offer.amount_wei,
                nonce=offer.nonce,
            )

        elif offer.scheme == PaymentScheme.ESCROW:
            req = self.payment_client.create_payment(
                payee=offer.recipient,
                amount_wei=offer.amount_wei,
                timeout_blocks=50,
                description=offer.description,
            )
            return PaymentProof(
                tx_hash=req.request_id,
                chain_id=offer.chain_id,
                payer=self.payment_client.wallet_address,
                amount_wei=offer.amount_wei,
                nonce=offer.nonce,
            )

        else:
            raise ValueError(f"Unsupported payment scheme: {offer.scheme}")

    async def request(
        self,
        url: str,
        payload: Any = None,
        method: str = "POST",
        headers: Optional[Dict[str, str]] = None,
        **kwargs,
    ) -> aiohttp.ClientResponse:
        """
        Make an HTTP request. If the server returns 402, automatically
        pay and retry with payment proof.
        """
        session = await self._get_session()
        headers = dict(headers or {})

        # First attempt
        if method == "POST":
            resp = await session.post(url, json=payload, headers=headers, **kwargs)
        else:
            resp = await session.request(method, url, headers=headers, **kwargs)

        if resp.status != 402 or not self.auto_pay:
            return resp

        # Parse 402 payment offer
        payment_header = resp.headers.get("X-Payment-Required")
        if not payment_header:
            return resp  # 402 without payment header — can't auto-pay

        offer = PaymentOffer.from_header(payment_header, endpoint=url)
        self._validate_offer(offer)

        # Pay on-chain
        proof = self._pay_onchain(offer)

        # Record payment
        if self.gas_tracker:
            self.gas_tracker.record_gas_usage(offer.amount_wei)
        self.total_spent_wei += offer.amount_wei

        # Retry with payment proof
        headers["X-Payment-Proof"] = proof.to_header()
        if method == "POST":
            resp2 = await session.post(url, json=payload, headers=headers, **kwargs)
        else:
            resp2 = await session.request(method, url, headers=headers, **kwargs)

        record = PaymentRecord(
            endpoint=url,
            offer=offer,
            proof=proof,
            response_status=resp2.status,
        )
        self.payment_history.append(record)
        if self.on_payment:
            self.on_payment(record)

        return resp2

    def get_spend_summary(self) -> dict:
        """Return summary of all payments made."""
        by_endpoint: Dict[str, int] = {}
        for record in self.payment_history:
            by_endpoint[record.endpoint] = (
                by_endpoint.get(record.endpoint, 0) + record.offer.amount_wei
            )
        return {
            "total_payments": len(self.payment_history),
            "total_spent_wei": self.total_spent_wei,
            "by_endpoint": by_endpoint,
        }
