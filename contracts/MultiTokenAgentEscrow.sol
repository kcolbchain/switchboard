// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {IOracleAggregator} from "./IOracleAggregator.sol";
import {IAgentEscrow} from "./IAgentEscrow.sol";

/**
 * @title MultiTokenAgentEscrow
 * @notice Multi-token generalization of `AgentEscrow` (design spec §3.2, unit ①).
 *         Same create → confirm → release / refund / cancel lifecycle as the
 *         native-ETH escrow, parameterized by `address token`:
 *
 *           - `token == address(0)` → native ETH via `msg.value` (the ETH
 *             profile; semantics match `AgentEscrow` exactly).
 *           - `token != address(0)` → an ERC-20 pulled via `transferFrom` on
 *             create and paid out via `transfer` on release / refund / cancel.
 *
 * @dev APPROACH A (spec §3.3 / §11): this is a NEW sibling contract. The shipped,
 *      EIP-drafted `AgentEscrow.sol` is left untouched so its audit/EIP surface
 *      is unchanged; abhicris makes the final A/B/C call.
 *
 *      Security posture (mirrors and extends `AgentEscrow`):
 *        - `is IAgentEscrow` — the shared multi-token interface.
 *        - Checks-Effects-Interactions on every path; `nonReentrant` as defense
 *          in depth (all release/refund/cancel/attestation paths do external
 *          transfers — ETH via low-level call, ERC-20 via SafeERC20).
 *        - **Balance-delta accounting**: the credited amount is the *measured*
 *          increase in this contract's token balance across `transferFrom`, not
 *          the declared amount. This makes fee-on-transfer / rebasing tokens
 *          safe (the escrow never promises to release more than it actually
 *          holds) and is why non-standard tokens can only be accepted behind the
 *          per-token allowlist.
 *        - **Allowlist**: ERC-20s must be owner-allowlisted (`setTokenAllowed`).
 *          Native ETH (`address(0)`) is always allowed — it is the core profile
 *          and needs no allowlisting, matching `AgentEscrow`.
 *        - SafeERC20 tolerates non-boolean-returning tokens (USDT-style).
 */
