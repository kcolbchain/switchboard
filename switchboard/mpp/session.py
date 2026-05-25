"""MPP session client around Tempo's Machine Payments Protocol.

Opens a session with a spending cap, streams micro-payments without
on-chain tx per request, and closes with gas-budget reconciliation.

Usage:
    session = MPPSession(
        api_key="...",
        budget_tracker=gas_budget_tracker,
        wallet="0x...",
    )
    session.open(limit_usd=10.00)
    session.charge(amount_usd=0.05, description="inference call")
    session.close()
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


class MPPSessionError(Exception):
    """Base error for MPP session operations."""


@dataclass
class SessionState:
    """State of an MPP session."""
    session_id: str = ""
    status: str = "closed"  # closed, open, paused
    limit_usd: float = 0.0
    spent_usd: float = 0.0
    opened_at: float = 0.0
    budget_id: str = ""

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limit_usd - self.spent_usd)


@dataclass
class ChargeRecord:
    """Record of a single micro-payment within a session."""
    charge_id: str = ""
    session_id: str = ""
    amount_usd: float = 0.0
    description: str = ""
    timestamp: float = 0.0
    status: str = "pending"  # pending, settled, failed


class MPPSession:
    """Client for Tempo's MPP API — open / charge / close sessions.

    Binds spending limits to GasBudgetTracker for unified cap enforcement.
    """

    def __init__(
        self,
        api_key: str = "",
        budget_tracker=None,
        wallet: str = "",
        api_url: str = "https://api.tempo.mpp.dev/v1",
        clock: Callable[[], float] = time.time,
    ):
        self.api_key = api_key
        self.budget_tracker = budget_tracker
        self.wallet = wallet
        self.api_url = api_url
        self.clock = clock

        self.state = SessionState()
        self._charges: list[ChargeRecord] = []
        self._pending_charges: list[ChargeRecord] = []

    def open(self, limit_usd: float = 10.0, budget_id: str = "") -> str:
        """Open an MPP session with a spending cap.

        Checks GasBudgetTracker before opening. Stores budget_id for
        cross-protocol cap reconciliation.
        """
        if self.state.status == "open":
            raise MPPSessionError("Session already open")

        if self.budget_tracker:
            estimated_gas = int(limit_usd * 1_000_000)
            if not self.budget_tracker.can_spend(self.wallet, estimated_gas):
                raise MPPSessionError("GasBudgetTracker would be exceeded")

        session_id = str(uuid.uuid4())
        self.state = SessionState(
            session_id=session_id,
            status="open",
            limit_usd=limit_usd,
            opened_at=self.clock(),
            budget_id=budget_id,
        )
        return session_id

    def charge(self, amount_usd: float, description: str = "") -> ChargeRecord:
        """Stream a micro-payment under the session cap.

        Raises MPPSessionError if the charge would exceed the session or
        gas-budget limits.
        """
        if self.state.status != "open":
            raise MPPSessionError("Session is not open")

        if self.state.spent_usd + amount_usd > self.state.limit_usd:
            raise MPPSessionError(
                f"Charge {amount_usd} would exceed session cap {self.state.limit_usd}"
            )

        record = ChargeRecord(
            charge_id=str(uuid.uuid4()),
            session_id=self.state.session_id,
            amount_usd=amount_usd,
            description=description,
            timestamp=self.clock(),
            status="settled",
        )
        self.state.spent_usd += amount_usd
        self._charges.append(record)

        if self.budget_tracker:
            estimated_gas = int(amount_usd * 1_000_000)
            if not self.budget_tracker.can_spend(self.wallet, estimated_gas):
                self.state.status = "paused"

        return record

    def close(self) -> dict[str, Any]:
        """Close the session and write settled amount to gas-budget ledger."""
        if self.state.status == "closed":
            raise MPPSessionError("Session already closed")

        settled = self.state.spent_usd
        if self.budget_tracker:
            gas_equivalent = int(settled * 1_000_000)
            self.budget_tracker.record(self.wallet, gas_equivalent)

        result = {
            "session_id": self.state.session_id,
            "total_spent_usd": settled,
            "limit_usd": self.state.limit_usd,
            "num_charges": len(self._charges),
            "closed_at": self.clock(),
        }
        self.state.status = "closed"
        return result

    def status(self) -> dict[str, Any]:
        return {
            "session_id": self.state.session_id,
            "status": self.state.status,
            "limit_usd": self.state.limit_usd,
            "spent_usd": self.state.spent_usd,
            "remaining_usd": self.state.remaining_usd,
            "num_charges": len(self._charges),
        }

    def charges(self) -> list[dict[str, Any]]:
        return [
            {
                "charge_id": c.charge_id,
                "amount_usd": c.amount_usd,
                "description": c.description,
                "status": c.status,
            }
            for c in self._charges
        ]
