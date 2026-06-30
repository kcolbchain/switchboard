# Live Agentic-Escrow Demo — design contract

> **SIMULATED / MOCK CHAIN — not a live network.** Everything here runs against
> an in-memory `MockChain` (see `onchain.py`) implementing the **AgentEscrow**
> surface (`lock → confirm → release / refund`). **No real ETH, no real RPC, no
> network, no funds.** The agents and keys are synthetic. Nothing in this folder
> is a live or production deployment.
>
> _Demo by Pattermesh (Patty / P. Sundaram)_ on top of **kcolbchain/switchboard**
> (the collective's agentic-payments rail; Abhishek Krishna / @abhicris leads).
> The escrow + x402 + SafeSwap orchestration being shown is switchboard's, not
> Patty's — this page only makes it **watchable**.

This document is the **fixed contract** between the three pieces of the live
demo. The Architect owns this file and `server.py`'s stub signatures; the
backend builder fills `observable.py` + the `server.py` handler bodies; the
frontend builder writes the page that calls the API below. **Do not change the
event field names, step ids, actor ids, or route strings** without updating all
three.

---

## 1. What already exists (do NOT rewrite)

`examples/agentic_demo/` already runs the whole A2A flow in one shot via
`scenario.run_scenario()`, which drives the **real** switchboard library
(`switchboard.x402_middleware.X402Middleware`, `switchboard.gas_tracker.GasTracker`)
against the mock substrate:

```
402 offer → validate(cap/allowlist/gas) → escrow lock → deliver
          → confirm/release → SafeSwap quote → SafeSwap execute
```

`scenario.py`, `onchain.py`, `safeswap.py` stay **byte-for-byte unchanged**. The
live demo *wraps* `run_scenario` to record an ordered, render-ready **timeline**,
then serves it over a stdlib HTTP server. The existing `web/lab/swap.html` is a
client-side cartoon with a hardcoded `STEPS` array; the live demo replaces those
fake numbers with the **real** orchestration output (real escrow state, real
SafeSwap quote, real spend summary).

### Canonical step ids (from `scenario.StepLog.step`, already matched by `swap.html`)

| seq | `step` id      | meaning                                              |
|-----|----------------|------------------------------------------------------|
| 0   | `setup`        | Agent A funded; Agent B empty (pre-roll, UI may skip)|
| 1   | `402`          | Agent B → `402 Payment Required` + x402 escrow offer |
| 2   | `validate`     | Agent A: offer passes cap / allowlist / gas budget   |
| 3   | `pay`          | Agent A locks funds in `AgentEscrow` → **Locked**    |
| 4   | `deliver`      | Agent B serves the work (`200 OK`)                   |
| 5   | `settle`       | Agent A `confirmPayment()` → escrow **Released**     |
| 6   | `swap.quote`   | SafeSwap best-execution route quoted                 |
| 7   | `swap.execute` | Routed swap settled → `SwapReceipt`                  |

These ids and their order are **load-bearing**: `tests/test_agentic_demo.py`
already asserts `402 < pay < settle < swap.quote < swap.execute`, and the
observable wrapper MUST reproduce the same ids in the same order.

---

## 2. The TIMELINE-EVENT model

A run is an **ordered list of `TimelineEvent`s** plus a small `summary`. One
event per step above (the `setup` pre-roll included). Each event is a frame the
UI can render to show the two agents transacting one step at a time: who acts,
what moves, what the escrow + balances look like *after* the step, and which
mock-chain block we're on.

### `TimelineEvent` (JSON object)

| field          | type             | notes                                                                                  |
|----------------|------------------|----------------------------------------------------------------------------------------|
| `seq`          | int              | 0-based order index (matches the table above).                                         |
| `step`         | string           | one of the canonical step ids.                                                         |
| `phase`        | string           | coarse grouping for styling: `"setup" \| "pay" \| "swap"`. `swap.*` → `"swap"`.        |
| `actor`        | string           | the node that **initiates** this step: `"A" \| "B" \| "escrow" \| "safeswap" \| "system"`. |
| `peer`         | string \| null   | the counterparty / destination node id (same vocabulary as `actor`), or `null`.        |
| `title`        | string           | short label, e.g. `"402 Payment Required"`.                                            |
| `detail`       | string           | one-line human description (plain text; UI may bold/format).                           |
| `amount`       | object \| null   | value moved **this step** (see **Amount** below), or `null` if nothing moves.          |
| `escrow`       | object \| null   | escrow snapshot **after** this step (see **Escrow** below); `null` before lock.        |
| `balances`     | object           | balances **after** this step: see **Balances** below.                                  |
| `block`        | int              | `MockChain.block_number` **after** this step (mock-chain "block height").              |
| `tx`           | string \| null   | tx hash / escrow `request_id` produced this step (`0x…` or a uuid), else `null`.       |
| `data`         | object           | step-specific extras, passthrough of `StepLog.data` plus the typed fields below.       |

#### Amount (`event.amount`)

```jsonc
{ "token": "USDC", "units": 5000000, "decimals": 6, "display": "5.00" }
```

* `units` is the integer base-unit amount (USDC = 6dp, ETH = 18dp), exactly as
  the library/`MockChain` track it — never a float.
* `display` is the human string the UI shows; computed as `units / 10**decimals`.

#### Escrow (`event.escrow`)

```jsonc
{ "request_id": "…", "state": "Locked", "amount": { /* Amount */ }, "payer": "A", "payee": "B" }
```

`state` is one of `EscrowState` values: `"Locked" | "Confirmed" | "Released" |
"Refunded" | "Cancelled"` (from `onchain.EscrowState`).

#### Balances (`event.balances`)

Map of **node id → Amount**, reflecting on-chain balances after the step:

```jsonc
{ "A": { "token": "USDC", "units": 95000000, "decimals": 6, "display": "95.00" },
  "B": { "token": "USDC", "units": 5000000,  "decimals": 6, "display": "5.00"  } }
```

After `swap.execute`, Agent B's balance Amount switches `token`/`decimals` to the
swap output asset (e.g. `ETH`, 18dp) — mirroring `swap.html`'s `balBUnit` flip.

### Run envelope (`POST /api/demo/run` → 200)

```jsonc
{
  "ok": true,
  "sandbox": "SIMULATED / MOCK CHAIN — not a live network. No real ETH/RPC/funds.",
  "deterministic": true,
  "params": { "price_units": 5000000, "swap_to": "ETH", "seed": 42 },
  "agents": {
    "A": { "id": "A", "role": "payer", "label": "Agent A", "address": "0x…" },
    "B": { "id": "B", "role": "payee", "label": "Agent B", "address": "0x…" },
    "escrow":   { "id": "escrow",   "role": "contract",     "label": "AgentEscrow" },
    "safeswap": { "id": "safeswap", "role": "orchestrator", "label": "SafeSwap"    }
  },
  "timeline": [ /* TimelineEvent, in seq order */ ],
  "summary": {
    "settled": true,
    "swap_routed": true,
    "escrow_request_id": "…",
    "escrow_state": "Released",
    "offer":  { "amount_units": 5000000, "currency": "USDC", "scheme": "escrow", "recipient": "0x…" },
    "swap":   { /* SwapReceipt.to_dict() */ },
    "spend_summary": { /* X402Middleware.get_spend_summary() */ },
    "blocks_mined": 4
  }
}
```

`summary` is derived from the real `ScenarioResult` (`settled`, `swap_routed`,
`spend_summary`, `swap_receipt.to_dict()`), so the page footer shows genuine
library output, not recomputed JS.

On error the envelope is `{ "ok": false, "error": "<message>" }` with HTTP 400/500.

---

## 3. Determinism

The page must be **replayable to the same bytes** so the demo and its tests are
stable. `scenario.run_scenario()` itself uses `time.time()` (offer `nonce` /
`expires_at`, `SwapReceipt.settled_at`) and `uuid4()` (escrow `request_id`, tx
hashes) — non-deterministic. The observable wrapper therefore runs in a
**seeded / logical-clock** mode:

* A **logical clock** starts at a fixed epoch and advances by a fixed tick per
  recorded event, so `expires_at`, `settled_at`, and event `at` are reproducible.
* A **seeded RNG** (default `seed=42`) derives the escrow `request_id` and any
  ids the wrapper itself mints, so `tx`/`request_id` are stable across runs.
* The **mock-chain block numbers** are already deterministic (every
  `transfer`/`escrow_*` calls `mine()`), so `block` is stable for free.

Determinism is opt-outable (`deterministic=False` / no seed) for an organic live
run, but the **default** for the server + all tests is deterministic. The
contract: **two `POST /api/demo/run` with identical params + seed return
byte-identical `timeline` + `summary`** (this is the headline backend test).

The wrapper **must not** edit `scenario.py`; it injects determinism by
constructing its own `MockChain` / seeded clock and recording the same step
sequence `run_scenario` performs (it may call `run_scenario(chain=…, safeswap=…)`
and post-process its `StepLog`s, or re-drive the identical calls — builder's
choice — but the resulting step ids/order MUST match §1).

---

## 4. HTTP API (stdlib `http.server`, no framework)

All JSON, `Content-Type: application/json`, permissive same-origin. Pure
`http.server.BaseHTTPRequestHandler` — **no Flask/FastAPI, no build step**, house
style. Bind `127.0.0.1` by default.

| Method + path          | body                                  | →                                                    |
|------------------------|---------------------------------------|------------------------------------------------------|
| `GET  /`               | —                                     | serves the demo HTML page (text/html).               |
| `GET  /api/demo/state` | —                                     | the **last** run envelope, or a fresh seeded run if none yet. |
| `POST /api/demo/run`   | `{ "price_units"?, "swap_to"?, "seed"?, "deterministic"? }` | runs a full scenario; returns the **run envelope** (§2). |
| `POST /api/demo/step`  | `{ "reset"? : bool }`                 | **optional** stepwise mode: advance one event and return `{ "ok", "done", "index", "event": TimelineEvent }`; `reset:true` rewinds to before `setup`. |
| `GET  /healthz`        | —                                     | `{ "ok": true }` liveness.                            |

* `price_units` integer base units (default `5_000_000` = 5 USDC); `swap_to` ∈
  `{"ETH","LUX","USDC"}` (default `"ETH"`); `seed` int (default `42`);
  `deterministic` bool (default `true`).
* `POST /api/demo/run` is the primary path: the page calls it once and animates
  the returned `timeline` client-side (it already has the canvas + ledger from
  `swap.html`). `POST /api/demo/step` is a convenience for a true server-driven
  step mode; if unimplemented it returns `501`.
* `GET /api/demo/state` lets a freshly-loaded page render the last run (or a
  default seeded run) without re-POSTing — handy for the PWA offline shell.

### Static page serving

`GET /` returns an HTML page. The server resolves it from (in order):

1. `examples/agentic_demo/demo.html` if present (a self-contained live page the
   frontend builder may add), else
2. the repo's `web/lab/swap.html` (the existing visualization), rewriting its
   hardcoded `STEPS`/balances to fetch `/api/demo/run` instead.

Either way the page is **vanilla ES + the existing `shared.css` tokens** (gold
`#d4a853`, emerald `#4ecb71`, violet `#a78bfa`, cyan `#67d4e0`, mono
`JetBrains Mono`). No external requests beyond what `swap.html` already declares.

---

## 5. Contract surface the builders implement

These names are **fixed** by `server.py`'s stubs. Backend builder fills the
bodies; frontend builder calls the routes.

**`examples/agentic_demo/observable.py`** (new; the timeline recorder — wraps
`run_scenario`, never edits it):

```python
@dataclass
class TimelineEvent:        # the §2 object; .to_dict() emits the JSON above
    seq: int; step: str; phase: str; actor: str; peer: str | None
    title: str; detail: str
    amount: dict | None; escrow: dict | None; balances: dict
    block: int; tx: str | None; data: dict
    def to_dict(self) -> dict: ...

@dataclass
class DemoRun:              # the run envelope (§2)
    params: dict; agents: dict; timeline: list[TimelineEvent]; summary: dict
    def to_dict(self) -> dict: ...

def run_observable(*, price_units: int = 5_000_000, swap_to: str = "ETH",
                   seed: int = 42, deterministic: bool = True) -> DemoRun: ...
    # drives the real scenario, returns the recorded, render-ready timeline.

class StepCursor:          # backs POST /api/demo/step (optional stepwise mode)
    def __init__(self, run: DemoRun): ...
    def reset(self) -> None: ...
    def advance(self) -> tuple[bool, int, TimelineEvent | None]: ...  # (done, index, event)
```

**`examples/agentic_demo/server.py`** (skeleton fixed by Architect; bodies by
backend builder):

```python
SANDBOX_NOTE = "SIMULATED / MOCK CHAIN — not a live network. No real ETH/RPC/funds."

class DemoHandler(BaseHTTPRequestHandler):
    def do_GET(self):  ...      # routes: / , /api/demo/state , /healthz
    def do_POST(self): ...      # routes: /api/demo/run , /api/demo/step
    # helpers (fixed names):
    def _send_json(self, obj, status=200): ...
    def _send_html(self, html, status=200): ...
    def _read_json_body(self) -> dict: ...
    def _serve_page(self): ...                 # resolves the §4 static page
    def _handle_run(self, body: dict): ...      # → run_observable → envelope
    def _handle_state(self): ...                # last run or default seeded run
    def _handle_step(self, body: dict): ...     # StepCursor; 501 if unimplemented

def make_server(host="127.0.0.1", port=8402) -> ThreadingHTTPServer: ...
def main(argv: list[str] | None = None) -> int: ...   # CLI: --host --port --open
```

The frontend calls only the §4 routes; it never imports Python. The whole thing
launches with `PYTHONPATH=. python examples/agentic_demo/server.py` and is then
viewable at `http://127.0.0.1:8402/`.

---

## 6. Tests the backend builder must turn green

Scoped to `tests/test_agentic_demo_live.py` (new file; does NOT touch the
existing `test_agentic_demo.py`). At minimum:

* **determinism** — two `run_observable(seed=42)` calls produce equal
  `to_dict()` (timeline + summary byte-identical).
* **step coverage + order** — the timeline contains exactly the §1 step ids in
  order; `402 < pay < settle < swap.quote < swap.execute`.
* **truthful amounts** — `pay` shows the escrow `Locked` at the offer amount;
  `settle` shows escrow `Released` and Agent B credited the price; balances after
  `settle` are A=95 / B=5 USDC (units), matching `test_agentic_demo`'s ledger
  assertions; after `swap.execute` B's balance Amount flips to the swap asset.
* **summary is real** — `summary.settled is True`, `summary.swap_routed is True`,
  `summary.spend_summary["total_spent_wei"] == price_units`,
  `summary.swap.amountOut > 0`.
* **HTTP** — spin up `make_server` on an ephemeral port; `POST /api/demo/run`
  returns `ok:true` + a non-empty `timeline`; `GET /healthz` → `ok:true`;
  unknown path → `404`; `GET /` → `200 text/html`.

`scenario.py`/`onchain.py`/`safeswap.py` and `test_agentic_demo.py` remain
unchanged; the live layer is purely additive.
