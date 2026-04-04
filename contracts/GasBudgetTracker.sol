// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title GasBudgetTracker
 * @notice Tracks cumulative gas spent per wallet with configurable limits.
 * 
 * Issue #5: Add gas budget tracker with configurable limits
 * 
 * Features:
 * - Track cumulative gas spent per wallet
 * - Configurable max gas per hour and per day
 * - Pause execution when budget exhausted
 * - Include pytest tests (written in tests/test_gas_tracker.py)
 */
contract GasBudgetTracker {
    // ─── Data Structures ─────────────────────────────────────────────────────

    struct WalletBudget {
        uint256 totalSpentWei;      // Total ETH spent on gas (refunded portion not tracked here)
        uint256 currentPeriodSpent; // Spent in current period
        uint256 periodStartBlock;   // Block when current period started
        uint256 lastResetBlock;     // Block of last daily reset
        uint256 hourlyLimit;        // Max gas cost per hour (in wei equivalent)
        uint256 dailyLimit;        // Max gas cost per day (in wei equivalent)
        bool paused;                // Whether execution is paused
        uint256 pauseReason;        // 0=none, 1=hourly exceeded, 2=daily exceeded
    }

    // ─── Constants ─────────────────────────────────────────────────────────

    uint256 public constant BLOCKS_PER_HOUR = 300; // ~12 sec blocks
    uint256 public constant BLOCKS_PER_DAY = 7200;

    // ─── State ──────────────────────────────────────────────────────────────

    mapping(address => WalletBudget) public budgets;

    // Global gas price feed (can be set to Chainlink or other oracle)
    mapping(address => uint256) public gasPriceFeeds;

    // ─── Events ───────────────────────────────────────────────────────────

    event WalletAdded(address indexed wallet, uint256 hourlyLimit, uint256 dailyLimit);
    event BudgetSet(address indexed wallet, uint256 hourlyLimit, uint256 dailyLimit);
    event GasSpentRecorded(address indexed wallet, uint256 gasUsed, uint256 costWei, uint256 remainingHourly, uint256 remainingDaily);
    event ExecutionPaused(address indexed wallet, uint256 reason);
    event ExecutionResumed(address indexed wallet);
    event HourlyReset(address indexed wallet, uint256 newPeriodStart);
    event DailyReset(address indexed wallet, uint256 newDayStart);
    event GasPriceUpdated(address indexed wallet, uint256 newPriceWei);

    // ─── Configuration ────────────────────────────────────────────────────

    /**
     * @notice Add a new wallet with budget limits
     * @param wallet The wallet to add
     * @param hourlyLimit Max gas cost per hour (in wei)
     * @param dailyLimit Max gas cost per day (in wei)
     */
    function addWallet(address wallet, uint256 hourlyLimit, uint256 dailyLimit) external {
        require(wallet != address(0), "Invalid wallet");
        require(hourlyLimit > 0 && dailyLimit > 0, "Limits must be > 0");
        require(dailyLimit >= hourlyLimit, "Daily limit must be >= hourly limit");
        require(budgets[wallet].hourlyLimit == 0, "Wallet already added");

        uint256 currentBlock = block.number;

        budgets[wallet] = WalletBudget({
            totalSpentWei: 0,
            currentPeriodSpent: 0,
            periodStartBlock: currentBlock,
            lastResetBlock: currentBlock,
            hourlyLimit: hourlyLimit,
            dailyLimit: dailyLimit,
            paused: false,
            pauseReason: 0
        });

        emit WalletAdded(wallet, hourlyLimit, dailyLimit);
    }

    /**
     * @notice Update budget limits for an existing wallet
     */
    function setBudgetLimits(address wallet, uint256 hourlyLimit, uint256 dailyLimit) external {
        require(budgets[wallet].hourlyLimit > 0, "Wallet not added");
        require(dailyLimit >= hourlyLimit, "Daily limit must be >= hourly limit");
        
        budgets[wallet].hourlyLimit = hourlyLimit;
        budgets[wallet].dailyLimit = dailyLimit;
        
        emit BudgetSet(wallet, hourlyLimit, dailyLimit);
    }

    // ─── Gas Tracking ─────────────────────────────────────────────────────

    /**
     * @notice Record gas spent for a transaction
     * @param wallet The wallet that spent the gas
     * @param gasUsed Actual gas used by the transaction
     * @param gasPrice Gas price in wei at time of transaction
     */
    function recordGas(address wallet, uint256 gasUsed, uint256 gasPrice) external {
        WalletBudget storage budget = budgets[wallet];
        require(budget.hourlyLimit > 0, "Wallet not tracked");

        // Check and handle period resets
        _checkHourlyReset(wallet, budget);
        _checkDailyReset(wallet, budget);

        // Calculate cost in wei
        uint256 costWei = gasUsed * gasPrice;
        
        // Update spent amounts
        budget.currentPeriodSpent += costWei;
        budget.totalSpentWei += costWei;

        // Check if limits exceeded
        bool hourlyExceeded = budget.currentPeriodSpent > budget.hourlyLimit;
        bool dailyExceeded = budget.currentPeriodSpent > budget.dailyLimit;

        if (hourlyExceeded && !budget.paused) {
            budget.paused = true;
            budget.pauseReason = 1; // hourly exceeded
            emit ExecutionPaused(wallet, 1);
        } else if (dailyExceeded && !budget.paused) {
            budget.paused = true;
            budget.pauseReason = 2; // daily exceeded
            emit ExecutionPaused(wallet, 2);
        }

        uint256 remainingHourly = budget.hourlyLimit > budget.currentPeriodSpent 
            ? budget.hourlyLimit - budget.currentPeriodSpent 
            : 0;
        uint256 remainingDaily = budget.dailyLimit > budget.currentPeriodSpent
            ? budget.dailyLimit - budget.currentPeriodSpent
            : 0;

        emit GasSpentRecorded(wallet, gasUsed, costWei, remainingHourly, remainingDaily);
    }

    // ─── Internal Reset Logic ──────────────────────────────────────────────

    function _checkHourlyReset(address wallet, WalletBudget storage budget) internal {
        uint256 blocksSincePeriodStart = block.number - budget.periodStartBlock;
        if (blocksSincePeriodStart >= BLOCKS_PER_HOUR) {
            // Hourly period reset
            budget.periodStartBlock = block.number;
            budget.currentPeriodSpent = 0;
            budget.paused = false;
            budget.pauseReason = 0;
            emit HourlyReset(wallet, block.number);
        }
    }

    function _checkDailyReset(address wallet, WalletBudget storage budget) internal {
        uint256 blocksSinceDailyReset = block.number - budget.lastResetBlock;
        if (blocksSinceDailyReset >= BLOCKS_PER_DAY) {
            // Daily reset (reset everything)
            budget.lastResetBlock = block.number;
            budget.periodStartBlock = block.number;
            budget.currentPeriodSpent = 0;
            budget.paused = false;
            budget.pauseReason = 0;
            emit DailyReset(wallet, block.number);
        }
    }

    // ─── Pause/Resume ─────────────────────────────────────────────────────

    /**
     * @notice Manually pause execution for a wallet
     */
    function pauseWallet(address wallet) external {
        WalletBudget storage budget = budgets[wallet];
        require(budget.hourlyLimit > 0, "Wallet not tracked");
        require(!budget.paused, "Already paused");
        
        budget.paused = true;
        budget.pauseReason = 99; // manual pause
        
        emit ExecutionPaused(wallet, 99);
    }

    /**
     * @notice Resume execution for a wallet (manual override)
     */
    function resumeWallet(address wallet) external {
        WalletBudget storage budget = budgets[wallet];
        require(budget.hourlyLimit > 0, "Wallet not tracked");
        
        budget.paused = false;
        budget.pauseReason = 0;
        
        emit ExecutionResumed(wallet);
    }

    // ─── Query Functions ──────────────────────────────────────────────────

    /**
     * @notice Check if a wallet can execute (not paused and has budget)
     */
    function canExecute(address wallet) external view returns (bool, uint256) {
        WalletBudget storage budget = budgets[wallet];
        if (budget.hourlyLimit == 0) return (true, 0); // Not tracked, allow
        return (!budget.paused, budget.pauseReason);
    }

    /**
     * @notice Get remaining budget for a wallet
     */
    function getRemainingBudget(address wallet) external view returns (uint256 hourlyRemaining, uint256 dailyRemaining) {
        WalletBudget storage budget = budgets[wallet];
        if (budget.hourlyLimit == 0) return (0, 0); // Not tracked
        
        hourlyRemaining = budget.hourlyLimit > budget.currentPeriodSpent
            ? budget.hourlyLimit - budget.currentPeriodSpent
            : 0;
        dailyRemaining = budget.dailyLimit > budget.currentPeriodSpent
            ? budget.dailyLimit - budget.currentPeriodSpent
            : 0;
    }

    /**
     * @notice Get full budget state for a wallet
     */
    function getBudgetState(address wallet) external view returns (
        uint256 totalSpentWei,
        uint256 currentPeriodSpent,
        uint256 hourlyLimit,
        uint256 dailyLimit,
        bool paused,
        uint256 pauseReason
    ) {
        WalletBudget storage budget = budgets[wallet];
        return (
            budget.totalSpentWei,
            budget.currentPeriodSpent,
            budget.hourlyLimit,
            budget.dailyLimit,
            budget.paused,
            budget.pauseReason
        );
    }
}
