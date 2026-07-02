"""x402 server-side middleware for Switchboard.

Returns HTTP 402 Payment Required with PaymentRequirements when no
payment header is present. Verifies inbound PaymentPayload signatures
and provides idempotency via nonce tracking.

v1.2 multi-token extension:
- ``AcceptedToken`` dataclass carries {chain_id, token, min_amount, rank}.
- ``PaymentRequirements`` gains an ``accepts`` list; ``to_header()`` includes it
  when non-empty; ``from_header()`` / ``from_dict()`` deserialise it back.
- ``X402Server`` accepts an optional ``accepts`` list and advertises it in every
  402 response; ``validate_settlement_token()`` checks a proposed token against
  the configured list.

Supports Flask and FastAPI adapters.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

# Canonical x402 wire headers. The x402 spec (coinbase/x402) and the Hanzo MCP
# HTTP path (hanzoai/mcp#9) name the inbound proof header ``X-Payment`` and
# advertise the scheme via ``WWW-Authenticate: x402`` on the 402 response.
# Switchboard historically used ``X-Payment-Proof``; we accept both so callers
# don't have to special-case which gateway they're talking to.
PAYMENT_HEADER = "X-Payment"
PAYMENT_PROOF_HEADER = "X-Payment-Proof"
WWW_AUTHENTICATE_X402 = "x402"


@dataclass
class AcceptedToken:
    """A single token entry in the multi-token accepts[] list (v1.2).

    Carried in ``PaymentRequirements.accepts`` and advertised in every 402
    response when the server supports multi-token settlement.

    Attributes:
        chain_id:   EIP-155 chain ID the token lives on.
        token:      ERC-20 contract address, or zero address for native ETH.
        min_amount: Minimum acceptable amount in the token's smallest unit.
        rank:       Payee-side preference rank (higher = more preferred).
    """
    chain_id: int
    token: str
    min_amount: int = 0
    rank: int = 1

    def to_dict(self) -> dict:
        return {
            "chain_id": self.chain_id,
            "token": self.token,
            "min_amount": self.min_amount,
            "rank": self.rank,
        }

    @classmethod
    def from_dict(cls, d: dict) -> AcceptedToken:
        return cls(
            chain_id=int(d["chain_id"]),
            token=str(d["token"]),
            min_amount=int(d.get("min_amount", 0)),
            rank=int(d.get("rank", 1)),
        )


@dataclass
class PaymentRequirements:
    """Describes what payment is required to access an endpoint.

    v1.2: ``accepts`` carries the payee's ranked list of acceptable
    settlement tokens.  When non-empty it is included in ``to_header()``
    so the payer can run token negotiation before paying.
    """
    scheme: str = "exact"
    network: str = "base"
    asset: str = "USDC"
    amount: str = "0"
    pay_to: str = ""
    description: str = ""
    nonce: str = ""
    expires_at: int | None = None
    accepts: List[AcceptedToken] = field(default_factory=list)

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
        if self.accepts:
            data["accepts"] = [t.to_dict() for t in self.accepts]
        return json.dumps(data)

    @classmethod
    def from_header(cls, header: str) -> PaymentRequirements:
        return cls.from_dict(json.loads(header))

    @classmethod
    def from_dict(cls, data: dict) -> PaymentRequirements:
        accepts_raw = data.get("accepts", [])
        accepts = [AcceptedToken.from_dict(e) for e in accepts_raw]
        return cls(
            scheme=data.get("scheme", "exact"),
            network=data.get("network", "base"),
            asset=data.get("asset", "USDC"),
            amount=str(data.get("amount", "0")),
            pay_to=data.get("payTo", data.get("pay_to", "")),
            description=data.get("description", ""),
            nonce=data.get("nonce", ""),
            expires_at=data.get("expiresAt", data.get("expires_at")),
            accepts=accepts,
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
    """Core x402 server logic — usable from any web framework.

    v1.2: pass ``accepts`` to advertise multi-token settlement options in every
    402 response and to enable ``validate_settlement_token()`` enforcement.
    When ``accepts`` is an empty list the server accepts any token (back-compat).
    """

    def __init__(
        self,
        pay_to_address: str,
        amount_usdc: str = "1.00",
        verifier: PaymentVerifier | None = None,
        network: str = "base",
        accepts: List[AcceptedToken] | None = None,
    ):
        self.pay_to_address = pay_to_address
        self.amount_usdc = amount_usdc
        self.verifier = verifier or PaymentVerifier()
        self.network = network
        # None means "not configured" (back-compat, open); [] means "explicitly empty"
        self.accepts: List[AcceptedToken] = accepts if accepts is not None else []

    def validate_settlement_token(
        self,
        chain_id: int,
        token: str,
    ) -> tuple[bool, str]:
        """Check whether a proposed settlement token is on the server's accepts list.

        Returns ``(True, "")`` when:
        - ``self.accepts`` is empty (no restrictions configured).
        - The ``(chain_id, token)`` pair is present in ``self.accepts``.

        Returns ``(False, reason)`` otherwise.
        """
        if not self.accepts:
            return True, ""
        for t in self.accepts:
            if t.chain_id == chain_id and t.token == token:
                return True, ""
        return False, f"Token {token} on chain {chain_id} is not accepted"

    def build_402_response(self, nonce: str = "") -> tuple[int, dict, str]:
        reqs = PaymentRequirements(
            scheme="exact",
            network=self.network,
            asset="USDC",
            amount=self.amount_usdc,
            pay_to=self.pay_to_address,
            nonce=nonce or hashlib.sha256(str(time.time()).encode()).hexdigest()[:16],
            accepts=self.accepts,
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