contract MultiTokenAgentEscrow is IAgentEscrow, Ownable, ReentrancyGuard {
    using SafeERC20 for IERC20;

    uint256 public immutable chainId;

    /// @notice Oracle aggregator consulted on `releaseByAttestation`. Set once
    ///         at construction. `address(0)` disables oracle release entirely.
    IOracleAggregator public immutable oracleAggregator;

    /// @notice requestId -> Payment.
    mapping(string => Payment) public payments;

    /// @notice Per-ERC-20 allowlist. Native ETH (`address(0)`) is implicitly
    ///         always allowed and is NOT represented here.
    mapping(address => bool) public allowlist;

    /// @notice Owner-curated allowlist of trusted agent addresses (parity with
    ///         `AgentEscrow`; kept for downstream policy checks).
    mapping(address => bool) public registeredAgents;

    event TokenAllowed(address indexed token, bool allowed);
    event AgentRegistered(address indexed agent);
    event AgentDeregistered(address indexed agent);

    /// @param _chainId    Chain id this contract is deployed on.
    /// @param _aggregator Optional oracle aggregator; `address(0)` disables oracle release.
    constructor(uint256 _chainId, IOracleAggregator _aggregator) Ownable(msg.sender) {
        chainId = _chainId;
        oracleAggregator = _aggregator;
    }

    // ─── Admin ─────────────────────────────────────────────────────────────────

    /// @notice Allow or disallow an ERC-20 as a settlement asset.
    /// @dev Non-standard tokens (fee-on-transfer/rebasing) are only ever accepted
    ///      through this gate, keeping the core safe-by-default.
    function setTokenAllowed(address token, bool allowed) external onlyOwner {
        require(token != address(0), "native ETH always allowed");
        allowlist[token] = allowed;
        emit TokenAllowed(token, allowed);
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

    // ─── Create ──────────────────────────────────────────────────────────────

    /// @inheritdoc IAgentEscrow
    /// @dev Payer-only release (policyHash = 0). See `createPaymentWithPolicy`
    ///      for the oracle-release variant.
    function createPayment(
        string calldata requestId,
        address payee,
        address token,
        uint256 amount,
        uint256 timeoutBlocks,
        uint256 challengePeriod
    ) external payable override nonReentrant returns (bool) {
        return _createPayment(requestId, payee, token, amount, timeoutBlocks, challengePeriod, bytes32(0));
    }

    /// @notice Create a payment with an oracle-release policy (multi-token).
    function createPaymentWithPolicy(
        string calldata requestId,
        address payee,
        address token,
        uint256 amount,
        uint256 timeoutBlocks,
        uint256 challengePeriod,
        bytes32 policyHash
    ) external payable nonReentrant returns (bool) {
        if (policyHash != bytes32(0)) {
            require(address(oracleAggregator) != address(0), "no aggregator configured");
        }
        return _createPayment(requestId, payee, token, amount, timeoutBlocks, challengePeriod, policyHash);
    }

    function _createPayment(
        string calldata requestId,
        address payee,
        address token,
        uint256 amount,
        uint256 timeoutBlocks,
        uint256 challengePeriod,
        bytes32 policyHash
    ) internal returns (bool) {
        require(bytes(requestId).length > 0, "requestId cannot be empty");
        require(payee != address(0), "payee cannot be zero address");
        require(payments[requestId].createdAt == 0, "requestId already exists");
        require(timeoutBlocks > 0, "timeoutBlocks must be > 0");
        require(amount > 0, "amount must be > 0");

        uint256 credited;
        if (token == address(0)) {
            // ── Native ETH profile: parity with AgentEscrow ──
            require(msg.value == amount, "ETH: msg.value != amount");
            credited = msg.value;
        } else {
            // ── ERC-20 profile ──
            require(msg.value == 0, "ERC20: no ETH");
            require(allowlist[token], "token not allowlisted");
            // Balance-delta accounting: credit exactly what arrived, which is
            // correct even for fee-on-transfer / rebasing tokens.
            uint256 balBefore = IERC20(token).balanceOf(address(this));
            IERC20(token).safeTransferFrom(msg.sender, address(this), amount);
            uint256 balAfter = IERC20(token).balanceOf(address(this));
            credited = balAfter - balBefore;
            require(credited > 0, "no tokens received");
        }

        payments[requestId] = Payment({
            payer: msg.sender,
            payee: payee,
            token: token,
            amount: credited,
            timeoutBlocks: timeoutBlocks,
            challengePeriod: challengePeriod,
            state: State.Locked,
            requestId: requestId,
            createdAt: block.number,
            policyHash: policyHash
        });

        emit PaymentCreated(requestId, msg.sender, payee, token, credited);
        emit PaymentLocked(requestId, token);
        return true;
    }

    // ─── Release / Refund / Cancel ─────────────────────────────────────────────

    /// @inheritdoc IAgentEscrow
    function confirmPayment(string calldata requestId)
        external
        override
        nonReentrant
        returns (bool)
    {
        Payment storage p = payments[requestId];
        require(p.payer == msg.sender, "Only payer can confirm");
        require(p.state == State.Locked, "Payment not in Locked state");
        require(block.number < p.createdAt + p.timeoutBlocks, "Payment has expired");

        uint256 amount = p.amount;
        address payee = p.payee;
        address token = p.token;
        p.state = State.Released;
        p.amount = 0;

        emit PaymentConfirmed(requestId, msg.sender, token);
        emit PaymentReleased(requestId, payee, token, amount);

        _payOut(token, payee, amount);
        return true;
    }

    /// @inheritdoc IAgentEscrow
    function releaseByAttestation(
        string calldata requestId,
        bytes32 attestationHash,
        bytes[] calldata signatures
    ) external override nonReentrant returns (bool) {
        Payment storage p = payments[requestId];
        require(p.state == State.Locked, "Payment not in Locked state");
        require(p.policyHash != bytes32(0), "No oracle policy on this payment");
        require(block.number < p.createdAt + p.timeoutBlocks, "Payment has expired");
        require(address(oracleAggregator) != address(0), "No aggregator");
        require(
            oracleAggregator.verifyRelease(p.policyHash, attestationHash, signatures),
            "Oracle attestation rejected"
        );

        uint256 amount = p.amount;
        address payee = p.payee;
        address token = p.token;
        bytes32 policyHash = p.policyHash;
        p.state = State.Released;
        p.amount = 0;

        emit PaymentReleasedByOracle(requestId, policyHash, attestationHash);
        emit PaymentReleased(requestId, payee, token, amount);

        _payOut(token, payee, amount);
        return true;
    }

    /// @inheritdoc IAgentEscrow
    function requestRefund(string calldata requestId)
        external
        override
        nonReentrant
        returns (bool)
    {
        Payment storage p = payments[requestId];
        require(p.payer == msg.sender, "Only payer can request refund");
        require(p.state == State.Locked, "Payment not in Locked state");
        require(
            block.number >= p.createdAt + p.timeoutBlocks + p.challengePeriod,
            "Challenge period not over"
        );

        uint256 amount = p.amount;
        address payer = p.payer;
        address token = p.token;
        p.state = State.Refunded;
        p.amount = 0;

        emit PaymentRefunded(requestId, payer, token, amount);

        _payOut(token, payer, amount);
        return true;
    }

    /// @inheritdoc IAgentEscrow
    function cancelPayment(string calldata requestId)
        external
        override
        nonReentrant
        returns (bool)
    {
        Payment storage p = payments[requestId];
        require(p.payer == msg.sender, "Only payer can cancel");
        require(p.state == State.Locked, "Payment not in Locked state");

        uint256 amount = p.amount;
        address payer = p.payer;
        address token = p.token;
        p.state = State.Cancelled;
        p.amount = 0;

        emit PaymentCancelled(requestId, payer, token, amount);

        _payOut(token, payer, amount);
        return true;
    }

    /// @dev Interaction step for both profiles. For ETH, a low-level call
    ///      (parity with AgentEscrow). For ERC-20, SafeERC20.transfer of the
    ///      held amount — for fee-on-transfer tokens the recipient receives
    ///      net-of-fee, and the escrow is drained of exactly what it held (no
    ///      underflow, no stuck dust attributable to this payment).
    function _payOut(address token, address to, uint256 amount) internal {
        if (amount == 0) return;
        if (token == address(0)) {
            (bool success,) = to.call{value: amount}("");
            require(success, "ETH transfer failed");
        } else {
            IERC20(token).safeTransfer(to, amount);
        }
    }

    // ─── Views ─────────────────────────────────────────────────────────────────

    /// @inheritdoc IAgentEscrow
    function getPayment(string calldata requestId) external view override returns (Payment memory) {
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
