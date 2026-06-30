"""Observable timeline recorder for the agentic-escrow demo.

================================================================================
SIMULATED / MOCK CHAIN — NOT A LIVE NETWORK.
No real ETH, no real RPC, no network calls, no funds. This module *wraps* the
existing ``examples.agentic_demo.scenario`` orchestration (which drives the real
``switchboard.x402_middleware.X402Middleware`` + ``switchboard.gas_tracker``
against an in-memory ``MockChain`` implementing the AgentEscrow surface) and
records an ordered, render-ready **timeline** of what happened, one frame per
step. Synthetic agents/keys only. Nothing here is a live or production deploy.

Demo by Pattermesh (Patty / P. Sundaram) on top of kcolbchain/switchboard — the
collective's agentic-payments rail (Abhishek Krishna / @abhicris leads). The
escrow / x402 / SafeSwap logic shown is switchboard's; this layer only makes it
*watchable*.
================================================================================

Design contract: ``DEMO.md`` (the Architect's spec). This file implements §2
(the ``TimelineEvent`` / ``DemoRun`` model), §3 (determinism), and the §5
contract surface (``run_observable`` / ``StepCursor``).

It does **not** edit ``scenario.py`` / ``onchain.py`` / ``safeswap.py``. Instead
it:

* re-drives the *identical* call sequence ``scenario.run_scenario`` performs (the
  same real-library methods, in the same order, producing the same canonical
  ``step`` ids — see DEMO.md §1), but against its **own** :class:`MockChain` so
  it can snapshot on-chain state (escrow, balances, block height) *after* every
  step, and
* runs under a **logical clock + seeded RNG** (DEMO.md §3) so two runs with the
  same params + seed are byte-identical. Determinism is injected by temporarily
  swapping ``time.time`` / ``uuid.uuid4`` on the shared ``time`` / ``uuid``
  modules for the duration of the run, then restoring them — the wrapped modules
  are never modified on disk and never see a patched clock outside this call.
"""

from __future__ import annotations

import time as _time
import uuid as _uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

from switchboard.x402_middleware import (
    PaymentProof,
    PaymentRecord,
    X402Middleware,
)

from .onchain import EscrowState, MockChain, MockPaymentClient
from .safeswap import (
    MockSafeSwapOrchestrator,
    SafeSwapClient,
    SwapRequest,
)
from .scenario import USDC, AgentBEndpoint, BudgetGuard


# ── default synthetic agents (mirror scenario.run_scenario's defaults) ─────────

AGENT_A_ADDR = "0xA0A0a0a0a0A0a0A0a0A0a0a0a0A0a0a0A0A0A0a0"
AGENT_B_ADDR = "0xB0b0B0b0b0b0b0B0b0b0b0b0B0b0b0B0b0B0B0b0"

# token display metadata: decimals + the on-chain ledger unit
_TOKEN_DECIMALS = {"USDC": 6, "ETH": 18, "LUX": 18}

# logical-clock epoch (a fixed, recognizable instant) + per-event tick (seconds).
# Far enough below any offer/quote expiry window (300s / 60s) that nothing the
# real library mints ever reads as expired during a recorded run.
_CLOCK_EPOCH = 1_700_000_000
_CLOCK_TICK = 1


# ── deterministic clock / id injection ────────────────────────────────────────


class _LogicalClock:
    """A monotonic, fixed-step stand-in for ``time.time`` (DEMO.md §3).

    Starts at ``_CLOCK_EPOCH`` and advances ``_CLOCK_TICK`` seconds each time it
    is read, so every ``time.time()`` the wrapped library calls is reproducible.
    """

    def __init__(self, epoch: int = _CLOCK_EPOCH, tick: int = _CLOCK_TICK):
        self._t = float(epoch)
        self._tick = tick

    def __call__(self) -> float:
        now = self._t
        self._t += self._tick
        return now


