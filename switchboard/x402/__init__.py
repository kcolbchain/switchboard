"""x402 payment middleware: client and server.

Client: wraps requests/httpx/fetch. On 402, parses PaymentRequirements,
signs a PaymentPayload, retries with PAYMENT-SIGNATURE header.

Server: Flask + FastAPI adapters that return 402 + PaymentRequirements
and verify inbound PaymentPayload.
"""
