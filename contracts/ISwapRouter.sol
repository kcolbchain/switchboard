// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title ISwapRouter
 * @notice Minimal DEX-router surface the `SwapSettlementAdapter` swaps through
 *         at release (design spec §3.5, plan unit ②).
 *
 * @dev This is a deliberately tiny, exact-input swap interface — the smallest
 *      thing the adapter needs to convert the escrowed `tokenIn` into the
 *      payee's `tokenOut`. The real wiring to `switchboard/adapters/lucidly.py`
 *      (the production DEX/liquidity engine) lands in a later unit; for now the
 *      adapter depends only on this interface so it can be swapped for lucidly,
 *      a Uniswap-style router, or a mock without any change to the adapter.
 *
 *      Semantics (Uniswap-v2 `swapExactTokensForTokens` shaped, single hop):
 *        - The caller (the adapter) must have `approve`d `amountIn` of `tokenIn`
 *          to this router before calling.
 *        - The router pulls exactly `amountIn` of `tokenIn` from the caller,
 *          performs the swap, and sends the resulting `tokenOut` to `recipient`.
 *        - `minAmountOut` is the router's OWN floor (it MUST revert if it cannot
 *          deliver at least this much). The adapter passes its slippage-derived
 *          floor here as defense-in-depth; the adapter ALSO re-checks the
 *          realized output itself after the call (never trusts the router alone).
 *        - Returns the actual `amountOut` delivered to `recipient`.
 */
interface ISwapRouter {
    function swapExactInput(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint256 minAmountOut,
        address recipient
    ) external returns (uint256 amountOut);
}
