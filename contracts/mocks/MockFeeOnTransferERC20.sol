// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {MockERC20} from "./MockERC20.sol";

/**
 * @title MockFeeOnTransferERC20
 * @notice A non-standard ERC-20 that burns a fixed basis-point fee on every
 *         transfer, so the recipient receives *less* than the sent amount.
 *         This is the canonical case that breaks "declared amount == credited
 *         amount" accounting and is why the escrow must credit by measured
 *         balance delta.
 * @dev `feeBps` of the transferred amount is destroyed (removed from supply);
 *      recipient gets `amount - fee`.
 */
contract MockFeeOnTransferERC20 is MockERC20 {
    uint256 public immutable feeBps; // e.g. 100 = 1%

    constructor(string memory _name, string memory _symbol, uint256 _feeBps)
        MockERC20(_name, _symbol)
    {
        require(_feeBps < 10_000, "fee too high");
        feeBps = _feeBps;
    }

    function _transfer(address from, address to, uint256 amount) internal override {
        require(balanceOf[from] >= amount, "ERC20: insufficient balance");
        require(to != address(0), "ERC20: transfer to zero");
        uint256 fee = (amount * feeBps) / 10_000;
        uint256 net = amount - fee;
        balanceOf[from] -= amount;
        balanceOf[to] += net;
        totalSupply -= fee; // burn the fee
        emit Transfer(from, to, net);
        if (fee > 0) {
            emit Transfer(from, address(0), fee);
        }
    }
}
