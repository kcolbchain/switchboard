# Create Protocol stable surface

This page pins the package-level Switchboard surface consumed by the Create
Protocol registry. The source dependency is
[`create-protocol/cr8/specs/switchboard-integration.md`](https://github.com/create-protocol/cr8/blob/8886a0939f3b763732e9b6797b2ebdd8e9d09a53/specs/switchboard-integration.md#4-switchboard-primitives-the-registry-depends-on)
at commit `8886a0939f3b763732e9b6797b2ebdd8e9d09a53`.

The functions below are exported from the top-level `switchboard` package.
Changing their name, required arguments, or return shape requires a new
Switchboard major version.

| Primitive | Stable surface | Return contract | Smoke coverage |
|---|---|---|---|
| MPC wallet provisioning | `switchboard.provision_wallet(quorum, recovery_set) -> Address` | EVM address string used as `wallet_id` by the other wallet primitives | `tests/test_create_protocol_surface.py` |
| Threshold signing | `switchboard.sign(wallet_id, payload) -> Signature` | Hex `0x` signature string over a canonical payload | `tests/test_create_protocol_surface.py` |
| Key rotation | `switchboard.rotate(wallet_id, new_quorum) -> Address` | Same address for quorum-only rotation | `tests/test_create_protocol_surface.py` |
| x402 metering | `switchboard.meter(session_id, rate) -> MeterReceipt` | Receipt with `session_id`, `rate`, `receipt_id`, and `issued_at` | `tests/test_create_protocol_surface.py` |
| A2A counterparty handshake | `switchboard.a2a_handshake(peer) -> A2AChannel` | Open channel with `peer`, `channel_id`, `status`, and `opened_at` | `tests/test_create_protocol_surface.py` |
| Recovery quorum lookup | `switchboard.recovery_quorum(wallet_id) -> set[Address]` | Copy of the configured recovery quorum | `tests/test_create_protocol_surface.py` |

## Version contract

- `wallet_id` is the EVM address returned by `provision_wallet`.
- `quorum` accepts `"threshold/parties"`, `(threshold, parties)`, or a mapping
  with `threshold` and `parties` keys.
- `rotate` preserves the wallet address for ordinary quorum rotation. Catastrophic
  key replacement to a new address remains an application-level migration and is
  not this stable primitive.
- `meter` and `a2a_handshake` return dataclasses with `to_dict()` for JSON/RPC
  clients.
- The current facade is in-process and delegates to existing Switchboard Python
  primitives. Durable MPC, metering, or A2A backends can replace the internals
  without changing these top-level calls.

## Gap handling

The Create Protocol registry depends on the function contracts above, not on a
specific backend implementation. If a future backend cannot satisfy one of the
rows, open a tracking issue before release and keep the current stable function
raising `CreateProtocolSurfaceError` instead of silently changing behavior.
