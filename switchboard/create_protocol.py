"""Stable Create Protocol facade for switchboard.

This module pins the small package-level surface that the Create Protocol
registry consumes. The implementation delegates to the current in-process
Switchboard primitives, so production deployments can swap in durable MPC,
metering, and A2A backends without changing the import contract.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

from switchboard.mpc_wallet import MPCWallet


Address = str
Signature = str


class CreateProtocolSurfaceError(ValueError):
    """Raised when the stable Create Protocol facade cannot service a request."""


@dataclass
class MeterReceipt:
    """Off-chain x402 metering receipt returned by ``meter``."""

    session_id: str
    rate: str
    receipt_id: str
    issued_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class A2AChannel:
    """Counterparty channel returned by ``a2a_handshake``."""

    peer: str
    channel_id: str
    status: str = "open"
    opened_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _ProvisionedWallet:
    wallet: MPCWallet
    recovery_set: set[Address]


_WALLETS: dict[Address, _ProvisionedWallet] = {}
_METER_RECEIPTS: list[MeterReceipt] = []
_CHANNELS: dict[str, A2AChannel] = {}


def provision_wallet(quorum: Any, recovery_set: Iterable[Address] = ()) -> Address:
    """Provision an MPC wallet and return its stable EVM address.

    ``quorum`` accepts ``"2/3"``, ``(2, 3)``, or
    ``{"threshold": 2, "parties": 3}``. The returned address is the wallet id
    accepted by ``sign``, ``rotate``, and ``recovery_quorum``.
    """

    threshold, parties = _parse_quorum(quorum)
    recovery = {str(address) for address in recovery_set}
    wallet = MPCWallet(
        parties=parties,
        threshold=threshold,
        wallet_label=_wallet_label(recovery),
    )
    address = wallet.get_evm_address()
    _WALLETS[address] = _ProvisionedWallet(wallet=wallet, recovery_set=recovery)
    return address


def sign(wallet_id: Address, payload: bytes | str | Mapping[str, Any]) -> Signature:
    """Sign an arbitrary payload with the provisioned MPC wallet."""

    provisioned = _require_wallet(wallet_id)
    canonical_payload = _canonical_payload(payload)
    return provisioned.wallet.sign_and_send({"payload": canonical_payload.hex()})


def rotate(wallet_id: Address, new_quorum: Any) -> Address:
    """Rotate the wallet quorum while preserving the on-chain address."""

    provisioned = _require_wallet(wallet_id)
    threshold, parties = _parse_quorum(new_quorum)
    replacement = MPCWallet(
        parties=parties,
        threshold=threshold,
        chain_id=provisioned.wallet.config.chain_id,
        wallet_label=provisioned.wallet.config.wallet_label,
    )
    if replacement.get_evm_address() != wallet_id:
        raise CreateProtocolSurfaceError("quorum rotation changed the wallet address")
    _WALLETS[wallet_id] = _ProvisionedWallet(
        wallet=replacement,
        recovery_set=set(provisioned.recovery_set),
    )
    return wallet_id


def meter(session_id: str, rate: str | int | float) -> MeterReceipt:
    """Record an x402 metering checkpoint and return a portable receipt."""

    if not session_id:
        raise CreateProtocolSurfaceError("session_id is required")
    rate_value = str(rate)
    receipt_seed = f"{session_id}:{rate_value}:{len(_METER_RECEIPTS)}"
    receipt_id = hashlib.sha256(receipt_seed.encode()).hexdigest()
    receipt = MeterReceipt(session_id=session_id, rate=rate_value, receipt_id="0x" + receipt_id)
    _METER_RECEIPTS.append(receipt)
    return receipt


def a2a_handshake(peer: str) -> A2AChannel:
    """Open an A2A counterparty channel for payment negotiation."""

    if not peer:
        raise CreateProtocolSurfaceError("peer is required")
    channel = A2AChannel(peer=peer, channel_id=str(uuid.uuid4()))
    _CHANNELS[channel.channel_id] = channel
    return channel


def recovery_quorum(wallet_id: Address) -> set[Address]:
    """Return the configured recovery quorum for a provisioned wallet."""

    return set(_require_wallet(wallet_id).recovery_set)


def _parse_quorum(quorum: Any) -> tuple[int, int]:
    if isinstance(quorum, str):
        try:
            threshold_raw, parties_raw = quorum.split("/", 1)
            threshold = int(threshold_raw)
            parties = int(parties_raw)
        except (TypeError, ValueError) as exc:
            raise CreateProtocolSurfaceError("quorum string must look like '2/3'") from exc
    elif isinstance(quorum, Mapping):
        try:
            threshold = int(quorum["threshold"])
            parties = int(quorum["parties"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CreateProtocolSurfaceError(
                "quorum mapping requires threshold and parties"
            ) from exc
    else:
        try:
            threshold, parties = quorum
            threshold = int(threshold)
            parties = int(parties)
        except (TypeError, ValueError) as exc:
            raise CreateProtocolSurfaceError("quorum must be a string, mapping, or pair") from exc

    if threshold < 1 or parties < 1:
        raise CreateProtocolSurfaceError("quorum values must be positive")
    if threshold > parties:
        raise CreateProtocolSurfaceError("threshold cannot exceed parties")
    return threshold, parties


def _canonical_payload(payload: bytes | str | Mapping[str, Any]) -> bytes:
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode()
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _require_wallet(wallet_id: Address) -> _ProvisionedWallet:
    try:
        return _WALLETS[wallet_id]
    except KeyError as exc:
        raise CreateProtocolSurfaceError(f"unknown wallet_id: {wallet_id}") from exc


def _wallet_label(recovery_set: set[Address]) -> str:
    seed = json.dumps(
        {"recovery_set": sorted(recovery_set), "nonce": uuid.uuid4().hex},
        separators=(",", ":"),
    )
    return "create-protocol-" + hashlib.sha256(seed.encode()).hexdigest()[:16]
