"""x402 payment middleware: client and server.

Client: wraps requests/httpx/fetch. On 402, parses PaymentRequirements,
signs a PaymentPayload, retries with PAYMENT-SIGNATURE header.

Server: Flask + FastAPI adapters that return 402 + PaymentRequirements
and verify inbound PaymentPayload.
"""

from .server import (
    PAYMENT_HEADER,
    PAYMENT_PROOF_HEADER,
    WWW_AUTHENTICATE_X402,
    PaymentRequirements,
    PaymentVerifier,
    X402Server,
    inject_payment,
    map_payment_envelope,
)

__all__ = [
    "PaymentRequirements",
    "PaymentVerifier",
    "X402Server",
    "inject_payment",
    "map_payment_envelope",
    "PAYMENT_HEADER",
    "PAYMENT_PROOF_HEADER",
    "WWW_AUTHENTICATE_X402",
]
