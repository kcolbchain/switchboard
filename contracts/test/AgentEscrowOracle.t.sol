// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {AgentEscrow} from "../AgentEscrow.sol";
import {IOracleAggregator} from "../IOracleAggregator.sol";
import {MockOracleAggregator} from "../mocks/MockOracleAggregator.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

/// @dev Inline Forge cheatcode interface. Avoids a `lib/forge-std` submodule
///      by declaring only the subset of `Vm` we use.
interface Vm {
    function deal(address who, uint256 amount) external;
    function prank(address who) external;
    function startPrank(address who) external;
    function stopPrank() external;
    function expectRevert(bytes calldata revertData) external;
    function expectRevert(bytes4 revertData) external;
    function expectRevert() external;
    function roll(uint256 newBlock) external;
    function addr(uint256 privateKey) external pure returns (address);
}

contract AgentEscrowOracleTest {
    Vm constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    AgentEscrow internal escrow;
    MockOracleAggregator internal agg;

    address internal payer  = address(0xA11CE);
    address internal payee  = address(0xB0B);
    address internal anyone = address(0xC0DE);

    bytes32 internal constant POLICY = keccak256("policy: GET ipfs://bafy.../delivery.json status==200 body_hash==0x4a");
    bytes32 internal constant ATTEST = keccak256("attestation: request_id=req-7f3e check_result=true observed_at=1234");

    function setUp() public {
        agg = new MockOracleAggregator();
        escrow = new AgentEscrow(31337, IOracleAggregator(address(agg)));
        vm.deal(payer, 10 ether);
    }

    // ─── Agent Registration Access Control ───────────────────────────────

    function test_ownerCanRegisterAgent() public {
        address newAgent = address(0xBEEF);
        escrow.registerAgent(newAgent);
        require(escrow.registeredAgents(newAgent), "agent should be registered");
    }

    function test_nonOwnerCannotRegisterAgent() public {
        vm.startPrank(anyone);
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, anyone));
        escrow.registerAgent(address(0xBEEF));
        vm.stopPrank();
    }

    function test_ownerCanDeregisterAgent() public {
        address agent = address(0xFEED);
        escrow.registerAgent(agent);
        require(escrow.registeredAgents(agent), "agent should be registered");
        escrow.deregisterAgent(agent);
        require(!escrow.registeredAgents(agent), "agent should be deregistered");
    }

    function test_deregisterUnregisteredIsIdempotent() public {
        escrow.deregisterAgent(address(0xDEAD));
        require(!escrow.registeredAgents(address(0xDEAD)), "should remain unregistered");
    }

    function test_nonOwnerCannotDeregister() public {
        address agent = address(0xC0FFEE);
        escrow.registerAgent(agent);
        vm.startPrank(anyone);
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, anyone));
        escrow.deregisterAgent(agent);
        vm.stopPrank();
    }

    function test_ownerCanTransferOwnership() public {
        address newOwner = address(0xF00D);
        escrow.transferOwnership(newOwner);
        require(escrow.owner() == newOwner, "ownership not transferred");
    }

    function test_nonOwnerCannotTransferOwnership() public {
        vm.startPrank(anyone);
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, anyone));
        escrow.transferOwnership(address(0xF00D));
        vm.stopPrank();
    }

    function test_transferOwnershipToZeroReverts() public {
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableInvalidOwner.selector, address(0)));
        escrow.transferOwnership(address(0));
    }

    // ─── Backward-compat: old 4-arg createPayment still works ─────────────

    function test_legacy_createPayment_storesZeroPolicyHash() public {
        vm.startPrank(payer);
        escrow.createPayment{value: 1 ether}("req-1", payee, 100, 10);
        vm.stopPrank();

        AgentEscrow.Payment memory p = escrow.getPayment("req-1");
        require(p.policyHash == bytes32(0), "legacy createPayment must default to policyHash 0");
        require(p.payer == payer, "payer set");
        require(p.amount == 1 ether, "amount set");
    }

    function test_legacy_payerCanStillConfirm() public {
        vm.startPrank(payer);
        escrow.createPayment{value: 1 ether}("req-2", payee, 100, 10);
        escrow.confirmPayment("req-2");
        vm.stopPrank();

        require(payee.balance == 1 ether, "payee received funds");
    }

    // ─── New 5-arg createPaymentWithPolicy ────────────────────────────────

    function test_createPaymentWithPolicy_storesHash() public {
        vm.startPrank(payer);
        escrow.createPaymentWithPolicy{value: 1 ether}("req-3", payee, 100, 10, POLICY);
        vm.stopPrank();

        AgentEscrow.Payment memory p = escrow.getPayment("req-3");
        require(p.policyHash == POLICY, "policyHash stored");
    }

    function test_createPaymentWithPolicy_zeroHashIsLegalAndEqualsLegacyForm() public {
        vm.startPrank(payer);
        escrow.createPaymentWithPolicy{value: 1 ether}("req-4", payee, 100, 10, bytes32(0));
        vm.stopPrank();

        AgentEscrow.Payment memory p = escrow.getPayment("req-4");
        require(p.policyHash == bytes32(0), "explicit zero policyHash treated as no-policy");
    }

    function test_createPaymentWithPolicy_revertsWhenAggregatorMissing() public {
        // Deploy a fresh escrow without an aggregator
        AgentEscrow bare = new AgentEscrow(31337, IOracleAggregator(address(0)));
        vm.deal(payer, 1 ether);

        vm.startPrank(payer);
        vm.expectRevert(bytes("no aggregator configured"));
        bare.createPaymentWithPolicy{value: 1 ether}("req-5", payee, 100, 10, POLICY);
        vm.stopPrank();
    }

    // ─── releaseByAttestation happy path ──────────────────────────────────

    function test_releaseByAttestation_success() public {
        vm.startPrank(payer);
        escrow.createPaymentWithPolicy{value: 1 ether}("req-6", payee, 100, 10, POLICY);
        vm.stopPrank();

        // Aggregator authorizes this (policy, attestation) pair.
        agg.setAccept(POLICY, ATTEST, true);

        // Anyone may submit — releaseByAttestation is permissionless.
        vm.prank(anyone);
        bytes[] memory sigs = new bytes[](2);
        sigs[0] = hex"deadbeef";
        sigs[1] = hex"cafef00d";
        escrow.releaseByAttestation("req-6", ATTEST, sigs);

        require(payee.balance == 1 ether, "payee received funds");
        AgentEscrow.Payment memory p = escrow.getPayment("req-6");
        require(uint8(p.state) == uint8(AgentEscrow.State.Released), "state is Released");
    }

    // ─── releaseByAttestation revert paths ────────────────────────────────

    function test_releaseByAttestation_revertsWhenAggregatorRejects() public {
        vm.startPrank(payer);
        escrow.createPaymentWithPolicy{value: 1 ether}("req-7", payee, 100, 10, POLICY);
        vm.stopPrank();

        // Default mock: rejects. (setAccept not called.)
        vm.prank(anyone);
        bytes[] memory sigs = new bytes[](0);
        vm.expectRevert(bytes("Oracle attestation rejected"));
        escrow.releaseByAttestation("req-7", ATTEST, sigs);
    }

    function test_releaseByAttestation_revertsOnNoPolicy() public {
        // Legacy payment without a policy
        vm.startPrank(payer);
        escrow.createPayment{value: 1 ether}("req-8", payee, 100, 10);
        vm.stopPrank();

        agg.setAccept(POLICY, ATTEST, true);

        vm.prank(anyone);
        bytes[] memory sigs = new bytes[](0);
        vm.expectRevert(bytes("No oracle policy on this payment"));
        escrow.releaseByAttestation("req-8", ATTEST, sigs);
    }

    function test_releaseByAttestation_revertsAfterTimeout() public {
        vm.startPrank(payer);
        escrow.createPaymentWithPolicy{value: 1 ether}("req-9", payee, 100, 10, POLICY);
        vm.stopPrank();

        agg.setAccept(POLICY, ATTEST, true);

        // Advance past timeout
        vm.roll(block.number + 101);

        vm.prank(anyone);
        bytes[] memory sigs = new bytes[](0);
        vm.expectRevert(bytes("Payment has expired"));
        escrow.releaseByAttestation("req-9", ATTEST, sigs);
    }

    function test_releaseByAttestation_revertsOnNonLocked() public {
        vm.startPrank(payer);
        escrow.createPaymentWithPolicy{value: 1 ether}("req-10", payee, 100, 10, POLICY);
        // Payer confirms early via the fallback path
        escrow.confirmPayment("req-10");
        vm.stopPrank();

        agg.setAccept(POLICY, ATTEST, true);

        // Now state is Released; oracle release must revert
        vm.prank(anyone);
        bytes[] memory sigs = new bytes[](0);
        vm.expectRevert(bytes("Payment not in Locked state"));
        escrow.releaseByAttestation("req-10", ATTEST, sigs);
    }

    // ─── Fallback semantics: payer can still confirm with policy set ──────

    function test_payerConfirmStillWorksWhenPolicySet() public {
        vm.startPrank(payer);
        escrow.createPaymentWithPolicy{value: 1 ether}("req-11", payee, 100, 10, POLICY);
        // Payer didn't wait for oracles; just confirms directly.
        escrow.confirmPayment("req-11");
        vm.stopPrank();

        require(payee.balance == 1 ether, "payee received via payer-only path");
    }

    // ─── Refund still works on a policy-bearing payment ───────────────────

    function test_payerRefundStillWorksWhenPolicySet() public {
        vm.startPrank(payer);
        escrow.createPaymentWithPolicy{value: 1 ether}("req-12", payee, 100, 10, POLICY);
        vm.stopPrank();

        // Advance past timeout + challenge
        vm.roll(block.number + 200);

        vm.startPrank(payer);
        escrow.requestRefund("req-12");
        vm.stopPrank();

        require(payer.balance == 10 ether - 0, "payer refunded"); // started with 10, locked 1, refunded 1
    }
}