class _SeededIds:
    """Deterministic ``uuid.uuid4`` replacement seeded from ``seed`` (DEMO.md §3).

    Yields a reproducible stream of :class:`uuid.UUID` values so the escrow
    ``request_id`` and the tx-hash entropy the mock chain / orchestrator derive
    from ``uuid4()`` are stable across runs. Uses a tiny SplitMix64-style counter
    hashed to 128 bits — no global RNG state, no cross-run leakage.
    """

    def __init__(self, seed: int):
        self._state = (seed & ((1 << 64) - 1)) or 0xA5A5A5A5

    def __call__(self) -> _uuid.UUID:
        # SplitMix64 step -> 64 bits, mixed into 128 for a full UUID.
        self._state = (self._state + 0x9E3779B97F4A7C15) & ((1 << 64) - 1)
        z = self._state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & ((1 << 64) - 1)
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & ((1 << 64) - 1)
        z = z ^ (z >> 31)
        hi = z
        # second mix for the high 64 bits
        self._state = (self._state + 0x9E3779B97F4A7C15) & ((1 << 64) - 1)
        w = self._state
        w = ((w ^ (w >> 30)) * 0xBF58476D1CE4E5B9) & ((1 << 64) - 1)
        w = ((w ^ (w >> 27)) * 0x94D049BB133111EB) & ((1 << 64) - 1)
        w = w ^ (w >> 31)
        return _uuid.UUID(int=((hi << 64) | w) & ((1 << 128) - 1))


@contextmanager
def _deterministic(seed: int) -> Iterator[None]:
    """Temporarily swap ``time.time`` + ``uuid.uuid4`` for seeded/logical versions.

    Restores the originals on exit (including on error), so the wrapped scenario
    modules only ever see the patched clock *inside* a recorded run.
    """
    orig_time, orig_uuid4 = _time.time, _uuid.uuid4
    _time.time = _LogicalClock()  # type: ignore[assignment]
    _uuid.uuid4 = _SeededIds(seed)  # type: ignore[assignment]
    try:
        yield
    finally:
        _time.time = orig_time  # type: ignore[assignment]
        _uuid.uuid4 = orig_uuid4  # type: ignore[assignment]


# ── render-ready model (DEMO.md §2) ────────────────────────────────────────────


def _amount(token: str, units: int) -> dict:
    """Build the DEMO.md §2 **Amount** object (integer base units + display)."""
    decimals = _TOKEN_DECIMALS.get(token, 18)
    return {
        "token": token,
        "units": int(units),
        "decimals": decimals,
        "display": _fmt_units(int(units), decimals),
    }


def _fmt_units(units: int, decimals: int) -> str:
    """``units / 10**decimals`` as a stable 2-dp-min decimal string (no float)."""
    sign = "-" if units < 0 else ""
    units = abs(units)
    scale = 10 ** decimals
    whole, frac = divmod(units, scale)
    if decimals == 0:
        return f"{sign}{whole}"
    frac_str = str(frac).rjust(decimals, "0").rstrip("0")
    if len(frac_str) < 2:
        frac_str = frac_str.ljust(2, "0")  # always show at least 2 dp
    return f"{sign}{whole}.{frac_str}"


@dataclass
class TimelineEvent:
    """One render-ready frame of the run (DEMO.md §2).

    Every field maps 1:1 to the §2 JSON; :meth:`to_dict` emits exactly that
    object. The fields capture *who* acted, *what moved* this step, and the
    escrow / balances / block height **after** the step.
    """

    seq: int
    step: str
    phase: str
    actor: str
    peer: str | None
    title: str
    detail: str
    amount: dict | None
    escrow: dict | None
    balances: dict
    block: int
    tx: str | None
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "step": self.step,
            "phase": self.phase,
            "actor": self.actor,
            "peer": self.peer,
            "title": self.title,
            "detail": self.detail,
            "amount": self.amount,
            "escrow": self.escrow,
            "balances": self.balances,
            "block": self.block,
            "tx": self.tx,
            "data": self.data,
        }


@dataclass
class DemoRun:
    """A full recorded run: the §2 run-envelope minus the transport-added keys
    (``ok`` / ``sandbox`` / ``deterministic``, which ``server.py`` merges in)."""

    params: dict
    agents: dict
    timeline: list[TimelineEvent]
    summary: dict

    def to_dict(self) -> dict:
        return {
            "params": self.params,
            "agents": self.agents,
            "timeline": [e.to_dict() for e in self.timeline],
            "summary": self.summary,
        }


# ── the recorder ───────────────────────────────────────────────────────────────


