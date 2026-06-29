"""SafeSwap orchestrator client (mockable).

In the demo, after Agent B is paid for its work, it routes the received token
through **SafeSwap** — an external best-execution swap orchestrator — to rebalance
into a target asset (e.g. swap the inbound USDC into ETH for gas, or into a yield
asset).

This module exposes a tiny client against SafeSwap's orchestrator HTTP API plus an
in-process ``MockSafeSwapOrchestrator`` so the whole flow is runnable and testable
with **no network**. The contract between the two is the ``SafeSwapClient`` surface:

    quote = client.quote(SwapRequest(...))   ->  SwapQuote
    receipt = client.execute(quote)           ->  SwapReceipt

A real deployment would point ``SafeSwapClient(base_url=...)`` at the live
orchestrator; the demo and tests inject ``transport=MockSafeSwapOrchestrator()``.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol


# ─── Wire types ──────────────────────────────────────────────────────────────


@dataclass
class SwapRequest:
    """A request to route ``amount_in`` of ``token_in`` into ``token_out``."""

    token_in: str
    token_out: str
    amount_in: int  # base units of token_in (e.g. wei / 6-dp USDC units)
    chain_id: int = 8453
    recipient: str = ""
    slippage_bps: int = 50  # 0.50% default max slippage
    deadline_s: int = 120

    def to_dict(self) -> dict:
        return {
            "tokenIn": self.token_in,
            "tokenOut": self.token_out,
            "amountIn": str(self.amount_in),
            "chainId": self.chain_id,
            "recipient": self.recipient,
            "slippageBps": self.slippage_bps,
            "deadlineS": self.deadline_s,
        }


@dataclass
class SwapQuote:
    """A priced route returned by the orchestrator. Must be ``execute``-d to settle."""

    quote_id: str
    token_in: str
    token_out: str
    amount_in: int
    amount_out: int  # quoted output in base units of token_out
    route: list[str]  # venue path, e.g. ["UniswapV3", "Curve"]
    price: str  # human-readable token_out per token_in
    fee_bps: int
    expires_at: int

    def is_expired(self, now: float | None = None) -> bool:
        return (now or time.time()) > self.expires_at


@dataclass
class SwapReceipt:
    """Proof a routed swap settled."""

    quote_id: str
    tx_hash: str
    token_in: str
    token_out: str
    amount_in: int
    amount_out: int
    route: list[str]
    settled_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "quoteId": self.quote_id,
            "txHash": self.tx_hash,
            "tokenIn": self.token_in,
            "tokenOut": self.token_out,
            "amountIn": str(self.amount_in),
            "amountOut": str(self.amount_out),
            "route": self.route,
            "settledAt": int(self.settled_at),
        }


class SafeSwapError(RuntimeError):
    """Raised when a quote or execution fails."""


# ─── Transport protocol ──────────────────────────────────────────────────────


class SafeSwapTransport(Protocol):
    """Minimal transport SafeSwapClient drives. The real impl is HTTP; the test
    impl is :class:`MockSafeSwapOrchestrator`."""

    def post(self, path: str, body: dict) -> dict: ...


# ─── Mock orchestrator (in-process, no network) ──────────────────────────────


class MockSafeSwapOrchestrator:
    """In-process stand-in for SafeSwap's orchestrator API.

    Deterministic pricing so tests can assert exact outputs:
    ``amount_out = amount_in * rate * (1 - fee)`` with a fixed per-pair rate table.
    Tracks calls so tests can assert the swap was actually routed.
    """

    # token_out units per 1 unit token_in (toy but deterministic)
    RATES: dict[tuple[str, str], Decimal] = {
        ("USDC", "ETH"): Decimal("0.0004"),   # 1 USDC -> 0.0004 ETH  (ETH ~ $2500)
        ("USDC", "LUX"): Decimal("2.0"),       # 1 USDC -> 2 LUX
        ("ETH", "USDC"): Decimal("2500"),
        ("USDC", "USDC"): Decimal("1"),
    }
    FEE_BPS = 30  # 0.30% orchestrator fee

    def __init__(self) -> None:
        self.quotes: dict[str, SwapQuote] = {}
        self.calls: list[tuple[str, dict]] = []

    def post(self, path: str, body: dict) -> dict:
        self.calls.append((path, body))
        if path == "/v1/quote":
            return self._quote(body)
        if path == "/v1/execute":
            return self._execute(body)
        raise SafeSwapError(f"unknown SafeSwap path: {path}")

    def _quote(self, body: dict) -> dict:
        token_in = body["tokenIn"]
        token_out = body["tokenOut"]
        amount_in = int(body["amountIn"])
        rate = self.RATES.get((token_in, token_out))
        if rate is None:
            raise SafeSwapError(f"no SafeSwap route for {token_in}->{token_out}")

        gross = Decimal(amount_in) * rate
        fee = gross * Decimal(self.FEE_BPS) / Decimal(10_000)
        amount_out = int(gross - fee)
        quote_id = "ssq_" + hashlib.sha256(
            json.dumps(body, sort_keys=True).encode() + uuid.uuid4().bytes
        ).hexdigest()[:16]

        route = ["SafeSwap.Router", "UniswapV3"] if token_out != "LUX" else ["SafeSwap.Router", "LuxDEX"]
        quote = SwapQuote(
            quote_id=quote_id,
            token_in=token_in,
            token_out=token_out,
            amount_in=amount_in,
            amount_out=amount_out,
            route=route,
            price=str(rate),
            fee_bps=self.FEE_BPS,
            expires_at=int(time.time()) + 60,
        )
        self.quotes[quote_id] = quote
        return {
            "quoteId": quote.quote_id,
            "tokenIn": quote.token_in,
            "tokenOut": quote.token_out,
            "amountIn": str(quote.amount_in),
            "amountOut": str(quote.amount_out),
            "route": quote.route,
            "price": quote.price,
            "feeBps": quote.fee_bps,
            "expiresAt": quote.expires_at,
        }

    def _execute(self, body: dict) -> dict:
        quote_id = body.get("quoteId", "")
        quote = self.quotes.get(quote_id)
        if quote is None:
            raise SafeSwapError(f"unknown or expired quoteId: {quote_id}")
        if quote.is_expired():
            raise SafeSwapError(f"quote {quote_id} expired")
        tx_hash = "0x" + hashlib.sha256(("exec" + quote_id).encode()).hexdigest()
        return {
            "quoteId": quote_id,
            "txHash": tx_hash,
            "tokenIn": quote.token_in,
            "tokenOut": quote.token_out,
            "amountIn": str(quote.amount_in),
            "amountOut": str(quote.amount_out),
            "route": quote.route,
            "settledAt": int(time.time()),
        }


# ─── Client ──────────────────────────────────────────────────────────────────


class SafeSwapClient:
    """Calls SafeSwap's orchestrator. ``transport`` defaults to a mock so the demo
    runs offline; pass ``base_url`` + a real HTTP transport for live routing."""

    def __init__(
        self,
        transport: SafeSwapTransport | None = None,
        base_url: str = "https://orchestrator.safeswap.example",
    ) -> None:
        self.transport: SafeSwapTransport = transport or MockSafeSwapOrchestrator()
        self.base_url = base_url

    def quote(self, req: SwapRequest) -> SwapQuote:
        data = self.transport.post("/v1/quote", req.to_dict())
        return SwapQuote(
            quote_id=data["quoteId"],
            token_in=data["tokenIn"],
            token_out=data["tokenOut"],
            amount_in=int(data["amountIn"]),
            amount_out=int(data["amountOut"]),
            route=list(data["route"]),
            price=data["price"],
            fee_bps=int(data["feeBps"]),
            expires_at=int(data["expiresAt"]),
        )

    def execute(self, quote: SwapQuote) -> SwapReceipt:
        if quote.is_expired():
            raise SafeSwapError(f"quote {quote.quote_id} expired before execute")
        data = self.transport.post("/v1/execute", {"quoteId": quote.quote_id})
        return SwapReceipt(
            quote_id=data["quoteId"],
            tx_hash=data["txHash"],
            token_in=data["tokenIn"],
            token_out=data["tokenOut"],
            amount_in=int(data["amountIn"]),
            amount_out=int(data["amountOut"]),
            route=list(data["route"]),
            settled_at=float(data["settledAt"]),
        )

    def route(self, req: SwapRequest) -> SwapReceipt:
        """Convenience: quote then execute in one call (best-execution route)."""
        return self.execute(self.quote(req))
