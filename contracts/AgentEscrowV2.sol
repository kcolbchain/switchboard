// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {IOracleAggregator} from "./IOracleAggregator.sol";
import {IRefundPolicy} from "./policies/IRefundPolicy.sol";

contract AgentEscrowV2 is Ownable, ReentrancyGuard {
    enum State {
        Created,
        Locked,
        Confirmed,
        Released,
        Refunded,
        Cancelled
    }

    struct Payment {
        address payer;
        address payee;
        uint256 amount;
        uint256 releasedAmount;
        uint256 refundedAmount;
        uint256 timeoutBlocks; 
        uint256 challengePeriod; 
        State state;
        string requestId;
        uint256 createdAt;
        bytes32 policyHash;         // 0x00 = payer-only release; non-zero enables oracle release
        IRefundPolicy refundPolicy; // The pluggable refund policy
        bytes32 policyDataHash;     // Hash of the policy data to save gas, verified during evaluate
    }

    uint256 public immutable chainId;
    IOracleAggregator public immutable oracleAggregator;

    mapping(string => Payment) public payments;
    mapping(address => bool) public registeredAgents;

    event PaymentCreated(string indexed requestId, address indexed payer, address indexed payee, uint256 amount);
    event PaymentLocked(string indexed requestId);
    event PaymentConfirmed(string indexed requestId, address indexed payer);
    event PaymentReleased(string indexed requestId, address indexed payee, uint256 amount);
    event PaymentReleasedByOracle(string indexed requestId, bytes32 policyHash, bytes32 attestationHash);
    event PaymentRefunded(string indexed requestId, address indexed payer, uint256 amount);
    event PaymentCancelled(string indexed requestId, address indexed payer, uint256 amount);
    event AgentRegistered(address indexed agent);
    event AgentDeregistered(address indexed agent);

    constructor(uint256 _chainId, IOracleAggregator _aggregator) Ownable(msg.sender) {
        chainId = _chainId;
        oracleAggregator = _aggregator;
    }

    function registerAgent(address agent) external onlyOwner {
        require(agent != address(0), "agent cannot be zero address");
        registeredAgents[agent] = true;
        emit AgentRegistered(agent);
    }

    function deregisterAgent(address agent) external onlyOwner {
        registeredAgents[agent] = false;
        emit AgentDeregistered(agent);
    }

    function createPayment(
        string calldata requestId,
        address payee,
        uint256 timeoutBlocks,
        uint256 challengePeriod
    ) external payable returns (bool) {
        return _createPayment(requestId, payee, timeoutBlocks, challengePeriod, bytes32(0), IRefundPolicy(address(0)), "");
    }

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
        return _createPayment(requestId, payee, timeoutBlocks, challengePeriod, policyHash, IRefundPolicy(address(0)), "");
    }

    function createPayment(
        string calldata requestId,
        address payee,
        IRefundPolicy policy,
        bytes calldata policyData,
        uint256 timeoutBlocks
    ) external payable returns (bool) {
        return _createPayment(requestId, payee, timeoutBlocks, 0, bytes32(0), policy, policyData);
    }

    function _createPayment(
        string calldata requestId,
        address payee,
        uint256 timeoutBlocks,
        uint256 challengePeriod,
        bytes32 policyHash,
        IRefundPolicy policy,
        bytes memory policyData
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
            releasedAmount: 0,
            refundedAmount: 0,
            timeoutBlocks: timeoutBlocks,
            challengePeriod: challengePeriod,
            state: State.Locked,
            requestId: requestId,
            createdAt: block.number,
            policyHash: policyHash,
            refundPolicy: policy,
            policyDataHash: keccak256(policyData)
        });

        emit PaymentCreated(requestId, msg.sender, payee, msg.value);
        emit PaymentLocked(requestId);
        return true;
    }

    function confirmPayment(string calldata requestId) external nonReentrant returns (bool) {
        Payment storage p = payments[requestId];
        require(p.payer == msg.sender, "Only payer can confirm");
        require(p.state == State.Locked, "Payment not in Locked state");
        require(block.number < p.createdAt + p.timeoutBlocks, "Payment has expired");

        uint256 amount = p.amount - p.releasedAmount - p.refundedAmount;
        address payee = p.payee;
        p.state = State.Released;
        p.releasedAmount += amount;

        emit PaymentConfirmed(requestId, msg.sender);
        emit PaymentReleased(requestId, payee, amount);

        (bool success,) = payee.call{value: amount}("");
        require(success, "Transfer to payee failed");
        return true;
    }

    function releaseByAttestation(
        string calldata requestId,
        bytes32 attestationHash,
        bytes[] calldata signatures
    ) external nonReentrant returns (bool) {
        Payment storage p = payments[requestId];
        require(p.state == State.Locked, "Payment not in Locked state");
        require(p.policyHash != bytes32(0), "No oracle policy on this payment");
        require(block.number < p.createdAt + p.timeoutBlocks, "Payment has expired");
        require(address(oracleAggregator) != address(0), "No aggregator");
        require(
            oracleAggregator.verifyRelease(p.policyHash, attestationHash, signatures),
            "Oracle attestation rejected"
        );

        uint256 amount = p.amount - p.releasedAmount - p.refundedAmount;
        address payee = p.payee;
        p.state = State.Released;
        p.releasedAmount += amount;

        emit PaymentReleasedByOracle(requestId, p.policyHash, attestationHash);
        emit PaymentReleased(requestId, payee, amount);

        (bool success, ) = payee.call{value: amount}("");
        require(success, "Transfer to payee failed");
        return true;
    }

    function requestRefund(string calldata requestId) external nonReentrant returns (bool) {
        Payment storage p = payments[requestId];
        require(address(p.refundPolicy) == address(0), "Use requestRefund with policyData");
        require(p.payer == msg.sender, "Only payer can request refund");
        require(p.state == State.Locked, "Payment not in Locked state");
        require(block.number >= p.createdAt + p.timeoutBlocks + p.challengePeriod, "Challenge period not over");

        uint256 amount = p.amount - p.releasedAmount - p.refundedAmount;
        address payer = p.payer;
        p.state = State.Refunded;
        p.refundedAmount += amount;

        emit PaymentRefunded(requestId, payer, amount);

        (bool success,) = payer.call{value: amount}("");
        require(success, "Refund transfer failed");
        return true;
    }

    function requestRefund(string calldata requestId, bytes calldata policyData) external nonReentrant returns (bool) {
        Payment storage p = payments[requestId];
        require(address(p.refundPolicy) != address(0), "No refund policy set");
        require(keccak256(policyData) == p.policyDataHash, "Invalid policyData");
        require(p.state == State.Locked, "Payment not in Locked state");

        (uint256 refundable, , bool terminal) = p.refundPolicy.evaluate(
            keccak256(abi.encodePacked(requestId)),
            policyData,
            block.number - p.createdAt
        );

        require(refundable > 0, "No refund available");
        uint256 pendingRefund = refundable - p.refundedAmount;
        require(pendingRefund > 0, "Refund already processed");

        p.refundedAmount += pendingRefund;
        if (terminal || p.refundedAmount + p.releasedAmount == p.amount) {
            p.state = State.Refunded;
        }

        emit PaymentRefunded(requestId, p.payer, pendingRefund);

        (bool success,) = p.payer.call{value: pendingRefund}("");
        require(success, "Refund transfer failed");
        return true;
    }

    function releasePartial(string calldata requestId, bytes calldata policyData) external nonReentrant returns (bool) {
        Payment storage p = payments[requestId];
        require(address(p.refundPolicy) != address(0), "No refund policy set");
        require(keccak256(policyData) == p.policyDataHash, "Invalid policyData");
        require(p.state == State.Locked, "Payment not in Locked state");

        (, uint256 releasable, bool terminal) = p.refundPolicy.evaluate(
            keccak256(abi.encodePacked(requestId)),
            policyData,
            block.number - p.createdAt
        );

        require(releasable > 0, "No release available");
        uint256 pendingRelease = releasable - p.releasedAmount;
        require(pendingRelease > 0, "Release already processed");

        p.releasedAmount += pendingRelease;
        if (terminal || p.refundedAmount + p.releasedAmount == p.amount) {
            p.state = State.Released;
        }

        emit PaymentReleased(requestId, p.payee, pendingRelease);

        (bool success,) = p.payee.call{value: pendingRelease}("");
        require(success, "Release transfer failed");
        return true;
    }

    function cancelPayment(string calldata requestId) external nonReentrant returns (bool) {
        Payment storage p = payments[requestId];
        require(p.payer == msg.sender, "Only payer can cancel");
        require(p.state == State.Locked, "Payment not in Locked state");

        uint256 amount = p.amount - p.releasedAmount - p.refundedAmount;
        address payer = p.payer;
        p.state = State.Cancelled;
        p.refundedAmount += amount;

        emit PaymentCancelled(requestId, payer, amount);

        (bool success,) = payer.call{value: amount}("");
        require(success, "Cancel refund failed");
        return true;
    }

    function getPayment(string calldata requestId) external view returns (Payment memory) {
        return payments[requestId];
    }

    function isState(string calldata requestId, State expected) external view returns (bool) {
        return payments[requestId].state == expected;
    }

    function isExpired(string calldata requestId) external view returns (bool) {
        Payment storage p = payments[requestId];
        if (p.createdAt == 0) return false;
        return block.number >= p.createdAt + p.timeoutBlocks && p.state == State.Locked;
    }
}
