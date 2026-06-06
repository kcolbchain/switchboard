// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

/**
 * @title StableSwapRouter
 * @notice Parity swap router for CR8-USD and MUSD.
 */
contract StableSwapRouter is Ownable, ReentrancyGuard {
    using SafeERC20 for IERC20;

    IERC20 public immutable cr8usd;
    IERC20 public immutable musd;
    
    address public treasury;
    uint256 public feeBps; // Default 5 bps
    
    uint256 public constant MAX_FEE_BPS = 1000; // 10% max fee
    
    uint256 public epochDuration = 1 days;
    uint256 public defaultEpochLimit = 100_000 * 10**18; // $100K default limit
    
    // integrator address -> epoch limit (if 0, uses defaultEpochLimit)
    mapping(address => uint256) public customLimits;
    
    // address -> epoch index -> swapped amount
    mapping(address => mapping(uint256 => uint256)) public epochVolume;

    event Swap(address indexed user, address indexed tokenIn, address indexed tokenOut, uint256 amountIn, uint256 amountOut, uint256 fee);
    event TreasuryUpdated(address newTreasury);
    event FeeUpdated(uint256 newFeeBps);
    event CustomLimitUpdated(address indexed integrator, uint256 newLimit);

    constructor(
        address _cr8usd,
        address _musd,
        address _treasury,
        uint256 _feeBps
    ) Ownable(msg.sender) {
        require(_cr8usd != address(0) && _musd != address(0), "Zero address");
        require(_treasury != address(0), "Zero treasury address");
        require(_feeBps <= MAX_FEE_BPS, "Fee too high");
        
        cr8usd = IERC20(_cr8usd);
        musd = IERC20(_musd);
        treasury = _treasury;
        feeBps = _feeBps;
    }

    function _getCurrentEpoch() internal view returns (uint256) {
        return block.timestamp / epochDuration;
    }

    function _checkAndRecordLimit(address user, uint256 amount) internal {
        uint256 epoch = _getCurrentEpoch();
        uint256 limit = customLimits[user] > 0 ? customLimits[user] : defaultEpochLimit;
        
        epochVolume[user][epoch] += amount;
        require(epochVolume[user][epoch] <= limit, "Epoch volume limit exceeded");
    }

    function swapCR8USDtoMUSD(uint256 amount, address to) external nonReentrant {
        require(amount > 0, "Zero amount");
        _checkAndRecordLimit(msg.sender, amount);

        uint256 fee = (amount * feeBps) / 10000;
        uint256 outAmount = amount - fee;

        cr8usd.safeTransferFrom(msg.sender, address(this), amount);
        if (fee > 0) {
            cr8usd.safeTransfer(treasury, fee);
        }
        
        musd.safeTransfer(to, outAmount);

        emit Swap(msg.sender, address(cr8usd), address(musd), amount, outAmount, fee);
    }

    function swapMUSDtoCR8USD(uint256 amount, address to) external nonReentrant {
        require(amount > 0, "Zero amount");
        _checkAndRecordLimit(msg.sender, amount);

        uint256 fee = (amount * feeBps) / 10000;
        uint256 outAmount = amount - fee;

        musd.safeTransferFrom(msg.sender, address(this), amount);
        if (fee > 0) {
            musd.safeTransfer(treasury, fee);
        }
        
        cr8usd.safeTransfer(to, outAmount);

        emit Swap(msg.sender, address(musd), address(cr8usd), amount, outAmount, fee);
    }

    function setTreasury(address _treasury) external onlyOwner {
        require(_treasury != address(0), "Zero address");
        treasury = _treasury;
        emit TreasuryUpdated(_treasury);
    }

    function setFeeBps(uint256 _feeBps) external onlyOwner {
        require(_feeBps <= MAX_FEE_BPS, "Fee too high");
        feeBps = _feeBps;
        emit FeeUpdated(_feeBps);
    }

    function setCustomLimit(address integrator, uint256 limit) external onlyOwner {
        customLimits[integrator] = limit;
        emit CustomLimitUpdated(integrator, limit);
    }
}
