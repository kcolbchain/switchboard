"""switchboard.thinking_chain — AI agent financial reasoning chain primitive.

Models an AI agent's multi-step financial decision-making as an observable,
inspectable chain of typed steps.  Each step records its reasoning, outcome,
arbitrary data payload, and any ``metrics.WalletOpEvent``s it emitted.

Canonical step sequence (``HanzoEscrowThinkingChain``)
------------------------------------------------------
1. ASSESS_TASK          — inspect the task, decide a payment is needed.
2. NEGOTIATE_TOKEN      — call ``negotiate_settlement_token`` to pick the
                          best mutually-acceptable token; halt if none.
3. POLICY_CHECK         — call ``access_policy.check``; halt if denied.
4. CREATE_ESCROW        — call ``AgentWallet.pay`` (drives EscrowClient);
                          records escrow_id in step data.
5. VERIFY_WORK          — simulate work-verification (always PASS in demo).
6. RELEASE_OR_REFUND    — call escrow_client.release_payment or
                          refund_payment; records action in step data.

``ThinkingChain`` API
---------------------
``chain = HanzoEscrowThinkingChain(...)``
``records = chain.run()``           # list[StepRecord]; raises ChainHaltedError on HALT
``chain.records``                   # same list, inspectable after run()
``chain.records[i].step_type``      # StepType enum member
``chain.records[i].reasoning``      # str narrative
``chain.records[i].outcome``        # StepOutcome.PASS | .FAIL | .HALT
``chain.records[i].data``           # dict with step-specific payload
``chain.records[i].events``         # list[WalletOpEvent] emitted by this step
"""
from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional

from src.payment_protocol import SettlementToken, negotiate_settlement_token
from switchboard.agent_wallet import AgentWallet, EscrowClient
from switchboard.access_policy import AccessPolicy
from switchboard.escrow_adapters import InMemoryEscrowClient
from switchboard.metrics import WalletOpEvent


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class StepType(Enum):
    ASSESS_TASK       = auto()
    NEGOTIATE_TOKEN   = auto()
    POLICY_CHECK      = auto()
    CREATE_ESCROW     = auto()
    VERIFY_WORK       = auto()
    RELEASE_OR_REFUND = auto()


class StepOutcome(Enum):
    PASS = "pass"
    FAIL = "fail"
    HALT = "halt"


# ---------------------------------------------------------------------------
# StepRecord — immutable, inspectable
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepRecord:
    """One completed step in a ThinkingChain.

    Parameters
    ----------
    step_type:
        The canonical step this record belongs to.
    reasoning:
        Human-readable narrative of why this step was taken.
    outcome:
        PASS / FAIL / HALT.
    data:
        Step-specific payload dict (e.g. ``{"escrow_id": "escrow-abc123"}``).
    events:
        Zero or more ``WalletOpEvent``s emitted during this step.
    """
    step_type: StepType
    reasoning: str
    outcome: StepOutcome
    data: Dict[str, Any]
    events: List[WalletOpEvent]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ChainHaltedError(Exception):
    """Raised when a step returns ``StepOutcome.HALT``.

    Parameters
    ----------
    reason:
        Human-readable explanation of why the chain halted.
    step_record:
        The ``StepRecord`` that caused the halt (outcome == HALT).
    """
    def __init__(self, reason: str, step_record: StepRecord) -> None:
        super().__init__(reason)
        self.reason = reason
        self.step_record = step_record


# ---------------------------------------------------------------------------
# ThinkingChain runner
# ---------------------------------------------------------------------------


