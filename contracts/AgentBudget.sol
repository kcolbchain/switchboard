// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

contract AgentBudget is Ownable {
    struct Budget {
        uint256 epoch; // timestamp
        uint256 hourlyCap;
        uint256 dailyCap;
        uint256 hourlySpent;
        uint256 dailySpent;
        uint256 lastResetBlock;
    }

    mapping(address => Budget) public budgets;
    mapping(address => bool) public isUpdater;

    event SpendRecorded(address indexed agent, uint256 amount);
    event CapWaived(address indexed agent, uint256 newHourly, uint256 newDaily);

    modifier onlyUpdater() {
        require(isUpdater[msg.sender] || msg.sender == owner(), "Not authorized updater");
        _;
    }

    constructor() Ownable(msg.sender) {}

    function setUpdater(address updater, bool authorized) external onlyOwner {
        isUpdater[updater] = authorized;
    }

    function setCaps(address agent, uint256 hourlyCap, uint256 dailyCap) external onlyOwner {
        budgets[agent].hourlyCap = hourlyCap;
        budgets[agent].dailyCap = dailyCap;
    }

    function recordSpend(address agent, uint256 gasAmount) external onlyUpdater {
        _resetIfNeeded(agent);
        budgets[agent].hourlySpent += gasAmount;
        budgets[agent].dailySpent += gasAmount;
        emit SpendRecorded(agent, gasAmount);
    }

    function waiveEpoch(address agentId, uint256 newHourlyCap, uint256 newDailyCap) external onlyOwner {
        _resetIfNeeded(agentId);
        budgets[agentId].hourlyCap = newHourlyCap;
        budgets[agentId].dailyCap = newDailyCap;
        emit CapWaived(agentId, newHourlyCap, newDailyCap);
    }

    function _resetIfNeeded(address agent) internal {
        Budget storage b = budgets[agent];
        uint256 currentHour = block.timestamp / 3600; 
        uint256 currentDay = block.timestamp / 86400;
        
        uint256 lastHour = b.epoch / 3600;
        uint256 lastDay = b.epoch / 86400;

        if (currentHour > lastHour) {
            b.hourlySpent = 0;
        }
        if (currentDay > lastDay) {
            b.dailySpent = 0;
        }
        if (block.timestamp > b.epoch) {
            b.epoch = block.timestamp;
            b.lastResetBlock = block.number;
        }
    }
}
