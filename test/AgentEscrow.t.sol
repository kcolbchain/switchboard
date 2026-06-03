// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test, console2} from "forge-std/Test.sol";
import {AgentEscrow} from "../contracts/AgentEscrow.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/// @notice Reentrancy attacker — re-enters confirmPayment from receive().
contract Reenterer {
    AgentEscrow public escrow;
    string public targetReq;
    bool public didReenter;

    constructor(AgentEscrow _escrow) {
        escrow = _escrow;
    }

    function setTarget(string calldata reqId) external {
        targetReq = reqId;
    }

    receive() external payable {
        if (!didReenter) {
            didReenter = true;
            // attempt re-entry; expected to revert under nonReentrant
            try escrow.confirmPayment(targetReq) {
            // shouldn't reach
            }
            catch {
                // swallow — outer call must still succeed for this test we WANT to revert
                revert("reentrancy blocked");
            }
        }
    }
}

contract AgentEscrowTest is Test {
    AgentEscrow internal escrow;

    address internal owner = address(0xA11CE);
    address internal payer = address(0xB0B);
    address internal payee = address(0xC0DE);
    address internal stranger = address(0xDEAD);

    uint256 internal constant TIMEOUT = 100;
    uint256 internal constant CHALLENGE = 10;
    uint256 internal constant AMOUNT = 1 ether;

    function setUp() public {
        vm.prank(owner);
        escrow = new AgentEscrow(31337);

        vm.deal(payer, 10 ether);
        vm.deal(stranger, 1 ether);
    }

    // ── Happy path ─────────────────────────────────────────────────────────

    function test_happyPath_createConfirmReleased() public {
        uint256 payeeBalBefore = payee.balance;

        vm.prank(payer);
        escrow.createPayment{value: AMOUNT}("req-1", payee, TIMEOUT, CHALLENGE);

        assertTrue(escrow.isState("req-1", AgentEscrow.State.Locked));

        vm.prank(payer);
        escrow.confirmPayment("req-1");

        assertTrue(escrow.isState("req-1", AgentEscrow.State.Released));
        assertEq(payee.balance, payeeBalBefore + AMOUNT);
    }

    // ── Timeout / refund path ──────────────────────────────────────────────

    function test_timeoutRefund_path() public {
        uint256 payerBalBefore = payer.balance;

        vm.prank(payer);
        escrow.createPayment{value: AMOUNT}("req-2", payee, TIMEOUT, CHALLENGE);

        // Try refund too early — must revert
        vm.prank(payer);
        vm.expectRevert(bytes("Challenge period not over"));
        escrow.requestRefund("req-2");

        // Roll past timeout + challenge
        vm.roll(block.number + TIMEOUT + CHALLENGE + 1);

        assertTrue(escrow.isExpired("req-2"));

        vm.prank(payer);
        escrow.requestRefund("req-2");

        assertTrue(escrow.isState("req-2", AgentEscrow.State.Refunded));
        // Net: payer paid AMOUNT, refund returned AMOUNT → balance unchanged
        assertEq(payer.balance, payerBalBefore);
    }

    // ── Double-confirm reverts ─────────────────────────────────────────────

    function test_doubleConfirm_reverts() public {
        vm.prank(payer);
        escrow.createPayment{value: AMOUNT}("req-3", payee, TIMEOUT, CHALLENGE);

        vm.prank(payer);
        escrow.confirmPayment("req-3");

        vm.prank(payer);
        vm.expectRevert(bytes("Payment not in Locked state"));
        escrow.confirmPayment("req-3");
    }

    // ── Regression: only owner can register agents ─────────────────────────
    // Previously registerAgent was permissionless — anyone could squat the
    // registry. This test pins the fix.

    function test_registerAgent_onlyOwner_strangerReverts() public {
        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, stranger));
        escrow.registerAgent(stranger);
        assertFalse(escrow.registeredAgents(stranger));
    }

    function test_registerAgent_onlyOwner_payerReverts() public {
        vm.prank(payer);
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, payer));
        escrow.registerAgent(payer);
    }

    function test_registerAgent_ownerSucceeds() public {
        vm.prank(owner);
        escrow.registerAgent(payee);
        assertTrue(escrow.registeredAgents(payee));
    }

    function test_registerAgent_zeroAddressReverts() public {
        vm.prank(owner);
        vm.expectRevert(bytes("agent cannot be zero address"));
        escrow.registerAgent(address(0));
    }

    // ── Reentrancy attempt reverts ─────────────────────────────────────────

    function test_reentrancy_confirmPayment_reverts() public {
        Reenterer attacker = new Reenterer(escrow);
        attacker.setTarget("req-r");

        // payer creates a payment whose payee is the attacker
        vm.prank(payer);
        escrow.createPayment{value: AMOUNT}("req-r", address(attacker), TIMEOUT, CHALLENGE);

        // Confirm — this triggers attacker.receive() which re-enters confirmPayment.
        // The Reenterer wraps the inner call in try/catch and reverts when re-entry
        // is blocked, which propagates out: the whole tx must revert with
        // "Transfer to payee failed" (since the payee call's success bool is false).
        vm.prank(payer);
        vm.expectRevert(bytes("Transfer to payee failed"));
        escrow.confirmPayment("req-r");

        // State must NOT be Released — refund still possible after timeout
        assertFalse(escrow.isState("req-r", AgentEscrow.State.Released));
    }

    // ── Cancel path ────────────────────────────────────────────────────────

    function test_cancel_returnsFunds() public {
        uint256 balBefore = payer.balance;

        vm.prank(payer);
        escrow.createPayment{value: AMOUNT}("req-c", payee, TIMEOUT, CHALLENGE);

        vm.prank(payer);
        escrow.cancelPayment("req-c");

        assertTrue(escrow.isState("req-c", AgentEscrow.State.Cancelled));
        assertEq(payer.balance, balBefore);
    }

    // ── Only payer can confirm ─────────────────────────────────────────────

    function test_onlyPayerCanConfirm() public {
        vm.prank(payer);
        escrow.createPayment{value: AMOUNT}("req-x", payee, TIMEOUT, CHALLENGE);

        vm.prank(stranger);
        vm.expectRevert(bytes("Only payer can confirm"));
        escrow.confirmPayment("req-x");
    }
}
