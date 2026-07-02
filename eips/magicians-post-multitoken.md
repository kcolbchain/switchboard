# ethereum-magicians forum post — draft

Post this to https://ethereum-magicians.org/ under **Magicians → EIPs** (category: ERC).
Once the topic is created, copy its URL into the `discussions-to:` frontmatter field of
`eips/draft-multitoken-a2a-escrow.md` before opening the ethereum/EIPs PR.

---

**Title:** `ERC: Multi-Token Agent-to-Agent Escrow (ETH + any ERC-20, fee-on-transfer safe, oracle release opt-in)`

**Tags:** `erc`, `escrow`, `payments`, `agents`, `erc-20`, `eip-165`

---

## Body

Sharing an early Standards-Track ERC draft for feedback before opening the PR to `ethereum/EIPs`. This draft generalizes our earlier native-ETH A2A escrow primitive (see that thread) to support any settlement asset.

**Background.** The native-ETH A2A escrow standard defines a minimal `payable` escrow keyed by a free-form `string requestId`, with payer-driven confirm / refund / cancel terminals and an explicit challenge period. It targets autonomous agent-to-agent payments and intentionally has no off-chain operator, no token dependency, and no arbitrator.

**What this ERC adds.** The same lifecycle — create, confirm/refund/cancel — parameterized by an `address token` field:

- **`token == address(0)` — ETH profile.** Semantics identical to the native-ETH ERC. The earlier draft becomes a profile of this one, not a separate standard.
- **`token != address(0)` — ERC-20 profile.** Payer calls `ERC20.approve(escrow, amount)` once, then `createPayment(…, token, amount, …)` with `msg.value == 0`. The escrow pulls via `transferFrom` and releases via `transfer`. Credited amount is the measured balance delta, making the design safe for fee-on-transfer tokens. ERC-20s must be owner-allowlisted to gate unsupported token types.
- **Optional oracle release.** A per-payment `policyHash` field enables a `releaseByAttestation(requestId, attestationHash, signatures)` path, verified by an `IOracleAggregator`. Payments with `policyHash == 0x00` are payer-only, identical to the native-ETH primitive. No oracle trust if you don't use it.

ERC-165 interface id: **`0x01dc5a49`** (XOR of the six member-function selectors — derivation is in the draft).

**Why generalize instead of compose?**

Composing the ETH escrow with a thin ERC-20 wrapper per token creates N adapters rather than one interface, fragments ERC-165 discovery, and forces indexers to track multiple contract addresses per agent pair. A single interface with a `token` parameter keeps the on-chain footprint minimal, the ERC-165 check decisive, and the indexer logic uniform across assets.

The ETH profile gives the native-ETH ERC a clean upgrade path: a payer that always sets `token = address(0)` interacts with an `IAgentEscrow`-conformant multi-token contract identically to the narrower native-ETH contract, modulo the extra parameter.

**Deliberate design choices (and the cases against them — see the draft's Rationale):**

- **Balance-delta accounting** for ERC-20: the contract credits what it actually receives, not the declared amount. One extra `balanceOf` on create (~700 gas) buys safety for all fee-on-transfer and some rebasing tokens.
- **Allowlist for ERC-20, not for ETH**: native ETH has no token-contract attack surface and needs no gate. ERC-20s go through an owner allowlist so the deployer can audit each token's edge cases before accepting it.
- **Oracle release stays opt-in and per-payment**: a payer that does not want oracle-mediated release sets no policy hash and cannot be subject to `releaseByAttestation` regardless of the aggregator state.
- **`block.number` not `block.timestamp`**: same reasoning as the native-ETH draft — reorg-stable and not manipulable within miner discretion.

**Reference implementation:**

- `contracts/MultiTokenAgentEscrow.sol` (Solidity ^0.8.20, MIT) — the multi-token escrow
- `contracts/IAgentEscrow.sol` — the shared interface
- `contracts/IOracleAggregator.sol` — oracle aggregator interface
- Deployed on Base Sepolia and Lux testnet

Repository: `github.com/kcolbchain/switchboard`
Draft: `eips/draft-multitoken-a2a-escrow.md` on `main` — <link>
Native-ETH companion thread: <link to earlier magicians post>

**Known gaps before ethereum/EIPs submission:**

1. ERC-165 `supportsInterface` not yet wired into the reference contract — a PR is in progress.
2. A `supportsInterface(0x01dc5a49) == true` Foundry test does not yet exist.
3. Fee-on-transfer behavior on the release path (payee receives net-of-fee, not gross) needs a user-facing note in the contract docs — clear in the EIP, not yet in the NatDoc.

## Open questions for the forum

1. **ETH profile backward compatibility.** The native-ETH ERC's interface id (`0x5c3738e9`) and this standard's (`0x01dc5a49`) are different because `createPayment`'s signature differs (extra `token` parameter). Is a dual-interface shim in the reference contract the right answer, or should we recommend that native-ETH deployments simply remain on the narrower interface?
2. **Balance-delta vs. declared amount.** The draft credits the measured delta. Some designs instead require the declared and received amounts to match (reverting for fee-on-transfer tokens rather than accepting them silently). Which default is better for A2A agents that may not know in advance which tokens are fee-on-transfer?
3. **Allowlist: deployer-controlled or DAO-controlled?** The current design is owner-gated (`Ownable`). For a canonical reference deployment, should there be a governance mechanism, or is per-deployment curation the right model?
4. **Oracle aggregator as a separate ERC?** `IOracleAggregator` is currently a repository-local interface. If oracle-mediated escrow release is useful beyond this standard, should `IOracleAggregator` be proposed as its own ERC with a registry?
5. **Rebasing tokens.** The draft documents the risks but does not mandate handling. Should a conformant implementation be REQUIRED to reject rebasing tokens outright, or is the allowlist-plus-documentation approach sufficient?
6. **`string` requestId cost in ERC-20 profile.** The ERC-20 `transferFrom` adds approximately 25k–46k gas on top of the string-storage cost from the base standard. For high-frequency micropayments in stablecoins, is the combined gas cost acceptable, or does this standard need a `bytes32`-keyed variant?

Happy to hear that the generalization is wrong-shaped for an ERC, or that the ETH-profile backward-compat story needs more thought. Would rather resolve it here than after submission.
