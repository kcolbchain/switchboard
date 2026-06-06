// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IRefundPolicy {
    /// @notice Evaluates the current refund/release amounts.
    /// @param paymentKey The unique key identifying the payment.
    /// @param policyData The custom data passed when the payment was created.
    /// @param blocksSinceCreation The number of blocks elapsed since the payment was created.
    /// @return refundable_to_payer The amount in wei that should be refunded to the payer.
    /// @return releasable_to_payee The amount in wei that should be released to the payee.
    /// @return terminal True if this evaluation resolves the payment fully.
    function evaluate(
        bytes32 paymentKey,
        bytes calldata policyData,
        uint256 blocksSinceCreation
    ) external view returns (uint256 refundable_to_payer, uint256 releasable_to_payee, bool terminal);
}
