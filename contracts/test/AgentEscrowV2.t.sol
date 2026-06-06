// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {AgentEscrowV2} from "../AgentEscrowV2.sol";
import {IRefundPolicy} from "../policies/IRefundPolicy.sol";
import {TimeoutPolicy} from "../policies/TimeoutPolicy.sol";
import {ProRataPolicy} from "../policies/ProRataPolicy.sol";
import {MultiNightPolicy} from "../policies/MultiNightPolicy.sol";
import {IOracleAggregator} from "../IOracleAggregator.sol";

contract MockOracleAggregator is IOracleAggregator {
    function verifyRelease(bytes32, bytes32, bytes[] calldata) external pure returns (bool) {
        return true;
    }
}

contract AgentEscrowV2Test is Test {
    AgentEscrowV2 public escrow;
    MockOracleAggregator public oracle;
    TimeoutPolicy public timeoutPolicy;
    ProRataPolicy public proRataPolicy;
    MultiNightPolicy public multiNightPolicy;

    address public payer = address(0x111);
    address public payee = address(0x222);

    function setUp() public {
        oracle = new MockOracleAggregator();
        escrow = new AgentEscrowV2(1, oracle);
        timeoutPolicy = new TimeoutPolicy();
        proRataPolicy = new ProRataPolicy();
        multiNightPolicy = new MultiNightPolicy();

        vm.deal(payer, 100 ether);
    }

    function test_TimeoutPolicy() public {
        bytes memory policyData = abi.encode(1 ether, 100, 10);
        
        vm.prank(payer);
        escrow.createPayment{value: 1 ether}(
            "req1",
            payee,
            timeoutPolicy,
            policyData,
            100
        );

        vm.roll(block.number + 50);

        // Refund should fail before timeout
        vm.prank(payer);
        vm.expectRevert("No refund available");
        escrow.requestRefund("req1", policyData);

        vm.roll(block.number + 60);

        // Refund should succeed after timeout + challenge
        vm.prank(payer);
        escrow.requestRefund("req1", policyData);

        AgentEscrowV2.Payment memory p = escrow.getPayment("req1");
        assertEq(uint(p.state), uint(AgentEscrowV2.State.Refunded));
        assertEq(payer.balance, 100 ether);
    }

    function test_ProRataPolicy() public {
        bytes memory policyData = abi.encode(100 ether, 100);
        
        vm.prank(payer);
        escrow.createPayment{value: 100 ether}(
            "req2",
            payee,
            proRataPolicy,
            policyData,
            100
        );

        vm.roll(block.number + 25);

        // After 25 blocks (25%), payee can release 25 ether, and 75 ether is refundable
        vm.prank(payer);
        escrow.requestRefund("req2", policyData);

        // Payer gets 75 ether back
        assertEq(payer.balance, 75 ether); // (Started with 100, spent 100, got 75 back)

        // Payee can release 25 ether
        escrow.releasePartial("req2", policyData);
        assertEq(payee.balance, 25 ether);

        AgentEscrowV2.Payment memory p = escrow.getPayment("req2");
        assertEq(uint(p.state), uint(AgentEscrowV2.State.Released)); // Payer refunded earlier and it didn't terminate, but maybe we should check if released+refunded == amount
    }

    function test_MultiNightPolicy() public {
        // totalAmount: 30 ether, blocksPerNight: 10, totalNights: 3
        bytes memory policyData = abi.encode(30 ether, 10, 3);
        
        vm.prank(payer);
        escrow.createPayment{value: 30 ether}(
            "req3",
            payee,
            multiNightPolicy,
            policyData,
            30
        );

        vm.roll(block.number + 15); // 1.5 nights elapsed -> 1 night completed

        // Payee gets 1 night's pay (10 ether)
        escrow.releasePartial("req3", policyData);
        assertEq(payee.balance, 10 ether);

        vm.roll(block.number + 20); // 3.5 nights elapsed -> 3 nights completed

        escrow.releasePartial("req3", policyData);
        assertEq(payee.balance, 30 ether); // 10 from before + 20 now

        AgentEscrowV2.Payment memory p = escrow.getPayment("req3");
        assertEq(uint(p.state), uint(AgentEscrowV2.State.Released));
    }
}
