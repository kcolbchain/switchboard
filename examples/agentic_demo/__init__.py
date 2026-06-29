"""Agentic payments demo: Agent A pays Agent B via x402 + escrow, then B routes
the received token through SafeSwap.

Run with::

    PYTHONPATH=. python examples/agentic_demo/run.py
"""

from .safeswap import (
    MockSafeSwapOrchestrator,
    SafeSwapClient,
    SafeSwapError,
    SwapQuote,
    SwapReceipt,
    SwapRequest,
)
from .onchain import EscrowState, MockChain, MockPaymentClient
from .scenario import AgentBEndpoint, ScenarioResult, StepLog, run_scenario

__all__ = [
    "run_scenario",
    "ScenarioResult",
    "StepLog",
    "AgentBEndpoint",
    "MockChain",
    "MockPaymentClient",
    "EscrowState",
    "SafeSwapClient",
    "MockSafeSwapOrchestrator",
    "SafeSwapError",
    "SwapRequest",
    "SwapQuote",
    "SwapReceipt",
]
