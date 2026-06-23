# ethereum-magicians forum post — draft

Post this to https://ethereum-magicians.org/ under **Magicians → EIPs** (category: ERC).
Once the topic is created, copy its URL into the `discussions-to:` frontmatter field of
`eips/draft-native-eth-a2a-escrow.md` before opening the ethereum/EIPs PR.

---

**Title:** `ERC: Native-ETH Agent-to-Agent Escrow (payable escrow primitive, no token, no arbitrator)`

**Tags:** `erc`, `escrow`, `payments`, `agents`, `eip-165`

---

## Body

Sharing an early Standards-Track ERC draft for feedback before I open the PR to `ethereum/EIPs`.

**The primitive.** A `payable` escrow keyed by a free-form `string requestId`. The payer calls
`createPayment{value}(requestId, payee, timeoutBlocks, challengePeriod)`; funds sit in `Locked`
until exactly one of three payer-driven terminals fires:

- `confirmPayment` — release to payee (only before the timeout window closes),
- `cancelPayment` — return to payer while still locked,
- `requestRefund` — return to payer, but only after `timeoutBlocks + challengePeriod` has elapsed.

All three terminals are absorbing. That is the whole surface. ERC-165 interface id **`0x5c3738e9`**
(XOR of the six function selectors — math is in the draft).

**Why it's worth a standard.** Every on-chain agent-payment primitive shipping today (x402's
SettlementContract, Google A2A payment-claims, Circle Nanopayments, Tempo/MPP) is (1) bound to a
specific ERC-20, usually USDC, so the payer needs an extra `approve` tx and must trust that token's
bridge on the target chain; (2) coupled to an off-chain settlement counterparty that can be
sanctioned or rate-limited; and (3) deployed per-chain by its operator with no portable bytecode.

Native ETH is the only asset present on every EVM chain with no token contract. A `payable` escrow
removes the `approve` step (saving ~21k–46k gas/payment), removes the off-chain operator, and is the
same source on mainnet, every L2, and any future EVM chain. We've been running the reference
implementation on Base Sepolia and Lux testnet since April.

**Deliberate non-goals (defended in the draft's Rationale):**

- **No oracle/arbitration release in the base standard.** Payer-only release is the minimum.
  Oracle-attested release is a strict *extension* — `releaseByAttestation(string,bytes32,bytes[])`
  gated by a separate `IOracleAggregator` — layered on, not baked in. Keeps the base auditable.
- **`string` requestId, not `bytes32`.** Real off-chain ids (UUIDs, HTTP `X-Request-Id`, JWT claims)
  aren't naturally `bytes32`; hashing them breaks on-chain↔off-chain correlation. The string cost is
  paid once, on create.
- **Explicit challenge period after the timeout**, so payer and payee don't race in the same block.

**Draft:** <link to `eips/draft-native-eth-a2a-escrow.md` on the stable `main` URL>
**Reference implementation:** `contracts/AgentEscrow.sol` (MIT) — <repo link>
**Animated walkthrough** of happy / refund / cancel paths: <lab link>

## Open questions for the forum

1. **Is the surface the right *minimum*?** payable create, `string` requestId key, timeout +
   challenge period, three payer-driven terminals. Missing anything load-bearing, or over-including?
2. **Oracle release — fold in or keep separate?** The draft argues for a separate composable EIP.
   Is that the right cut, or should attestation-release be in the base interface?
3. **`string` vs `bytes32` key.** Worth the per-create gas and the indexed-string topic-hash quirk,
   or should the base standard be `bytes32` with a `string` variant layered on?
4. **Challenge-period semantics.** The window currently hard-favors the payer (after timeout, only
   `requestRefund` is callable — payee can no longer confirm). Is "no confirmation in time ⇒ funds
   return to payer" the right default, or should there be a symmetric payee-claim path?
5. **`block.number` vs `block.timestamp`** for the windows. We chose block height for reorg-stability
   reasoning; on chains with variable block times this pushes configuration onto the deployer. Is
   timestamp the more portable choice?
6. **ERC-165 only, or also an on-chain registry?** Discovery of conformant deployments per chain is
   currently out of scope. Does the standard need a canonical registry, or is that a separate concern?

Happy to be told the shape is wrong for an ERC — would rather hear it here than after submission.
