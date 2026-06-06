// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IRefundPolicy} from "./IRefundPolicy.sol";

contract TimeoutPolicy is IRefundPolicy {
    function evaluate(
        bytes32,
        bytes calldata policyData,
        uint256 blocksSinceCreation
    ) external pure returns (uint256 refundable_to_payer, uint256 releasable_to_payee, bool terminal) {
        (uint256 amount, uint256 timeoutBlocks, uint256 challengePeriod) = abi.decode(policyData, (uint256, uint256, uint256));
        
        if (blocksSinceCreation >= timeoutBlocks + challengePeriod) {
            return (amount, 0, true);
        }
        return (0, 0, false);
    }
}
