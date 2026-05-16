"""A2A payment discovery for Switchboard.

Agents publish a JSON profile at ``/.well-known/agent-payment.json`` so a paying
agent can learn the service agent's address, accepted assets, and supported
schemes before initiating any payment.

The module is intentionally small and pure: dataclasses, canonical JSON, and a
thin ``aiohttp`` fetch helper. It does not sign profiles, verify signatures,
or settle payments. Those responsibilities stay with the wallet and the
payment client. Profile contents are advisory and SHOULD be verified
on-chain after the first interaction.

See ``docs/agent-payment-discovery.md`` for the wire spec.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping

from switchboard.x402_middleware import PaymentScheme

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    aiohttp = None
    HAS_AIOHTTP = False

if TYPE_CHECKING:
    import aiohttp as aiohttp_typing  # noqa: F401


DISCOVERY_PATH = "/.well-known/agent-payment.json"
DISCOVERY_VERSION = "1"


class DiscoveryError(RuntimeError):
    """Raised when a discovery profile cannot be fetched or parsed safely."""


@dataclass
class PaymentEndpoint:
    """A single ``{network, asset, pay_to}`` payment endpoint.

    ``schemes`` is the subset of ``PaymentScheme`` values this endpoint
    accepts. ``escrow_contract`` is optional and only meaningful when
    ``PaymentScheme.ESCROW`` is in ``schemes``.
    """

    network: str
    asset: str
    pay_to: str
    schemes: list[PaymentScheme]
    escrow_contract: str | None = None


@dataclass
class AgentPaymentProfile:
    """Discovery profile published at ``/.well-known/agent-payment.json``.

    ``rails`` is a list of supported transports (``"http-402"``,
    ``"a2a-x402"``, ``"zap-binary"``). ``prices`` is free-form rate-hint
    metadata that payers MAY use for budgeting; the authoritative price
    always comes from the 402 / payment-required response at request time.
    """

    agent_name: str
    rails: list[str]
    accepts: list[PaymentEndpoint]
    updated_at: str
    version: str = DISCOVERY_VERSION
    did: str | None = None
    prices: dict | None = None


# ─── Wire encoding ──────────────────────────────────────────────────────────

def to_dict(profile: AgentPaymentProfile) -> dict:
    """Convert a profile to a plain dict matching the wire schema."""

    agent: dict[str, Any] = {"name": profile.agent_name}
    if profile.did:
        agent["did"] = profile.did

    accepts: list[dict[str, Any]] = []
    for endpoint in profile.accepts:
        entry: dict[str, Any] = {
            "network": endpoint.network,
            "asset": endpoint.asset,
            "pay_to": endpoint.pay_to,
            "schemes": [scheme.value for scheme in endpoint.schemes],
        }
        if endpoint.escrow_contract:
            entry["escrow_contract"] = endpoint.escrow_contract
        accepts.append(entry)

    out: dict[str, Any] = {
        "version": profile.version,
        "agent": agent,
        "rails": list(profile.rails),
        "accepts": accepts,
        "updated_at": profile.updated_at,
    }
    if profile.prices is not None:
        out["prices"] = profile.prices
    return out


def to_json(profile: AgentPaymentProfile) -> str:
    """Serialize a profile to canonical JSON (sorted keys, tight separators)."""

    return json.dumps(to_dict(profile), sort_keys=True, separators=(",", ":"))


def from_dict(d: Mapping[str, Any]) -> AgentPaymentProfile:
    """Parse a discovery dict into ``AgentPaymentProfile``.

    Raises ``DiscoveryError`` on schema mismatch or missing required fields.
    """

    if not isinstance(d, Mapping):
        raise DiscoveryError("discovery profile must be a JSON object")

    version = d.get("version")
    if version != DISCOVERY_VERSION:
        raise DiscoveryError(
            f"unsupported discovery version: {version!r} (expected {DISCOVERY_VERSION!r})"
        )

    agent = d.get("agent")
    if not isinstance(agent, Mapping):
        raise DiscoveryError("discovery profile is missing required 'agent' object")
    agent_name = agent.get("name")
    if not isinstance(agent_name, str) or not agent_name:
        raise DiscoveryError("discovery profile is missing required 'agent.name'")

    rails_raw = d.get("rails", [])
    if not isinstance(rails_raw, list) or not all(isinstance(r, str) for r in rails_raw):
        raise DiscoveryError("discovery profile 'rails' must be a list of strings")

    accepts_raw = d.get("accepts")
    if not isinstance(accepts_raw, list) or not accepts_raw:
        raise DiscoveryError("discovery profile 'accepts' must be a non-empty list")

    accepts: list[PaymentEndpoint] = []
    for index, entry in enumerate(accepts_raw):
        if not isinstance(entry, Mapping):
            raise DiscoveryError(f"accepts[{index}] must be an object")
        accepts.append(_endpoint_from_dict(entry, index))

    updated_at = d.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at:
        raise DiscoveryError("discovery profile is missing required 'updated_at'")

    did_raw = agent.get("did")
    did = did_raw if isinstance(did_raw, str) and did_raw else None

    prices_raw = d.get("prices")
    if prices_raw is not None and not isinstance(prices_raw, Mapping):
        raise DiscoveryError("discovery profile 'prices' must be an object if present")
    prices = dict(prices_raw) if prices_raw is not None else None

    return AgentPaymentProfile(
        agent_name=agent_name,
        rails=list(rails_raw),
        accepts=accepts,
        updated_at=updated_at,
        version=version,
        did=did,
        prices=prices,
    )


def from_json(text: str) -> AgentPaymentProfile:
    """Parse a canonical JSON discovery profile into ``AgentPaymentProfile``."""

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DiscoveryError(f"discovery profile is not valid JSON: {exc}") from exc
    return from_dict(data)


def _endpoint_from_dict(entry: Mapping[str, Any], index: int) -> PaymentEndpoint:
    network = entry.get("network")
    if not isinstance(network, str) or not network:
        raise DiscoveryError(f"accepts[{index}] is missing required 'network'")
    asset = entry.get("asset")
    if not isinstance(asset, str) or not asset:
        raise DiscoveryError(f"accepts[{index}] is missing required 'asset'")
    pay_to = entry.get("pay_to")
    if not isinstance(pay_to, str) or not pay_to:
        raise DiscoveryError(f"accepts[{index}] is missing required 'pay_to'")

    schemes_raw = entry.get("schemes")
    if not isinstance(schemes_raw, list) or not schemes_raw:
        raise DiscoveryError(f"accepts[{index}] 'schemes' must be a non-empty list")
    schemes: list[PaymentScheme] = []
    for scheme_value in schemes_raw:
        if not isinstance(scheme_value, str):
            raise DiscoveryError(f"accepts[{index}] scheme must be a string")
        try:
            schemes.append(PaymentScheme(scheme_value))
        except ValueError as exc:
            raise DiscoveryError(
                f"accepts[{index}] has unknown scheme {scheme_value!r}"
            ) from exc

    escrow_contract_raw = entry.get("escrow_contract")
    escrow_contract = (
        escrow_contract_raw
        if isinstance(escrow_contract_raw, str) and escrow_contract_raw
        else None
    )

    return PaymentEndpoint(
        network=network,
        asset=asset,
        pay_to=pay_to,
        schemes=schemes,
        escrow_contract=escrow_contract,
    )


# ─── A2A x402 mapping helper ────────────────────────────────────────────────

def to_a2a_accepts(profile: AgentPaymentProfile) -> list[dict[str, Any]]:
    """Project a discovery profile into A2A x402 ``accepts`` shape.

    A discovery profile lists endpoints in Switchboard's snake_case wire
    format. The A2A x402 v0.1 extension uses camelCase (``payTo``,
    ``maxAmountRequired``) on a per-requirement basis. This helper expands
    the profile's ``accepts`` into one A2A x402 requirement per
    ``(endpoint, scheme)`` pair, with ``maxAmountRequired`` left to the
    caller — discovery is advisory and does not pin a price.
    """

    out: list[dict[str, Any]] = []
    for endpoint in profile.accepts:
        for scheme in endpoint.schemes:
            requirement: dict[str, Any] = {
                "scheme": scheme.value,
                "network": endpoint.network,
                "asset": endpoint.asset,
                "payTo": endpoint.pay_to,
            }
            if endpoint.escrow_contract and scheme == PaymentScheme.ESCROW:
                requirement["extra"] = {"escrowContract": endpoint.escrow_contract}
            out.append(requirement)
    return out


# ─── Fetch helper ───────────────────────────────────────────────────────────

async def fetch_profile(
    base_url: str,
    *,
    session: "aiohttp_typing.ClientSession | None" = None,
    timeout: float = 5.0,
) -> AgentPaymentProfile:
    """Fetch ``/.well-known/agent-payment.json`` from ``base_url``.

    Uses ``aiohttp``. If a ``session`` is provided it will be used directly;
    otherwise a short-lived session is created and closed before returning.

    Raises ``DiscoveryError`` for any non-200 status, transport failure,
    malformed JSON, or schema mismatch.
    """

    if not HAS_AIOHTTP:
        raise ImportError("aiohttp required: pip install aiohttp")

    url = f"{base_url.rstrip('/')}{DISCOVERY_PATH}"
    client_timeout = aiohttp.ClientTimeout(total=timeout)

    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession(timeout=client_timeout)

    try:
        try:
            response = await session.get(url, timeout=client_timeout)
        except aiohttp.ClientError as exc:
            raise DiscoveryError(f"failed to fetch {url}: {exc}") from exc

        async with response:
            if response.status != 200:
                raise DiscoveryError(
                    f"discovery fetch returned HTTP {response.status} for {url}"
                )
            try:
                text = await response.text()
            except aiohttp.ClientError as exc:
                raise DiscoveryError(f"failed to read body of {url}: {exc}") from exc
    finally:
        if owns_session:
            await session.close()

    return from_json(text)


__all__ = [
    "DISCOVERY_PATH",
    "DISCOVERY_VERSION",
    "AgentPaymentProfile",
    "DiscoveryError",
    "PaymentEndpoint",
    "fetch_profile",
    "from_dict",
    "from_json",
    "to_a2a_accepts",
    "to_dict",
    "to_json",
]
