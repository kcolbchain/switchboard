// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IPriceOracle} from "../IPriceOracle.sol";

/**
 * @title MockPriceOracle
 * @notice Test-only price oracle. Returns a deterministic expected-out driven by
 *         a per-pair rate and a settable `updatedAt`, so tests can exercise the
 *         swap adapter's slippage bound AND its staleness rejection without a
 *         real price feed.
 *
 * @dev `rate1e18[tokenIn][tokenOut]` is the price of 1e18 units of `tokenIn`
 *      expressed in `tokenOut`, scaled by 1e18. `amountOut = amountIn * rate / 1e18`.
 *      Both mock tokens are 18-decimals, so no decimal normalization is needed
 *      for the mock (a production oracle would normalize by token decimals).
 */
contract MockPriceOracle is IPriceOracle {
    /// @dev price of 1e18 `tokenIn` in `tokenOut`, scaled 1e18.
    mapping(address => mapping(address => uint256)) public rate1e18;
    /// @dev unix timestamp of the last price update, per pair. 0 => never set.
    mapping(address => mapping(address => uint256)) public updatedAtOf;

    function setRate(address tokenIn, address tokenOut, uint256 rate, uint256 updatedAt) external {
        rate1e18[tokenIn][tokenOut] = rate;
        updatedAtOf[tokenIn][tokenOut] = updatedAt;
    }

    function quote(address tokenIn, address tokenOut, uint256 amountIn)
        external
        view
        override
        returns (uint256 amountOut, uint256 updatedAt)
    {
        uint256 rate = rate1e18[tokenIn][tokenOut];
        require(rate > 0, "MockPriceOracle: no rate");
        amountOut = (amountIn * rate) / 1e18;
        updatedAt = updatedAtOf[tokenIn][tokenOut];
    }
}
