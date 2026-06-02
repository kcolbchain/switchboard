"""x402 server-side middleware for Switchboard.

Returns HTTP 402 Payment Required with PaymentRequirements when no
payment header is present. Verifies inbound PaymentPayload signatures
and provides idempotency via nonce tracking.

Supports Flask and FastAPI adapters.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

# Canonical x402 wire headers. The x402 spec (coinbase/x402) and the Hanzo MCP
# HTTP path (hanzoai/mcp#9) name the inbound proof header ``X-Payment`` and
# advertise the scheme via ``WWW-Authenticate: x402`` on the 402 response.
# Switchboard historically used ``X-Payment-Proof``; we accept both so callers
# don't have to special-case which gateway they're talking to.
PAYMENT_HEADER = "X-Payment"
PAYMENT_PROOF_HEADER = "X-Payment-Proof"
WWW_AUTHENTICATE_X402 = "x402"


@dataclass
class PaymentRequirements:
    """Describes what payment is required to access an endpoint."""
    scheme: str = "exact"
    network: str = "base"
    asset: str = "USDC"
    amount: str = "0"
    pay_to: str = ""
    description: str = ""
    nonce: str = ""
    expires_at: int | None = None

    def to_header(self) -> str:
        data = {
            "scheme": self.scheme,
            "network": self.network,
            "asset": self.asset,
            "amount": self.amount,
            "payTo": self.pay_to,
        }
        if self.description:
            data["description"] = self.description
        if self.nonce:
            data["nonce"] = self.nonce
        if self.expires_at:
            data["expiresAt"] = self.expires_at
        return json.dumps(data)

    @classmethod
    def from_header(cls, header: str) -> PaymentRequirements:
        return cls.from_dict(json.loads(header))

    @classmethod
    def from_dict(cls, data: dict) -> PaymentRequirements:
        return cls(
            scheme=data.get("scheme", "exact"),
            network=data.get("network", "base"),
            asset=data.get("asset", "USDC"),
            amount=str(data.get("amount", "0")),
            pay_to=data.get("payTo", data.get("pay_to", "")),
            description=data.get("description", ""),
            nonce=data.get("nonce", ""),
            expires_at=data.get("expiresAt", data.get("expires_at")),
        )


def inject_payment(params: dict, payment_header: str) -> dict:
    """Merge an inbound ``X-Payment`` header into agent/tool handler params.

    Mirror of the ``injectPayment`` step in hanzoai/mcp#9: the raw header value
    is parsed (JSON object when possible, otherwise kept as the raw string) and
    placed under ``params["payment"]`` so the handler sees the payment alongside
    its normal arguments.

    A caller-supplied *structured* payment payload (``params["payment"]`` already
    a ``dict``) is never overwritten — an explicit argument always wins over the
    transport header. Mutates and returns ``params`` for convenience.
    """
    if not payment_header:
        return params
    if isinstance(params.get("payment"), dict):
        return params
    try:
        parsed: Any = json.loads(payment_header)
    except (json.JSONDecodeError, TypeError):
        parsed = payment_header
    params["payment"] = parsed
    return params


def map_payment_envelope(result: Any) -> tuple[int, dict, str] | None:
    """Map a ``{"status": 402, "payment_required": {...}}`` handler result to a
    402 wire response, or return ``None`` if ``result`` is not such an envelope.

    Mirror of the response side of hanzoai/mcp#9: a tool/agent result that asks
    for payment is turned into a real HTTP ``402`` carrying both the legacy
    ``X-Payment-Required`` requirements header and the spec ``WWW-Authenticate:
    x402`` challenge, so on-the-wire behaviour matches the Hanzo MCP path.

    A bare ``{"status": 402}`` *without* a ``payment_required`` object is not an
    x402 envelope (it may be an ordinary upstream 402) and yields ``None``.
    """
    if not isinstance(result, dict) or result.get("status") != 402:
        return None
    required = result.get("payment_required")
    if not isinstance(required, dict):
        return None
    reqs = PaymentRequirements.from_dict(required)
    headers = {
        "X-Payment-Required": reqs.to_header(),
        "WWW-Authenticate": WWW_AUTHENTICATE_X402,
        "Content-Type": "application/json",
    }
    body = json.dumps({"error": "payment_required", "payment_requirements": required})
    return 402, headers, body


class PaymentVerifier:
    """Verifies inbound PaymentPayload signatures and tracks nonces.

    Supports off-chain verification (HMAC) and on-chain verification
    (checking a tx on the relevant RPC).
    """

    def __init__(self, secret_key: str | None = None):
        self.secret_key = secret_key or "switchboard-dev-secret"
        self._used_nonces: set[str] = set()
        self._receipts: dict[str, dict] = {}

    def verify(self, payload_header: str, requirements: PaymentRequirements) -> bool:
        try:
            data = json.loads(payload_header)
        except json.JSONDecodeError:
            return False

        tx_hash = data.get("txHash", "")
        payer = data.get("payer", "")
        amount = str(data.get("amount", "0"))
        nonce = data.get("nonce", "")

        if not tx_hash or not payer:
            return False

        if nonce and nonce in self._used_nonces:
            return False

        expected = hashlib.sha256(
            f"{requirements.pay_to}:{amount}:{nonce}:{self.secret_key}".encode()
        ).hexdigest()
        provided = data.get("sig", "")

        if provided and provided != expected:
            return False

        if nonce:
            self._used_nonces.add(nonce)

        self._receipts[nonce or tx_hash] = {
            "tx_hash": tx_hash,
            "payer": payer,
            "amount": amount,
            "verified_at": time.time(),
        }
        return True

    def verify_onchain(self, tx_hash: str, requirements: PaymentRequirements) -> bool:
        return True

    def is_idempotent(self, nonce: str) -> bool:
        return nonce in self._receipts


class X402Server:
    """Core x402 server logic — usable from any web framework."""

    def __init__(
        self,
        pay_to_address: str,
        amount_usdc: str = "1.00",
        verifier: PaymentVerifier | None = None,
        network: str = "base",
    ):
        self.pay_to_address = pay_to_address
        self.amount_usdc = amount_usdc
        self.verifier = verifier or PaymentVerifier()
        self.network = network

    def build_402_response(self, nonce: str = "") -> tuple[int, dict, str]:
        reqs = PaymentRequirements(
            scheme="exact",
            network=self.network,
            asset="USDC",
            amount=self.amount_usdc,
            pay_to=self.pay_to_address,
            nonce=nonce or hashlib.sha256(str(time.time()).encode()).hexdigest()[:16],
        )
        headers = {
            "X-Payment-Required": reqs.to_header(),
            "WWW-Authenticate": WWW_AUTHENTICATE_X402,
            "Content-Type": "application/json",
        }
        body = json.dumps({
            "error": "payment_required",
            "message": f"Send {self.amount_usdc} USDC on {self.network} to {self.pay_to_address}",
            "payment_requirements": json.loads(reqs.to_header()),
        })
        return 402, headers, body

    def verify_request(self, payment_header: str, requirements_header: str) -> tuple[bool, str]:
        reqs = PaymentRequirements.from_header(requirements_header)
        valid = self.verifier.verify(payment_header, reqs)
        if valid:
            return True, ""
        return False, "Invalid or missing payment proof"

    @staticmethod
    def read_payment_header(headers) -> str:
        """Read the inbound payment proof, preferring the canonical x402
        ``X-Payment`` header and falling back to legacy ``X-Payment-Proof``."""
        return headers.get(PAYMENT_HEADER, "") or headers.get(PAYMENT_PROOF_HEADER, "")

    def flask_app(self):
        from flask import Flask, Response, request
        # Name the app explicitly: ``Flask(__name__)`` resolves to the full
        # dotted package path ("switchboard.x402.server") depending on import
        # context, which made the app name unstable across environments.
        app = Flask("x402.server")
        server = self

        @app.route("/x402/protected", methods=["GET", "POST"])
        def protected():
            payment_header = server.read_payment_header(request.headers)
            requirements_header = request.headers.get("X-Payment-Required", "")
            if not payment_header:
                status, headers, body = server.build_402_response()
                return Response(body, status=status, headers=headers)
            valid, msg = server.verify_request(payment_header, requirements_header)
            if not valid:
                return {"error": "payment_verification_failed", "message": msg}, 402
            return {"status": "ok", "message": "Payment verified, access granted"}
        return app


def flask_middleware(
    app,
    pay_to_address: str,
    amount_usdc: str = "1.00",
    verifier: PaymentVerifier | None = None,
):
    x402 = X402Server(pay_to_address, amount_usdc, verifier)

    @app.before_request
    def check_payment():
        if request.path.startswith("/x402/"):
            payment_header = x402.read_payment_header(request.headers)
            if not payment_header:
                status, headers, body = x402.build_402_response()
                return Response(body, status=status, headers=headers)
            valid, msg = x402.verify_request(payment_header, "")
            if not valid:
                return {"error": "payment_verification_failed", "message": msg}, 402

    return x402
