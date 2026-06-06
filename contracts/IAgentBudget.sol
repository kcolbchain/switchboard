// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IAgentBudget {
    function recordSpend(address agent, uint256 gasAmount) external;
}
