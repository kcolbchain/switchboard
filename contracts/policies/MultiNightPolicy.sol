// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IRefundPolicy} from "./IRefundPolicy.sol";

contract MultiNightPolicy is IRefundPolicy {
    function evaluate(
        bytes32,
        bytes calldata policyData,
        uint256 blocksSinceCreation
    ) external pure returns (uint256 refundable_to_payer, uint256 releasable_to_payee, bool terminal) {
        (uint256 totalAmount, uint256 blocksPerNight, uint256 totalNights) = abi.decode(policyData, (uint256, uint256, uint256));
        
        uint256 nightsElapsed = blocksSinceCreation / blocksPerNight;
        if (nightsElapsed > totalNights) {
            nightsElapsed = totalNights;
        }
        
        uint256 amountPerNight = totalAmount / totalNights;
        uint256 releasable = amountPerNight * nightsElapsed;
        uint256 refundable = totalAmount - releasable;
        
        bool terminalState = (nightsElapsed == totalNights);
        return (refundable, releasable, terminalState);
    }
}
