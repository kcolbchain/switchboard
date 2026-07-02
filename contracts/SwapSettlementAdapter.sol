// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {IAgentEscrow} from "./IAgentEscrow.sol";
import {ISwapRouter} from "./ISwapRouter.sol";
import {IPriceOracle} from "./IPriceOracle.sol";

/**
 * @title SwapSettlementAdapter
 * @notice Opt-in swap-at-release layer (design spec §3.5, plan unit ②). Converts
 *         the escrowed payer-token (`tokenIn`) into the payee's desired token
 *         (`tokenOut`) at release, bounded by a payee-set `maxSlippageBps` and
 *         an oracle staleness window.
 *
 * @dev DELIBERATELY OUTSIDE THE ESCROW CORE (spec §3.5 / §8). The trustless
 *      `MultiTokenAgentEscrow` primitive keeps ZERO DEX/oracle attack surface;
 *      all swap logic lives here and is only ever reached when a payer opts in.
 *
 *      How it composes with the untouched escrow: the adapter is BOTH the escrow
 *      `payer` and the escrow `payee` for a swap-settled payment. `openSwapEscrow`
 *      pulls `tokenIn` from the real payer, funds a normal escrow payment
 *      (adapter = payer, adapter = payee), and records the swap intent (real
 *      payee, tokenOut, slippage bound). `settleWithSwap` then:
 *        1. confirms the escrow (adapter is the payer, so the core's
 *           `Only payer can confirm` guard is satisfied) → escrow transfers the
 *           held `tokenIn` to the adapter;
 *        2. quotes the oracle for the fair `tokenOut` out, rejecting a STALE
 *           price so a frozen feed can never justify an off-market swap;
 *        3. derives a minimum acceptable output from `maxSlippageBps` and swaps
 *           through the router;
 *        4. RE-CHECKS the realized output itself (never trusts the router alone)
 *           and reverts the WHOLE call if realized < min — because the escrow
 *           confirm and the swap are in one transaction, the revert leaves the
 *           escrow FUNDED and Locked (spec §6: "escrow stays funded");
 *        5. forwards the received `tokenOut` to the real payee.
 *
 *      Security posture:
 *        - Checks-Effects-Interactions + `nonReentrant` across the whole
 *          escrow↔adapter↔router↔payee call chain (the escrow is ALSO
 *          nonReentrant, so the confirm cannot re-enter the adapter mid-swap).
 *        - Slippage is bounded by an ORACLE reference, not the router's own
 *          quote; the router's `minAmountOut` floor is passed as defense in
 *          depth but the adapter independently re-verifies realized output.
 *        - The oracle is used ONLY to price the slippage bound, never to move
 *          funds or authorize a release (spec §8).
 *        - `settleWithSwap` can only be called by the intent's payer, cannot
 *          change the negotiated `tokenOut`, and cannot LOOSEN the negotiated
 *          slippage bound.
 */
