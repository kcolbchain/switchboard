// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title IAgentEscrow
 * @notice Shared lifecycle interface for agent-to-agent escrow, generalized
 *         over the settlement asset. This is the "Multi-Token A2A Escrow"
 *         surface referenced by the design spec §3.1 / §5 unit ⑤.
 *
 * @dev The native-ETH escrow (`AgentEscrow.sol`) is the **ETH profile** of this
 *      standard: the case where `token == address(0)`. `MultiTokenAgentEscrow`
 *      implements this interface for both native ETH (`address(0)`, via
 *      `msg.value`) and ERC-20 tokens (via `transferFrom` / `transfer`).
 *
 *      Lifecycle (unchanged from the native EIP, now parameterized by `token`):
 *        create → (Locked) → confirm | releaseByAttestation → Released
 *                          → requestRefund (after timeout+challenge) → Refunded
 *                          → cancelPayment → Cancelled
 *
 *      Design notes:
 *        - `Payment` carries an `address token` field. `address(0)` denotes the
 *          native-ETH profile; any other address is the ERC-20 being escrowed.
 *        - `amount` in `createPayment` is the *declared* amount. For the ETH
 *          profile it must equal `msg.value`. For ERC-20s it is the amount the
 *          escrow will attempt to pull; the *credited* amount is the measured
 *          balance delta (fee-on-transfer safe) — see `MultiTokenAgentEscrow`.
 *        - Every escrow event carries `token` so indexers can attribute flows
 *          per asset.
 */
interface IAgentEscrow {
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
        address token; // address(0) = native ETH profile; else the ERC-20 escrowed
        uint256 amount; // credited amount held in escrow (balance-delta for ERC-20)
        uint256 timeoutBlocks; // blocks until auto-expire
        uint256 challengePeriod; // blocks payer must wait to reclaim after timeout
        State state;
        string requestId; // off-chain payment request ID
        uint256 createdAt; // block number at creation
        bytes32 policyHash; // 0x00 = payer-only release; non-zero enables oracle release
    }

    // ─── Events (all carry `token`) ──────────────────────────────────────────

    event PaymentCreated(
        string indexed requestId,
        address indexed payer,
        address indexed payee,
        address token,
        uint256 amount
    );
    event PaymentLocked(string indexed requestId, address token);
    event PaymentConfirmed(string indexed requestId, address indexed payer, address token);
    event PaymentReleased(string indexed requestId, address indexed payee, address token, uint256 amount);
    event PaymentReleasedByOracle(string indexed requestId, bytes32 policyHash, bytes32 attestationHash);
    event PaymentRefunded(string indexed requestId, address indexed payer, address token, uint256 amount);
    event PaymentCancelled(string indexed requestId, address indexed payer, address token, uint256 amount);

    // ─── Lifecycle ────────────────────────────────────────────────────────────

    /**
     * @notice Create a payment request and lock funds in escrow.
     * @param requestId       Off-chain payment request ID (unique).
     * @param payee           Recipient on release.
     * @param token           Settlement asset. `address(0)` = native ETH (send via `msg.value`);
     *                        otherwise an ERC-20 the payer has approved to this contract.
     * @param amount          Declared amount. Native profile requires `amount == msg.value`;
     *                        ERC-20 profile pulls up to `amount` via `transferFrom` and credits
     *                        the measured balance delta.
     * @param timeoutBlocks   Blocks until the payment auto-expires.
     * @param challengePeriod Blocks the payer must additionally wait after timeout to reclaim.
     */
    function createPayment(
        string calldata requestId,
        address payee,
        address token,
        uint256 amount,
        uint256 timeoutBlocks,
        uint256 challengePeriod
    ) external payable returns (bool);

    /// @notice Payer confirms work is done -> release funds to payee.
    function confirmPayment(string calldata requestId) external returns (bool);

    /// @notice Oracle-mediated release, gated by the payment's `policyHash`.
    function releaseByAttestation(
        string calldata requestId,
        bytes32 attestationHash,
        bytes[] calldata signatures
    ) external returns (bool);

    /// @notice Payer reclaims funds after timeout + challenge period.
    function requestRefund(string calldata requestId) external returns (bool);

    /// @notice Cancel a still-locked payment (mutual agreement / pre-timeout).
    function cancelPayment(string calldata requestId) external returns (bool);

    /// @notice Read a payment record.
    function getPayment(string calldata requestId) external view returns (Payment memory);
}
