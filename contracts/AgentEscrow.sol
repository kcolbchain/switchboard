// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/// @title AgentEscrow
/// @notice Lightweight escrow for agent-to-agent payments with timeout refund
///         and optional multi-signature release.
contract AgentEscrow is ReentrancyGuard {
    using SafeERC20 for IERC20;

    // -------------------------------------------------------------------------
    // Types
    // -------------------------------------------------------------------------

    enum Status { Created, Released, Refunded }

    struct Escrow {
        address buyer;
        address seller;
        address token;      // address(0) = native ETH
        uint256 amount;
        uint256 deadline;
        uint256 nonce;
        Status  status;
        // Multi-sig fields
        address[] approvers;
        uint256 threshold;
        uint256 approvalCount;
    }

    // -------------------------------------------------------------------------
    // State
    // -------------------------------------------------------------------------

    uint256 public nextEscrowId;

    /// escrowId => Escrow
    mapping(uint256 => Escrow) public escrows;

    /// escrowId => approver => bool
    mapping(uint256 => mapping(address => bool)) public hasApproved;

    /// buyer => nonce => bool  (replay protection)
    mapping(address => mapping(uint256 => bool)) public usedNonces;

    // -------------------------------------------------------------------------
    // Events
    // -------------------------------------------------------------------------

    event EscrowCreated(
        uint256 indexed escrowId,
        address indexed buyer,
        address indexed seller,
        address token,
        uint256 amount,
        uint256 deadline,
        uint256 nonce
    );

    event EscrowReleased(uint256 indexed escrowId);

    event EscrowRefunded(uint256 indexed escrowId);

    event ApprovalSubmitted(uint256 indexed escrowId, address indexed approver);

    // -------------------------------------------------------------------------
    // Errors
    // -------------------------------------------------------------------------

    error InvalidSeller();
    error InvalidAmount();
    error DeadlineTooSoon();
    error NonceAlreadyUsed();
    error IncorrectETHValue();
    error EscrowNotCreated();
    error NotBuyer();
    error DeadlineNotReached();
    error ThresholdNotReached();
    error AlreadyApproved();
    error NotApprover();
    error InvalidThreshold();

    // -------------------------------------------------------------------------
    // Core Functions
    // -------------------------------------------------------------------------

    /// @notice Create a new escrow and deposit funds.
    /// @dev For ETH escrows, send the exact `amount` as msg.value.
    ///      For ERC-20 escrows, the caller must have approved this contract.
    /// @param seller     Recipient of funds on successful delivery.
    /// @param token      ERC-20 token address; address(0) for native ETH.
    /// @param amount     Amount in wei / smallest unit.
    /// @param deadline   Unix timestamp after which a refund is possible.
    /// @param nonce      Buyer-chosen nonce (must not have been used before).
    /// @param approvers  Addresses required to approve release (may be empty).
    /// @param threshold  Number of approvals needed (0 = buyer-only release).
    /// @return escrowId  The identifier of the newly created escrow.
    function createEscrow(
        address seller,
        address token,
        uint256 amount,
        uint256 deadline,
        uint256 nonce,
        address[] calldata approvers,
        uint256 threshold
    ) external payable nonReentrant returns (uint256 escrowId) {
        if (seller == address(0)) revert InvalidSeller();
        if (amount == 0) revert InvalidAmount();
        if (deadline <= block.timestamp) revert DeadlineTooSoon();
        if (usedNonces[msg.sender][nonce]) revert NonceAlreadyUsed();
        if (threshold > approvers.length) revert InvalidThreshold();

        usedNonces[msg.sender][nonce] = true;

        // Transfer funds into the contract
        if (token == address(0)) {
            if (msg.value != amount) revert IncorrectETHValue();
        } else {
            if (msg.value != 0) revert IncorrectETHValue();
            IERC20(token).safeTransferFrom(msg.sender, address(this), amount);
        }

        escrowId = nextEscrowId++;

        Escrow storage e = escrows[escrowId];
        e.buyer     = msg.sender;
        e.seller    = seller;
        e.token     = token;
        e.amount    = amount;
        e.deadline  = deadline;
        e.nonce     = nonce;
        e.status    = Status.Created;
        e.threshold = threshold;

        for (uint256 i = 0; i < approvers.length; i++) {
            e.approvers.push(approvers[i]);
        }

        emit EscrowCreated(escrowId, msg.sender, seller, token, amount, deadline, nonce);
    }

    /// @notice Release escrowed funds to the seller.
    /// @dev Only the buyer can call.  If a multi-sig threshold is set the
    ///      required number of approvals must have been submitted first.
    function releaseEscrow(uint256 escrowId) external nonReentrant {
        Escrow storage e = escrows[escrowId];
        if (e.status != Status.Created) revert EscrowNotCreated();
        if (msg.sender != e.buyer) revert NotBuyer();
        if (e.threshold > 0 && e.approvalCount < e.threshold) revert ThresholdNotReached();

        e.status = Status.Released;

        _transferOut(e.token, e.seller, e.amount);

        emit EscrowReleased(escrowId);
    }

    /// @notice Refund escrowed funds to the buyer after the deadline.
    /// @dev Anyone may call once the deadline has passed.
    function refundEscrow(uint256 escrowId) external nonReentrant {
        Escrow storage e = escrows[escrowId];
        if (e.status != Status.Created) revert EscrowNotCreated();
        if (block.timestamp <= e.deadline) revert DeadlineNotReached();

        e.status = Status.Refunded;

        _transferOut(e.token, e.buyer, e.amount);

        emit EscrowRefunded(escrowId);
    }

    /// @notice Submit an approval for multi-sig release.
    function approveRelease(uint256 escrowId) external {
        Escrow storage e = escrows[escrowId];
        if (e.status != Status.Created) revert EscrowNotCreated();
        if (hasApproved[escrowId][msg.sender]) revert AlreadyApproved();

        bool isApprover = false;
        for (uint256 i = 0; i < e.approvers.length; i++) {
            if (e.approvers[i] == msg.sender) {
                isApprover = true;
                break;
            }
        }
        if (!isApprover) revert NotApprover();

        hasApproved[escrowId][msg.sender] = true;
        e.approvalCount++;

        emit ApprovalSubmitted(escrowId, msg.sender);
    }

    // -------------------------------------------------------------------------
    // Views
    // -------------------------------------------------------------------------

    function getEscrow(uint256 escrowId)
        external
        view
        returns (
            address buyer,
            address seller,
            address token,
            uint256 amount,
            uint256 deadline,
            uint256 nonce,
            Status  status,
            uint256 approvalCount,
            uint256 threshold
        )
    {
        Escrow storage e = escrows[escrowId];
        return (
            e.buyer, e.seller, e.token, e.amount,
            e.deadline, e.nonce, e.status,
            e.approvalCount, e.threshold
        );
    }

    // -------------------------------------------------------------------------
    // Internal
    // -------------------------------------------------------------------------

    function _transferOut(address token, address to, uint256 amount) internal {
        if (token == address(0)) {
            (bool ok, ) = to.call{value: amount}("");
            require(ok, "ETH transfer failed");
        } else {
            IERC20(token).safeTransfer(to, amount);
        }
    }
}