contract SwapSettlementAdapter is ReentrancyGuard {
    using SafeERC20 for IERC20;

    /// @notice The escrow this adapter settles through. Immutable — the adapter
    ///         is pinned to one escrow instance so the trust surface is fixed.
    IAgentEscrow public immutable escrow;
    /// @notice DEX router used for the actual conversion.
    ISwapRouter public immutable swapRouter;
    /// @notice Price oracle used ONLY to bound slippage (never to move funds).
    IPriceOracle public immutable priceOracle;
    /// @notice Max age (seconds) of an oracle price observation before it is
    ///         rejected as stale.
    uint256 public immutable maxPriceStaleness;

    uint256 internal constant BPS_DENOM = 10_000;

    /// @notice Per-requestId swap intent, recorded at `openSwapEscrow`.
    struct SwapIntent {
        address payer; // the real payer who opened the intent
        address realPayee; // who ultimately receives tokenOut
        address tokenIn; // token held in escrow
        address tokenOut; // token the payee wants
        uint256 maxSlippageBps; // payee-set slippage ceiling
        bool exists;
        bool settled;
    }

    mapping(string => SwapIntent) public intents;

    event SwapIntentOpened(
        string indexed requestId,
        address indexed payer,
        address indexed realPayee,
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint256 maxSlippageBps
    );
    event SwapSettled(
        string indexed requestId,
        address indexed realPayee,
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint256 amountOut
    );

    constructor(
        IAgentEscrow _escrow,
        ISwapRouter _swapRouter,
        IPriceOracle _priceOracle,
        uint256 _maxPriceStaleness
    ) {
        require(address(_escrow) != address(0), "escrow required");
        require(address(_swapRouter) != address(0), "router required");
        require(address(_priceOracle) != address(0), "oracle required");
        require(_maxPriceStaleness > 0, "staleness required");
        escrow = _escrow;
        swapRouter = _swapRouter;
        priceOracle = _priceOracle;
        maxPriceStaleness = _maxPriceStaleness;
    }

    /**
     * @notice Open a swap-settled escrow payment. Pulls `amountIn` of `tokenIn`
     *         from the caller, funds an escrow payment with the ADAPTER as both
     *         payer and payee, and records the swap intent.
     * @dev The caller must `approve` `amountIn` of `tokenIn` to this adapter
     *      first. `tokenIn` must be allowlisted on the escrow (the adapter only
     *      forwards; the escrow's allowlist still governs what it will hold).
     */
    function openSwapEscrow(
        string calldata requestId,
        address realPayee,
        address tokenIn,
        uint256 amountIn,
        address tokenOut,
        uint256 maxSlippageBps,
        uint256 timeoutBlocks,
        uint256 challengePeriod
    ) external nonReentrant returns (bool) {
        require(!intents[requestId].exists, "intent exists");
        require(realPayee != address(0), "payee required");
        require(tokenIn != address(0), "tokenIn must be ERC20");
        require(tokenOut != address(0), "tokenOut must be ERC20");
        require(tokenIn != tokenOut, "same token: use core escrow");
        require(amountIn > 0, "amountIn > 0");
        require(maxSlippageBps < BPS_DENOM, "slippage bps too high");

        // Pull tokenIn from the payer into the adapter, then approve the escrow.
        // Balance-delta so the escrow is funded with exactly what arrived.
        uint256 balBefore = IERC20(tokenIn).balanceOf(address(this));
        IERC20(tokenIn).safeTransferFrom(msg.sender, address(this), amountIn);
        uint256 received = IERC20(tokenIn).balanceOf(address(this)) - balBefore;
        require(received > 0, "no tokenIn received");

        intents[requestId] = SwapIntent({
            payer: msg.sender,
            realPayee: realPayee,
            tokenIn: tokenIn,
            tokenOut: tokenOut,
            maxSlippageBps: maxSlippageBps,
            exists: true,
            settled: false
        });

        IERC20(tokenIn).forceApprove(address(escrow), received);
        escrow.createPayment(requestId, address(this), tokenIn, received, timeoutBlocks, challengePeriod);

        emit SwapIntentOpened(requestId, msg.sender, realPayee, tokenIn, tokenOut, received, maxSlippageBps);
        return true;
    }

    /**
     * @notice Release the escrow and swap the held `tokenIn` into `tokenOut`,
     *         forwarding it to the real payee. Reverts the WHOLE call (leaving
     *         the escrow funded) if realized slippage exceeds the negotiated
     *         bound or the oracle price is stale.
     * @param requestId       The swap-settled escrow payment.
     * @param tokenOut        Must equal the negotiated tokenOut (guard against
     *                        redirecting the swap output).
     * @param maxSlippageBps  Must be <= the negotiated bound (cannot be loosened).
     */
    function settleWithSwap(string calldata requestId, address tokenOut, uint256 maxSlippageBps)
        external
        nonReentrant
        returns (uint256 amountOut)
    {
        SwapIntent storage intent = intents[requestId];
        require(intent.exists, "no intent");
        require(!intent.settled, "already settled");
        require(msg.sender == intent.payer, "only intent payer");
        require(tokenOut == intent.tokenOut, "tokenOut mismatch");
        require(maxSlippageBps <= intent.maxSlippageBps, "slippage bound too loose");

        address tokenIn = intent.tokenIn;
        address realPayee = intent.realPayee;

        // ── Effects: mark settled before any external call (CEI + reentrancy). ──
        intent.settled = true;

        // ── Release the escrow to this adapter (adapter is the escrow payer). ──
        uint256 inBefore = IERC20(tokenIn).balanceOf(address(this));
        escrow.confirmPayment(requestId);
        uint256 amountIn = IERC20(tokenIn).balanceOf(address(this)) - inBefore;
        require(amountIn > 0, "escrow released nothing");

        // ── Price the swap via the oracle; reject a stale observation. ──
        (uint256 expectedOut, uint256 updatedAt) = priceOracle.quote(tokenIn, tokenOut, amountIn);
        require(expectedOut > 0, "oracle expected out = 0");
        require(block.timestamp - updatedAt <= maxPriceStaleness, "oracle price stale");

        // Minimum acceptable output = expected * (1 - slippage).
        uint256 minOut = (expectedOut * (BPS_DENOM - maxSlippageBps)) / BPS_DENOM;

        // ── Swap: approve router, execute, then RE-CHECK realized output. ──
        uint256 outBefore = IERC20(tokenOut).balanceOf(address(this));
        IERC20(tokenIn).forceApprove(address(swapRouter), amountIn);
        swapRouter.swapExactInput(tokenIn, tokenOut, amountIn, minOut, address(this));
        amountOut = IERC20(tokenOut).balanceOf(address(this)) - outBefore;

        // Independent slippage enforcement — never trust the router's floor alone.
        require(amountOut >= minOut, "slippage exceeds bound");

        // Clear any dangling router allowance (belt-and-suspenders).
        IERC20(tokenIn).forceApprove(address(swapRouter), 0);

        // ── Forward the payee's token. ──
        IERC20(tokenOut).safeTransfer(realPayee, amountOut);

        emit SwapSettled(requestId, realPayee, tokenIn, tokenOut, amountIn, amountOut);
    }
}
