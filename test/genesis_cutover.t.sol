// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {AgentEscrow} from "../contracts/AgentEscrow.sol";
import {IOracleAggregator} from "../contracts/IOracleAggregator.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

contract AgentEscrowGenesisCutoverTest is Test {
    address internal constant CANDIDATE_GENESIS_ADDRESS = address(0xA002);
    uint256 internal constant MAINNET_CHAIN_ID = 7777777;
    uint256 internal constant TIMEOUT = 100;
    uint256 internal constant CHALLENGE = 10;
    uint256 internal constant AMOUNT = 0.01 ether;

    AgentEscrow internal genesisEscrow;

    address internal council = address(0xC011C11);
    address internal replacementCouncil = address(0xC011C12);
    address internal payer = address(0xB0B);
    address internal payee = address(0xC0DE);
    address internal unapprovedAgent = address(0xA6E17);

    function setUp() public {
        vm.prank(council);
        genesisEscrow = new AgentEscrow(MAINNET_CHAIN_ID, IOracleAggregator(address(0)));

        vm.deal(payer, 1 ether);
    }

    function testGenesisRuntimeCodeMatchesFreshDeploy() public {
        vm.prank(council);
        AgentEscrow freshEscrow = new AgentEscrow(MAINNET_CHAIN_ID, IOracleAggregator(address(0)));

        assertEq(address(genesisEscrow).codehash, address(freshEscrow).codehash);
        assertEq(CANDIDATE_GENESIS_ADDRESS, address(0x000000000000000000000000000000000000A002));
    }

    function testGenesisOwnerIsCouncilMultisig() public view {
        assertEq(genesisEscrow.owner(), council);
    }

    function testGenesisStartsWithNoBalanceOrPayments() public view {
        assertEq(address(genesisEscrow).balance, 0);
        assertEq(genesisEscrow.chainId(), MAINNET_CHAIN_ID);
        assertEq(address(genesisEscrow.oracleAggregator()), address(0));
        assertFalse(genesisEscrow.registeredAgents(unapprovedAgent));

        AgentEscrow.Payment memory neverCreated = genesisEscrow.getPayment("never-created");
        assertEq(neverCreated.payer, address(0));
        assertEq(neverCreated.payee, address(0));
        assertEq(neverCreated.amount, 0);
        assertEq(neverCreated.createdAt, 0);
        assertEq(uint8(neverCreated.state), uint8(AgentEscrow.State.Created));
    }

    function testCreatePaymentHappyPathAfterGenesis() public {
        uint256 payeeBalanceBefore = payee.balance;

        vm.prank(payer);
        genesisEscrow.createPayment{value: AMOUNT}("mainnet:req-1", payee, TIMEOUT, CHALLENGE);

        assertTrue(genesisEscrow.isState("mainnet:req-1", AgentEscrow.State.Locked));

        vm.prank(payer);
        genesisEscrow.confirmPayment("mainnet:req-1");

        assertTrue(genesisEscrow.isState("mainnet:req-1", AgentEscrow.State.Released));
        assertEq(payee.balance, payeeBalanceBefore + AMOUNT);
        assertEq(address(genesisEscrow).balance, 0);
    }

    function testTimeoutRefundAfterGenesis() public {
        uint256 payerBalanceBefore = payer.balance;

        vm.prank(payer);
        genesisEscrow.createPayment{value: AMOUNT}("mainnet:req-refund", payee, TIMEOUT, CHALLENGE);

        vm.roll(block.number + TIMEOUT + CHALLENGE + 1);

        vm.prank(payer);
        genesisEscrow.requestRefund("mainnet:req-refund");

        assertTrue(genesisEscrow.isState("mainnet:req-refund", AgentEscrow.State.Refunded));
        assertEq(payer.balance, payerBalanceBefore);
        assertEq(address(genesisEscrow).balance, 0);
    }

    function testChallengePathAfterGenesis() public {
        vm.prank(payer);
        genesisEscrow.createPayment{value: AMOUNT}("mainnet:req-challenge", payee, TIMEOUT, CHALLENGE);

        vm.roll(block.number + TIMEOUT);

        assertTrue(genesisEscrow.isExpired("mainnet:req-challenge"));

        vm.prank(payer);
        vm.expectRevert(bytes("Challenge period not over"));
        genesisEscrow.requestRefund("mainnet:req-challenge");

        vm.roll(block.number + CHALLENGE + 1);

        vm.prank(payer);
        genesisEscrow.requestRefund("mainnet:req-challenge");

        assertTrue(genesisEscrow.isState("mainnet:req-challenge", AgentEscrow.State.Refunded));
    }

    function testCouncilOwnershipTransfer() public {
        vm.prank(council);
        genesisEscrow.transferOwnership(replacementCouncil);

        vm.prank(council);
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, council));
        genesisEscrow.registerAgent(unapprovedAgent);

        vm.prank(replacementCouncil);
        genesisEscrow.registerAgent(unapprovedAgent);

        assertEq(genesisEscrow.owner(), replacementCouncil);
        assertTrue(genesisEscrow.registeredAgents(unapprovedAgent));
    }

    function testPreGenesisRequestIdDoesNotCollide() public {
        vm.prank(payer);
        genesisEscrow.createPayment{value: AMOUNT}("testnet:req-1", payee, TIMEOUT, CHALLENGE);

        vm.prank(payer);
        genesisEscrow.createPayment{value: AMOUNT}("mainnet:req-1", payee, TIMEOUT, CHALLENGE);

        AgentEscrow.Payment memory testnetPayment = genesisEscrow.getPayment("testnet:req-1");
        AgentEscrow.Payment memory mainnetPayment = genesisEscrow.getPayment("mainnet:req-1");

        assertEq(testnetPayment.requestId, "testnet:req-1");
        assertEq(mainnetPayment.requestId, "mainnet:req-1");
        assertTrue(genesisEscrow.isState("testnet:req-1", AgentEscrow.State.Locked));
        assertTrue(genesisEscrow.isState("mainnet:req-1", AgentEscrow.State.Locked));
    }
}
