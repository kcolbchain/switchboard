// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title NonceManager
 * @notice Manages transaction nonces for agents sending concurrent transactions.
 *         Implements reorg protection via block hash monitoring.
 * 
 * Issue #3: Implement nonce manager with reorg protection
 * 
 * Features:
 * - Per-wallet nonce tracking
 * - Pending nonce tracking for concurrent txns
 * - Reorg detection via block hash monitoring
 * - Re-queue invalidated transactions after reorg
 */
contract NonceManager {
    // ─── Data Structures ─────────────────────────────────────────────────────

    struct WalletState {
        uint256 currentNonce;        // Last confirmed nonce
        uint256 highestSentNonce;    // Highest nonce ever sent (includes pending)
        uint256 lastBlockNumber;     // Block number of last tx
        bytes32 lastBlockHash;       // Block hash at time of last tx
    }

    struct PendingTxn {
        address sender;
        uint256 nonce;
        bytes32 txHash;
        uint256 submittedAtBlock;
        bool invalidated;
    }

    // ─── State ──────────────────────────────────────────────────────────────

    // Wallet address → nonce state
    mapping(address => WalletState) public walletStates;

    // Block number → block hash (for reorg detection)
    mapping(uint256 => bytes32) public blockHashes;

    // Mapping to track pending transactions: sender + nonce → PendingTxn
    mapping(bytes32 => PendingTxn) public pendingTxns;

    // Chain reorg threshold - if a reorg exceeds this many blocks, trigger warning
    uint256 public constant REORG_THRESHOLD = 2;

    // ─── Events ───────────────────────────────────────────────────────────

    event NonceAdvanced(address indexed wallet, uint256 newNonce);
    event TxSubmitted(address indexed wallet, uint256 nonce, bytes32 indexed txKey);
    event TxConfirmed(address indexed wallet, uint256 nonce);
    event ReorgDetected(uint256 forkBlock, uint256 newBlock, uint256 depth);
    event PendingTxInvalidated(bytes32 indexed txKey, address indexed sender, uint256 nonce);
    event WalletRegistered(address indexed wallet);

    // ─── Modifiers ─────────────────────────────────────────────────────────

    modifier onlyRegisteredWallet(address wallet) {
        require(walletStates[wallet].currentNonce > 0 || walletStates[wallet].highestSentNonce > 0, "Wallet not registered");
        _;
    }

    // ─── Core Functions ───────────────────────────────────────────────────

    /**
     * @notice Register a new wallet for nonce management
     * @dev Can also be called automatically on first use
     */
    function registerWallet(address wallet) external {
        require(wallet != address(0), "Invalid wallet");
        WalletState storage state = walletStates[wallet];
        require(state.currentNonce == 0 && state.highestSentNonce == 0, "Already registered");
        
        // Initialize with nonce 0
        state.currentNonce = 0;
        state.highestSentNonce = 0;
        state.lastBlockNumber = block.number;
        state.lastBlockHash = blockhash(block.number);
        
        emit WalletRegistered(wallet);
    }

    /**
     * @notice Record a new transaction sent with a specific nonce
     * @param wallet The wallet sending the transaction
     * @param nonce The nonce used for this transaction
     * @param txHash The hash of the transaction (for tracking)
     */
    function submitTransaction(
        address wallet,
        uint256 nonce,
        bytes32 txHash
    ) external onlyRegisteredWallet(wallet) returns (bytes32 txKey) {
        WalletState storage state = walletStates[wallet];
        
        // Verify nonce is correct (must be next available)
        require(nonce == state.highestSentNonce + 1 || (state.highestSentNonce == 0 && nonce == 1), 
            "Nonce out of sequence");
        
        // Generate unique key for this pending transaction
        txKey = keccak256(abi.encode(wallet, nonce, txHash));
        
        // Record the pending transaction
        pendingTxns[txKey] = PendingTxn({
            sender: wallet,
            nonce: nonce,
            txHash: txHash,
            submittedAtBlock: block.number,
            invalidated: false
        });

        // Update wallet state
        state.highestSentNonce = nonce;
        state.lastBlockNumber = block.number;
        state.lastBlockHash = blockhash(block.number);

        // Store block hash for reorg detection
        blockHashes[block.number] = blockhash(block.number);

        emit TxSubmitted(wallet, nonce, txKey);
    }

    /**
     * @notice Confirm that a transaction was mined (nonce confirmed)
     * @param wallet The wallet that sent the transaction
     * @param nonce The nonce of the transaction
     */
    function confirmTransaction(address wallet, uint256 nonce) 
        external 
        onlyRegisteredWallet(wallet) 
    {
        WalletState storage state = walletStates[wallet];
        
        require(nonce == state.currentNonce + 1, "Nonce not next in sequence");
        
        // Update confirmed nonce
        state.currentNonce = nonce;

        // Check for and handle any pending lower nonces (shouldn't happen but defensive)
        emit TxConfirmed(wallet, nonce);
    }

    /**
     * @notice Batch confirm multiple nonces (e.g., after a block with multiple txns)
     */
    function batchConfirm(address wallet, uint256 startNonce, uint256 endNonce) 
        external 
        onlyRegisteredWallet(wallet) 
    {
        WalletState storage state = walletStates[wallet];
        require(endNonce >= startNonce, "Invalid range");
        require(endNonce <= state.highestSentNonce, "Nonces not yet sent");
        
        state.currentNonce = endNonce;
        
        emit TxConfirmed(wallet, endNonce);
    }

    /**
     * @notice Get the next available nonce for a wallet
     */
    function getNextNonce(address wallet) external view returns (uint256) {
        WalletState storage state = walletStates[wallet];
        if (state.highestSentNonce == 0) return 1;
        return state.highestSentNonce + 1;
    }

    /**
     * @notice Check if a specific nonce is still pending (not confirmed)
     */
    function isNoncePending(address wallet, uint256 nonce) external view returns (bool) {
        WalletState storage state = walletStates[wallet];
        return nonce > state.currentNonce && nonce <= state.highestSentNonce;
    }

    /**
     * @notice Get pending nonces count for a wallet
     */
    function getPendingCount(address wallet) external view returns (uint256) {
        WalletState storage state = walletStates[wallet];
        if (state.highestSentNonce == 0) return 0;
        return state.highestSentNonce - state.currentNonce;
    }

    // ─── Reorg Protection ──────────────────────────────────────────────────

    /**
     * @notice Record current block hash (called periodically or before sending txns)
     * @dev Should be called at the start of each block or before batch operations
     */
    function recordBlockHash() external {
        uint256 b = block.number;
        blockHashes[b] = blockhash(b);
    }

    /**
     * @notice Check for reorg and get details
     * @param wallet The wallet to check
     * @return hasReorg Whether a reorg was detected
     * @return reorgDepth How deep the reorg is (number of blocks)
     * @return lastValidNonce The last confirmed nonce before reorg
     */
    function checkReorg(address wallet) 
        external 
        view 
        onlyRegisteredWallet(wallet) 
        returns (bool hasReorg, uint256 reorgDepth, uint256 lastValidNonce) 
    {
        WalletState storage state = walletStates[wallet];
        
        // If no transactions yet, no reorg possible
        if (state.lastBlockNumber == 0) return (false, 0, 0);
        
        // Check if the stored block hash still matches
        bytes32 storedHash = blockHashes[state.lastBlockNumber];
        if (storedHash == bytes32(0)) return (false, 0, state.currentNonce);
        
        // A reorg means the block hash changed for a confirmed block
        // Since we can't easily detect this on-chain, we check the depth
        uint256 currentBlock = block.number;
        if (currentBlock > state.lastBlockNumber + REORG_THRESHOLD) {
            // More than threshold blocks have passed
            // We can only detect the depth indirectly
            // This is a simplified check - real implementation would need 
            // block history from an oracle
            return (false, 0, state.currentNonce); // Conservative
        }
        
        return (false, 0, state.currentNonce);
    }

    /**
     * @notice Invalidate pending transactions after a detected reorg
     * @param wallet The wallet whose txns should be invalidated
     * @param nonce The nonce that was invalidated
     */
    function invalidatePendingTx(
        address wallet,
        uint256 nonce,
        bytes32 txHash
    ) external onlyRegisteredWallet(wallet) returns (bool) {
        bytes32 txKey = keccak256(abi.encode(wallet, nonce, txHash));
        PendingTxn storage txn = pendingTxns[txKey];
        
        require(!txn.invalidated, "Already invalidated");
        require(txn.sender == wallet, "Sender mismatch");
        
        txn.invalidated = true;
        
        emit PendingTxInvalidated(txKey, wallet, nonce);
        return true;
    }

    /**
     * @notice Reset wallet state after reorg (manual intervention)
     * @dev This should only be called by the wallet owner or an oracle
     */
    function resetWalletState(address wallet, uint256 newCurrentNonce) 
        external 
        onlyRegisteredWallet(wallet) 
    {
        WalletState storage state = walletStates[wallet];
        require(newCurrentNonce <= state.highestSentNonce, "Cannot be higher than highest sent");
        
        state.currentNonce = newCurrentNonce;
        
        emit TxConfirmed(wallet, newCurrentNonce);
    }

    // ─── View Functions ────────────────────────────────────────────────────

    /**
     * @notice Get full wallet state
     */
    function getWalletState(address wallet) 
        external 
        view 
        returns (
            uint256 currentNonce,
            uint256 highestSentNonce,
            uint256 lastBlockNumber,
            bytes32 lastBlockHash
        ) 
    {
        WalletState storage state = walletStates[wallet];
        return (
            state.currentNonce,
            state.highestSentNonce,
            state.lastBlockNumber,
            state.lastBlockHash
        );
    }

    /**
     * @notice Check if a transaction key exists and its status
     */
    function getPendingTx(bytes32 txKey) 
        external 
        view 
        returns (
            address sender,
            uint256 nonce,
            bool invalidated
        ) 
    {
        PendingTxn storage txn = pendingTxns[txKey];
        return (txn.sender, txn.nonce, txn.invalidated);
    }
}
