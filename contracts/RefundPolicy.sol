// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

interface IRefundPolicy {
    function canRefund(bytes32 paymentId, address payer) external view returns (bool);
    function policyType() external pure returns (string memory);
}

contract TimeoutRefundPolicy is IRefundPolicy {
    uint256 public immutable timeoutBlocks;
    constructor(uint256 _timeout) { timeoutBlocks = _timeout; }
    function canRefund(bytes32, address) external view override returns (bool) { return true; }
    function policyType() external pure override returns (string memory) { return "timeout"; }
}

contract MilestoneRefundPolicy is IRefundPolicy {
    mapping(bytes32 => uint256) public completedMilestones;
    uint256 public immutable requiredMilestones;
    constructor(uint256 _required) { requiredMilestones = _required; }
    function completeMilestone(bytes32 paymentId) external { completedMilestones[paymentId]++; }
    function canRefund(bytes32 paymentId, address) external view override returns (bool) {
        return completedMilestones[paymentId] < requiredMilestones;
    }
    function policyType() external pure override returns (string memory) { return "milestone"; }
}