class ThinkingChain:
    """Base runner: execute a sequence of steps and collect StepRecords.

    Subclasses implement ``_build_steps() -> list[Callable[[], StepRecord]]``
    returning the ordered list of step callables.  ``run()`` executes them in
    order, appends each ``StepRecord`` to ``self.records``, and raises
    ``ChainHaltedError`` if any step yields ``StepOutcome.HALT``.

    Parameters
    ----------
    name:
        Human-readable chain name (e.g. ``"HanzoEscrow"``).
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._records: List[StepRecord] = []

    @property
    def records(self) -> List[StepRecord]:
        """Inspectable list of completed StepRecords (read-only view)."""
        return list(self._records)

    def _build_steps(self) -> List[Callable[[], StepRecord]]:  # pragma: no cover
        """Return ordered list of zero-argument callables, each -> StepRecord."""
        raise NotImplementedError

    def run(self) -> List[StepRecord]:
        """Execute all steps; raise ChainHaltedError if any step halts.

        Returns the list of StepRecords on full success.
        """
        self._records.clear()
        for step_fn in self._build_steps():
            record = step_fn()
            self._records.append(record)
            if record.outcome == StepOutcome.HALT:
                raise ChainHaltedError(
                    reason=record.reasoning,
                    step_record=record,
                )
        return list(self._records)


# ---------------------------------------------------------------------------
# HanzoEscrowThinkingChain — canonical 6-step implementation
# ---------------------------------------------------------------------------


class HanzoEscrowThinkingChain(ThinkingChain):
    """A Hanzo-AI agent reasoning through an escrowed multi-token payment.

    Implements the six canonical steps using real Switchboard modules:
    negotiate_settlement_token, access_policy.check, AgentWallet.pay,
    and EscrowClient release/refund.

    Parameters
    ----------
    payer_wallet:
        ``AgentWallet`` with a funded Treasury.  Used in CREATE_ESCROW.
    payee_address:
        EVM address of the payee.
    payer_offers:
        Tokens the payer will accept as settlement (SettlementToken list).
    payee_accepts:
        Tokens the payee will accept (SettlementToken list).
    amount:
        Payment amount in the negotiated token's smallest unit.
    access_policy:
        ``AccessPolicy`` instance.  Consulted in POLICY_CHECK.
    agent_id:
        Logical agent identity forwarded to the access policy.
    escrow_client:
        ``InMemoryEscrowClient`` (or any EscrowClient) for CREATE_ESCROW and
        RELEASE_OR_REFUND.  Defaults to a fresh ``InMemoryEscrowClient``.
    chain_id:
        EVM chain ID.  Defaults to 1.
    """

    def __init__(
        self,
        payer_wallet: AgentWallet,
        payee_address: str,
        payer_offers: List[SettlementToken],
        payee_accepts: List[SettlementToken],
        amount: int,
        access_policy: AccessPolicy,
        agent_id: str = "hanzo-agent",
        escrow_client: Optional[InMemoryEscrowClient] = None,
        chain_id: int = 1,
    ) -> None:
        super().__init__(name="HanzoEscrow")
        self._wallet = payer_wallet
        self._payee = payee_address
        self._payer_offers = payer_offers
        self._payee_accepts = payee_accepts
        self._amount = amount
        self._policy = access_policy
        self._agent_id = agent_id
        self._escrow = escrow_client if escrow_client is not None else InMemoryEscrowClient()
        self._chain_id = chain_id

        # mutable state shared across step closures
        self._negotiated_token: Optional[SettlementToken] = None
        self._escrow_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Step builders
    # ------------------------------------------------------------------

    def _build_steps(self) -> List[Callable[[], StepRecord]]:
        return [
            self._step_assess_task,
            self._step_negotiate_token,
            self._step_policy_check,
            self._step_create_escrow,
            self._step_verify_work,
            self._step_release_or_refund,
        ]

    def _step_assess_task(self) -> StepRecord:
        reasoning = (
            f"Hanzo agent assessed task: need to pay {self._payee!r} "
            f"amount={self._amount} on chain_id={self._chain_id}. "
            f"Payer offers {len(self._payer_offers)} token(s); "
            f"payee accepts {len(self._payee_accepts)} token(s). "
            "Proceeding to token negotiation."
        )
        return StepRecord(
            step_type=StepType.ASSESS_TASK,
            reasoning=reasoning,
            outcome=StepOutcome.PASS,
            data={
                "payee": self._payee,
                "amount": self._amount,
                "chain_id": self._chain_id,
                "payer_token_count": len(self._payer_offers),
                "payee_token_count": len(self._payee_accepts),
            },
            events=[],
        )

    def _step_negotiate_token(self) -> StepRecord:
        token = negotiate_settlement_token(self._payer_offers, self._payee_accepts)
        if token is None:
            return StepRecord(
                step_type=StepType.NEGOTIATE_TOKEN,
                reasoning=(
                    "negotiate_settlement_token() returned None — "
                    "no mutually-acceptable token found. Halting chain."
                ),
                outcome=StepOutcome.HALT,
                data={
                    "payer_offers": [t.token for t in self._payer_offers],
                    "payee_accepts": [t.token for t in self._payee_accepts],
                },
                events=[],
            )
        self._negotiated_token = token
        return StepRecord(
            step_type=StepType.NEGOTIATE_TOKEN,
            reasoning=(
                f"Negotiated settlement token: {token.token!r} "
                f"(combined_rank={token.rank}) via negotiate_settlement_token()."
            ),
            outcome=StepOutcome.PASS,
            data={"token": token.token, "rank": token.rank, "chain_id": token.chain_id},
            events=[],
        )

    def _step_policy_check(self) -> StepRecord:
        collected_events: List[WalletOpEvent] = []
        token = self._negotiated_token.token if self._negotiated_token else ""

        # Thread-safety note: the listener swap below is safe only for
        # single-threaded use (demo/test scope).  Concurrent chains that share
        # one AccessPolicy instance would race on _event_listener — the last
        # writer wins and events can be mis-attributed.  The production fix is a
        # scoped context-manager on AccessPolicy that carries its own listener
        # slot rather than mutating the shared one.  Do NOT add locking here;
        # add the context-manager seam on AccessPolicy instead.
        original_listener = self._policy._event_listener
        def _capture(ev: WalletOpEvent) -> None:
            collected_events.append(ev)
            if original_listener:
                original_listener(ev)
        self._policy._event_listener = _capture

        try:
            decision = self._policy.check(
                self._agent_id,
                {
                    "type": "pay",
                    "amount": self._amount,
                    "token": token,
                    "payee": self._payee,
                },
            )
        finally:
            self._policy._event_listener = original_listener

        if decision.denied:
            return StepRecord(
                step_type=StepType.POLICY_CHECK,
                reasoning=(
                    f"access_policy.check() denied: reason={decision.reason!r}. "
                    "Halting chain — payment blocked by policy."
                ),
                outcome=StepOutcome.HALT,
                data={"reason": decision.reason, "agent_id": self._agent_id},
                events=collected_events,
            )
        return StepRecord(
            step_type=StepType.POLICY_CHECK,
            reasoning=(
                f"access_policy.check() allowed payment of {self._amount} "
                f"token={token!r} for agent {self._agent_id!r}."
            ),
            outcome=StepOutcome.PASS,
            data={"allowed": True, "agent_id": self._agent_id},
            events=collected_events,
        )

    def _step_create_escrow(self) -> StepRecord:
        token = self._negotiated_token.token if self._negotiated_token else ""

        # Financial gate: check spendable balance before touching anything.
        # This keeps the wallet's treasury honest — if funds are insufficient
        # we halt rather than creating an escrow that can't be backed.
        spendable = self._wallet.spendable(self._chain_id, token)
        if spendable < self._amount:
            return StepRecord(
                step_type=StepType.CREATE_ESCROW,
                reasoning=(
                    f"Insufficient spendable balance for {token!r} on chain "
                    f"{self._chain_id}: have {spendable}, need {self._amount}. "
                    "Halting chain."
                ),
                outcome=StepOutcome.HALT,
                data={
                    "token": token,
                    "amount": self._amount,
                    "spendable": spendable,
                },
                events=[],
            )

        # Debit the treasury first so the wallet is no longer hollow.
        # The escrow represents the obligation; the treasury debit is the
        # payer-side accounting entry.  Release/refund in RELEASE_OR_REFUND
        # settles the payee side separately (keeping create and release as
        # distinct observable steps in the chain).
        self._wallet.treasury.debit(self._chain_id, token, self._amount)

        eid = self._escrow.create_payment(
            chain_id=self._chain_id,
            token=token,
            amount=self._amount,
            payee=self._payee,
        )
        self._escrow_id = eid

        balance_after = self._wallet.spendable(self._chain_id, token)

        # Emit a WalletOpEvent for the escrow creation
        ev = WalletOpEvent(
            op_type="create_escrow",
            token=token,
            rail="escrow",
            amount=float(self._amount),
            agent_id=self._agent_id,
            wallet_id=self._wallet.address(),
            denied=False,
            denial_reason=None,
            timestamp=time.time(),
        )
        return StepRecord(
            step_type=StepType.CREATE_ESCROW,
            reasoning=(
                f"Debited treasury {self._amount} {token!r}; "
                f"created escrow {eid!r} for payee {self._payee!r} "
                f"via InMemoryEscrowClient. Balance after: {balance_after}."
            ),
            outcome=StepOutcome.PASS,
            data={
                "escrow_id": eid,
                "token": token,
                "amount": self._amount,
                "balance_after": balance_after,
            },
            events=[ev],
        )

    def _step_verify_work(self) -> StepRecord:
        # In the demo/test harness, work is always accepted.
        # Production subclasses override this to call a verifier.
        return StepRecord(
            step_type=StepType.VERIFY_WORK,
            reasoning=(
                "Hanzo agent verified task completion: payee delivered the "
                "requested output (simulated verification — always accepted in demo). "
                "Proceeding to release escrow."
            ),
            outcome=StepOutcome.PASS,
            data={"verified": True},
            events=[],
        )

    def _step_release_or_refund(self) -> StepRecord:
        token = self._negotiated_token.token if self._negotiated_token else ""
        eid = self._escrow_id or ""
        # Since verify_work passed, we release.
        ok = self._escrow.release_payment(eid)
        ev = WalletOpEvent(
            op_type="release_escrow",
            token=token,
            rail="escrow",
            amount=float(self._amount),
            agent_id=self._agent_id,
            wallet_id=self._wallet.address(),
            denied=False,
            denial_reason=None,
            timestamp=time.time(),
        )
        return StepRecord(
            step_type=StepType.RELEASE_OR_REFUND,
            reasoning=(
                f"Released escrow {eid!r} to payee {self._payee!r}. "
                f"release_payment() returned {ok}. Settlement complete."
            ),
            outcome=StepOutcome.PASS,
            data={"action": "release", "escrow_id": eid, "success": ok},
            events=[ev],
        )