# static per-step presentation metadata (phase / actor / peer / title), keyed by
# the canonical step id (DEMO.md §1). ``swap.*`` collapse to phase "swap".
_STEP_META: dict[str, dict] = {
    "setup":        {"phase": "setup", "actor": "system",   "peer": None,       "title": "Agents provisioned"},
    "402":          {"phase": "pay",   "actor": "B",        "peer": "A",        "title": "402 Payment Required"},
    "validate":     {"phase": "pay",   "actor": "A",        "peer": "escrow",   "title": "Offer validated"},
    "pay":          {"phase": "pay",   "actor": "A",        "peer": "escrow",   "title": "Funds locked in escrow"},
    "deliver":      {"phase": "pay",   "actor": "B",        "peer": "A",        "title": "Work delivered"},
    "settle":       {"phase": "pay",   "actor": "A",        "peer": "escrow",   "title": "Escrow released"},
    "swap.quote":   {"phase": "swap",  "actor": "safeswap", "peer": "B",        "title": "SafeSwap route quoted"},
    "swap.execute": {"phase": "swap",  "actor": "safeswap", "peer": "B",        "title": "Swap settled"},
}

# canonical seq order (DEMO.md §1); used to assign + assert seq.
_STEP_ORDER = ["setup", "402", "validate", "pay", "deliver", "settle", "swap.quote", "swap.execute"]


