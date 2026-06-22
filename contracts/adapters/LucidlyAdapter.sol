// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ILucidlyVault} from "./ILucidlyVault.sol";

/**
 * @title LucidlyAdapter
 * @notice On-chain idle-balance adapter that parks agent-wallet float into
 *         Lucidly's yield-bearing syUSD vault and pulls it back on demand.
 *         Mirrors `switchboard/adapters/lucidly.py` (`LucidlyAutoPark`) at the
 *         EVM layer. Resolves kcolbchain/switchboard#80 (refresh of #20).
 *
 * @dev Model
 *   - The adapter custodies each wallet's working float (the underlying asset,
 *     a CR8-USD / USDC stand-in here). Per wallet it tracks a `liquid` buffer
 *     and the vault `shares` it parked on that wallet's behalf.
 *   - `rebalance(wallet)` is the post-settlement hook. It compares the liquid
 *     buffer against the wallet's idle target with a 5% deadband:
 *       * liquid above (target + 5% of total) -> park the excess
 *       * liquid below (target - 5% of total) -> unpark toward target
 *       * otherwise no-op (avoids churn / dust round-trips)
 *   - `unpark(wallet, amount)` pulls liquidity from the vault before a tx that
 *     would otherwise overdraw the buffer.
 *
 * @dev Per-wallet config (bps): `idleTargetBps` (fraction parked),
 *      `unparkThresholdBps` (floor the buffer is topped up to), and
 *      `maxEntrySlippageBps` (skip-and-log if a park's realized entry slippage
 *      exceeds this cap). Unconfigured wallets fall back to protocol defaults
 *      8000 / 1500 / 25.
 *
 * @dev Slippage cap: before parking, the adapter computes realized entry
 *      slippage as (assetsIn - previewRedeem(sharesMinted)) / assetsIn and
 *      reverts the deposit (skips, emits {ParkSkipped}) if it exceeds the cap.
 *      "No yield without a slippage cap" — avoids the yield-surface trap where
 *      the vault looks attractive until you actually try to enter.
 *
 * @dev Hardening: `rebalance` and `unpark` are `nonReentrant`; they make
 *      external token + vault calls, so the guard is defense-in-depth on top of
 *      checks-effects-interactions.
 *
 * @dev FOLLOW-UP: {ILucidlyVault} here is a mock-facing subset. The real syUSD
 *      vault ABI lives in `kcolbchain/stablecoin-toolkit` (ERC-4626 shaped).
 *      Binding the real vault address + ABI on testnet is tracked as a
 *      follow-up; this contract deliberately depends only on the minimal
 *      interface so that binding is a constructor/config swap.
 */
