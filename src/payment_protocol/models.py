from dataclasses import dataclass
from typing import Optional
from hexbytes import HexBytes

@dataclass
class PaymentRequest:
    """
    Represents an off-chain payment request from one agent to another.
    This structure defines the parameters for an on-chain escrow.
    """
    sender_address: str         # The blockchain address of the sending agent.
    receiver_address: str       # The blockchain address of the receiving agent.
    amount: int                 # The amount of tokens/native currency.
    token_address: str          # The ERC-20 token contract address, or '0x0...' for native currency.
    deadline: int               # Unix timestamp by which the service should be completed, or refund is possible.
    reference_id: Optional[str] = None # An optional identifier for linking to off-chain service/job details.
    escrow_id: Optional[HexBytes] = None # The unique on-chain escrow ID, typically derived from other parameters.

    def to_dict(self) -> dict:
        """Converts the PaymentRequest to a dictionary for serialization (e.g., signing)."""
        return {
            "sender_address": self.sender_address,
            "receiver_address": self.receiver_address,
            "amount": self.amount,
            "token_address": self.token_address,
            "deadline": self.deadline,
            "reference_id": self.reference_id,
            "escrow_id": self.escrow_id.hex() if self.escrow_id else None,
        }

