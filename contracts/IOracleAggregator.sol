// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title IOracleAggregator
 * @notice Interface AgentEscrow consults to authorize attestation-based release.
 *
 * @dev The aggregator owns the policy-attestation rules (K-of-N threshold,
 *      operator registry, signature scheme). AgentEscrow only asks: "given
 *      this `policyHash` and `attestationHash`, are these `signatures` enough
 *      to release?" — yes/no.
 *
 *      Separating the aggregator from the escrow lets the attestation rules
 *      evolve (FROST aggregation, optimistic challenge mode, slashing)
 *      without touching the escrow contract.
 *
 *      See `kcolbchain/escrow-oracles` for the concrete spec and reference
 *      aggregator implementations.
 */
interface IOracleAggregator {
    /// @notice True iff `attestationHash` is supported by enough valid
    ///         signatures from registered oracles for `policyHash`'s ruleset.
    /// @param policyHash      keccak256 of the canonical policy JSON (escrow has this)
    /// @param attestationHash keccak256 of the canonical attestation transcript
    /// @param signatures      Array of oracle signatures over `attestationHash`
    function verifyRelease(
        bytes32 policyHash,
        bytes32 attestationHash,
        bytes[] calldata signatures
    ) external view returns (bool);
}
