"""Tests for switchboard.discovery — A2A payment-profile schema + fetch helper.

Sync tests (roundtrip, canonicalization, validation) always run. The async
``fetch_profile`` tests are skipped when ``aiohttp`` is not installed, matching
how ``tests/test_zap_transport.py`` skips when ``zap_py`` is missing.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from switchboard.discovery import (
    DISCOVERY_PATH,
    DISCOVERY_VERSION,
    AgentPaymentProfile,
    DiscoveryError,
    PaymentEndpoint,
    from_dict,
    from_json,
    to_a2a_accepts,
    to_dict,
    to_json,
)
from switchboard.x402_middleware import PaymentScheme


def _sample_profile() -> AgentPaymentProfile:
    return AgentPaymentProfile(
        agent_name="image-gen-bot",
        rails=["http-402", "a2a-x402"],
        accepts=[
            PaymentEndpoint(
                network="base",
                asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bda02913",
                pay_to="0xServerWalletAddressHere",
                schemes=[PaymentScheme.EXACT, PaymentScheme.ESCROW],
                escrow_contract="0xEscrowContractAddressHere",
            )
        ],
        updated_at="2026-05-16T12:00:00Z",
        did="did:web:image-gen-bot.example.com",
        prices={"per_call_usd": "0.0012"},
    )


# ─── Roundtrip + canonicalization ────────────────────────────────────────────


def test_to_dict_from_dict_roundtrips():
    original = _sample_profile()

    parsed = from_dict(to_dict(original))

    assert parsed == original


def test_to_json_is_byte_stable():
    profile = _sample_profile()

    first = to_json(profile)
    second = to_json(profile)

    assert first == second
    # Re-parsing and re-serializing must be the same bytes too.
    assert to_json(from_json(first)) == first


def test_to_json_uses_canonical_separators():
    profile = _sample_profile()

    blob = to_json(profile)

    # No whitespace between tokens, matching PaymentRequest canonical form.
    assert ", " not in blob
    assert ": " not in blob


def test_to_json_minimal_profile_omits_optional_fields():
    profile = AgentPaymentProfile(
        agent_name="minimal-bot",
        rails=["http-402"],
        accepts=[
            PaymentEndpoint(
                network="base",
                asset="USDC",
                pay_to="0xabc",
                schemes=[PaymentScheme.EXACT],
            )
        ],
        updated_at="2026-05-16T12:00:00Z",
    )

    data = json.loads(to_json(profile))

    assert "did" not in data["agent"]
    assert "prices" not in data
    assert "escrow_contract" not in data["accepts"][0]


# ─── Schema validation ──────────────────────────────────────────────────────


def test_from_dict_rejects_unknown_version():
    payload = to_dict(_sample_profile())
    payload["version"] = "2"

    with pytest.raises(DiscoveryError, match="unsupported discovery version"):
        from_dict(payload)


def test_from_dict_requires_agent_name():
    payload = to_dict(_sample_profile())
    payload["agent"] = {}

    with pytest.raises(DiscoveryError, match="agent.name"):
        from_dict(payload)


def test_from_dict_requires_non_empty_accepts():
    payload = to_dict(_sample_profile())
    payload["accepts"] = []

    with pytest.raises(DiscoveryError, match="accepts"):
        from_dict(payload)


def test_from_dict_requires_endpoint_pay_to():
    payload = to_dict(_sample_profile())
    payload["accepts"][0].pop("pay_to")

    with pytest.raises(DiscoveryError, match="pay_to"):
        from_dict(payload)


def test_from_dict_requires_updated_at():
    payload = to_dict(_sample_profile())
    payload.pop("updated_at")

    with pytest.raises(DiscoveryError, match="updated_at"):
        from_dict(payload)


def test_from_dict_rejects_unknown_scheme():
    payload = to_dict(_sample_profile())
    payload["accepts"][0]["schemes"] = ["exact", "wire-transfer"]

    with pytest.raises(DiscoveryError, match="unknown scheme"):
        from_dict(payload)


def test_from_dict_requires_non_empty_schemes():
    payload = to_dict(_sample_profile())
    payload["accepts"][0]["schemes"] = []

    with pytest.raises(DiscoveryError, match="schemes"):
        from_dict(payload)


def test_from_json_rejects_malformed_json():
    with pytest.raises(DiscoveryError, match="not valid JSON"):
        from_json("{not json}")


# ─── A2A x402 mapping helper ────────────────────────────────────────────────


def test_to_a2a_accepts_emits_one_requirement_per_scheme():
    profile = _sample_profile()

    requirements = to_a2a_accepts(profile)

    assert len(requirements) == 2
    schemes = {req["scheme"] for req in requirements}
    assert schemes == {"exact", "escrow"}
    for req in requirements:
        assert req["payTo"] == "0xServerWalletAddressHere"
        assert req["network"] == "base"
        assert req["asset"] == "0x833589fCD6eDb6E08f4c7C32D4f71b54bda02913"
    escrow_req = next(r for r in requirements if r["scheme"] == "escrow")
    assert escrow_req["extra"]["escrowContract"] == "0xEscrowContractAddressHere"


# ─── Constants ──────────────────────────────────────────────────────────────


def test_discovery_path_and_version_constants():
    assert DISCOVERY_PATH == "/.well-known/agent-payment.json"
    assert DISCOVERY_VERSION == "1"


# ─── fetch_profile (requires aiohttp) ───────────────────────────────────────

# The async ``fetch_profile`` path uses ``aiohttp``. We gate the four fetch
# tests below behind an ``importorskip`` at function scope so the sync tests
# above still run when aiohttp is not installed locally — mirroring how
# ``tests/test_zap_transport.py`` skips when ``zap_py`` is missing.


from switchboard import discovery as discovery_module  # noqa: E402


class _FakeResponse:
    """Async-context-manager response that mimics ``aiohttp.ClientResponse``."""

    def __init__(self, status: int, body: str):
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    """Minimal stand-in for ``aiohttp.ClientSession`` used in fetch tests."""

    def __init__(self, response: _FakeResponse):
        self._response = response
        self.requested_url: str | None = None
        self.closed = False

    async def get(self, url, **_kwargs):
        self.requested_url = url
        return self._response

    async def close(self):
        self.closed = True


def test_fetch_profile_parses_well_known_json():
    pytest.importorskip("aiohttp")
    profile = _sample_profile()
    body = to_json(profile)
    session = _FakeSession(_FakeResponse(200, body))

    result = asyncio.run(
        discovery_module.fetch_profile("https://image-gen-bot.example.com", session=session)
    )

    assert session.requested_url == (
        "https://image-gen-bot.example.com/.well-known/agent-payment.json"
    )
    assert result == profile
    # Caller-owned session must not be closed by fetch_profile.
    assert session.closed is False


def test_fetch_profile_strips_trailing_slash():
    pytest.importorskip("aiohttp")
    profile = _sample_profile()
    session = _FakeSession(_FakeResponse(200, to_json(profile)))

    asyncio.run(
        discovery_module.fetch_profile("https://agent.example.com/", session=session)
    )

    assert session.requested_url == (
        "https://agent.example.com/.well-known/agent-payment.json"
    )


def test_fetch_profile_404_raises_discovery_error():
    pytest.importorskip("aiohttp")
    session = _FakeSession(_FakeResponse(404, "not found"))

    with pytest.raises(DiscoveryError, match="HTTP 404"):
        asyncio.run(
            discovery_module.fetch_profile("https://agent.example.com", session=session)
        )


def test_fetch_profile_malformed_json_raises_discovery_error():
    pytest.importorskip("aiohttp")
    session = _FakeSession(_FakeResponse(200, "{not json}"))

    with pytest.raises(DiscoveryError, match="not valid JSON"):
        asyncio.run(
            discovery_module.fetch_profile("https://agent.example.com", session=session)
        )


def test_fetch_profile_schema_mismatch_raises_discovery_error():
    pytest.importorskip("aiohttp")
    bad_body = json.dumps({"version": "2", "agent": {"name": "x"}, "rails": [], "accepts": []})
    session = _FakeSession(_FakeResponse(200, bad_body))

    with pytest.raises(DiscoveryError, match="unsupported discovery version"):
        asyncio.run(
            discovery_module.fetch_profile("https://agent.example.com", session=session)
        )
