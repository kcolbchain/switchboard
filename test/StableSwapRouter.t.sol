// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test, console2} from "forge-std/Test.sol";
import {StableSwapRouter} from "../contracts/StableSwapRouter.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

contract MockToken is ERC20 {
    constructor(string memory name, string memory symbol) ERC20(name, symbol) {}
    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }
}

contract StableSwapRouterTest is Test {
    StableSwapRouter router;
    MockToken cr8usd;
    MockToken musd;
    address treasury = address(0x111);
    address user1 = address(0x222);
    address user2 = address(0x333);

    function setUp() public {
        cr8usd = new MockToken("CR8-USD", "CR8-USD");
        musd = new MockToken("MUSD", "MUSD");
        
        router = new StableSwapRouter(address(cr8usd), address(musd), treasury, 5);

        // Mint tokens to router to act as reserve
        cr8usd.mint(address(router), 1_000_000 * 10**18);
        musd.mint(address(router), 1_000_000 * 10**18);
    }

    function test_constructor() public view {
        assertEq(address(router.cr8usd()), address(cr8usd));
        assertEq(address(router.musd()), address(musd));
        assertEq(router.treasury(), treasury);
        assertEq(router.feeBps(), 5);
    }

    function test_swapCR8USDtoMUSD() public {
        uint256 swapAmount = 1000 * 10**18;
        cr8usd.mint(user1, swapAmount);
        
        vm.startPrank(user1);
        cr8usd.approve(address(router), swapAmount);
        
        uint256 expectedFee = swapAmount * 5 / 10000;
        uint256 expectedOut = swapAmount - expectedFee;
        
        router.swapCR8USDtoMUSD(swapAmount, user2);
        vm.stopPrank();
        
        assertEq(cr8usd.balanceOf(user1), 0);
        assertEq(cr8usd.balanceOf(treasury), expectedFee);
        assertEq(cr8usd.balanceOf(address(router)), 1_000_000 * 10**18 + swapAmount - expectedFee);
        
        assertEq(musd.balanceOf(user2), expectedOut);
        assertEq(musd.balanceOf(address(router)), 1_000_000 * 10**18 - expectedOut);
    }

    function test_swapMUSDtoCR8USD() public {
        uint256 swapAmount = 2000 * 10**18;
        musd.mint(user1, swapAmount);
        
        vm.startPrank(user1);
        musd.approve(address(router), swapAmount);
        
        uint256 expectedFee = swapAmount * 5 / 10000;
        uint256 expectedOut = swapAmount - expectedFee;
        
        router.swapMUSDtoCR8USD(swapAmount, user2);
        vm.stopPrank();
        
        assertEq(musd.balanceOf(user1), 0);
        assertEq(musd.balanceOf(treasury), expectedFee);
        assertEq(musd.balanceOf(address(router)), 1_000_000 * 10**18 + swapAmount - expectedFee);
        
        assertEq(cr8usd.balanceOf(user2), expectedOut);
        assertEq(cr8usd.balanceOf(address(router)), 1_000_000 * 10**18 - expectedOut);
    }

    function test_rate_limit() public {
        uint256 limit = router.defaultEpochLimit();
        uint256 overLimit = limit + 1;
        
        cr8usd.mint(user1, overLimit);
        vm.startPrank(user1);
        cr8usd.approve(address(router), overLimit);
        
        vm.expectRevert("Epoch volume limit exceeded");
        router.swapCR8USDtoMUSD(overLimit, user1);
        vm.stopPrank();
    }
}
