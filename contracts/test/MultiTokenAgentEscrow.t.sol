// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {MultiTokenAgentEscrow} from "../MultiTokenAgentEscrow.sol";
import {IAgentEscrow} from "../IAgentEscrow.sol";
import {IOracleAggregator} from "../IOracleAggregator.sol";
import {MockOracleAggregator} from "../mocks/MockOracleAggregator.sol";
import {MockERC20} from "../mocks/MockERC20.sol";
import {MockFeeOnTransferERC20} from "../mocks/MockFeeOnTransferERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

/// @dev Inline Forge cheatcode interface — mirrors AgentEscrowOracle.t.sol so we
///      don't depend on a forge-std submodule being present.
interface Vm {
    function deal(address who, uint256 amount) external;
    function prank(address who) external;
    function startPrank(address who) external;
    function stopPrank() external;
    function expectRevert(bytes calldata revertData) external;
    function expectRevert(bytes4 revertData) external;
    function expectRevert() external;
    function roll(uint256 newBlock) external;
}

contract MultiTokenAgentEscrowTest {
    Vm constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    MultiTokenAgentEscrow internal escrow;
    MockOracleAggregator internal agg;
    MockERC20 internal usdc;
    MockFeeOnTransferERC20 internal feeToken;

    address internal constant NATIVE = address(0);
    address internal payer = address(0xA11CE);
    address internal payee = address(0xB0B);
    address internal anyone = address(0xC0DE);

    bytes32 internal constant POLICY = keccak256("policy: deliver report");
    bytes32 internal constant ATTEST = keccak256("attestation: delivered ok");

    function setUp() public {
        agg = new MockOracleAggregator();
        escrow = new MultiTokenAgentEscrow(31337, IOracleAggregator(address(agg)));

        usdc = new MockERC20("USD Coin", "USDC");
        feeToken = new MockFeeOnTransferERC20("Fee Token", "FEE", 100); // 1% fee

        // Owner (this test contract) allowlists the standard + fee tokens.
        escrow.setTokenAllowed(address(usdc), true);
        escrow.setTokenAllowed(address(feeToken), true);

        vm.deal(payer, 100 ether);
        usdc.mint(payer, 1_000_000e18);
        feeToken.mint(payer, 1_000_000e18);
    }

    // ─── Interface conformance ────────────────────────────────────────────────

    function test_isIAgentEscrow() public view {
        // Must be usable purely through the shared interface.
        IAgentEscrow esc = IAgentEscrow(address(escrow));
        esc.getPayment("nope");
    }

    // ─── ETH profile parity with AgentEscrow ──────────────────────────────────

    function test_eth_createAndConfirm_parity() public {
        vm.startPrank(payer);
        escrow.createPayment{value: 1 ether}("eth-1", payee, NATIVE, 1 ether, 100, 10);
        escrow.confirmPayment("eth-1");
        vm.stopPrank();

        require(payee.balance == 1 ether, "payee received 1 ETH");
        IAgentEscrow.Payment memory p = escrow.getPayment("eth-1");
        require(p.token == NATIVE, "token is native");
        require(uint8(p.state) == uint8(IAgentEscrow.State.Released), "released");
    }

    function test_eth_requiresMsgValueEqualsAmount() public {
        vm.startPrank(payer);
        vm.expectRevert(bytes("ETH: msg.value != amount"));
        escrow.createPayment{value: 0.5 ether}("eth-bad", payee, NATIVE, 1 ether, 100, 10);
        vm.stopPrank();
    }

    function test_eth_rejectsZeroAmount() public {
        vm.startPrank(payer);
        vm.expectRevert(bytes("amount must be > 0"));
        escrow.createPayment{value: 0}("eth-zero", payee, NATIVE, 0, 100, 10);
        vm.stopPrank();
    }

    function test_eth_timeoutRefund() public {
        vm.startPrank(payer);
        escrow.createPayment{value: 2 ether}("eth-refund", payee, NATIVE, 2 ether, 100, 10);
        vm.stopPrank();

        vm.roll(block.number + 111); // timeout(100) + challenge(10) + 1
        vm.prank(payer);
        escrow.requestRefund("eth-refund");

        require(payer.balance == 100 ether, "payer fully refunded");
    }

    function test_eth_cancel() public {
        vm.startPrank(payer);
        escrow.createPayment{value: 3 ether}("eth-cancel", payee, NATIVE, 3 ether, 100, 10);
        escrow.cancelPayment("eth-cancel");
        vm.stopPrank();
        require(payer.balance == 100 ether, "payer got ETH back on cancel");
    }

    function test_eth_releaseByAttestation() public {
        vm.startPrank(payer);
        escrow.createPaymentWithPolicy{value: 1 ether}("eth-att", payee, NATIVE, 1 ether, 100, 10, POLICY);
        vm.stopPrank();

        agg.setAccept(POLICY, ATTEST, true);
        bytes[] memory sigs = new bytes[](1);
        sigs[0] = hex"beef";
        vm.prank(anyone);
        escrow.releaseByAttestation("eth-att", ATTEST, sigs);

        require(payee.balance == 1 ether, "payee received via oracle release");
    }

    // ─── ERC-20 happy path ─────────────────────────────────────────────────────

    function test_erc20_createAndConfirm() public {
        vm.startPrank(payer);
        usdc.approve(address(escrow), 500e18);
        escrow.createPayment("usdc-1", payee, address(usdc), 500e18, 100, 10);

        // Credited amount recorded at creation, before release zeroes it out.
        IAgentEscrow.Payment memory created = escrow.getPayment("usdc-1");
        require(created.token == address(usdc), "token recorded");
        require(created.amount == 500e18, "credited amount for standard token == declared");

        escrow.confirmPayment("usdc-1");
        vm.stopPrank();

        require(usdc.balanceOf(payee) == 500e18, "payee received 500 USDC");
        require(usdc.balanceOf(address(escrow)) == 0, "escrow drained");
        IAgentEscrow.Payment memory released = escrow.getPayment("usdc-1");
        require(uint8(released.state) == uint8(IAgentEscrow.State.Released), "released");
        require(released.amount == 0, "amount zeroed on release");
    }

    function test_erc20_pullsExactlyDeclaredForStandardToken() public {
        uint256 before = usdc.balanceOf(payer);
        vm.startPrank(payer);
        usdc.approve(address(escrow), 500e18);
        escrow.createPayment("usdc-pull", payee, address(usdc), 500e18, 100, 10);
        vm.stopPrank();
        require(usdc.balanceOf(payer) == before - 500e18, "exactly 500 pulled");
        require(usdc.balanceOf(address(escrow)) == 500e18, "escrow holds 500");
    }

    function test_erc20_mustNotSendETH() public {
        vm.startPrank(payer);
        usdc.approve(address(escrow), 500e18);
        vm.expectRevert(bytes("ERC20: no ETH"));
        escrow.createPayment{value: 1 wei}("usdc-eth", payee, address(usdc), 500e18, 100, 10);
        vm.stopPrank();
    }

    function test_erc20_timeoutRefund() public {
        vm.startPrank(payer);
        usdc.approve(address(escrow), 200e18);
        escrow.createPayment("usdc-refund", payee, address(usdc), 200e18, 100, 10);
        vm.stopPrank();

        uint256 balBefore = usdc.balanceOf(payer);
        vm.roll(block.number + 111);
        vm.prank(payer);
        escrow.requestRefund("usdc-refund");
        require(usdc.balanceOf(payer) == balBefore + 200e18, "USDC refunded");
    }

    function test_erc20_cancel() public {
        vm.startPrank(payer);
        usdc.approve(address(escrow), 200e18);
        escrow.createPayment("usdc-cancel", payee, address(usdc), 200e18, 100, 10);
        uint256 balBefore = usdc.balanceOf(payer);
        escrow.cancelPayment("usdc-cancel");
        vm.stopPrank();
        require(usdc.balanceOf(payer) == balBefore + 200e18, "USDC returned on cancel");
    }

    function test_erc20_releaseByAttestation() public {
        vm.startPrank(payer);
        usdc.approve(address(escrow), 300e18);
        escrow.createPaymentWithPolicy("usdc-att", payee, address(usdc), 300e18, 100, 10, POLICY);
        vm.stopPrank();

        agg.setAccept(POLICY, ATTEST, true);
        bytes[] memory sigs = new bytes[](1);
        sigs[0] = hex"beef";
        vm.prank(anyone);
        escrow.releaseByAttestation("usdc-att", ATTEST, sigs);

        require(usdc.balanceOf(payee) == 300e18, "payee received USDC via oracle");
    }

    // ─── Fee-on-transfer via balance-delta accounting ──────────────────────────

    function test_feeOnTransfer_creditsMeasuredDelta_notDeclared() public {
        // Declared 1000; 1% fee => escrow actually receives 990. The credited
        // amount MUST be the measured 990, not the declared 1000.
        vm.startPrank(payer);
        feeToken.approve(address(escrow), 1000e18);
        escrow.createPayment("fee-1", payee, address(feeToken), 1000e18, 100, 10);
        vm.stopPrank();

        IAgentEscrow.Payment memory p = escrow.getPayment("fee-1");
        require(p.amount == 990e18, "credited = measured balance delta (990), not declared (1000)");
        require(feeToken.balanceOf(address(escrow)) == 990e18, "escrow holds exactly what arrived");
    }

    function test_feeOnTransfer_releaseTransfersHeldAmount_noUnderflow() public {
        vm.startPrank(payer);
        feeToken.approve(address(escrow), 1000e18);
        escrow.createPayment("fee-2", payee, address(feeToken), 1000e18, 100, 10);
        escrow.confirmPayment("fee-2");
        vm.stopPrank();

        // Escrow held 990; transfer out applies another 1% fee => payee gets 980.1.
        // Critically: escrow must be fully drained and must not revert on underflow.
        require(feeToken.balanceOf(address(escrow)) == 0, "escrow fully drained on release");
        // 990 - 1% = 980.1
        require(feeToken.balanceOf(payee) == 9801e17, "payee received net-of-second-fee");
    }

    function test_feeOnTransfer_refundReturnsHeldAmount() public {
        vm.startPrank(payer);
        feeToken.approve(address(escrow), 1000e18);
        escrow.createPayment("fee-3", payee, address(feeToken), 1000e18, 100, 10);
        vm.stopPrank();

        uint256 balBefore = feeToken.balanceOf(payer);
        vm.roll(block.number + 111);
        vm.prank(payer);
        escrow.requestRefund("fee-3");
        // Escrow held 990, refund transfer applies 1% => payer gets 980.1 back.
        require(feeToken.balanceOf(payer) == balBefore + 9801e17, "payer refunded held-net");
        require(feeToken.balanceOf(address(escrow)) == 0, "escrow drained on refund");
    }

    // ─── Allowlist gating ──────────────────────────────────────────────────────

    function test_nonAllowlistedToken_rejected() public {
        MockERC20 rando = new MockERC20("Random", "RND");
        rando.mint(payer, 1000e18);
        vm.startPrank(payer);
        rando.approve(address(escrow), 100e18);
        vm.expectRevert(bytes("token not allowlisted"));
        escrow.createPayment("rnd-1", payee, address(rando), 100e18, 100, 10);
        vm.stopPrank();
    }

    function test_nativeEth_alwaysAllowed_noAllowlistNeeded() public {
        // Deploy a fresh escrow with NO allowlist entries; ETH must still work.
        MultiTokenAgentEscrow fresh = new MultiTokenAgentEscrow(31337, IOracleAggregator(address(agg)));
        vm.deal(payer, 5 ether);
        vm.startPrank(payer);
        fresh.createPayment{value: 1 ether}("eth-fresh", payee, NATIVE, 1 ether, 100, 10);
        fresh.confirmPayment("eth-fresh");
        vm.stopPrank();
        require(payee.balance == 1 ether, "ETH works with empty allowlist");
    }

    function test_setTokenAllowed_onlyOwner() public {
        vm.prank(anyone);
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, anyone));
        escrow.setTokenAllowed(address(usdc), false);
    }

    function test_deallowlist_blocksNewPayments() public {
        escrow.setTokenAllowed(address(usdc), false);
        vm.startPrank(payer);
        usdc.approve(address(escrow), 100e18);
        vm.expectRevert(bytes("token not allowlisted"));
        escrow.createPayment("usdc-off", payee, address(usdc), 100e18, 100, 10);
        vm.stopPrank();
    }

    // ─── Lifecycle guards unchanged ────────────────────────────────────────────

    function test_duplicateRequestIdRejected() public {
        vm.startPrank(payer);
        escrow.createPayment{value: 1 ether}("dup", payee, NATIVE, 1 ether, 100, 10);
        vm.expectRevert(bytes("requestId already exists"));
        escrow.createPayment{value: 1 ether}("dup", payee, NATIVE, 1 ether, 100, 10);
        vm.stopPrank();
    }

    function test_onlyPayerCanConfirm() public {
        vm.prank(payer);
        escrow.createPayment{value: 1 ether}("c1", payee, NATIVE, 1 ether, 100, 10);
        vm.prank(anyone);
        vm.expectRevert(bytes("Only payer can confirm"));
        escrow.confirmPayment("c1");
    }

    function test_confirmAfterTimeoutReverts() public {
        vm.prank(payer);
        escrow.createPayment{value: 1 ether}("c2", payee, NATIVE, 1 ether, 100, 10);
        vm.roll(block.number + 101);
        vm.prank(payer);
        vm.expectRevert(bytes("Payment has expired"));
        escrow.confirmPayment("c2");
    }

    function test_refundBeforeChallengeEndsReverts() public {
        vm.prank(payer);
        escrow.createPayment{value: 1 ether}("c3", payee, NATIVE, 1 ether, 100, 10);
        vm.roll(block.number + 100); // timeout hit but challenge not over
        vm.prank(payer);
        vm.expectRevert(bytes("Challenge period not over"));
        escrow.requestRefund("c3");
    }

    function test_releaseByAttestation_revertsWhenAggregatorRejects() public {
        vm.startPrank(payer);
        escrow.createPaymentWithPolicy{value: 1 ether}("c4", payee, NATIVE, 1 ether, 100, 10, POLICY);
        vm.stopPrank();
        bytes[] memory sigs = new bytes[](0);
        vm.prank(anyone);
        vm.expectRevert(bytes("Oracle attestation rejected"));
        escrow.releaseByAttestation("c4", ATTEST, sigs);
    }

    function test_releaseByAttestation_revertsOnNoPolicy() public {
        vm.prank(payer);
        escrow.createPayment{value: 1 ether}("c5", payee, NATIVE, 1 ether, 100, 10);
        agg.setAccept(POLICY, ATTEST, true);
        bytes[] memory sigs = new bytes[](0);
        vm.prank(anyone);
        vm.expectRevert(bytes("No oracle policy on this payment"));
        escrow.releaseByAttestation("c5", ATTEST, sigs);
    }
}
