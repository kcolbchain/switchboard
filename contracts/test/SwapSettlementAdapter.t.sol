// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {MultiTokenAgentEscrow} from "../MultiTokenAgentEscrow.sol";
import {IAgentEscrow} from "../IAgentEscrow.sol";
import {IOracleAggregator} from "../IOracleAggregator.sol";
import {SwapSettlementAdapter} from "../SwapSettlementAdapter.sol";
import {ISwapRouter} from "../ISwapRouter.sol";
import {IPriceOracle} from "../IPriceOracle.sol";
import {MockOracleAggregator} from "../mocks/MockOracleAggregator.sol";
import {MockPriceOracle} from "../mocks/MockPriceOracle.sol";
import {MockSwapRouter} from "../mocks/MockSwapRouter.sol";
import {MockERC20} from "../mocks/MockERC20.sol";

/// @dev Inline Forge cheatcode interface — mirrors MultiTokenAgentEscrow.t.sol so
///      we don't depend on a forge-std submodule being present.
interface Vm {
    function deal(address who, uint256 amount) external;
    function prank(address who) external;
    function startPrank(address who) external;
    function stopPrank() external;
    function expectRevert(bytes calldata revertData) external;
    function expectRevert(bytes4 revertData) external;
    function expectRevert() external;
    function roll(uint256 newBlock) external;
    function warp(uint256 newTimestamp) external;
}

/**
 * @title SwapSettlementAdapterTest
 * @notice Unit ② tests (design spec §3.5 / §5): swap-at-release converts the
 *         escrowed payer-token into the payee's desired token, bounded by an
 *         oracle-derived slippage floor. The adapter sits OUTSIDE the escrow
 *         core — it acts as the escrow `payee`/`payer` for swap-settled
 *         payments, so the trustless escrow primitive keeps zero DEX/oracle
 *         attack surface.
 *
 *         Required cases:
 *           - successful X→Y swap at release (payer USDC -> payee DAI);
 *           - slippage-exceeded revert leaves the escrow FUNDED (atomic);
 *           - oracle-stale rejection (frozen price cannot justify a swap).
 */
