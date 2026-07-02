// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IAgentEscrow} from "../IAgentEscrow.sol";

/// @dev Unit ⑤ — interface conformance.
///      A minimal stub `is IAgentEscrow` must compile, proving the interface
///      is implementable. The `Payment` struct must carry a `token` field, and
///      the interface must declare the full lifecycle
///      (createPayment / confirmPayment / releaseByAttestation / requestRefund
///       / cancelPayment / getPayment).
contract IAgentEscrowStub is IAgentEscrow {
    IAgentEscrow.Payment internal _p;

    function createPayment(
        string calldata,
        address, // payee
        address, // token
        uint256, // amount
        uint256, // timeoutBlocks
        uint256 // challengePeriod
    ) external payable override returns (bool) {
        return true;
    }

    function confirmPayment(string calldata) external override returns (bool) {
        return true;
    }

    function releaseByAttestation(
        string calldata,
        bytes32,
        bytes[] calldata
    ) external override returns (bool) {
        return true;
    }

    function requestRefund(string calldata) external override returns (bool) {
        return true;
    }

    function cancelPayment(string calldata) external override returns (bool) {
        return true;
    }

    function getPayment(string calldata) external view override returns (IAgentEscrow.Payment memory) {
        return _p;
    }
}

contract IAgentEscrowTest {
    IAgentEscrowStub internal stub;

    function setUp() public {
        stub = new IAgentEscrowStub();
    }

    /// The `Payment` struct must expose a `token` field (this is the whole point
    /// of the multi-token generalization). Compiling the read proves it exists.
    function test_paymentStructHasTokenField() public view {
        IAgentEscrow.Payment memory p = stub.getPayment("req-x");
        // reference `.token` so the compiler enforces the field's existence
        require(p.token == address(0), "default token is zero (native ETH profile)");
    }

    /// A concrete implementation can be handled purely through the interface
    /// type — proving the ABI is complete for the Python/off-chain client.
    function test_reachableThroughInterfaceType() public {
        IAgentEscrow esc = IAgentEscrow(address(stub));
        require(esc.confirmPayment("req-x"), "confirm via interface");
        require(esc.requestRefund("req-x"), "refund via interface");
        require(esc.cancelPayment("req-x"), "cancel via interface");
        bytes[] memory sigs = new bytes[](0);
        require(esc.releaseByAttestation("req-x", bytes32(0), sigs), "release via interface");
    }
}
