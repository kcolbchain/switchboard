// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IRefundPolicy} from "./IRefundPolicy.sol";

contract ProRataPolicy is IRefundPolicy {
    function evaluate(
        bytes32,
        bytes calldata policyData,
        uint256 blocksSinceCreation
    ) external pure returns (uint256 refundable_to_payer, uint256 releasable_to_payee, bool terminal) {
        (uint256 totalAmount, uint256 totalBlocks) = abi.decode(policyData, (uint256, uint256));
        
        if (blocksSinceCreation >= totalBlocks) {
            return (0, totalAmount, true); 
        }
        
        uint256 releasable = (totalAmount * blocksSinceCreation) / totalBlocks;
        uint256 refundable = totalAmount - releasable;
        
        return (refundable, releasable, false);
    }
}
