// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title AgentEscrow
 * @notice Escrow contract for agent-to-agent payments with timeout and refund.
 * @dev Implements a payment protocol:
 *   1. Payer creates escrow with payment + timeout
 *   2. Agent performs work off-chain
 *   3. Payer confirms → funds released to payee
 *   4. Timeout expires → payer can reclaim (after challenge period)
 */
contract AgentEscrow {
    enum State { Created, Locked, Confirmed, Released, Refunded, Cancelled }

    struct Payment {
        address payer;
        address payee;
        uint256 amount;
        uint256 timeoutBlocks;      // blocks until auto-expire
        uint256 challengePeriod;     // blocks payer must wait to reclaim after timeout
        State state;
        string requestId;           // off-chain payment request ID
        uint256 createdAt;
    }

    uint256 public immutable chainId;

    // requestId → Payment
    mapping(string => Payment) public payments;

    // Access control for agents
    mapping(address => bool) public registeredAgents;

    // Events
    event PaymentCreated(string indexed requestId, address indexed payer, address indexed payee, uint256 amount);
    event PaymentLocked(string indexed requestId);
    event PaymentConfirmed(string indexed requestId, address indexed payer);
    event PaymentReleased(string indexed requestId, address indexed payee, uint256 amount);
    event PaymentRefunded(string indexed requestId, address indexed payer, uint256 amount);
    event AgentRegistered(address indexed agent);
    event AgentDeregistered(address indexed agent);

    constructor(uint256 _chainId) {
        chainId = _chainId;
    }

    modifier onlyRegisteredAgent() {
        require(registeredAgents[msg.sender], "Caller is not a registered agent");
        _;
    }

    /**
     * @notice Register an agent address (permissioned)
     */
    function registerAgent(address agent) external {
        registeredAgents[agent] = true;
        emit AgentRegistered(agent);
    }

    /**
     * @notice Create a payment request and lock funds in escrow
     * @param requestId Unique off-chain request ID
     * @param payee Recipient agent address
     * @param timeoutBlocks Blocks until the payment can be auto-expired
     * @param challengePeriod Blocks payer must wait after timeout to reclaim
     */
    function createPayment(
        string calldata requestId,
        address payee,
        uint256 timeoutBlocks,
        uint256 challengePeriod
    ) external payable returns (bool) {
        require(msg.value > 0, "Must send ETH");
        require(bytes(requestId).length > 0, "requestId cannot be empty");
        require(payee != address(0), "payee cannot be zero address");
        require(payments[requestId].createdAt == 0, "requestId already exists");
        require(timeoutBlocks > 0, "timeoutBlocks must be > 0");

        payments[requestId] = Payment({
            payer: msg.sender,
            payee: payee,
            amount: msg.value,
            timeoutBlocks: timeoutBlocks,
            challengePeriod: challengePeriod,
            state: State.Locked,
            requestId: requestId,
            createdAt: block.number
        });

        emit PaymentCreated(requestId, msg.sender, payee, msg.value);
        emit PaymentLocked(requestId);
        return true;
    }

    /**
     * @notice Payer confirms work is done → release funds to payee
     * @dev Can only be called by the original payer. Only in Locked state.
     */
    function confirmPayment(string calldata requestId) external returns (bool) {
        Payment storage p = payments[requestId];
        require(p.payer == msg.sender, "Only payer can confirm");
        require(p.state == State.Locked, "Payment not in Locked state");
        require(block.number < p.createdAt + p.timeoutBlocks, "Payment has expired");

        p.state = State.Released;

        (bool success, ) = p.payee.call{value: p.amount}("");
        require(success, "Transfer to payee failed");

        emit PaymentConfirmed(requestId, msg.sender);
        emit PaymentReleased(requestId, p.payee, p.amount);
        return true;
    }

    /**
     * @notice Payer requests refund after timeout + challenge period
     * @dev After timeout expires AND challenge period passes, payer can reclaim.
     */
    function requestRefund(string calldata requestId) external returns (bool) {
        Payment storage p = payments[requestId];
        require(p.payer == msg.sender, "Only payer can request refund");
        require(p.state == State.Locked, "Payment not in Locked state");
        require(
            block.number >= p.createdAt + p.timeoutBlocks + p.challengePeriod,
            "Challenge period not over"
        );

        p.state = State.Refunded;

        (bool success, ) = p.payer.call{value: p.amount}("");
        require(success, "Refund transfer failed");

        emit PaymentRefunded(requestId, p.payer, p.amount);
        return true;
    }

    /**
     * @notice Cancel a payment before timeout (mutual agreement)
     */
    function cancelPayment(string calldata requestId) external returns (bool) {
        Payment storage p = payments[requestId];
        require(p.payer == msg.sender, "Only payer can cancel");
        require(p.state == State.Locked, "Payment not in Locked state");

        uint256 amount = p.amount;
        p.state = State.Cancelled;
        p.amount = 0;

        (bool success, ) = p.payer.call{value: amount}("");
        require(success, "Cancel refund failed");

        return true;
    }

    /**
     * @notice Get payment details
     */
    function getPayment(string calldata requestId) external view returns (Payment memory) {
        return payments[requestId];
    }

    /**
     * @notice Check if a payment is in a given state
     */
    function isState(string calldata requestId, State expected) external view returns (bool) {
        return payments[requestId].state == expected;
    }

    /**
     * @notice Check if a payment has expired (timeout passed but not yet in refundable window)
     */
    function isExpired(string calldata requestId) external view returns (bool) {
        Payment storage p = payments[requestId];
        if (p.createdAt == 0) return false;
        return block.number >= p.createdAt + p.timeoutBlocks && p.state == State.Locked;
    }
}
