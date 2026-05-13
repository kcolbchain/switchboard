"""A2A x402 adapter for Switchboard payment envelopes.

The google-agentic-commerce/a2a-x402 extension carries payment requests and
payment submissions as JSON metadata on A2A tasks/messages. Switchboard's core
payment middleware uses small Python dataclasses (``PaymentOffer`` and
``PaymentProof``) and can additionally encode them as ZAP binary messages.

This module is intentionally dependency-free: it maps between the stable JSON
shape documented by the A2A x402 spec and Switchboard's existing dataclasses
without importing the A2A SDK, pydantic models, or x402 facilitator libraries.
That keeps the adapter usable in clients that only need to bridge wire formats.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from switchboard.x402_middleware import PaymentOffer, PaymentProof

PAYMENT_STATUS_KEY = "x402.payment.status"
PAYMENT_REQUIRED_KEY = "x402.payment.required"
PAYMENT_PAYLOAD_KEY = "x402.payment.payload"
PAYMENT_REQUIRED = "payment-required"
PAYMENT_SUBMITTED = "payment-submitted"

_CHAIN_TO_NETWORK = {
    1: "ethereum",
    8453: "base",
    84532: "base-sepolia",
    11155111: "sepolia",
}
_NETWORK_TO_CHAIN = {network: chain_id for chain_id, network in _CHAIN_TO_NETWORK.items()}
_NETWORK_TO_CHAIN.update({"mainnet": 1, "eth": 1})


def to_a2a_request(offer: PaymentOffer) -> dict[str, Any]:
    """Convert a Switchboard ``PaymentOffer`` to standalone A2A x402 JSON.

    The returned object is a complete ``message/send`` JSON-RPC request body
    whose message metadata contains ``x402.payment.status`` and
    ``x402.payment.required``. Callers that already build their own A2A message
    can reuse ``request["params"]["message"]["metadata"]`` directly.
    """

    requirements = _offer_to_payment_requirements(offer)
    return {
        "jsonrpc": "2.0",
        "method": "message/send",
        "params": {
            "message": {
                "role": "agent",
                "parts": [
                    {
                        "kind": "text",
                        "text": offer.description or "Payment required.",
                    }
                ],
                "metadata": {
                    PAYMENT_STATUS_KEY: PAYMENT_REQUIRED,
                    PAYMENT_REQUIRED_KEY: {
                        "x402Version": 1,
                        "accepts": [requirements],
                        "error": "Payment required",
                    },
                },
            }
        },
    }


def from_a2a_response(payload: Mapping[str, Any]) -> PaymentProof:
    """Extract a Switchboard ``PaymentProof`` from A2A x402 response JSON.

    ``payload`` may be any of the common standalone-flow shapes:

    - a full JSON-RPC ``message/send`` request body,
    - an A2A ``message`` object,
    - a metadata dict containing ``x402.payment.payload``, or
    - the ``PaymentPayload`` object itself.

    The adapter accepts both camelCase aliases from x402/a2a JSON and the
    snake_case field names used by Python model dumps.
    """

    payment_payload = _extract_payment_payload(payload)
    inner = _as_mapping(payment_payload.get("payload"), "payload")
    authorization = _as_mapping(inner.get("authorization", {}), "payload.authorization")

    network = _string(payment_payload.get("network"), "network")
    chain_id = _chain_id_from_network(network)

    payer = _first_string(
        inner,
        authorization,
        keys=("payer", "from", "fromAddress", "from_address"),
        field_name="payer/from",
    )
    amount = _first_int(
        inner,
        authorization,
        keys=("amount", "value", "maxAmountRequired", "max_amount_required"),
        field_name="amount/value",
    )
    tx_hash = _optional_first_string(
        inner,
        payment_payload,
        keys=("txHash", "tx_hash", "transaction", "hash", "signature"),
    ) or _synthetic_signature_reference(inner)
    nonce = _optional_first_string(
        inner,
        authorization,
        keys=("nonce", "paymentNonce", "payment_nonce"),
    ) or ""

    return PaymentProof(
        tx_hash=tx_hash,
        chain_id=chain_id,
        payer=payer,
        amount_wei=amount,
        nonce=nonce,
        timestamp=float(int(time.time())),
    )


def _offer_to_payment_requirements(offer: PaymentOffer) -> dict[str, Any]:
    max_timeout = 600
    if offer.expires_at is not None:
        max_timeout = max(0, int(offer.expires_at - time.time()))

    return {
        "scheme": offer.scheme.value,
        "network": _network_from_chain_id(offer.chain_id),
        "payTo": offer.recipient,
        "maxAmountRequired": str(offer.amount_wei),
        "asset": offer.currency,
        "resource": offer.endpoint or "/",
        "description": offer.description,
        "mimeType": "application/json",
        "maxTimeoutSeconds": max_timeout,
        "extra": {
            "switchboard": {
                "chainId": offer.chain_id,
                "currency": offer.currency,
                "nonce": offer.nonce,
                "scheme": offer.scheme.value,
            }
        },
    }


def _extract_payment_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    current: Any = payload

    if "params" in current:
        current = _as_mapping(current["params"], "params").get("message", current)
    if "message" in current:
        current = current["message"]
    if "metadata" in current:
        current = _as_mapping(current["metadata"], "metadata")
    if PAYMENT_PAYLOAD_KEY in current:
        current = current[PAYMENT_PAYLOAD_KEY]

    current = _as_mapping(current, PAYMENT_PAYLOAD_KEY)
    if "payload" not in current:
        raise ValueError("A2A x402 response is missing payment payload data")
    return current


def _network_from_chain_id(chain_id: int) -> str:
    return _CHAIN_TO_NETWORK.get(chain_id, f"eip155:{chain_id}")


def _chain_id_from_network(network: str) -> int:
    if network in _NETWORK_TO_CHAIN:
        return _NETWORK_TO_CHAIN[network]
    if network.startswith("eip155:"):
        return int(network.split(":", 1)[1])
    raise ValueError(f"Unsupported x402 network: {network}")


def _as_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    raise ValueError(f"{field_name} must be an object")


def _string(value: Any, field_name: str) -> str:
    if isinstance(value, str) and value:
        return value
    raise ValueError(f"{field_name} must be a non-empty string")


def _first_string(
    primary: Mapping[str, Any],
    secondary: Mapping[str, Any],
    *,
    keys: tuple[str, ...],
    field_name: str,
) -> str:
    value = _optional_first_string(primary, secondary, keys=keys)
    if value is None:
        raise ValueError(f"A2A x402 payload is missing {field_name}")
    return value


def _optional_first_string(
    primary: Mapping[str, Any],
    secondary: Mapping[str, Any],
    *,
    keys: tuple[str, ...],
) -> str | None:
    for mapping in (primary, secondary):
        for key in keys:
            value = mapping.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _first_int(
    primary: Mapping[str, Any],
    secondary: Mapping[str, Any],
    *,
    keys: tuple[str, ...],
    field_name: str,
) -> int:
    for mapping in (primary, secondary):
        for key in keys:
            if key not in mapping:
                continue
            value = mapping[key]
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
    raise ValueError(f"A2A x402 payload is missing numeric {field_name}")


def _synthetic_signature_reference(inner: Mapping[str, Any]) -> str:
    """Return a stable proof reference when a facilitator tx is not present yet."""

    signature = _optional_first_string(inner, {}, keys=("signature",))
    if not signature:
        raise ValueError("A2A x402 payload is missing txHash/transaction/signature")
    return signature
