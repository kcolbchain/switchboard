// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Script, console2} from "forge-std/Script.sol";
import {AgentEscrow} from "../contracts/AgentEscrow.sol";
import {IOracleAggregator} from "../contracts/IOracleAggregator.sol";

/// @title Deploy
/// @notice Deploys AgentEscrow with the active chain's ID baked in via constructor.
/// @dev Run: forge script script/Deploy.s.sol --rpc-url <url> --broadcast --verify
contract Deploy is Script {
    function run() external returns (AgentEscrow escrow) {
        uint256 deployerKey = vm.envUint("DEPLOYER_PRIVATE_KEY");
        uint256 chainId = block.chainid;

        vm.startBroadcast(deployerKey);
        escrow = new AgentEscrow(chainId, IOracleAggregator(address(0)));
        vm.stopBroadcast();

        console2.log("AgentEscrow deployed:");
        console2.log("  chainId:", chainId);
        console2.log("  address:", address(escrow));
        console2.log("  owner:  ", escrow.owner());
    }
}
