// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;
import "./RefundPolicy.sol";

contract RefundPolicyRegistry {
    mapping(bytes32 => address) public paymentPolicies;
    event PolicySet(bytes32 indexed paymentId, address indexed policy);

    function setPolicy(bytes32 paymentId, address policy) external {
        require(policy != address(0), "invalid policy");
        paymentPolicies[paymentId] = policy;
        emit PolicySet(paymentId, policy);
    }

    function canRefund(bytes32 paymentId, address payer) external view returns (bool) {
        address policy = paymentPolicies[paymentId];
        if (policy == address(0)) return false;
        return IRefundPolicy(policy).canRefund(paymentId, payer);
    }
}
