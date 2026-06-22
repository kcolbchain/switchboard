// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ILucidlyVault} from "../adapters/ILucidlyVault.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

/**
 * @title MockLucidlyVault
 * @notice Test-only ERC-4626-shaped stand-in for Lucidly's syUSD vault.
 *
 * @dev Shares are minted 1:1 with deposited assets for arithmetic clarity.
 *      Entry slippage is modeled in `previewRedeem`: the redeemable value of
 *      freshly minted shares is discounted by `entrySlippageBps`, which is what
 *      the adapter inspects to enforce its slippage cap. Default slippage is 0
 *      so the happy-path tests can use exact round numbers.
 *
 *      The real vault interface lives in `kcolbchain/stablecoin-toolkit`; this
 *      mock implements only `deposit` / `withdraw` / `previewRedeem`.
 */
contract MockLucidlyVault is ILucidlyVault {
    IERC20 public immutable asset;

    /// @notice Modeled entry slippage in basis points applied to share value.
    uint16 public entrySlippageBps;

    /// @notice Shares outstanding per owner (the adapter).
    mapping(address => uint256) public sharesOf;

    constructor(address _asset) {
        asset = IERC20(_asset);
    }

    /// @notice Test hook: set the modeled entry slippage in bps.
    function setEntrySlippageBps(uint16 bps) external {
        entrySlippageBps = bps;
    }

    function deposit(uint256 assets, address receiver) external override returns (uint256 shares) {
        require(asset.transferFrom(msg.sender, address(this), assets), "vault: transferFrom failed");
        shares = assets; // 1:1
        sharesOf[receiver] += shares;
    }

    function withdraw(uint256 assets, address receiver, address owner) external override returns (uint256 shares) {
        require(sharesOf[owner] >= assets, "vault: insufficient shares");
        sharesOf[owner] -= assets; // 1:1 burn
        shares = assets;
        require(asset.transfer(receiver, assets), "vault: transfer failed");
    }

    function previewRedeem(uint256 shares) external view override returns (uint256 assets) {
        // Discount redeemable value by the modeled entry slippage.
        assets = (shares * (10_000 - entrySlippageBps)) / 10_000;
    }
}
