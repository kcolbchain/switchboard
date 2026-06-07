"""switchboard — programmable payments for AI agents.

Distributed on PyPI as ``switchboard-agent``; the import name is ``switchboard``.
"""
from __future__ import annotations

import json
from importlib import resources
from typing import Any

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "load_registry",
    "GasManager",
    "GasLimits",
    "BudgetStatus",
    "BudgetExhausted",
    "A2AChannel",
    "CreateProtocolSurfaceError",
    "MeterReceipt",
    "a2a_handshake",
    "meter",
    "provision_wallet",
    "recovery_quorum",
    "rotate",
    "sign",
]


from switchboard.gas_manager import BudgetExhausted, GasLimits, GasManager, BudgetStatus
from switchboard.create_protocol import (
    A2AChannel,
    CreateProtocolSurfaceError,
    MeterReceipt,
    a2a_handshake,
    meter,
    provision_wallet,
    recovery_quorum,
    rotate,
    sign,
)


def load_registry() -> dict[str, Any]:
    """Return the bundled chain registry (chainId -> {name, escrow, usdc})."""
    with resources.files(__package__).joinpath("registry.json").open("r") as f:
        return json.load(f)