class _Recorder:
    """Re-drives scenario.run_scenario's exact call sequence against its own
    chain, emitting one :class:`TimelineEvent` per canonical step with a faithful
    post-step snapshot of escrow + balances + block height."""

    def __init__(self, *, price_units: int, swap_to: str,
                 chain: MockChain, safeswap: SafeSwapClient):
        self.price_units = price_units
        self.swap_to = swap_to
        self.chain = chain
        self.safeswap = safeswap
        self.a = AGENT_A_ADDR
        self.b = AGENT_B_ADDR
        self.events: list[TimelineEvent] = []
        # B's on-chain token after the swap flips from USDC to the swap output
        # asset (DEMO.md §2 — mirrors swap.html's balBUnit flip).
        self._b_token = "USDC"
        self._escrow_snapshot: dict | None = None

    # — balance / escrow snapshots (state AFTER a step) —

    def _balances(self) -> dict:
        return {
            "A": _amount("USDC", self.chain.balance_of(self.a)),
            "B": _amount(self._b_token, self.chain.balance_of(self.b)),
        }

    def _escrow_dict(self, request_id: str) -> dict:
        esc = self.chain.escrows[request_id]
        return {
            "request_id": request_id,
            "state": esc.state.value,
            "amount": _amount("USDC", esc.amount_wei),
            "payer": "A",
            "payee": "B",
        }

    def _emit(self, step: str, detail: str, *, amount: dict | None,
              escrow: dict | None, tx: str | None, data: dict) -> None:
        meta = _STEP_META[step]
        self.events.append(TimelineEvent(
            seq=_STEP_ORDER.index(step),
            step=step,
            phase=meta["phase"],
            actor=meta["actor"],
            peer=meta["peer"],
            title=meta["title"],
            detail=detail,
            amount=amount,
            escrow=escrow,
            balances=self._balances(),
            block=self.chain.block_number,
            tx=tx,
            data=data,
        ))

    # — the flow (mirrors scenario.run_scenario step-for-step) —

    def run(self) -> tuple[list[TimelineEvent], dict]:
        usdc = lambda n: f"{n / USDC:g}"  # noqa: E731 - tiny local formatter

        # Agent A funded with USDC to pay for work; Agent B starts empty.
        self.chain.fund(self.a, 100 * USDC)
        self._emit(
            "setup",
            f"Agent A funded with 100 USDC; Agent B starts empty.",
            amount=None, escrow=None, tx=None,
            data={"agent_a_balance": self.chain.balance_of(self.a)},
        )

        # — Agent A's real payment stack (genuine switchboard middleware) —
        payment_client = MockPaymentClient(self.chain, wallet_address=self.a)
        gas_tracker = BudgetGuard(hourly_limit=50 * USDC, daily_limit=200 * USDC)
        middleware = X402Middleware(
            payment_client=payment_client,
            gas_tracker=gas_tracker,
            max_payment_wei=20 * USDC,
            allowed_recipients={self.b},
        )

        # — Agent B's paid endpoint —
        agent_b = AgentBEndpoint(recipient=self.b, price_units=self.price_units, chain_id=8453)
        endpoint = "https://agent-b.example/v1/inference"

        # 1. Cold call -> 402 offer
        offer = agent_b.offer(endpoint)
        self._emit(
            "402",
            f"Agent B -> 402 Payment Required: {usdc(self.price_units)} USDC "
            f"(escrow scheme — funds locked, released on delivery).",
            amount=_amount("USDC", offer.amount_wei),
            escrow=None, tx=None,
            data={
                "recipient": offer.recipient,
                "amount": offer.amount_wei,
                "scheme": offer.scheme.value,
                "currency": offer.currency,
                "endpoint": endpoint,
                "nonce": offer.nonce,
                "expires_at": offer.expires_at,
            },
        )

        # 2. Agent A validates + pays into escrow (real middleware logic)
        middleware._validate_offer(offer)
        self._emit(
            "validate",
            f"Agent A: offer passes policy — under cap "
            f"({usdc(middleware.max_payment_wei)} USDC), recipient allow-listed, "
            f"within gas budget.",
            amount=None, escrow=None, tx=None,
            data={
                "cap_units": middleware.max_payment_wei,
                "amount_units": offer.amount_wei,
                "under_cap": offer.amount_wei <= middleware.max_payment_wei,
                "recipient_allowed": offer.recipient in (middleware.allowed_recipients or set()),
                "gas_budget_ok": gas_tracker.can_send_transaction(self.a, offer.amount_wei),
            },
        )

        proof: PaymentProof = middleware._pay_onchain(offer)  # ESCROW -> create_payment -> lock
        escrow_request_id = proof.tx_hash
        gas_tracker.record_gas_usage(self.a, offer.amount_wei)
        middleware.total_spent_wei += offer.amount_wei
        self._escrow_snapshot = self._escrow_dict(escrow_request_id)
        self._emit(
            "pay",
            f"Agent A locked {usdc(self.price_units)} USDC in AgentEscrow "
            f"(state: {self._escrow_snapshot['state']}).",
            amount=_amount("USDC", offer.amount_wei),
            escrow=self._escrow_snapshot, tx=escrow_request_id,
            data={
                "request_id": escrow_request_id,
                "escrow_state": payment_client.get_payment_state(escrow_request_id),
            },
        )

        # 3. Agent B delivers the work against the proof
        delivery = agent_b.deliver(proof)
        self._emit(
            "deliver",
            f"Agent B delivered the work (HTTP {delivery['status']}).",
            amount=None,
            escrow=self._escrow_dict(escrow_request_id), tx=None,
            data={"result": delivery["result"], "status": delivery["status"]},
        )

        # 4. Agent A settles: confirm -> release escrow to Agent B
        payment_client.confirm_payment(escrow_request_id)
        state_after = payment_client.get_payment_state(escrow_request_id)
        self._escrow_snapshot = self._escrow_dict(escrow_request_id)
        self._emit(
            "settle",
            f"Agent A confirmed -> escrow {state_after}; "
            f"{usdc(self.price_units)} USDC released to Agent B.",
            amount=_amount("USDC", offer.amount_wei),
            escrow=self._escrow_snapshot, tx=escrow_request_id,
            data={
                "escrow_state": state_after,
                "agent_b_balance": self.chain.balance_of(self.b),
            },
        )

        # record the completed payment so get_spend_summary() is truthful
        middleware.payment_history.append(
            PaymentRecord(endpoint=endpoint, offer=offer, proof=proof,
                          response_status=delivery["status"])
        )

        # 5. AGENTIC SWAP — Agent B routes received USDC through SafeSwap
        received = self.chain.balance_of(self.b)
        swap_req = SwapRequest(
            token_in="USDC", token_out=self.swap_to, amount_in=received,
            chain_id=offer.chain_id, recipient=self.b,
        )
        quote = self.safeswap.quote(swap_req)
        self._emit(
            "swap.quote",
            f"SafeSwap best-execution route: {usdc(received)} USDC -> "
            f"{quote.amount_out} {self.swap_to} units via "
            f"{' -> '.join(quote.route)} (fee {quote.fee_bps}bps).",
            amount=_amount("USDC", received),
            escrow=self._escrow_snapshot, tx=None,
            data={
                "route": quote.route,
                "amount_in": quote.amount_in,
                "amount_out": quote.amount_out,
                "price": quote.price,
                "fee_bps": quote.fee_bps,
                "quote_id": quote.quote_id,
            },
        )

        receipt = self.safeswap.execute(quote)
        # B's on-chain holding now reads as the swap output asset.
        self._b_token = self.swap_to
        self._emit(
            "swap.execute",
            f"SafeSwap routed swap settled — Agent B now holds "
            f"{receipt.amount_out} {self.swap_to} units (tx {receipt.tx_hash[:12]}...).",
            amount=_amount(self.swap_to, receipt.amount_out),
            escrow=self._escrow_snapshot, tx=receipt.tx_hash,
            data={
                "tx": receipt.tx_hash,
                "amount_out": receipt.amount_out,
                "route": receipt.route,
                "settled_at": int(receipt.settled_at),
            },
        )

        # — summary derived from genuine library output (DEMO.md §2) —
        summary = {
            "settled": state_after == EscrowState.RELEASED.value,
            "swap_routed": bool(receipt.route and receipt.amount_out > 0),
            "escrow_request_id": escrow_request_id,
            "escrow_state": state_after,
            "offer": {
                "amount_units": offer.amount_wei,
                "currency": offer.currency,
                "scheme": offer.scheme.value,
                "recipient": offer.recipient,
            },
            "swap": receipt.to_dict(),
            "spend_summary": middleware.get_spend_summary(),
            "blocks_mined": self.chain.block_number,
        }
        return self.events, summary


