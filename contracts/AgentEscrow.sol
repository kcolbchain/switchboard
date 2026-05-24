// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IOracleAggregator} from "./IOracleAggregator.sol";

/**
 * @title AgentEscrow
 * @notice Escrow contract for agent-to-agent payments with timeout and refund.
 * @dev Implements a payment protocol:
 *   1. Payer creates escrow with payment + timeout (+ optional policyHash)
 *   2. Agent performs work off-chain
 *   3. Payer confirms → funds released to payee
 *      OR oracle aggregator authorizes release via attestation (when policyHash != 0)
 *   4. Timeout expires → payer can reclaim (after challenge period)
 *
 * Backward compatibility:
 *   - The original 4-arg `createPayment(string, address, uint256, uint256)`
 *     is preserved. It is equivalent to passing `policyHash = bytes32(0)`,
 *     which means oracle release is disabled for that payment.
 *   - All existing functions (`confirmPayment`, `requestRefund`,
 *     `cancelPayment`, `getPayment`, `isState`, `isExpired`) are unchanged.
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
        bytes32 policyHash;         // 0x00 = payer-only release; non-zero enables oracle release
    }

    uint256 public immutable chainId;

    /// @notice Oracle aggregator consulted on `releaseByAttestation`. Set
    ///         once at construction. address(0) disables oracle release
    ///         even for payments that declared a non-zero policyHash.
    IOracleAggregator public immutable oracleAggregator;

    // requestId → Payment
    mapping(string => Payment) public payments;

    // Access control for agents
    mapping(address => bool) public registeredAgents;

    // Events
    event PaymentCreated(string indexed requestId, address indexed payer, address indexed payee, uint256 amount);
    event PaymentLocked(string indexed requestId);
    event PaymentConfirmed(string indexed requestId, address indexed payer);
    event PaymentReleased(string indexed requestId, address indexed payee, uint256 amount);
    event PaymentReleasedByOracle(string indexed requestId, bytes32 policyHash, bytes32 attestationHash);
    event PaymentRefunded(string indexed requestId, address indexed payer, uint256 amount);
    event AgentRegistered(address indexed agent);
    event AgentDeregistered(address indexed agent);

    /// @param _chainId       Chain id this contract is deployed on.
    /// @param _aggregator    Optional oracle aggregator. Pass `address(0)`
    ///                       to deploy without oracle-release support;
    ///                       in that case any non-zero `policyHash` in
    ///                       `createPaymentWithPolicy` will revert.
    constructor(uint256 _chainId, IOracleAggregator _aggregator) {
        chainId = _chainId;
        oracleAggregator = _aggregator;
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
     * @notice Create a payment request and lock funds in escrow (payer-only release).
     * @dev    Backward-compatible 4-arg form. Equivalent to
     *         `createPaymentWithPolicy(..., bytes32(0))`.
     */
    function createPayment(
        string calldata requestId,
        address payee,
        uint256 timeoutBlocks,
        uint256 challengePeriod
    ) external payable returns (bool) {
        return _createPayment(requestId, payee, timeoutBlocks, challengePeriod, bytes32(0));
    }

    /**
     * @notice Create a payment with an oracle-release policy.
     * @dev    `policyHash` is the keccak256 of the canonical policy JSON
     *         (see kcolbchain/escrow-oracles SPEC.md §3). A non-zero hash
     *         enables `releaseByAttestation` for this payment; payer-only
     *         release via `confirmPayment` continues to work as a fallback.
     */
    function createPaymentWithPolicy(
        string calldata requestId,
        address payee,
        uint256 timeoutBlocks,
        uint256 challengePeriod,
        bytes32 policyHash
    ) external payable returns (bool) {
        if (policyHash != bytes32(0)) {
            require(address(oracleAggregator) != address(0), "no aggregator configured");
        }
        return _createPayment(requestId, payee, timeoutBlocks, challengePeriod, policyHash);
    }

    function _createPayment(
        string calldata requestId,
        address payee,
        uint256 timeoutBlocks,
        uint256 challengePeriod,
        bytes32 policyHash
    ) internal returns (bool) {
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
            createdAt: block.number,
            policyHash: policyHash
        });

        emit PaymentCreated(requestId, msg.sender, payee, msg.value);
        emit PaymentLocked(requestId);
        return true;
    }

    /**
     * @notice Payer confirms work is done → release funds to payee
     * @dev Can only be called by the original payer. Only in Locked state.
     *      Works regardless of whether the payment has an oracle policy;
     *      payer-only confirmation is always available as a fallback.
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
     * @notice Oracle-mediated release. Anyone may submit; the configured
     *         `oracleAggregator` verifies that `attestationHash` has enough
     *         signatures from registered oracles for this payment's
     *         `policyHash`.
     *
     * @dev Reverts if:
     *      - payment is not Locked
     *      - payment has no policyHash (oracle release was not opted into)
     *      - timeout has elapsed (use `requestRefund` instead)
     *      - oracle aggregator rejects the attestation
     */
    function releaseByAttestation(
        string calldata requestId,
        bytes32 attestationHash,
        bytes[] calldata signatures
    ) external returns (bool) {
        Payment storage p = payments[requestId];
        require(p.state == State.Locked, "Payment not in Locked state");
        require(p.policyHash != bytes32(0), "No oracle policy on this payment");
        require(block.number < p.createdAt + p.timeoutBlocks, "Payment has expired");
        require(address(oracleAggregator) != address(0), "No aggregator");
        require(
            oracleAggregator.verifyRelease(p.policyHash, attestationHash, signatures),
            "Oracle attestation rejected"
        );

        p.state = State.Released;

        (bool success, ) = p.payee.call{value: p.amount}("");
        require(success, "Transfer to payee failed");

        emit PaymentReleasedByOracle(requestId, p.policyHash, attestationHash);
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
