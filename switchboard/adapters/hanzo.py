"""Hanzo.ai MCP compatibility adapter for Switchboard (Unit ②-H).

Two concerns in one module:

1. **x402 interop** — bridges Hanzo MCP's ``fetch`` tool and switchboard's
   HTTP 402 / x402 payment envelope so a Hanzo agent can discover
   ``accepts[]``, fund a call, and pay through switchboard without
   hand-rolling the translation.

   Concrete mismatch fixed here
   ----------------------------
   Switchboard's ``X402Server.build_402_response()`` puts payment details
   under a ``payment_requirements`` key in the JSON body::

       {"error": "payment_required",
        "payment_requirements": { "scheme": ..., "payTo": ..., ... }}

   The Hanzo ``fetch`` tool's ``parsePaymentRequired()`` looks for
   ``body.accepts`` (a top-level array per the x402.org v2 spec)::

       if (Array.isArray(body.accepts)) { accepts = body.accepts; }

   ``normalize_402_body()`` in this adapter re-shapes the switchboard body
   so ``accepts`` is top-level — making it visible to the Hanzo tool.
   ``build_hanzo_402_body()`` lets you generate a Hanzo-native 402 body
   directly when you control the server.

2. **HanzoAgentWallet** — maps a hanzo.ai agent identity
   (``owner/name``, e.g. ``"admin/my-bot"``) to a switchboard
   ``AgentWallet`` plus a scoped, revocable ``SessionKey``.  The Hanzo
   agent operates *its own* wallet on switchboard, gated by the
   ``SpendPolicy`` / ``AccessPolicy`` fairness layers.

Wire-up example::

    from switchboard.adapters.hanzo import HanzoAgentWallet
    from switchboard.delegation import SpendPolicy
    from datetime import datetime, timezone, timedelta

    USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"

    hab = HanzoAgentWallet(
        hanzo_agent_id="admin/my-bot",
        policy=SpendPolicy(
            expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
            token_allowlist=[USDC],
            per_tx_cap=50_000_000,          # 50 USDC
            daily_cap=500_000_000,          # 500 USDC / day
        ),
    )
    hab.credit(chain_id=8453, token=USDC, amount=1_000_000_000)
    receipt = hab.pay(chain_id=8453, token=USDC, amount=10_000_000,
                      payee="0xServiceProvider")
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from switchboard.agent_wallet import AgentWallet, PaymentReceipt
from switchboard.delegation import Delegation, SessionKey, SpendPolicy
from switchboard.mpc_wallet import MPCWallet
from switchboard.treasury import Treasury
from switchboard.x402.server import (
    AcceptedToken,
    PaymentRequirements,
    PAYMENT_HEADER,
    PAYMENT_PROOF_HEADER,
    WWW_AUTHENTICATE_X402,
)

# The x402.org version that hanzoai/mcp targets.
HANZO_X402_VERSION = "1"


# ---------------------------------------------------------------------------
# Section 1: x402 envelope interop
# ---------------------------------------------------------------------------


def normalize_402_body(body: Dict[str, Any]) -> Dict[str, Any]:
    """Re-shape a switchboard 402 body so Hanzo's ``fetch`` tool can find ``accepts``.

    Switchboard's ``X402Server.build_402_response()`` puts payment details
    under ``body["payment_requirements"]``.  The Hanzo ``fetch`` tool parses
    ``body.accepts`` (top-level) per the x402.org v2 spec::

        if (Array.isArray(body.accepts)) { accepts = body.accepts; }

    This function promotes ``payment_requirements.accepts`` (when present) to
    the top level and adds ``x402Version`` so the Hanzo tool recognises the
    response as a native x402 envelope.

    The original ``payment_requirements`` key is preserved for back-compat with
    other consumers (e.g. A2A adapters) that still read it.

    If the body is already Hanzo-native (has top-level ``accepts``) it is
    returned unchanged.
    """
    if isinstance(body.get("accepts"), list):
        # Already Hanzo-native; ensure x402Version is set.
        out = dict(body)
        out.setdefault("x402Version", HANZO_X402_VERSION)
        return out

    pr: Any = body.get("payment_requirements")
    if not isinstance(pr, dict):
        return body  # nothing to promote — pass through

    out = dict(body)
    accepts_raw: List[Dict] = pr.get("accepts", [])
    if accepts_raw:
        out["accepts"] = accepts_raw
    else:
        # No multi-token list — synthesise a single-entry accepts[] so Hanzo
        # can still parse it without falling back to the raw body path.
        entry: Dict[str, Any] = {
            "scheme": pr.get("scheme", "exact"),
            "network": pr.get("network", "base"),
            "asset": pr.get("asset", "USDC"),
            "amount": pr.get("amount", "0"),
            "payTo": pr.get("payTo", pr.get("pay_to", "")),
        }
        if pr.get("description"):
            entry["description"] = pr["description"]
        if pr.get("nonce"):
            entry["nonce"] = pr["nonce"]
        if pr.get("expiresAt") or pr.get("expires_at"):
            entry["expiresAt"] = pr.get("expiresAt") or pr.get("expires_at")
        out["accepts"] = [entry]

    out["x402Version"] = HANZO_X402_VERSION
    return out


def build_hanzo_402_body(
    requirements: PaymentRequirements,
) -> Dict[str, Any]:
    """Build a 402 response body in Hanzo-native format.

    Returns a dict with top-level ``accepts`` (Hanzo-compatible) AND
    ``payment_requirements`` (switchboard back-compat).  Suitable for use
    as a JSON response body when the server knows its caller is a Hanzo
    agent.
    """
    pr_dict = json.loads(requirements.to_header())
    body: Dict[str, Any] = {
        "error": "payment_required",
        "x402Version": HANZO_X402_VERSION,
        "payment_requirements": pr_dict,
    }
    # Top-level accepts: use the multi-token list if present, otherwise
    # synthesise from the primary fields.
    if requirements.accepts:
        body["accepts"] = [t.to_dict() for t in requirements.accepts]
    else:
        body["accepts"] = [
            {
                "scheme": requirements.scheme,
                "network": requirements.network,
                "asset": requirements.asset,
                "amount": requirements.amount,
                "payTo": requirements.pay_to,
            }
        ]
    return body


def decode_hanzo_payment_header(header_value: str) -> Dict[str, Any]:
    """Decode the ``X-PAYMENT`` header that Hanzo's fetch tool sends.

    Hanzo encodes the payment payload as base64(JSON).  Returns the decoded
    dict.  Raises ``ValueError`` on invalid input.
    """
    try:
        decoded_bytes = base64.b64decode(header_value)
        return json.loads(decoded_bytes)
    except Exception as exc:
        raise ValueError(f"Invalid X-PAYMENT header: {exc}") from exc


def encode_hanzo_payment_header(payload: Dict[str, Any]) -> str:
    """Encode a payment payload dict as Hanzo's ``X-PAYMENT`` header value.

    Returns a base64-encoded JSON string suitable for the ``X-PAYMENT``
    header.
    """
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def read_payment_header(headers: Dict[str, str]) -> Tuple[str, str]:
    """Return ``(header_name, header_value)`` for the best payment header found.

    Priority:
    1. ``X-PAYMENT``        — Hanzo native (base64 JSON per x402.org v2)
    2. ``X-Payment``        — x402 canonical (same as ``X-PAYMENT``, case variant)
    3. ``X-Payment-Proof``  — switchboard legacy

    Returns ``("", "")`` when no payment header is present.
    """
    for name in ("X-PAYMENT", "X-Payment", PAYMENT_HEADER, PAYMENT_PROOF_HEADER):
        val = headers.get(name) or headers.get(name.lower(), "")
        if val:
            return name, val
    return "", ""


def payment_requirements_from_hanzo_accepts(
    accepts: List[Dict[str, Any]],
) -> PaymentRequirements:
    """Build a switchboard ``PaymentRequirements`` from a Hanzo ``accepts[]`` list.

    Takes the first entry as the primary requirement (highest-ranked or
    first-listed) and wraps the full list into ``AcceptedToken`` objects
    for multi-token negotiation.
    """
    if not accepts:
        raise ValueError("Hanzo accepts[] must be non-empty")

    primary = accepts[0]
    tokens: List[AcceptedToken] = []
    for i, entry in enumerate(accepts):
        chain_id_raw = entry.get("chain_id") or entry.get("chainId")
        if chain_id_raw is not None:
            chain_id = int(chain_id_raw)
        else:
            chain_id = _network_to_chain_id(entry.get("network", "base"))
        tokens.append(
            AcceptedToken(
                chain_id=chain_id,
                token=str(entry.get("token", entry.get("asset", "USDC"))),
                min_amount=int(entry.get("min_amount", entry.get("amount", 0))),
                rank=int(entry.get("rank", len(accepts) - i)),
            )
        )

    return PaymentRequirements(
        scheme=primary.get("scheme", "exact"),
        network=primary.get("network", "base"),
        asset=primary.get("asset", "USDC"),
        amount=str(primary.get("amount", "0")),
        pay_to=primary.get("payTo", primary.get("pay_to", "")),
        description=primary.get("description", ""),
        nonce=primary.get("nonce", ""),
        expires_at=primary.get("expiresAt") or primary.get("expires_at"),
        accepts=tokens,
    )


def _network_to_chain_id(network: str) -> int:
    """Best-effort network-name to chain_id mapping (mirrors A2A adapter)."""
    _MAP: Dict[str, int] = {
        "ethereum": 1,
        "base": 8453,
        "base-sepolia": 84532,
        "mainnet": 1,
    }
    if network in _MAP:
        return _MAP[network]
    if network.startswith("eip155:"):
        return int(network.split(":", 1)[1])
    return 8453  # default to Base


# ---------------------------------------------------------------------------
# Section 2: HanzoAgentWallet — agent identity to wallet + session key
# ---------------------------------------------------------------------------


@dataclass
class HanzoAgentWallet:
    """Binds a hanzo.ai agent identity to a switchboard wallet + session key.

    The Hanzo IAM system identifies agents as ``"owner/name"`` strings
    (e.g. ``"admin/my-bot"``).  This class:

    * Derives a stable ``agent_id`` from the Hanzo identity.
    * Creates (or accepts) an ``AgentWallet`` for the agent.
    * Issues a scoped ``SessionKey`` via ``Delegation`` so every payment
      goes through ``SpendPolicy`` enforcement and the ``AccessPolicy``
      fairness gate.

    Parameters
    ----------
    hanzo_agent_id:
        The Hanzo IAM identity string (``"owner/name"`` format).
        Used verbatim as the ``agent_id`` in switchboard events, access
        policy checks, and ``WalletOpEvent`` attribution.
    policy:
        ``SpendPolicy`` controlling what this agent may spend.  If
        ``None``, a permissive 24-hour policy is created (suitable for
        tests).
    wallet:
        Pre-built ``AgentWallet``.  When ``None``, a fresh wallet with an
        empty treasury is created.  Pass a funded wallet for real usage.
    delegation:
        ``Delegation`` layer.  When ``None``, a fresh ``Delegation``
        wrapping ``wallet`` is created.
    access_policy:
        Optional ``AccessPolicy`` engine wired into the wallet.  When
        ``None``, the wallet uses no access policy (``SpendPolicy`` alone
        gates spending).
    """

    hanzo_agent_id: str
    policy: Optional[SpendPolicy] = None
    wallet: Optional[AgentWallet] = None
    delegation: Optional[Delegation] = None
    access_policy: Optional[object] = None

    # Populated in __post_init__
    _session_key: SessionKey = field(init=False, repr=False)
    _delegation: Delegation = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.policy is None:
            self.policy = SpendPolicy(
                expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
                token_allowlist=None,   # any token
                per_tx_cap=None,        # no per-tx cap
                daily_cap=None,         # no daily cap
            )

        if self.wallet is None:
            self.wallet = AgentWallet(
                mpc=MPCWallet(),
                treasury=Treasury(),
                access_policy=self.access_policy,
            )

        if self.delegation is None:
            self._delegation = Delegation(wallet=self.wallet)
        else:
            self._delegation = self.delegation

        self._session_key = self._delegation.grant(
            agent_id=self.hanzo_agent_id,
            policy=self.policy,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def agent_id(self) -> str:
        """The switchboard agent_id (identical to ``hanzo_agent_id``)."""
        return self.hanzo_agent_id

    @property
    def session_key(self) -> SessionKey:
        """The active ``SessionKey`` for this agent."""
        return self._session_key

    @property
    def address(self) -> str:
        """EVM address of the underlying ``AgentWallet``."""
        return self.wallet.address()

    def pay(
        self,
        chain_id: int,
        token: str,
        amount: int,
        payee: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PaymentReceipt:
        """Pay ``amount`` of ``token`` to ``payee`` on ``chain_id``.

        Routes through ``Delegation.pay_with_key()`` so the full
        ``SpendPolicy`` (token allowlist, per-tx cap, daily cap, expiry)
        is enforced *before* any on-chain action.

        Parameters
        ----------
        chain_id:
            EIP-155 chain ID.
        token:
            ERC-20 contract address or zero address for native ETH.
        amount:
            Amount in the token's smallest unit.
        payee:
            EVM address of the recipient.
        metadata:
            Optional dict attached to the ``PaymentRequest`` for routing /
            audit purposes.  ``agent_id`` is merged in automatically.

        Returns
        -------
        PaymentReceipt
            Includes ``tx_id``, ``escrow_id``, ``rail``, and ``wallet``
            from the Router (when wired).

        Raises
        ------
        PolicyViolation
            When the payment would violate the ``SpendPolicy``.
        InsufficientBalance
            When the treasury cannot cover ``amount``.
        AccessDenied
            When the ``AccessPolicy`` engine (if wired) denies the action.
        """
        from src.payment_protocol import PaymentRequest

        meta = dict(metadata or {})
        meta["agent_id"] = self.hanzo_agent_id

        request = PaymentRequest(
            chain_id=chain_id,
            token=token,
            amount_wei=amount,
            payee=payee,
            metadata=meta,
        )
        return self._delegation.pay_with_key(self._session_key, request)

    def escrow(
        self,
        chain_id: int,
        token: str,
        amount: int,
        payee: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PaymentReceipt:
        """Convenience alias for ``pay()`` that signals escrow intent.

        Passes ``{"action": "escrow"}`` in metadata so Router / access-policy
        engines can distinguish escrow-oriented payments from direct transfers.
        The underlying flow is identical — the escrow semantics live in the
        ``EscrowClient`` wired to the ``AgentWallet``.
        """
        meta = dict(metadata or {})
        meta["action"] = "escrow"
        return self.pay(chain_id=chain_id, token=token, amount=amount,
                        payee=payee, metadata=meta)

    def revoke(self) -> None:
        """Revoke the current ``SessionKey``.

        After calling this, ``pay()`` / ``escrow()`` will raise
        ``PolicyViolation``.  A new ``HanzoAgentWallet`` must be created to
        resume payments.
        """
        self._delegation.revoke(self._session_key)

    def is_active(self) -> bool:
        """Return ``True`` if the session key has not been revoked."""
        return self._delegation.is_active(self._session_key)

    def balance(self, chain_id: int, token: str) -> int:
        """Total treasury balance for ``(chain_id, token)``."""
        return self.wallet.balance(chain_id, token)

    def spendable(self, chain_id: int, token: str) -> int:
        """Spendable balance (minus reserve) for ``(chain_id, token)``."""
        return self.wallet.spendable(chain_id, token)

    def credit(self, chain_id: int, token: str, amount: int) -> None:
        """Add funds to the treasury (test / top-up helper)."""
        self.wallet.treasury.credit(chain_id=chain_id, token=token, amount=amount)
