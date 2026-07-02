// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title MockERC20
 * @notice Minimal, standard ERC-20 for tests. No fees, no rebasing — the
 *         "well-behaved token" baseline for the ERC-20 happy path.
 * @dev Deliberately dependency-free (no OZ import) so the mock stays trivially
 *      auditable and matches exactly the transfer semantics the escrow relies on.
 */
contract MockERC20 {
    string public name;
    string public symbol;
    uint8 public constant decimals = 18;

    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    constructor(string memory _name, string memory _symbol) {
        name = _name;
        symbol = _symbol;
    }

    function mint(address to, uint256 amount) external {
        totalSupply += amount;
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        _transfer(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        require(allowed >= amount, "ERC20: insufficient allowance");
        if (allowed != type(uint256).max) {
            allowance[from][msg.sender] = allowed - amount;
        }
        _transfer(from, to, amount);
        return true;
    }

    function _transfer(address from, address to, uint256 amount) internal virtual {
        require(balanceOf[from] >= amount, "ERC20: insufficient balance");
        require(to != address(0), "ERC20: transfer to zero");
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        emit Transfer(from, to, amount);
    }
}
