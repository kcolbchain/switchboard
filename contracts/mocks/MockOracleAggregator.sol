// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IOracleAggregator} from "../IOracleAggregator.sol";

/**
 * @title MockOracleAggregator
 * @notice Test-only aggregator that returns a deterministic verify result
 *         driven by storage flags. Lets tests cover the full release-by-
 *         attestation path without depending on a real signature scheme.
 *
 *         Production aggregators live in `kcolbchain/escrow-oracles` and
 *         implement real K-of-N threshold verification (ECDSA in v1,
 *         FROST in v2).
 */
contract MockOracleAggregator is IOracleAggregator {
    /// @dev `accept[policyHash][attestationHash]` flips the aggregator to
    ///      "yes, release". Default is always-reject.
    mapping(bytes32 => mapping(bytes32 => bool)) public accept;

    function setAccept(bytes32 policyHash, bytes32 attestationHash, bool ok) external {
        accept[policyHash][attestationHash] = ok;
    }

    function verifyRelease(
        bytes32 policyHash,
        bytes32 attestationHash,
        bytes[] calldata /* signatures */
    ) external view override returns (bool) {
        return accept[policyHash][attestationHash];
    }
}
