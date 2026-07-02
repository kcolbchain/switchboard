// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ISwapRouter} from "../ISwapRouter.sol";

/**
 * @title MockSwapRouter
 * @notice Test-only DEX router. Pulls `tokenIn` from the caller and pays out
 *         `tokenOut` to `recipient` at a settable per-pair rate, so tests can
 *         drive BOTH a fair swap and a bad-execution (high-slippage) swap.
 *
 * @dev The router must be pre-funded with `tokenOut` (like real liquidity).
 *      `rate1e18[tokenIn][tokenOut]` is the realized output of 1e18 `tokenIn`
 *      in `tokenOut`, scaled 1e18 — set it BELOW the oracle rate to simulate
 *      slippage/bad execution, or equal to it for a fair fill.
 *
 *      By default it honors its own `minAmountOut` floor (reverts if it can't
 *      meet it), same as a real router. `setEnforceMinOut(false)` makes it
 *      IGNORE its floor — modeling a broken/malicious/misconfigured router — so
 *      tests can prove the ADAPTER'S OWN realized-out re-check catches slippage
 *      independently ("never trust the router alone", spec §8).
 */
contract MockSwapRouter is ISwapRouter {
    /// @dev realized output of 1e18 `tokenIn` in `tokenOut`, scaled 1e18.
    mapping(address => mapping(address => uint256)) public rate1e18;
    /// @dev when false, the router does NOT enforce its own minAmountOut floor.
    bool public enforceMinOut = true;

    function setRate(address tokenIn, address tokenOut, uint256 rate) external {
        rate1e18[tokenIn][tokenOut] = rate;
    }

    function setEnforceMinOut(bool on) external {
        enforceMinOut = on;
    }

    function swapExactInput(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint256 minAmountOut,
        address recipient
    ) external override returns (uint256 amountOut) {
        uint256 rate = rate1e18[tokenIn][tokenOut];
        require(rate > 0, "MockSwapRouter: no liquidity");

        // Pull exactly amountIn of tokenIn from the caller (adapter).
        require(
            IERC20(tokenIn).transferFrom(msg.sender, address(this), amountIn),
            "MockSwapRouter: transferFrom failed"
        );

        amountOut = (amountIn * rate) / 1e18;
        if (enforceMinOut) {
            require(amountOut >= minAmountOut, "MockSwapRouter: insufficient output");
        }

        require(
            IERC20(tokenOut).transfer(recipient, amountOut),
            "MockSwapRouter: payout failed"
        );
    }
}
