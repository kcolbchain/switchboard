# AgentEscrow Genesis Migration Plan

Status: draft for issue #86
Last updated: 2026-06-06

## Summary

`AgentEscrow` should ship as an ordinary Solidity contract placed into genesis state at a fixed well-known address, not as a system contract that can only be changed through chain forks. The genesis deployment should be byte-for-byte equivalent to a normal `forge create` deployment with the same constructor arguments, zero escrow balance, Council multisig ownership, and no in-flight payments.

This plan defines the address-selection process, allowed state transitions during testnet-to-mainnet cutover, the smoke-test corpus for proving genesis parity, and the future upgrade path.

## Goals

- Keep `AgentEscrow` redeployable by governance without giving kcolbchain system-contract control over downstream chains.
- Make genesis state auditable before mainnet launch.
- Prove the genesis deployment behaves the same as a fresh contract deployment.
- Preserve in-flight payment safety during testnet and mainnet cutovers.

## Non-Goals

- This document does not change `AgentEscrow.sol`.
- This document does not choose the final mainnet address by itself.
- This document does not define bridge or oracle committee governance beyond the owner and cutover checks needed for `AgentEscrow`.

## Target Address

The candidate address for the genesis deploy is:

```text
0x000000000000000000000000000000000000A002
```

The final address MUST be confirmed against the Lux subnet genesis config before mainnet freeze. The address is valid only if all of the following are true:

| Check | Requirement |
| --- | --- |
| Namespace collision | No existing precompile, system contract, bridge, oracle, or reserved account uses the address. |
| Explorer metadata | The address can be labelled as `AgentEscrow` before public testnet cutover. |
| Genesis reproducibility | The genesis account code hash matches the locally deployed `AgentEscrow` runtime bytecode hash. |
| Operator documentation | Validators can find the address in public docs before mainnet genesis state ships. |

If any check fails, the Council should choose the next reserved application address in the `0xA0xx` range and repeat the same checks.

## Genesis State

The genesis allocation for `AgentEscrow` MUST contain contract code and constructor-derived storage only. It MUST NOT carry testnet payment state into mainnet.

| Field | Required value at genesis |
| --- | --- |
| ETH balance | `0` |
| Owner | Council multisig |
| Chain id constructor arg | Mainnet chain id |
| Oracle aggregator constructor arg | Mainnet aggregator address, or `address(0)` if attestation release is not enabled at launch |
| In-flight payments | none |
| Registered agents | none unless explicitly approved in the public RFC |
| Paused/deprecated flag | not applicable unless added by a future contract version |

The testnet genesis state MAY use a test Council multisig and test oracle aggregator, but it MUST retain the same zero-balance and no-in-flight-payment invariants.

## Valid Cutover State Transitions

Cutover has three phases.

| Phase | Allowed actions | Disallowed actions |
| --- | --- | --- |
| Pre-freeze | Deploy test contracts, run payments, update docs, publish candidate address. | Claim finality for any address not yet confirmed in subnet config. |
| Freeze window | Stop creating new testnet payments, snapshot open request ids, run genesis parity tests, publish RFC. | Mutate genesis code, migrate in-flight testnet payments into mainnet, change owner without Council sign-off. |
| Mainnet launch | Ship zero-balance genesis deploy, verify owner, verify code hash, run smoke tests. | Treat testnet tx hashes as mainnet continuity, accept payments before owner and code hash are confirmed. |

In-flight testnet payments should be settled, refunded, or explicitly abandoned before the freeze window closes. They MUST NOT be copied into mainnet storage. Mainnet payment request ids should be domain-separated from testnet ids at the application layer.

## Constructor and Storage Invariants

The smoke-test suite should assert the following invariants immediately after genesis import and after a fresh `forge create` deployment:

| Invariant | Expected result |
| --- | --- |
| Runtime code hash | Genesis deployment equals fresh deployment runtime bytecode. |
| Owner | `owner()` returns the Council multisig. |
| Chain id | Any exposed chain-id dependent state matches the configured mainnet chain id. |
| Oracle aggregator | Aggregator address equals the launch configuration. |
| Balance | Contract balance is zero. |
| Payment lookup | A never-created request id returns empty/default state. |
| Agent registry | No unapproved agent is registered. |

If constructor arguments make exact storage roots differ across testnet and mainnet, the test should compare the expected decoded fields rather than asserting a single hard-coded storage root.

## Smoke Test Corpus

The implementation target for this plan is `test/genesis_cutover.t.sol`.

| Test | Purpose |
| --- | --- |
| `testGenesisRuntimeCodeMatchesFreshDeploy` | Compare runtime bytecode hash of the genesis address and a fresh `new AgentEscrow(...)`. |
| `testGenesisOwnerIsCouncilMultisig` | Ensure ownership is not left with a deployer EOA. |
| `testGenesisStartsWithNoBalanceOrPayments` | Assert zero ETH balance and no initialized request state. |
| `testCreatePaymentHappyPathAfterGenesis` | Run `createPayment{value: 0.01 ether}` and release through the payer-confirmed happy path. |
| `testTimeoutRefundAfterGenesis` | Create a payment, advance blocks past timeout, and verify payer refund behavior. |
| `testChallengePathAfterGenesis` | Exercise challenge-period behavior using the existing AgentEscrow test pattern. |
| `testCouncilOwnershipTransfer` | Transfer ownership from Council multisig to a replacement Council address and assert old owner cannot mutate registry state. |
| `testPreGenesisRequestIdDoesNotCollide` | Use a testnet-style request id and a mainnet-domain request id, proving application-level separation. |

The first test proves bytecode parity. The remaining tests prove the genesis deployment is operationally equivalent to a fresh deployment.

## Testnet Dry Run

At least 14 days before mainnet, run a public testnet cutover dry run:

1. Publish the candidate address, owner, chain id, oracle aggregator, and expected runtime bytecode hash.
2. Stop creating new testnet payments for the dry-run window.
3. Resolve or list all in-flight testnet request ids.
4. Import the genesis state into a new testnet fork.
5. Run `test/genesis_cutover.t.sol`.
6. Publish a short report with the code hash, owner, smoke-test output, and any unresolved gaps.

The mainnet launch should not proceed until the dry-run report has no critical failures.

## Upgrade Path

Future upgrades should follow this path:

1. Council vote approves `AgentEscrow_v2` and the migration window.
2. Deploy `AgentEscrow_v2` at a new well-known address.
3. Publish a deprecation notice for v1, including the final block for new v1 payments.
4. Keep v1 live long enough for existing locked payments to release, refund, cancel, or be manually resolved.
5. Update the chain proxy/router or application config to point new payments at v2.
6. Archive v1 docs with final state, event ranges, and migration notes.

This is explicitly not a system-contract upgrade-via-fork. Forks may include a new genesis allocation for new networks, but live-network upgrades should be governed redeployments plus routing updates.

## Public RFC Checklist

The public RFC should be posted at least 14 days before mainnet and include:

- Final `AgentEscrow` address.
- Council multisig address and signer policy.
- Oracle aggregator address or launch-time disabled status.
- Runtime bytecode hash.
- Constructor arguments.
- Smoke-test command and output.
- Testnet dry-run report link.
- Migration and deprecation path for future versions.

## Open Questions

- Is `0x000000000000000000000000000000000000A002` reserved in all target Lux subnet configurations?
- Will launch enable oracle-attested release, or should the genesis constructor use `address(0)` for the aggregator?
- Should registered agents be empty at genesis, or should a small allowlist be approved in the public RFC?
- Which public surface should host the dry-run report: repository docs, kcolbchain.com, or both?

