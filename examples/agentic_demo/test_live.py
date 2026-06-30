"""Tests for the LIVE/observable agentic-escrow demo layer (DEMO.md §6).

These cover the additive live layer only — ``observable.py`` (the timeline
recorder) and ``server.py`` (the stdlib HTTP transport). They never touch
``scenario.py`` / ``onchain.py`` / ``safeswap.py`` or the existing
``tests/test_agentic_demo.py``; the wrapped orchestration is exercised through
``run_observable`` exactly as the real ``run_scenario`` would drive it.

SANDBOX: everything runs against the in-memory ``MockChain`` (AgentEscrow
surface). No real ETH / RPC / network / funds.

Run:  cd <repo> && PYTHONPATH=. python3 -m pytest examples/agentic_demo/ -q
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from examples.agentic_demo.observable import (
    DemoRun,
    StepCursor,
    TimelineEvent,
    run_observable,
)
from examples.agentic_demo.scenario import USDC
from examples.agentic_demo.server import SANDBOX_NOTE, make_server

# canonical step ids + order (DEMO.md §1).
CANONICAL_STEPS = ["setup", "402", "validate", "pay", "deliver", "settle",
                   "swap.quote", "swap.execute"]


# ── fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture()
def run() -> DemoRun:
    """A default seeded deterministic run (seed=42, 5 USDC, swap_to=ETH)."""
    return run_observable()


@pytest.fixture()
def live_server():
    """A demo HTTP server on an ephemeral port, served in a daemon thread.

    Yields the base URL; torn down (shutdown + close) after the test.
    """
    server = make_server("127.0.0.1", 0)
    host, port = server.server_address[0], server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# small HTTP helpers (stdlib only — no requests dependency)

def _get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=5) as resp:
        return resp.status, resp.headers, resp.read()


def _get_json(base: str, path: str):
    status, headers, body = _get(base, path)
    return status, json.loads(body)


def _post_json(base: str, path: str, body: dict):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read())


# ── determinism (the headline contract, DEMO.md §3) ─────────────────────────────


def test_two_runs_same_seed_are_byte_identical():
    a = run_observable(seed=42).to_dict()
    b = run_observable(seed=42).to_dict()
    # byte-for-byte equal: timeline + summary + params + agents.
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_default_run_is_deterministic_too():
    # The server's default (no-arg) run is the one GET /state serves; it must be
    # stable across process-internal calls.
    assert run_observable().to_dict() == run_observable().to_dict()


def test_different_seed_changes_minted_ids_but_not_structure():
    a = run_observable(seed=42)
    b = run_observable(seed=7)
    # structure (steps, balances, amounts, blocks) is seed-independent…
    assert [e.step for e in a.timeline] == [e.step for e in b.timeline]
    assert [e.block for e in a.timeline] == [e.block for e in b.timeline]
    # …but the seeded escrow request_id differs.
    assert a.summary["escrow_request_id"] != b.summary["escrow_request_id"]


# ── step coverage + order (DEMO.md §1) ──────────────────────────────────────────


def test_timeline_has_exactly_the_canonical_steps_in_order(run: DemoRun):
    steps = [e.step for e in run.timeline]
    assert steps == CANONICAL_STEPS


def test_seq_indices_are_dense_and_ordered(run: DemoRun):
    assert [e.seq for e in run.timeline] == list(range(len(CANONICAL_STEPS)))


def test_load_bearing_ordering(run: DemoRun):
    steps = [e.step for e in run.timeline]
    # the same ordering tests/test_agentic_demo.py asserts on the raw scenario.
    assert steps.index("402") < steps.index("pay") < steps.index("settle")
    assert steps.index("settle") < steps.index("swap.quote") < steps.index("swap.execute")


def test_every_event_has_required_render_fields(run: DemoRun):
    for e in run.timeline:
        d = e.to_dict()
        for key in ("seq", "step", "phase", "actor", "peer", "title", "detail",
                    "amount", "escrow", "balances", "block", "tx", "data"):
            assert key in d, f"{e.step} missing {key}"
        assert d["phase"] in {"setup", "pay", "swap"}
        assert d["actor"] in {"A", "B", "escrow", "safeswap", "system"}
        assert isinstance(d["block"], int)
        # balances is always a node-id -> Amount map.
        for node, amt in d["balances"].items():
            assert set(amt) >= {"token", "units", "decimals", "display"}


def test_phases_group_correctly(run: DemoRun):
    by_step = {e.step: e for e in run.timeline}
    assert by_step["setup"].phase == "setup"
    assert by_step["402"].phase == "pay"
    assert by_step["swap.quote"].phase == "swap"
    assert by_step["swap.execute"].phase == "swap"


# ── truthful amounts / escrow / balances (DEMO.md §6) ───────────────────────────


def test_pay_shows_escrow_locked_at_offer_amount(run: DemoRun):
    pay = next(e for e in run.timeline if e.step == "pay")
    assert pay.escrow is not None
    assert pay.escrow["state"] == "Locked"
    assert pay.escrow["amount"]["units"] == 5 * USDC
    assert pay.amount["units"] == 5 * USDC
    assert pay.amount["token"] == "USDC"
    # the escrow request_id is surfaced as the tx for this step.
    assert pay.tx == pay.escrow["request_id"]


def test_settle_releases_escrow_and_credits_payee(run: DemoRun):
    settle = next(e for e in run.timeline if e.step == "settle")
    assert settle.escrow is not None
    assert settle.escrow["state"] == "Released"
    # balances after settle: A = 95 USDC, B = 5 USDC (matches test_agentic_demo).
    assert settle.balances["A"]["units"] == 95 * USDC
    assert settle.balances["B"]["units"] == 5 * USDC
    assert settle.balances["A"]["display"] == "95.00"
    assert settle.balances["B"]["display"] == "5.00"


def test_balance_b_flips_to_swap_asset_after_execute(run: DemoRun):
    settle = next(e for e in run.timeline if e.step == "settle")
    execute = next(e for e in run.timeline if e.step == "swap.execute")
    # before the swap, B holds USDC; after execute its on-chain Amount reads ETH.
    assert settle.balances["B"]["token"] == "USDC"
    assert execute.balances["B"]["token"] == "ETH"
    assert execute.balances["B"]["decimals"] == 18
    # the swap.execute *amount* shows the routed output (genuine SafeSwap number;
    # SwapReceipt.to_dict() serializes amountOut as a string, hence the int()).
    assert execute.amount["token"] == "ETH"
    assert execute.amount["units"] == int(run.summary["swap"]["amountOut"])
    assert int(run.summary["swap"]["amountOut"]) > 0


def test_blocks_are_monotonic_and_advance_on_chain_writes(run: DemoRun):
    blocks = [e.block for e in run.timeline]
    assert blocks == sorted(blocks)  # never goes backwards
    by_step = {e.step: e.block for e in run.timeline}
    # the lock mines a block; the release mines another.
    assert by_step["pay"] > by_step["validate"]
    assert by_step["settle"] > by_step["pay"]


def test_validate_event_carries_gas_and_cap_check(run: DemoRun):
    validate = next(e for e in run.timeline if e.step == "validate")
    d = validate.data
    # the gas-budget / cap / allowlist validation is an explicit, present event.
    assert d["under_cap"] is True
    assert d["recipient_allowed"] is True
    assert d["gas_budget_ok"] is True
    assert d["amount_units"] == 5 * USDC
    assert d["cap_units"] == 20 * USDC


# ── summary is the REAL library output (DEMO.md §2/§6) ──────────────────────────


def test_summary_reflects_genuine_scenario_result(run: DemoRun):
    s = run.summary
    assert s["settled"] is True
    assert s["swap_routed"] is True
    assert s["escrow_state"] == "Released"
    # spend summary comes straight from X402Middleware.get_spend_summary().
    assert s["spend_summary"]["total_payments"] == 1
    assert s["spend_summary"]["total_spent_wei"] == 5 * USDC
    # swap receipt is SwapReceipt.to_dict() with a real, positive output.
    assert int(s["swap"]["amountOut"]) > 0
    assert s["swap"]["tokenIn"] == "USDC"
    assert s["swap"]["tokenOut"] == "ETH"
    assert s["swap"]["txHash"].startswith("0x")
    assert s["offer"]["amount_units"] == 5 * USDC
    assert s["offer"]["scheme"] == "escrow"


def test_swap_to_lux_routes_through_lux(run: DemoRun):
    lux = run_observable(swap_to="LUX")
    assert lux.summary["swap"]["tokenOut"] == "LUX"
    assert "LuxDEX" in lux.summary["swap"]["route"]
    execute = next(e for e in lux.timeline if e.step == "swap.execute")
    assert execute.balances["B"]["token"] == "LUX"


def test_custom_price_units_flow_through(run: DemoRun):
    r = run_observable(price_units=3 * USDC)
    pay = next(e for e in r.timeline if e.step == "pay")
    settle = next(e for e in r.timeline if e.step == "settle")
    assert pay.escrow["amount"]["units"] == 3 * USDC
    assert settle.balances["A"]["units"] == 97 * USDC
    assert settle.balances["B"]["units"] == 3 * USDC
    assert r.summary["spend_summary"]["total_spent_wei"] == 3 * USDC


# ── non-deterministic mode still produces a valid run ──────────────────────────


def test_non_deterministic_run_is_well_formed_but_not_pinned():
    a = run_observable(deterministic=False)
    b = run_observable(deterministic=False)
    # structurally identical…
    assert [e.step for e in a.timeline] == CANONICAL_STEPS
    assert a.summary["settled"] is True and b.summary["settled"] is True
    # …but the un-seeded escrow ids differ between runs (live, organic mode).
    assert a.summary["escrow_request_id"] != b.summary["escrow_request_id"]


# ── StepCursor (backs POST /api/demo/step) ──────────────────────────────────────


def test_step_cursor_walks_then_signals_done(run: DemoRun):
    cur = StepCursor(run)
    assert cur.index == -1
    seen: list[TimelineEvent] = []
    done = False
    # advance until done; the final advance past the last event yields no event.
    for _ in range(len(run.timeline) + 1):
        done, idx, ev = cur.advance()
        if ev is not None:
            seen.append(ev)
    assert done is True
    assert [e.step for e in seen] == CANONICAL_STEPS


def test_step_cursor_reset_rewinds(run: DemoRun):
    cur = StepCursor(run)
    cur.advance(); cur.advance()
    assert cur.index == 1
    cur.reset()
    assert cur.index == -1
    done, idx, ev = cur.advance()
    assert idx == 0 and ev is not None and ev.step == "setup"


# ── HTTP transport (DEMO.md §4/§6) ──────────────────────────────────────────────


def test_healthz_ok(live_server: str):
    status, payload = _get_json(live_server, "/healthz")
    assert status == 200
    assert payload == {"ok": True}


def test_post_run_returns_envelope_with_timeline(live_server: str):
    status, env = _post_json(live_server, "/api/demo/run",
                             {"price_units": 5 * USDC, "swap_to": "ETH", "seed": 42})
    assert status == 200
    assert env["ok"] is True
    assert env["sandbox"] == SANDBOX_NOTE
    assert env["deterministic"] is True
    assert [e["step"] for e in env["timeline"]] == CANONICAL_STEPS
    assert env["summary"]["escrow_state"] == "Released"
    assert env["summary"]["settled"] is True
    # the run envelope matches the in-process recorder byte-for-byte.
    local = run_observable(price_units=5 * USDC, swap_to="ETH", seed=42).to_dict()
    assert env["timeline"] == local["timeline"]
    assert env["summary"] == local["summary"]


def test_get_state_serves_a_run_without_posting(live_server: str):
    # cold GET /state performs + caches a default seeded run.
    status, env = _get_json(live_server, "/api/demo/state")
    assert status == 200
    assert env["ok"] is True
    assert [e["step"] for e in env["timeline"]] == CANONICAL_STEPS


def test_state_echoes_last_run(live_server: str):
    _, run_env = _post_json(live_server, "/api/demo/run", {"swap_to": "LUX", "seed": 99})
    _, state_env = _get_json(live_server, "/api/demo/state")
    # GET /state replays exactly the last POST /run result.
    assert state_env["timeline"] == run_env["timeline"]
    assert state_env["summary"] == run_env["summary"]
    assert state_env["params"]["swap_to"] == "LUX"


def test_post_step_advances_one_event(live_server: str):
    _, reset = _post_json(live_server, "/api/demo/step", {"reset": True})
    assert reset["ok"] is True and reset["index"] == -1 and reset["event"] is None
    status, step = _post_json(live_server, "/api/demo/step", {})
    assert status == 200
    assert step["ok"] is True
    assert step["index"] == 0
    assert step["event"]["step"] == "setup"
    assert step["done"] is False


def test_root_serves_html(live_server: str):
    status, headers, body = _get(live_server, "/")
    assert status == 200
    assert headers.get("Content-Type", "").startswith("text/html")
    assert len(body) > 0


def test_unknown_path_is_404(live_server: str):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(live_server, "/nope")
    assert exc.value.code == 404


def test_bad_params_are_400(live_server: str):
    # price above the middleware cap is rejected before any orchestration runs.
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post_json(live_server, "/api/demo/run", {"price_units": 999_000_000})
    assert exc.value.code == 400
    payload = json.loads(exc.value.read())
    assert payload["ok"] is False
    assert "price_units" in payload["error"]


def test_bad_swap_target_is_400(live_server: str):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post_json(live_server, "/api/demo/run", {"swap_to": "DOGE"})
    assert exc.value.code == 400


def test_empty_body_run_uses_defaults(live_server: str):
    # an empty POST body falls back to the seeded defaults (DEMO.md §4).
    req = urllib.request.Request(live_server + "/api/demo/run", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        env = json.loads(resp.read())
    assert env["ok"] is True
    assert env["params"]["price_units"] == 5 * USDC
    assert env["params"]["swap_to"] == "ETH"
