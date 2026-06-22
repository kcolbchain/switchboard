// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title ILucidlyVault
 * @notice Minimal interface the LucidlyAdapter consults to park / unpark idle
 *         agent float into Lucidly's yield-bearing syUSD vault.
 *
 * @dev This is a MOCK-FACING interface for this repository. The real syUSD
 *      vault ABI lives in `kcolbchain/stablecoin-toolkit` and is ERC-4626
 *      shaped (it also exposes `previewDeposit`, `mint`, `redeem`, `asset`,
 *      `totalAssets`, etc.). We bind only the subset the adapter needs so the
 *      on-chain surface stays auditable and the real binding can be swapped in
 *      as a follow-up (see the adapter's NatSpec). Slippage handling is
 *      asymmetric on purpose: entry slippage is bounded by deriving the shares'
 *      redeemable value via `previewRedeem` and comparing against the assets
 *      deposited.
 *
 *      `deposit` / `withdraw` follow the ERC-4626 signatures so the real vault
 *      drops in without reshaping call sites.
 */
interface ILucidlyVault {
    /// @notice Deposit `assets` of the underlying and mint vault shares to `receiver`.
    /// @return shares The amount of vault shares minted.
    function deposit(uint256 assets, address receiver) external returns (uint256 shares);

    /// @notice Withdraw `assets` of the underlying to `receiver`, burning shares from `owner`.
    /// @return shares The amount of vault shares burned.
    function withdraw(uint256 assets, address receiver, address owner) external returns (uint256 shares);

    /// @notice Underlying assets currently redeemable for `shares`, net of any exit fee.
    /// @dev Used to derive realized entry slippage at park time:
    ///      slippageBps = (assetsIn - previewRedeem(sharesOut)) / assetsIn * 10_000.
    function previewRedeem(uint256 shares) external view returns (uint256 assets);
}
