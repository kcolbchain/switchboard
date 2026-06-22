// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {LucidlyAdapter} from "../adapters/LucidlyAdapter.sol";
import {ILucidlyVault} from "../adapters/ILucidlyVault.sol";
import {MockLucidlyVault} from "../mocks/MockLucidlyVault.sol";
import {MockERC20} from "../mocks/MockERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/// @dev Inline Forge cheatcode interface. Mirrors AgentEscrowOracle.t.sol so
///      the suite does not need a `lib/forge-std` submodule.
interface Vm {
    function prank(address who) external;
    function startPrank(address who) external;
    function stopPrank() external;
    function expectRevert(bytes calldata revertData) external;
    function expectRevert(bytes4 revertData) external;
    function expectRevert() external;
    function expectEmit(bool, bool, bool, bool) external;
    function addr(uint256 privateKey) external pure returns (address);
}

contract LucidlyAdapterTest {
    Vm constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    LucidlyAdapter internal adapter;
    MockLucidlyVault internal vault;
    MockERC20 internal asset; // CR8-USD / USDC stand-in (6 decimals)

    address internal wallet = address(0xA11CE);
    address internal other = address(0xB0B);

    // 1 USDC == 1e6 (6 decimals). Use round numbers throughout.
    uint256 internal constant USDC = 1e6;

    // Mirror of the adapter's event so `expectEmit` can match it (Foundry idiom).
    event ParkSkipped(address indexed wallet, uint256 assets, uint16 slippageBps, uint16 capBps);

    function setUp() public {
        asset = new MockERC20("CR8-USD", "CR8USD", 6);
        vault = new MockLucidlyVault(address(asset));
        adapter = new LucidlyAdapter(ILucidlyVault(address(vault)), address(asset));

        // Seed the adapter as if it custodies this wallet's idle float.
        asset.mint(address(adapter), 10_000 * USDC);
    }

    // ─── default config ──────────────────────────────────────────────────

    function test_defaultConfigValues() public {
        (uint16 idle, uint16 unpark, uint16 slip, bool enabled) = adapter.configOf(wallet);
        // Unconfigured wallets fall back to the protocol defaults.
        require(idle == 8000, "default idle_target_bps == 8000");
        require(unpark == 1500, "default unpark_threshold_bps == 1500");
        require(slip == 25, "default max_entry_slippage_bps == 25");
        require(enabled, "enabled by default");
    }

    // ─── per-wallet config ────────────────────────────────────────────────

    function test_ownerCanSetPerWalletConfig() public {
        adapter.setConfig(wallet, 7000, 2000, 50);
        (uint16 idle, uint16 unpark, uint16 slip,) = adapter.configOf(wallet);
        require(idle == 7000, "idle set");
        require(unpark == 2000, "unpark set");
        require(slip == 50, "slip set");
    }

    function test_nonOwnerCannotSetConfig() public {
        vm.prank(other);
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, other));
        adapter.setConfig(wallet, 7000, 2000, 50);
    }

    function test_setConfigRejectsBpsAboveTenThousand() public {
        vm.expectRevert(bytes("idle_target_bps > 10000"));
        adapter.setConfig(wallet, 10001, 1500, 25);
    }

    function test_setConfigRejectsUnparkAboveTenThousand() public {
        vm.expectRevert(bytes("unpark_threshold_bps > 10000"));
        adapter.setConfig(wallet, 8000, 10001, 25);
    }

    // ─── park excess above idle target ────────────────────────────────────

    function test_rebalanceParksExcessAboveTarget() public {
        // Wallet custodies 10_000. Default idle_target_bps = 8000 (80% parked),
        // so target liquid = 20% = 2_000. Excess = 8_000 should be parked.
        uint256 parked = adapter.rebalance(wallet);
        require(parked == 8_000 * USDC, "parked 80% of float");
        require(asset.balanceOf(address(vault)) == 8_000 * USDC, "vault holds parked assets");
        require(adapter.liquidOf(wallet) == 2_000 * USDC, "20% buffer kept liquid");
        require(adapter.parkedOf(wallet) > 0, "shares recorded for wallet");
    }

    function test_rebalanceSkipsWhenWithinDeadband() public {
        // idle_target_bps = 0 means keep 100% liquid; nothing to park.
        adapter.setConfig(wallet, 0, 1500, 25);
        uint256 parked = adapter.rebalance(wallet);
        require(parked == 0, "nothing parked when already at/under target");
        require(adapter.parkedOf(wallet) == 0, "no shares");
    }

    function test_rebalanceHonorsFivePercentDeadband() public {
        // current liquid = 10_000. idle_target_bps = 400 => parked target 4%,
        // target liquid = 9_600. Excess = 400 which is < 5% of 10_000 (=500),
        // inside the deadband -> should NOT park (avoids churn).
        adapter.setConfig(wallet, 400, 1500, 25);
        uint256 parked = adapter.rebalance(wallet);
        require(parked == 0, "excess inside 5% deadband is not parked");
    }

    // ─── slippage cap ──────────────────────────────────────────────────────

    function test_rebalanceParksWhenSlippageAtCap() public {
        // 25 bps slippage == cap; entry is allowed (<=).
        vault.setEntrySlippageBps(25);
        uint256 parked = adapter.rebalance(wallet);
        require(parked == 8_000 * USDC, "park allowed at exactly the cap");
    }

    function test_rebalanceSkipsWhenSlippageExceedsCap() public {
        // 26 bps slippage > 25 bps cap -> skip + log, park nothing.
        vault.setEntrySlippageBps(26);
        uint256 parked = adapter.rebalance(wallet);
        require(parked == 0, "park skipped when slippage exceeds cap");
        require(asset.balanceOf(address(vault)) == 0, "no assets entered vault");
        require(adapter.liquidOf(wallet) == 10_000 * USDC, "full float stays liquid");
    }

    function test_rebalanceEmitsParkSkippedOnSlippage() public {
        vault.setEntrySlippageBps(100);
        vm.expectEmit(true, false, false, false);
        emit ParkSkipped(wallet, 0, 0, 0);
        adapter.rebalance(wallet);
    }

    // ─── unpark on demand ────────────────────────────────────────────────

    function test_unparkPullsFromVault() public {
        adapter.rebalance(wallet); // parks 8_000
        uint256 beforeLiquid = adapter.liquidOf(wallet); // 2_000

        uint256 pulled = adapter.unpark(wallet, 1_000 * USDC);

        require(pulled == 1_000 * USDC, "pulled requested amount");
        require(adapter.liquidOf(wallet) == beforeLiquid + 1_000 * USDC, "liquid grew");
        require(asset.balanceOf(address(vault)) == 7_000 * USDC, "vault shrank");
    }

    function test_unparkCapsAtAvailable() public {
        adapter.rebalance(wallet); // parks 8_000
        // Ask for more than parked; should pull only what is available.
        uint256 pulled = adapter.unpark(wallet, 100_000 * USDC);
        require(pulled == 8_000 * USDC, "pull capped at parked balance");
        require(adapter.parkedOf(wallet) == 0, "vault drained for wallet");
    }

    function test_unparkZeroWhenNothingParked() public {
        uint256 pulled = adapter.unpark(wallet, 1_000 * USDC);
        require(pulled == 0, "nothing to unpark");
    }

    // ─── rebalance withdraws when liquid below target band ─────────────────

    function test_rebalanceWithdrawsWhenBelowTargetBand() public {
        adapter.rebalance(wallet); // liquid 2_000, parked 8_000, target liquid 2_000

        // Drain liquid below the lower deadband: simulate a settlement spending
        // 1_500, leaving 500 liquid (target 2_000, lower band 2_000 - 5% of
        // 10_000 = 1_500).
        adapter.debitLiquid(wallet, 1_500 * USDC); // test helper to simulate a spend

        uint256 moved = adapter.rebalance(wallet);
        // Should pull from vault to restore toward the 2_000 target.
        require(moved > 0, "withdrew to refill buffer");
        require(adapter.liquidOf(wallet) >= 1_500 * USDC, "buffer restored toward target");
    }

    // ─── reentrancy guard ──────────────────────────────────────────────────

    function test_rebalanceIsNonReentrant() public {
        // A malicious vault re-enters rebalance() during deposit. The guard must trip.
        ReentrantVault evil = new ReentrantVault(address(asset));
        LucidlyAdapter a = new LucidlyAdapter(ILucidlyVault(address(evil)), address(asset));
        asset.mint(address(a), 10_000 * USDC);
        evil.arm(a, wallet);

        vm.expectRevert(ReentrancyGuard.ReentrancyGuardReentrantCall.selector);
        a.rebalance(wallet);
    }
}

/// @dev Vault that re-enters the adapter's rebalance() inside deposit().
contract ReentrantVault is ILucidlyVault {
    MockERC20 internal asset;
    LucidlyAdapter internal target;
    address internal wallet;
    bool internal armed;

    constructor(address _asset) {
        asset = MockERC20(_asset);
    }

    function arm(LucidlyAdapter _target, address _wallet) external {
        target = _target;
        wallet = _wallet;
        armed = true;
    }

    function deposit(uint256 assets, address) external returns (uint256 shares) {
        // Pull the assets then attempt re-entry.
        asset.transferFrom(msg.sender, address(this), assets);
        if (armed) {
            target.rebalance(wallet); // should revert via nonReentrant
        }
        return assets;
    }

    function withdraw(uint256 assets, address, address) external returns (uint256) {
        asset.transfer(msg.sender, assets);
        return assets;
    }

    function previewRedeem(uint256 shares) external pure returns (uint256) {
        return shares;
    }
}
