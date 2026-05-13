"""A2A x402 JSON adapter for Switchboard payment envelopes.

The adapter is intentionally small and pure: it converts Switchboard's
``PaymentOffer`` and ``PaymentProof`` data classes to and from the JSON
metadata shape used by the A2A x402 extension. It does not sign, verify, or
settle payments.
"""

from __future__ import annotations

from typing import Any, Mapping

from switchboard.x402_middleware import PaymentOffer, PaymentProof, PaymentScheme


A2A_X402_EXTENSION_URI = "https://github.com/google-a2a/a2a-x402/v0.1"
X402_VERSION = 1

_CHAIN_ID_TO_NETWORK = {
    1: "ethereum",
    8453: "base",
    84532: "base-sepolia",
}
_NETWORK_TO_CHAIN_ID = {network: chain_id for chain_id, network in _CHAIN_ID_TO_NETWORK.items()}


class A2AX402AdapterError(ValueError):
    """Raised when an A2A x402 payload cannot be converted safely."""


def to_a2a_request(offer: PaymentOffer, *, mime_type: str = "application/json") -> dict[str, Any]:
    """Convert a Switchboard payment offer to A2A x402 payment-required metadata.

    The returned object is suitable for the ``x402.payment.required`` metadata
    field described by the A2A x402 v0.1 specification.
    """

    requirement: dict[str, Any] = {
        "scheme": offer.scheme.value,
        "network": network_from_chain_id(offer.chain_id),
        "asset": offer.currency,
        "payTo": offer.recipient,
        "maxAmountRequired": str(offer.amount_wei),
    }

    if offer.endpoint:
        requirement["resource"] = offer.endpoint
    if offer.description:
        requirement["description"] = offer.description
    if mime_type:
        requirement["mimeType"] = mime_type

    extra: dict[str, Any] = {}
    if offer.nonce:
        extra["nonce"] = offer.nonce
    if offer.expires_at is not None:
        extra["expiresAt"] = offer.expires_at
    if extra:
        requirement["extra"] = extra

    return {
        "x402Version": X402_VERSION,
        "accepts": [requirement],
    }


def from_a2a_request(payload: Mapping[str, Any], *, endpoint: str = "") -> PaymentOffer:
    """Convert A2A x402 payment-required metadata back to ``PaymentOffer``."""

    required = _metadata_value(payload, "x402.payment.required") or payload
    accepts = required.get("accepts")
    if not isinstance(accepts, list) or not accepts:
        raise A2AX402AdapterError("A2A x402 request must contain a non-empty accepts list")

    requirement = accepts[0]
    if not isinstance(requirement, Mapping):
        raise A2AX402AdapterError("A2A x402 payment requirement must be an object")

    extra = requirement.get("extra")
    if extra is None:
        extra = {}
    if not isinstance(extra, Mapping):
        raise A2AX402AdapterError("A2A x402 payment requirement extra must be an object")

    chain_id = chain_id_from_network(_required_str(requirement, "network"))

    return PaymentOffer(
        amount_wei=_required_int(requirement, "maxAmountRequired"),
        currency=_required_str(requirement, "asset"),
        recipient=_required_str(requirement, "payTo"),
        chain_id=chain_id,
        scheme=PaymentScheme(str(requirement.get("scheme", "exact"))),
        description=str(requirement.get("description", "")),
        endpoint=endpoint or str(requirement.get("resource", "")),
        nonce=str(extra.get("nonce", "")),
        expires_at=_optional_int(extra.get("expiresAt")),
    )


