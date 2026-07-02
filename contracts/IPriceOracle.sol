// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title IPriceOracle
 * @notice Price-quote surface the `SwapSettlementAdapter` uses to compute the
 *         *expected* output of a swap and to bound realized slippage
 *         (design spec §3.5; plan unit ⑤'s `quote(tokenIn, tokenOut, amountIn)`).
 *
 * @dev DISTINCT FROM `IOracleAggregator`. `IOracleAggregator` answers a boolean
 *      "is this release attested?" for the escrow core. This interface answers a
 *      *pricing* question — "how much `tokenOut` is `amountIn` of `tokenIn`
 *      worth right now, and how fresh is that price?" — for the OPT-IN swap
 *      layer only. Keeping them separate honors spec §8: "Oracle used only for
 *      slippage bounds, never as the settlement authority" — the price oracle
 *      never moves funds and never authorizes a release; it only sets the
 *      expected-out reference the adapter measures realized slippage against.
 *
 *      `updatedAt` is the unix timestamp of the underlying price observation.
 *      The adapter rejects the swap if `block.timestamp - updatedAt` exceeds its
 *      configured `maxPriceStaleness`, so a frozen/stale oracle can never be
 *      used to justify an off-market swap.
 */
interface IPriceOracle {
    /// @notice Expected output of swapping `amountIn` of `tokenIn` into `tokenOut`.
    /// @return amountOut  Fair-value output at the oracle's current price.
    /// @return updatedAt  Unix timestamp of the price observation (for staleness).
    function quote(address tokenIn, address tokenOut, uint256 amountIn)
        external
        view
        returns (uint256 amountOut, uint256 updatedAt);
}