def run_observable(*, price_units: int = 5 * USDC, swap_to: str = "ETH",
                   seed: int = 42, deterministic: bool = True) -> DemoRun:
    """Drive the real scenario and return the recorded, render-ready timeline.

    Mirrors ``scenario.run_scenario``'s exact call sequence (same real-library
    methods, same order, same canonical ``step`` ids — DEMO.md §1) against a
    private :class:`MockChain`, recording a :class:`TimelineEvent` per step. When
    ``deterministic`` (the default), the whole run executes under a logical clock
    + seeded id stream (DEMO.md §3), so two calls with identical params + seed
    return byte-identical :meth:`DemoRun.to_dict`.
    """
    swap_to = (swap_to or "ETH").upper()
    chain = MockChain()
    safeswap = SafeSwapClient(transport=MockSafeSwapOrchestrator())
    recorder = _Recorder(price_units=price_units, swap_to=swap_to,
                         chain=chain, safeswap=safeswap)

    if deterministic:
        with _deterministic(seed):
            timeline, summary = recorder.run()
    else:
        timeline, summary = recorder.run()

    params = {
        "price_units": price_units,
        "swap_to": swap_to,
        "seed": seed,
        "deterministic": deterministic,
    }
    agents = {
        "A": {"id": "A", "role": "payer", "label": "Agent A", "address": AGENT_A_ADDR},
        "B": {"id": "B", "role": "payee", "label": "Agent B", "address": AGENT_B_ADDR},
        "escrow":   {"id": "escrow",   "role": "contract",     "label": "AgentEscrow"},
        "safeswap": {"id": "safeswap", "role": "orchestrator", "label": "SafeSwap"},
    }
    return DemoRun(params=params, agents=agents, timeline=timeline, summary=summary)


# ── stepwise cursor (backs POST /api/demo/step) ────────────────────────────────


class StepCursor:
    """A server-side cursor over a :class:`DemoRun`'s timeline (DEMO.md §5).

    Starts *before* ``setup`` (index ``-1``). Each :meth:`advance` reveals the
    next event; :meth:`reset` rewinds. Backs the optional ``POST /api/demo/step``
    server-driven step mode.
    """

    def __init__(self, run: DemoRun):
        self.run = run
        self._i = -1

    def reset(self) -> None:
        self._i = -1

    @property
    def index(self) -> int:
        return self._i

    def advance(self) -> tuple[bool, int, TimelineEvent | None]:
        """Reveal the next event. Returns ``(done, index, event)``.

        ``done`` is ``True`` once the timeline is exhausted (the final
        ``advance`` past the last event returns ``(True, last_index, None)``).
        """
        if self._i >= len(self.run.timeline) - 1:
            self._i = len(self.run.timeline) - 1
            return True, self._i, None
        self._i += 1
        event = self.run.timeline[self._i]
        done = self._i >= len(self.run.timeline) - 1
        return done, self._i, event