def from_a2a_response(payload: Mapping[str, Any]) -> PaymentProof:
    """Convert A2A x402 payment receipt/submission metadata to ``PaymentProof``.

    The function accepts both final ``x402.payment.receipts`` entries and direct
    ``x402.payment.payload``/``payload`` objects, because client and merchant
    implementations expose different envelope depths.
    """

    proof_payload = _extract_proof_payload(payload)

    tx_hash = _first_str(proof_payload, "transaction", "txHash", "transactionHash")
    if not tx_hash:
        raise A2AX402AdapterError("A2A x402 response is missing transaction/txHash")

    network = _first_str(proof_payload, "network")
    chain_id_value = _first_value(proof_payload, "chainId", "chain_id")
    if chain_id_value is not None:
        chain_id = _to_int(chain_id_value, "chainId")
    elif network:
        chain_id = chain_id_from_network(network)
    else:
        raise A2AX402AdapterError("A2A x402 response is missing network/chainId")

    amount_value = _first_value(proof_payload, "amount", "amountWei", "maxAmountRequired")
    amount_wei = _to_int(amount_value, "amount") if amount_value is not None else 0

    timestamp_value = _first_value(proof_payload, "timestamp")
    proof_kwargs: dict[str, Any] = {
        "tx_hash": tx_hash,
        "chain_id": chain_id,
        "payer": _first_str(proof_payload, "payer", "from") or "",
        "amount_wei": amount_wei,
        "nonce": _first_str(proof_payload, "nonce") or "",
    }
    if timestamp_value is not None:
        proof_kwargs["timestamp"] = float(timestamp_value)

    return PaymentProof(**proof_kwargs)


def to_a2a_submission(proof: PaymentProof, *, network: str | None = None) -> dict[str, Any]:
    """Convert a Switchboard proof to A2A x402 payment-submitted metadata."""

    return {
        "x402.payment.status": "payment-submitted",
        "x402.payment.payload": {
            "x402Version": X402_VERSION,
            "network": network or network_from_chain_id(proof.chain_id),
            "scheme": "exact",
            "payload": {
                "txHash": proof.tx_hash,
                "chainId": proof.chain_id,
                "payer": proof.payer,
                "amount": str(proof.amount_wei),
                "nonce": proof.nonce,
                "timestamp": int(proof.timestamp),
            },
        },
    }


def to_a2a_receipt(proof: PaymentProof, *, success: bool = True) -> dict[str, Any]:
    """Convert a Switchboard proof to an A2A x402 settlement receipt."""

    return {
        "success": success,
        "transaction": proof.tx_hash if success else "",
        "network": network_from_chain_id(proof.chain_id),
        "payer": proof.payer,
        "amount": str(proof.amount_wei),
        "nonce": proof.nonce,
        "timestamp": int(proof.timestamp),
    }


def network_from_chain_id(chain_id: int) -> str:
    """Return the A2A x402 network name for a numeric EVM chain id."""

    return _CHAIN_ID_TO_NETWORK.get(chain_id, f"eip155:{chain_id}")


def chain_id_from_network(network: str) -> int:
    """Return a numeric chain id for an A2A x402 network name."""

    if network in _NETWORK_TO_CHAIN_ID:
        return _NETWORK_TO_CHAIN_ID[network]
    if network.startswith("eip155:"):
        return _to_int(network.split(":", 1)[1], "network")
    raise A2AX402AdapterError(f"Unknown A2A x402 network: {network}")


def _extract_proof_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping):
        nested = _extract_from_metadata(metadata)
        if nested is not None:
            return nested

    nested = _extract_from_metadata(payload)
    if nested is not None:
        return nested

    return payload


def _extract_from_metadata(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    receipts = payload.get("x402.payment.receipts")
    if isinstance(receipts, list) and receipts:
        last_receipt = receipts[-1]
        if isinstance(last_receipt, Mapping):
            return last_receipt

    for key in ("x402.payment.payload", "paymentPayload", "payment", "proof", "payload"):
        candidate = payload.get(key)
        if isinstance(candidate, Mapping):
            if key == "payload":
                merged = dict(payload)
                merged.update(candidate)
                return merged
            nested_payload = candidate.get("payload")
            if isinstance(nested_payload, Mapping):
                merged = dict(candidate)
                merged.update(nested_payload)
                return merged
            return candidate

    return None


def _metadata_value(payload: Mapping[str, Any], key: str) -> Any:
    if key in payload:
        return payload[key]
    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping):
        return metadata.get(key)
    return None


def _required_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None or value == "":
        raise A2AX402AdapterError(f"A2A x402 payload is missing {key}")
    return str(value)


def _required_int(payload: Mapping[str, Any], key: str) -> int:
    return _to_int(payload.get(key), key)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return _to_int(value, "optional integer")


def _first_value(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def _first_str(payload: Mapping[str, Any], *keys: str) -> str:
    value = _first_value(payload, *keys)
    return "" if value is None else str(value)


def _to_int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise A2AX402AdapterError(f"A2A x402 field {field} must be an integer") from exc
