# feat(discovery): .well-known/agent-payment.json profile + fetch helper

## Motivation

`docs/agent-payment-protocol.md` §10 "Future work" calls this out explicitly:

> **A2A discovery** — how does Agent A know Agent B's address and accepted currencies? Out of scope for v1.0; consider `.well-known/agent-payment.json`.

Without a discovery mechanism, every paying agent has to learn the merchant's
address, accepted assets, and supported schemes out-of-band — usually
hardcoded, sometimes from a 402 response that already presumed they knew
which network to ask on. This PR ships the smallest thing that closes that
gap: one well-known URL, one JSON document, one fetch helper.

## What's included

- **`switchboard/discovery.py`** — new module. Exports `DISCOVERY_PATH`,
  `DISCOVERY_VERSION`, `PaymentEndpoint`, `AgentPaymentProfile`,
  `DiscoveryError`, `to_dict` / `to_json` (canonical, sorted, tight
  separators — same encoding as `PaymentRequest`), `from_dict` /
  `from_json`, plus an async `fetch_profile(base_url, *, session=None,
  timeout=5.0)` helper using `aiohttp` (already a soft dep via
  `x402_middleware`, same `try: import aiohttp` pattern). Also exports
  `to_a2a_accepts(profile)` to bridge into the A2A x402 adapter's
  camelCase `accepts[]` shape.
- **`docs/agent-payment-discovery.md`** — spec doc. Schema table, wire
  encoding, full example, security considerations (no signing in v1, payer
  SHOULD verify on-chain after first use, profile is advisory), backwards-
  compat policy, future work.
- **`docs/agent-payment-protocol.md`** §10 — flipped the A2A discovery bullet
  from "out of scope" to a pointer at the new spec + impl.
- **`README.md`** — added a row to "What's in the box".
- **`tests/test_discovery.py`** — 19 tests. 14 sync tests cover roundtrip,
  byte-stable canonical JSON, schema-mismatch / missing-field / unknown-
  scheme rejection, malformed JSON, the A2A mapping helper. 5 async tests
  cover `fetch_profile` success, trailing-slash normalization, 404, malformed
  JSON, and schema mismatch — gated behind `pytest.importorskip("aiohttp")`
  the same way `test_zap_transport.py` gates on `zap_py`.

## What's NOT included

All listed as future work in `docs/agent-payment-discovery.md` §9:

- **Profile signing** — no JWS, no detached signatures, no DID-derived
  proof. v1 trusts TLS + DNS only.
- **CAIP-2 addressing** — `network` stays freeform (`"base"`,
  `"eip155:<id>"`, …), matching what the A2A x402 adapter already does.
  CAIP-2 migration is tracked alongside the same migration for
  `PaymentRequest.chain_id`.
- **Rate negotiation handshake** — `prices` is a free-form hint object.
  Authoritative price still comes from the 402 / `x402.payment.required`
  response at request time.

No changes to `pyproject.toml`: `aiohttp` stays an opt-in soft dep, picked
up via the same `try: import aiohttp` block already used by
`x402_middleware`.

## Test plan

- [x] `python3 -m pytest tests/test_discovery.py -v` — 14 passed, 5 skipped
  (aiohttp not installed locally)
- [x] `python3 -m pytest tests/test_discovery.py -v` with aiohttp
  installed — 19 passed
- [x] `python3 -m pytest tests/test_a2a_x402_adapter.py
  tests/test_x402_middleware.py tests/test_payment_protocol.py` — no
  regressions in adjacent modules (52 passed)
- [ ] CI runs the full suite with aiohttp present (the 5 async tests
  will exercise the fetch path)
