// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import "../contracts/AgentEscrow.sol";
import "@openzeppelin/contracts/token/ERC20/ERC20.sol";

/// @dev Minimal ERC-20 for testing.
contract MockToken is ERC20 {
    constructor() ERC20("Mock", "MCK") {
        _mint(msg.sender, 1_000_000 ether);
    }
}

contract AgentEscrowTest is Test {
    AgentEscrow escrow;
    MockToken   token;

    address buyer  = address(0xB0B);
    address seller = address(0x5E11);

    function setUp() public {
        escrow = new AgentEscrow();
        token  = new MockToken();

        // Fund buyer with ETH and tokens
        vm.deal(buyer, 100 ether);
        token.transfer(buyer, 10_000 ether);
    }

    // -----------------------------------------------------------------------
    // ETH escrow happy path
    // -----------------------------------------------------------------------

    function test_createAndReleaseETH() public {
        uint256 amount   = 1 ether;
        uint256 deadline = block.timestamp + 1 hours;

        vm.startPrank(buyer);
        uint256 id = escrow.createEscrow{value: amount}(
            seller, address(0), amount, deadline, 1, new address[](0), 0
        );
        vm.stopPrank();

        // Verify state
        (
            address b, address s, address t, uint256 amt,
            uint256 dl, uint256 n, AgentEscrow.Status status,,
        ) = escrow.getEscrow(id);
        assertEq(b, buyer);
        assertEq(s, seller);
        assertEq(t, address(0));
        assertEq(amt, amount);
        assertEq(dl, deadline);
        assertEq(n, 1);
        assertTrue(status == AgentEscrow.Status.Created);

        // Release
        uint256 sellerBefore = seller.balance;
        vm.prank(buyer);
        escrow.releaseEscrow(id);

        (,,,,,,AgentEscrow.Status st,,) = escrow.getEscrow(id);
        assertTrue(st == AgentEscrow.Status.Released);
        assertEq(seller.balance - sellerBefore, amount);
    }

    // -----------------------------------------------------------------------
    // ERC-20 escrow happy path
    // -----------------------------------------------------------------------

    function test_createAndReleaseERC20() public {
        uint256 amount   = 500 ether;
        uint256 deadline = block.timestamp + 1 hours;

        vm.startPrank(buyer);
        token.approve(address(escrow), amount);
        uint256 id = escrow.createEscrow(
            seller, address(token), amount, deadline, 2, new address[](0), 0
        );
        vm.stopPrank();

        assertEq(token.balanceOf(address(escrow)), amount);

        vm.prank(buyer);
        escrow.releaseEscrow(id);

        assertEq(token.balanceOf(seller), amount);
    }

    // -----------------------------------------------------------------------
    // Refund after deadline
    // -----------------------------------------------------------------------

    function test_refundAfterDeadline() public {
        uint256 amount   = 1 ether;
        uint256 deadline = block.timestamp + 1 hours;

        vm.prank(buyer);
        uint256 id = escrow.createEscrow{value: amount}(
            seller, address(0), amount, deadline, 3, new address[](0), 0
        );

        // Cannot refund before deadline
        vm.expectRevert(AgentEscrow.DeadlineNotReached.selector);
        escrow.refundEscrow(id);

        // Warp past deadline
        vm.warp(deadline + 1);

        uint256 buyerBefore = buyer.balance;
        escrow.refundEscrow(id);

        (,,,,,,AgentEscrow.Status st,,) = escrow.getEscrow(id);
        assertTrue(st == AgentEscrow.Status.Refunded);
        assertEq(buyer.balance - buyerBefore, amount);
    }

    // -----------------------------------------------------------------------
    // Nonce replay protection
    // -----------------------------------------------------------------------

    function test_nonceReplay() public {
        uint256 deadline = block.timestamp + 1 hours;

        vm.startPrank(buyer);
        escrow.createEscrow{value: 1 ether}(
            seller, address(0), 1 ether, deadline, 10, new address[](0), 0
        );

        vm.expectRevert(AgentEscrow.NonceAlreadyUsed.selector);
        escrow.createEscrow{value: 1 ether}(
            seller, address(0), 1 ether, deadline, 10, new address[](0), 0
        );
        vm.stopPrank();
    }

    // -----------------------------------------------------------------------
    // Only buyer can release
    // -----------------------------------------------------------------------

    function test_onlyBuyerCanRelease() public {
        uint256 deadline = block.timestamp + 1 hours;

        vm.prank(buyer);
        uint256 id = escrow.createEscrow{value: 1 ether}(
            seller, address(0), 1 ether, deadline, 20, new address[](0), 0
        );

        vm.prank(seller);
        vm.expectRevert(AgentEscrow.NotBuyer.selector);
        escrow.releaseEscrow(id);
    }

    // -----------------------------------------------------------------------
    // Multi-sig approval flow
    // -----------------------------------------------------------------------

    function test_multiSigRelease() public {
        address approver1 = address(0xA1);
        address approver2 = address(0xA2);
        address[] memory approvers = new address[](2);
        approvers[0] = approver1;
        approvers[1] = approver2;

        uint256 deadline = block.timestamp + 1 hours;

        vm.prank(buyer);
        uint256 id = escrow.createEscrow{value: 1 ether}(
            seller, address(0), 1 ether, deadline, 30, approvers, 2
        );

        // Buyer cannot release without approvals
        vm.prank(buyer);
        vm.expectRevert(AgentEscrow.ThresholdNotReached.selector);
        escrow.releaseEscrow(id);

        // First approval
        vm.prank(approver1);
        escrow.approveRelease(id);

        // Still can't release (need 2)
        vm.prank(buyer);
        vm.expectRevert(AgentEscrow.ThresholdNotReached.selector);
        escrow.releaseEscrow(id);

        // Second approval
        vm.prank(approver2);
        escrow.approveRelease(id);

        // Now release succeeds
        vm.prank(buyer);
        escrow.releaseEscrow(id);

        (,,,,,,AgentEscrow.Status st,,) = escrow.getEscrow(id);
        assertTrue(st == AgentEscrow.Status.Released);
    }

    // -----------------------------------------------------------------------
    // Cannot double-approve
    // -----------------------------------------------------------------------

    function test_doubleApproveReverts() public {
        address approver = address(0xA1);
        address[] memory approvers = new address[](1);
        approvers[0] = approver;

        vm.prank(buyer);
        uint256 id = escrow.createEscrow{value: 1 ether}(
            seller, address(0), 1 ether, block.timestamp + 1 hours, 40, approvers, 1
        );

        vm.startPrank(approver);
        escrow.approveRelease(id);

        vm.expectRevert(AgentEscrow.AlreadyApproved.selector);
        escrow.approveRelease(id);
        vm.stopPrank();
    }

    // -----------------------------------------------------------------------
    // Invalid parameters
    // -----------------------------------------------------------------------

    function test_zeroAmountReverts() public {
        vm.prank(buyer);
        vm.expectRevert(AgentEscrow.InvalidAmount.selector);
        escrow.createEscrow(
            seller, address(0), 0, block.timestamp + 1 hours, 50, new address[](0), 0
        );
    }

    function test_zeroSellerReverts() public {
        vm.prank(buyer);
        vm.expectRevert(AgentEscrow.InvalidSeller.selector);
        escrow.createEscrow{value: 1 ether}(
            address(0), address(0), 1 ether, block.timestamp + 1 hours, 60, new address[](0), 0
        );
    }

    function test_pastDeadlineReverts() public {
        vm.prank(buyer);
        vm.expectRevert(AgentEscrow.DeadlineTooSoon.selector);
        escrow.createEscrow{value: 1 ether}(
            seller, address(0), 1 ether, block.timestamp - 1, 70, new address[](0), 0
        );
    }

    function test_wrongETHValueReverts() public {
        vm.prank(buyer);
        vm.expectRevert(AgentEscrow.IncorrectETHValue.selector);
        escrow.createEscrow{value: 0.5 ether}(
            seller, address(0), 1 ether, block.timestamp + 1 hours, 80, new address[](0), 0
        );
    }
}
