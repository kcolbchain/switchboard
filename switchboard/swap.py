"""Thin client wrapper for StableSwapRouter.

Allows AgentEscrow and other agent infrastructure to call the CR8-USD <-> MUSD
parity swap router inline as part of the settle flow.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict


class StableSwapError(Exception):
    """Base error for StableSwap operations."""


@dataclass
class SwapQuote:
    """A quote for a parity swap."""
    token_in: str
    token_out: str
    amount_in: float
    amount_out: float
    fee: float
    fee_bps: int


class StableSwapRouterClient:
    """Client wrapper for StableSwapRouter contract.
    
    Generates payload data for interacting with the on-chain router.
    """
    
    # Standard function selectors (keccak256 signatures)
    # swapCR8USDtoMUSD(uint256,address) -> 0x82f254e0
    # swapMUSDtoCR8USD(uint256,address) -> 0x5a18a994
    SWAP_CR8USD_TO_MUSD_SELECTOR = "0x82f254e0"
    SWAP_MUSD_TO_CR8USD_SELECTOR = "0x5a18a994"

    def __init__(
        self,
        router_address: str,
        cr8usd_address: str,
        musd_address: str,
        fee_bps: int = 5,
        default_limit: float = 100_000.0
    ):
        self.router_address = router_address
        self.cr8usd_address = cr8usd_address
        self.musd_address = musd_address
        self.fee_bps = fee_bps
        self.default_limit = default_limit

    def quote_swap(self, token_in: str, amount: float) -> SwapQuote:
        """Calculate expected output and fee for a parity swap."""
        if amount <= 0:
            raise StableSwapError("Amount must be positive")
            
        fee = amount * (self.fee_bps / 10000.0)
        out = amount - fee
        
        token_out = self.musd_address if token_in.lower() == self.cr8usd_address.lower() else self.cr8usd_address
        
        return SwapQuote(
            token_in=token_in,
            token_out=token_out,
            amount_in=amount,
            amount_out=out,
            fee=fee,
            fee_bps=self.fee_bps
        )

    def _pad_hex(self, val: str | int, length: int = 64) -> str:
        if isinstance(val, int):
            val = hex(val)
        val = val.replace("0x", "")
        return val.zfill(length)

    def build_swap_cr8usd_to_musd_tx(self, amount_wei: int, recipient: str) -> Dict[str, Any]:
        """Build transaction payload to swap CR8-USD to MUSD."""
        amount_hex = self._pad_hex(amount_wei)
        recipient_hex = self._pad_hex(recipient)
        
        data = f"{self.SWAP_CR8USD_TO_MUSD_SELECTOR}{amount_hex}{recipient_hex}"
        
        return {
            "to": self.router_address,
            "data": data,
            "value": 0
        }

    def build_swap_musd_to_cr8usd_tx(self, amount_wei: int, recipient: str) -> Dict[str, Any]:
        """Build transaction payload to swap MUSD to CR8-USD."""
        amount_hex = self._pad_hex(amount_wei)
        recipient_hex = self._pad_hex(recipient)
        
        data = f"{self.SWAP_MUSD_TO_CR8USD_SELECTOR}{amount_hex}{recipient_hex}"
        
        return {
            "to": self.router_address,
            "data": data,
            "value": 0
        }