contract SwapSettlementAdapterTest {
    Vm constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    MultiTokenAgentEscrow internal escrow;
    MockOracleAggregator internal agg;
    SwapSettlementAdapter internal adapter;
    MockPriceOracle internal oracle;
    MockSwapRouter internal router;

    MockERC20 internal usdc; // payer token (tokenIn)
    MockERC20 internal dai; // payee token (tokenOut)

    address internal payer = address(0xA11CE);
    address internal payee = address(0xB0B);
    address internal anyone = address(0xC0DE);

    uint256 internal constant MAX_STALENESS = 3600; // 1 hour
    uint256 internal constant NOW = 1_700_000_000;

    function setUp() public {
        agg = new MockOracleAggregator();
        escrow = new MultiTokenAgentEscrow(31337, IOracleAggregator(address(agg)));

        oracle = new MockPriceOracle();
        router = new MockSwapRouter();

        adapter = new SwapSettlementAdapter(
            IAgentEscrow(address(escrow)),
            ISwapRouter(address(router)),
            IPriceOracle(address(oracle)),
            MAX_STALENESS
        );

        usdc = new MockERC20("USD Coin", "USDC");
        dai = new MockERC20("Dai", "DAI");

        // The escrow must accept USDC (the token actually held in escrow).
        escrow.setTokenAllowed(address(usdc), true);

        // 1 USDC == 1 DAI fair value.
        oracle.setRate(address(usdc), address(dai), 1e18, NOW);
        // Router can fill 1:1 by default (fair execution). Fund its DAI liquidity.
        router.setRate(address(usdc), address(dai), 1e18);
        dai.mint(address(router), 1_000_000e18);

        usdc.mint(payer, 1_000_000e18);
        vm.warp(NOW);
    }

    // ─── Happy path: X→Y swap at release ────────────────────────────────────────

    function test_settleWithSwap_convertsPayerTokenToPayeeToken() public {
        // Payer opens a swap-settled escrow: fund 1000 USDC, payee wants DAI,
        // tolerate up to 0.5% slippage.
        vm.startPrank(payer);
        usdc.approve(address(adapter), 1000e18);
        adapter.openSwapEscrow(
            "swap-1",
            payee,
            address(usdc),
            1000e18,
            address(dai),
            50, // 0.5%
            100,
            10
        );
        // Escrow now holds the USDC, with the ADAPTER as payee.
        IAgentEscrow.Payment memory p = escrow.getPayment("swap-1");
        require(p.token == address(usdc), "escrow holds USDC");
        require(p.amount == 1000e18, "escrow credited 1000 USDC");
        require(p.payee == address(adapter), "adapter is escrow payee");
        require(usdc.balanceOf(address(escrow)) == 1000e18, "escrow funded");

        // Release + swap.
        adapter.settleWithSwap("swap-1", address(dai), 50);
        vm.stopPrank();

        // Payee received DAI (1:1), escrow drained, adapter holds nothing.
        require(dai.balanceOf(payee) == 1000e18, "payee received 1000 DAI");
        require(usdc.balanceOf(address(escrow)) == 0, "escrow drained of USDC");
        require(usdc.balanceOf(address(adapter)) == 0, "adapter holds no USDC");
        require(dai.balanceOf(address(adapter)) == 0, "adapter holds no DAI");

        IAgentEscrow.Payment memory released = escrow.getPayment("swap-1");
        require(uint8(released.state) == uint8(IAgentEscrow.State.Released), "escrow released");
    }

    // ─── Slippage-exceeded revert leaves escrow funded ──────────────────────────

    function test_settleWithSwap_revertsWhenSlippageExceedsBound_escrowStaysFunded() public {
        vm.startPrank(payer);
        usdc.approve(address(adapter), 1000e18);
        adapter.openSwapEscrow(
            "swap-slip",
            payee,
            address(usdc),
            1000e18,
            address(dai),
            50, // tolerate only 0.5%
            100,
            10
        );
        vm.stopPrank();

        // The router fills at 0.98 DAI per USDC (2% realized slippage, over the
        // 0.5% bound) AND does not enforce its own minOut floor — modeling a
        // broken/malicious router. The ADAPTER'S OWN realized-out re-check must
        // still catch it and revert the WHOLE release.
        router.setRate(address(usdc), address(dai), 98e16);
        router.setEnforceMinOut(false);

        vm.prank(payer);
        vm.expectRevert(bytes("slippage exceeds bound"));
        adapter.settleWithSwap("swap-slip", address(dai), 50);

        // Escrow stays funded and Locked; payee got nothing.
        require(usdc.balanceOf(address(escrow)) == 1000e18, "escrow still funded");
        require(dai.balanceOf(payee) == 0, "payee got nothing");
        IAgentEscrow.Payment memory p = escrow.getPayment("swap-slip");
        require(uint8(p.state) == uint8(IAgentEscrow.State.Locked), "escrow still Locked");
        require(p.amount == 1000e18, "escrow amount intact");
    }

    function test_settleWithSwap_revertsWhenRouterEnforcesFloor_escrowStaysFunded() public {
        // Same over-bound fill, but here the router DOES enforce its own floor.
        // The revert still leaves the escrow funded and Locked (atomicity holds
        // regardless of which guard fires first).
        vm.startPrank(payer);
        usdc.approve(address(adapter), 1000e18);
        adapter.openSwapEscrow("swap-floor", payee, address(usdc), 1000e18, address(dai), 50, 100, 10);
        vm.stopPrank();

        router.setRate(address(usdc), address(dai), 98e16); // 2% slippage
        // enforceMinOut defaults true.

        vm.prank(payer);
        vm.expectRevert(); // router-side "insufficient output"
        adapter.settleWithSwap("swap-floor", address(dai), 50);

        require(usdc.balanceOf(address(escrow)) == 1000e18, "escrow still funded");
        require(dai.balanceOf(payee) == 0, "payee got nothing");
        IAgentEscrow.Payment memory p = escrow.getPayment("swap-floor");
        require(uint8(p.state) == uint8(IAgentEscrow.State.Locked), "escrow still Locked");
    }

    // ─── Oracle-stale rejection ─────────────────────────────────────────────────

    function test_settleWithSwap_revertsWhenOraclePriceStale_escrowStaysFunded() public {
        vm.startPrank(payer);
        usdc.approve(address(adapter), 1000e18);
        adapter.openSwapEscrow(
            "swap-stale",
            payee,
            address(usdc),
            1000e18,
            address(dai),
            50,
            100,
            10
        );
        vm.stopPrank();

        // Advance time so the oracle observation (set at NOW) is older than the
        // max staleness window. Even though the router could fill fairly, the
        // stale price must not be trusted to bound the swap.
        vm.warp(NOW + MAX_STALENESS + 1);

        vm.prank(payer);
        vm.expectRevert(bytes("oracle price stale"));
        adapter.settleWithSwap("swap-stale", address(dai), 50);

        require(usdc.balanceOf(address(escrow)) == 1000e18, "escrow still funded");
        require(dai.balanceOf(payee) == 0, "payee got nothing");
        IAgentEscrow.Payment memory p = escrow.getPayment("swap-stale");
        require(uint8(p.state) == uint8(IAgentEscrow.State.Locked), "escrow still Locked");
    }

    // ─── Guards ─────────────────────────────────────────────────────────────────

    function test_settleWithSwap_onlyPayerCanSettle() public {
        vm.startPrank(payer);
        usdc.approve(address(adapter), 1000e18);
        adapter.openSwapEscrow("swap-auth", payee, address(usdc), 1000e18, address(dai), 50, 100, 10);
        vm.stopPrank();

        vm.prank(anyone);
        vm.expectRevert(bytes("only intent payer"));
        adapter.settleWithSwap("swap-auth", address(dai), 50);
    }

    function test_settleWithSwap_rejectsMismatchedTokenOut() public {
        vm.startPrank(payer);
        usdc.approve(address(adapter), 1000e18);
        adapter.openSwapEscrow("swap-tok", payee, address(usdc), 1000e18, address(dai), 50, 100, 10);
        // Caller passes a different tokenOut than was negotiated at open.
        vm.expectRevert(bytes("tokenOut mismatch"));
        adapter.settleWithSwap("swap-tok", address(usdc), 50);
        vm.stopPrank();
    }

    function test_settleWithSwap_rejectsWeakerSlippageThanNegotiated() public {
        // Payee negotiated <=0.5%; caller must not be able to loosen it to 5%.
        vm.startPrank(payer);
        usdc.approve(address(adapter), 1000e18);
        adapter.openSwapEscrow("swap-loose", payee, address(usdc), 1000e18, address(dai), 50, 100, 10);
        vm.expectRevert(bytes("slippage bound too loose"));
        adapter.settleWithSwap("swap-loose", address(dai), 500);
        vm.stopPrank();
    }

    function test_openSwapEscrow_pullsPayerTokenIntoEscrow() public {
        uint256 before = usdc.balanceOf(payer);
        vm.startPrank(payer);
        usdc.approve(address(adapter), 250e18);
        adapter.openSwapEscrow("swap-pull", payee, address(usdc), 250e18, address(dai), 50, 100, 10);
        vm.stopPrank();
        require(usdc.balanceOf(payer) == before - 250e18, "250 USDC pulled from payer");
        require(usdc.balanceOf(address(escrow)) == 250e18, "escrow holds 250 USDC");
        require(usdc.balanceOf(address(adapter)) == 0, "adapter passes funds straight through to escrow");
    }
}
