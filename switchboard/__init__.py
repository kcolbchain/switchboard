"""switchboard — programmable payments for AI agents.

The shared payment substrate for agent-to-agent settlement: HTTP/402 middleware,
on-chain escrow, ZAP binary wire, gas budgets, and reorg-safe nonce management.

See https://github.com/kcolbchain/switchboard for full docs.
"""

from .payment_protocol import (
    AsyncPaymentClient,
    PaymentClient,
    PaymentRequest,
    PaymentState,
    format_wei,
    parse_wei,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "AsyncPaymentClient",
    "PaymentClient",
    "PaymentRequest",
    "PaymentState",
    "format_wei",
    "parse_wei",
]