contract LucidlyAdapter is Ownable, ReentrancyGuard {
    /// @notice Per-wallet auto-park configuration (all values in basis points).
    struct Config {
        uint16 idleTargetBps;       // e.g. 8000 = 80% parked, 20% liquid buffer
        uint16 unparkThresholdBps;  // buffer floor topped up to on a low-liquid rebalance
        uint16 maxEntrySlippageBps; // park is skipped + logged above this cap
        bool enabled;               // master switch (defaults on)
        bool configured;            // whether setConfig has been called
    }

    /// @notice Default config applied to wallets that never called setConfig.
    uint16 internal constant DEFAULT_IDLE_TARGET_BPS = 8000;
    uint16 internal constant DEFAULT_UNPARK_THRESHOLD_BPS = 1500;
    uint16 internal constant DEFAULT_MAX_ENTRY_SLIPPAGE_BPS = 25;

    /// @notice Deadband around the target liquid buffer, in bps of total value,
    ///         within which rebalance does nothing (avoids churn). 500 = 5%.
    uint16 public constant DEADBAND_BPS = 500;

    uint16 internal constant BPS = 10_000;

    /// @notice Lucidly syUSD vault assets are parked into.
    ILucidlyVault public immutable vault;

    /// @notice Underlying asset (CR8-USD / USDC) the adapter custodies.
    IERC20 public immutable asset;

    mapping(address => Config) internal _config;
    mapping(address => uint256) public liquidOf;  // custodied buffer per wallet
    mapping(address => uint256) public parkedOf;  // vault shares held per wallet
    mapping(address => bool) internal _seeded;    // liquid lazily seeded?

    /// @notice Sum of liquid attributed across all wallets (for lazy seeding).
    uint256 internal _attributedLiquid;

    event ConfigSet(
        address indexed wallet,
        uint16 idleTargetBps,
        uint16 unparkThresholdBps,
        uint16 maxEntrySlippageBps
    );
    event Parked(address indexed wallet, uint256 assets, uint256 shares);
    event Unparked(address indexed wallet, uint256 assets);
    event ParkSkipped(address indexed wallet, uint256 assets, uint16 slippageBps, uint16 capBps);

    constructor(ILucidlyVault _vault, address _asset) Ownable(msg.sender) {
        require(address(_vault) != address(0), "vault is zero");
        require(_asset != address(0), "asset is zero");
        vault = _vault;
        asset = IERC20(_asset);
    }

    // ─── config ───────────────────────────────────────────────────────────

    /// @notice Set per-wallet auto-park config. Owner-only (the switchboard
    ///         operator curates wallet policy).
    function setConfig(
        address wallet,
        uint16 idleTargetBps,
        uint16 unparkThresholdBps,
        uint16 maxEntrySlippageBps
    ) external onlyOwner {
        require(idleTargetBps <= BPS, "idle_target_bps > 10000");
        require(unparkThresholdBps <= BPS, "unpark_threshold_bps > 10000");
        require(maxEntrySlippageBps <= BPS, "max_entry_slippage_bps > 10000");

        Config storage c = _config[wallet];
        c.idleTargetBps = idleTargetBps;
        c.unparkThresholdBps = unparkThresholdBps;
        c.maxEntrySlippageBps = maxEntrySlippageBps;
        c.enabled = true;
        c.configured = true;

        emit ConfigSet(wallet, idleTargetBps, unparkThresholdBps, maxEntrySlippageBps);
    }

    /// @notice Toggle a wallet's auto-park on/off without dropping its config.
    function setEnabled(address wallet, bool enabled) external onlyOwner {
        _config[wallet].enabled = enabled;
        if (!_config[wallet].configured) {
            // Persist defaults so the flag survives.
            _config[wallet].idleTargetBps = DEFAULT_IDLE_TARGET_BPS;
            _config[wallet].unparkThresholdBps = DEFAULT_UNPARK_THRESHOLD_BPS;
            _config[wallet].maxEntrySlippageBps = DEFAULT_MAX_ENTRY_SLIPPAGE_BPS;
            _config[wallet].configured = true;
        }
    }

    /// @notice Effective config for `wallet` (defaults if never set).
    function configOf(address wallet)
        public
        view
        returns (uint16 idleTargetBps, uint16 unparkThresholdBps, uint16 maxEntrySlippageBps, bool enabled)
    {
        Config storage c = _config[wallet];
        if (!c.configured) {
            return (
                DEFAULT_IDLE_TARGET_BPS,
                DEFAULT_UNPARK_THRESHOLD_BPS,
                DEFAULT_MAX_ENTRY_SLIPPAGE_BPS,
                true
            );
        }
        return (c.idleTargetBps, c.unparkThresholdBps, c.maxEntrySlippageBps, c.enabled);
    }

    // ─── accounting helpers ────────────────────────────────────────────────

    /// @dev Lazily attribute un-assigned adapter float to a wallet on first touch.
    function _seed(address wallet) internal {
        if (_seeded[wallet]) return;
        uint256 bal = asset.balanceOf(address(this));
        uint256 unattributed = bal > _attributedLiquid ? bal - _attributedLiquid : 0;
        liquidOf[wallet] = unattributed;
        _attributedLiquid += unattributed;
        _seeded[wallet] = true;
    }

    /// @notice Total economic value a wallet holds: liquid buffer + parked value.
    function totalValueOf(address wallet) public view returns (uint256) {
        uint256 parkedValue = parkedOf[wallet] == 0 ? 0 : vault.previewRedeem(parkedOf[wallet]);
        // Use the seeded liquid if present; otherwise the un-attributed balance.
        uint256 liquid = _seeded[wallet]
            ? liquidOf[wallet]
            : (asset.balanceOf(address(this)) > _attributedLiquid
                ? asset.balanceOf(address(this)) - _attributedLiquid
                : 0);
        return liquid + parkedValue;
    }

    // ─── core: rebalance ───────────────────────────────────────────────────

    /// @notice Post-settlement hook. Parks excess liquid above the idle target,
    ///         or pulls liquidity back when the buffer drops below it. Returns
    ///         the asset amount moved (parked or unparked); 0 on a no-op.
    function rebalance(address wallet) external nonReentrant returns (uint256 moved) {
        _seed(wallet);

        (uint16 idleTargetBps, , uint16 maxEntrySlippageBps, bool enabled) = configOf(wallet);
        if (!enabled) return 0;

        uint256 liquid = liquidOf[wallet];
        uint256 parkedValue = parkedOf[wallet] == 0 ? 0 : vault.previewRedeem(parkedOf[wallet]);
        uint256 total = liquid + parkedValue;
        if (total == 0) return 0;

        uint256 targetLiquid = (total * (BPS - idleTargetBps)) / BPS;
        uint256 deadband = (total * DEADBAND_BPS) / BPS;

        if (liquid > targetLiquid + deadband) {
            // Park the excess above target.
            uint256 excess = liquid - targetLiquid;
            return _park(wallet, excess, maxEntrySlippageBps);
        } else if (liquid + deadband < targetLiquid) {
            // Refill the buffer up toward target from the vault.
            uint256 deficit = targetLiquid - liquid;
            return _unpark(wallet, deficit);
        }
        return 0;
    }

    function _park(address wallet, uint256 assets, uint16 maxEntrySlippageBps)
        internal
        returns (uint256)
    {
        if (assets == 0) return 0;

        // Approve + deposit. Shares are minted to the adapter (this contract).
        asset.approve(address(vault), assets);
        uint256 sharesBefore = parkedOf[wallet];
        uint256 shares = vault.deposit(assets, address(this));

        // Enforce the entry-slippage cap on the realized share value.
        uint256 redeemable = vault.previewRedeem(shares);
        uint256 slippageBps = redeemable >= assets ? 0 : ((assets - redeemable) * BPS) / assets;
        if (slippageBps > maxEntrySlippageBps) {
            // Unwind: pull the assets straight back out and skip + log.
            vault.withdraw(assets, address(this), address(this));
            emit ParkSkipped(wallet, assets, uint16(slippageBps), maxEntrySlippageBps);
            return 0;
        }

        // Effects.
        parkedOf[wallet] = sharesBefore + shares;
        liquidOf[wallet] -= assets;
        _attributedLiquid -= assets;

        emit Parked(wallet, assets, shares);
        return assets;
    }

    // ─── core: unpark ────────────────────────────────────────────────────

    /// @notice Pull `amount` (capped at the wallet's parked balance) out of the
    ///         vault and back into the liquid buffer. Returns assets pulled.
    function unpark(address wallet, uint256 amount) external nonReentrant returns (uint256) {
        _seed(wallet);
        return _unpark(wallet, amount);
    }

    function _unpark(address wallet, uint256 amount) internal returns (uint256) {
        if (amount == 0) return 0;

        uint256 shares = parkedOf[wallet];
        if (shares == 0) return 0;

        uint256 available = vault.previewRedeem(shares);
        uint256 toWithdraw = amount > available ? available : amount;
        if (toWithdraw == 0) return 0;

        uint256 burned = vault.withdraw(toWithdraw, address(this), address(this));

        // Effects: shares are 1:1 in the mock; clamp defensively.
        parkedOf[wallet] = burned >= shares ? 0 : shares - burned;
        liquidOf[wallet] += toWithdraw;
        _attributedLiquid += toWithdraw;

        emit Unparked(wallet, toWithdraw);
        return toWithdraw;
    }

    // ─── test / simulation helper ────────────────────────────────────────

    /// @notice Reduce a wallet's tracked liquid buffer and send the assets out,
    ///         simulating a settlement spend. Owner-gated; used by the
    ///         post-settlement test harness and by integrations that custody
    ///         the float through this adapter.
    function debitLiquid(address wallet, uint256 amount) external onlyOwner {
        _seed(wallet);
        require(liquidOf[wallet] >= amount, "insufficient liquid");
        liquidOf[wallet] -= amount;
        _attributedLiquid -= amount;
        require(asset.transfer(msg.sender, amount), "debit transfer failed");
    }
}
