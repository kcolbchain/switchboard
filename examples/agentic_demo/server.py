#!/usr/bin/env python3
"""Live, watchable agentic-escrow demo — stdlib HTTP server.

    PYTHONPATH=. python examples/agentic_demo/server.py
    # then open http://127.0.0.1:8402/

================================================================================
SIMULATED / MOCK CHAIN — NOT A LIVE NETWORK.
No real ETH, no real RPC, no network calls, no funds. This serves the EXISTING
``examples/agentic_demo`` orchestration (which drives the real
``switchboard.x402_middleware`` + ``switchboard.gas_tracker`` against an
in-memory ``MockChain`` implementing the AgentEscrow surface) and makes it
*watchable* one step at a time. Synthetic agents/keys only. Nothing here is a
live or production deployment.

Demo by Pattermesh (Patty / P. Sundaram) on top of kcolbchain/switchboard — the
collective's agentic-payments rail (Abhishek Krishna / @abhicris leads). The
escrow/x402/SafeSwap logic shown is switchboard's; this server only renders it.
================================================================================

This module is a thin transport. It exposes the JSON API specified in
``DEMO.md`` and serves the demo page; all orchestration lives in
``scenario.py`` (UNCHANGED) and the timeline recording in ``observable.py``.

ARCHITECT CONTRACT — these route mappings, handler names, and helper signatures
are FIXED. The backend builder fills the ``NotImplementedError`` bodies; the
frontend builder calls only the HTTP routes below (it never imports Python).
Keep ``scenario.py`` / ``onchain.py`` / ``safeswap.py`` byte-for-byte unchanged.

Routes (see DEMO.md §4):
    GET  /                 -> the demo HTML page (text/html)
    GET  /api/demo/state   -> last run envelope, or a fresh seeded run
    POST /api/demo/run     -> run a full scenario; returns the run envelope
    POST /api/demo/step    -> optional stepwise mode (501 if unimplemented)
    GET  /healthz          -> {"ok": true}
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Allow ``python examples/agentic_demo/server.py`` from the repo root without
# requiring PYTHONPATH=. (mirrors run.py), while still preferring an installed pkg.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Import the timeline recorder via the package path when available (so the
# wrapped ``examples.agentic_demo`` modules resolve), falling back to a flat
# import when this file is run directly as a script (mirrors run.py).
try:
    from examples.agentic_demo.observable import (
        DemoRun,
        StepCursor,
        run_observable,
    )
except ModuleNotFoundError:  # pragma: no cover - script-style fallback
    from observable import DemoRun, StepCursor, run_observable  # type: ignore

# The one-line sandbox banner echoed into every run envelope + page header.
SANDBOX_NOTE = "SIMULATED / MOCK CHAIN — not a live network. No real ETH/RPC/funds."

# Default page lookup order (see DEMO.md §4): a self-contained live page if the
# frontend builder adds one, else the existing lab visualization.
_PAGE_CANDIDATES = (
    _REPO_ROOT / "examples" / "agentic_demo" / "demo.html",
    _REPO_ROOT / "web" / "lab" / "swap.html",
)

# Validation bounds for ``POST /api/demo/run`` params (DEMO.md §4).
_SWAP_TARGETS = {"ETH", "LUX", "USDC"}
_MIN_PRICE_UNITS = 1
_MAX_PRICE_UNITS = 20 * 10**6  # the middleware's max_payment cap (20 USDC); above it the offer fails validation


def _envelope(run: DemoRun) -> dict:
    """Build the §2 run-envelope from a :class:`DemoRun`, merging the transport
    keys (``ok`` / ``sandbox`` / ``deterministic``) the handlers add on top."""
    out = {
        "ok": True,
        "sandbox": SANDBOX_NOTE,
        "deterministic": bool(run.params.get("deterministic", True)),
    }
    out.update(run.to_dict())
    return out


def _parse_run_params(body: dict) -> dict:
    """Validate + normalize ``POST /api/demo/run`` params (DEMO.md §4).

    Raises :class:`ValueError` (-> HTTP 400) on bad input; otherwise returns the
    kwargs for :func:`observable.run_observable`.
    """
    price_units = body.get("price_units", 5 * 10**6)
    swap_to = body.get("swap_to", "ETH")
    seed = body.get("seed", 42)
    deterministic = body.get("deterministic", True)

    if isinstance(price_units, bool) or not isinstance(price_units, int):
        raise ValueError("price_units must be an integer (USDC base units)")
    if not (_MIN_PRICE_UNITS <= price_units <= _MAX_PRICE_UNITS):
        raise ValueError(
            f"price_units out of range [{_MIN_PRICE_UNITS}, {_MAX_PRICE_UNITS}]"
        )
    if not isinstance(swap_to, str) or swap_to.upper() not in _SWAP_TARGETS:
        raise ValueError(f"swap_to must be one of {sorted(_SWAP_TARGETS)}")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    if not isinstance(deterministic, bool):
        raise ValueError("deterministic must be a boolean")

    return {
        "price_units": price_units,
        "swap_to": swap_to.upper(),
        "seed": seed,
        "deterministic": deterministic,
    }


# Minimal, dependency-free page used only when neither ``demo.html`` nor
# ``web/lab/swap.html`` is on disk (so the server is never "broken"). It drives
# the real ``/api/demo/run`` endpoint and prints the timeline — the frontend
# builder's richer ``demo.html`` supersedes it the moment it lands. Vanilla ES +
# inline styles in the shared.css palette; no external requests.
_FALLBACK_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Switchboard — Live Agentic-Escrow Demo (MOCK CHAIN)</title>
<style>
  :root { --gold:#d4a853; --emerald:#4ecb71; --violet:#a78bfa; --cyan:#67d4e0; --bg:#0e1116; --panel:#161b22; --ink:#e6edf3; --mut:#8b949e; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.5 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace; }
  header { padding:20px 24px; border-bottom:1px solid #21262d; }
  h1 { margin:0 0 4px; font-size:18px; color:var(--gold); }
  .sandbox { color:var(--mut); font-size:12px; max-width:70ch; }
  main { padding:24px; }
  button { background:var(--gold); color:#0e1116; border:0; padding:10px 18px; border-radius:6px; font:inherit; font-weight:700; cursor:pointer; }
  button:disabled { opacity:.5; cursor:default; }
  #timeline { margin-top:20px; display:flex; flex-direction:column; gap:8px; }
  .ev { background:var(--panel); border:1px solid #21262d; border-left:3px solid var(--cyan); border-radius:6px; padding:10px 14px; overflow-x:auto; }
  .ev .h { display:flex; gap:10px; align-items:baseline; flex-wrap:wrap; }
  .ev .seq { color:var(--mut); }
  .ev .step { color:var(--gold); font-weight:700; }
  .ev .actor { color:var(--violet); }
  .ev .det { color:var(--ink); margin-top:4px; }
  .ev .meta { color:var(--mut); font-size:12px; margin-top:4px; }
  .ev[data-phase="swap"] { border-left-color:var(--violet); }
  .ev[data-phase="setup"] { border-left-color:var(--mut); }
  #summary { margin-top:20px; color:var(--emerald); white-space:pre-wrap; }
  .err { color:#f85149; }
</style></head>
<body>
<header>
  <h1>Live Agentic-Escrow Demo</h1>
  <div class="sandbox" id="sandbox">SIMULATED / MOCK CHAIN — not a live network. No real ETH, RPC, or funds. Synthetic agents only.</div>
</header>
<main>
  <button id="run">Run A&rarr;B payment</button>
  <div id="timeline"></div>
  <div id="summary"></div>
</main>
<script>
const $ = (s) => document.querySelector(s);
const fmtAmt = (a) => a ? `${a.display} ${a.token}` : "—";
async function run() {
  const btn = $("#run"); btn.disabled = true;
  $("#timeline").innerHTML = ""; $("#summary").textContent = "running…";
  try {
    const res = await fetch("/api/demo/run", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ price_units: 5000000, swap_to: "ETH", seed: 42 }),
    });
    const env = await res.json();
    if (!env.ok) { $("#summary").innerHTML = `<span class="err">${env.error}</span>`; return; }
    $("#sandbox").textContent = env.sandbox;
    for (const e of env.timeline) {
      const el = document.createElement("div");
      el.className = "ev"; el.dataset.phase = e.phase;
      el.innerHTML =
        `<div class="h"><span class="seq">#${e.seq}</span>` +
        `<span class="step">${e.step}</span>` +
        `<span class="actor">${e.actor}${e.peer ? " → " + e.peer : ""}</span></div>` +
        `<div class="det">${e.title} — ${e.detail}</div>` +
        `<div class="meta">block ${e.block} · move ${fmtAmt(e.amount)}` +
        ` · A ${fmtAmt(e.balances.A)} · B ${fmtAmt(e.balances.B)}` +
        (e.escrow ? ` · escrow ${e.escrow.state}` : "") + `</div>`;
      $("#timeline").appendChild(el);
    }
    const s = env.summary;
    $("#summary").textContent =
      `settled=${s.settled}  swap_routed=${s.swap_routed}  escrow=${s.escrow_state}\\n` +
      `spent=${s.spend_summary.total_spent_wei} units  swap out=${s.swap.amountOut} ${s.swap.tokenOut}  blocks=${s.blocks_mined}`;
  } catch (err) {
    $("#summary").innerHTML = `<span class="err">${err}</span>`;
  } finally { btn.disabled = false; }
}
$("#run").addEventListener("click", run);
run();
</script>
</body></html>"""


class DemoHandler(BaseHTTPRequestHandler):
    """Serves the demo page + the ``/api/demo/*`` JSON API from DEMO.md.

    The server keeps the most-recent :class:`observable.DemoRun` on the server
    class (``server.last_run``) so ``GET /api/demo/state`` can replay it, and an
    optional :class:`observable.StepCursor` (``server.cursor``) for stepwise mode.
    """

    server_version = "SwitchboardAgenticDemo/1.0"

    # ── routing ──────────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/":
            self._serve_page()
        elif path == "/api/demo/state":
            self._handle_state()
        elif path == "/healthz":
            self._send_json({"ok": True})
        else:
            self._send_json({"ok": False, "error": f"not found: {path}"}, status=404)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/api/demo/run":
            self._handle_run(self._read_json_body())
        elif path == "/api/demo/step":
            self._handle_step(self._read_json_body())
        else:
            self._send_json({"ok": False, "error": f"not found: {path}"}, status=404)

    def do_HEAD(self) -> None:
        # Mirror do_GET routing for liveness/curl -I; the helpers skip the body
        # for HEAD (``self.command`` is "HEAD").
        self.do_GET()

    # ── transport helpers (FIXED signatures) ─────────────────────────────────

    def _send_json(self, obj: dict, status: int = 200) -> None:
        """Serialize ``obj`` as JSON and write it with the given status."""
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Permissive same-origin: the page is served from this same server, but
        # allow a dev page on another localhost port to call the API too.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200) -> None:
        """Write an HTML (``text/html; charset=utf-8``) response."""
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_json_body(self) -> dict:
        """Read + parse the request JSON body. Returns ``{}`` for an empty body;
        on malformed JSON, returns ``{}`` (handlers validate fields)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    # ── handlers (FIXED names) ────────────────────────────────────────────────

    def _serve_page(self) -> None:
        """Resolve + serve the demo page per DEMO.md §4 (``demo.html`` if present,
        else ``web/lab/swap.html``). Uses ``_PAGE_CANDIDATES``."""
        for candidate in _PAGE_CANDIDATES:
            try:
                html = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            self._send_html(html)
            return
        # Neither page is on disk yet (e.g. the frontend builder's file is not in
        # this tree): still return a valid, self-describing 200 so the API host is
        # never broken and the sandbox framing is always visible.
        self._send_html(_FALLBACK_PAGE)

    def _handle_run(self, body: dict) -> None:
        """``POST /api/demo/run`` — parse ``price_units`` / ``swap_to`` / ``seed``
        / ``deterministic`` from ``body``, call ``observable.run_observable(...)``,
        stash it on ``self.server.last_run``, and reply with the run envelope
        (``run.to_dict()`` merged with ``sandbox`` + ``deterministic``).
        On error reply ``{"ok": False, "error": ...}`` with status 400/500."""
        try:
            params = _parse_run_params(body)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
            return
        try:
            run = run_observable(**params)
        except Exception as exc:  # noqa: BLE001 - surface any orchestration error as 500
            self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)
            return
        # Cache for GET /api/demo/state and seed a fresh StepCursor for step mode.
        self.server.last_run = run  # type: ignore[attr-defined]
        self.server.cursor = StepCursor(run)  # type: ignore[attr-defined]
        self._send_json(_envelope(run))

    def _handle_state(self) -> None:
        """``GET /api/demo/state`` — reply with ``self.server.last_run`` if a run
        has happened, else perform + cache a default seeded ``run_observable()``
        and reply with its envelope."""
        run: DemoRun | None = getattr(self.server, "last_run", None)
        if run is None:
            try:
                run = run_observable()  # default seeded run (seed=42, deterministic)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)
                return
            self.server.last_run = run  # type: ignore[attr-defined]
            self.server.cursor = StepCursor(run)  # type: ignore[attr-defined]
        self._send_json(_envelope(run))

    def _handle_step(self, body: dict) -> None:
        """``POST /api/demo/step`` — server-driven stepwise mode backed by
        ``observable.StepCursor`` on ``self.server.cursor``. ``{"reset": true}``
        rewinds to before ``setup``; otherwise advances one event. Reply
        ``{"ok", "done", "index", "event"}``."""
        cursor: StepCursor | None = getattr(self.server, "cursor", None)
        if cursor is None:
            # No run yet: bootstrap a default seeded run so /step works standalone.
            try:
                run = run_observable()
            except Exception as exc:  # noqa: BLE001
                self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)
                return
            self.server.last_run = run  # type: ignore[attr-defined]
            cursor = StepCursor(run)
            self.server.cursor = cursor  # type: ignore[attr-defined]

        if body.get("reset"):
            cursor.reset()
            self._send_json({"ok": True, "done": False, "index": cursor.index, "event": None})
            return

        done, index, event = cursor.advance()
        self._send_json({
            "ok": True,
            "done": done,
            "index": index,
            "event": event.to_dict() if event is not None else None,
        })

    # Keep the demo console quiet-ish; backend builder may route through logging.
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib name
        sys.stderr.write("[demo] " + (fmt % args) + "\n")


def make_server(host: str = "127.0.0.1", port: int = 8402) -> ThreadingHTTPServer:
    """Build (but do not start) the demo HTTP server.

    Initializes the per-server state the handlers rely on:
    ``server.last_run`` (Optional[DemoRun]) and ``server.cursor``
    (Optional[StepCursor]). Tests bind ``port=0`` for an ephemeral port.
    """
    server = ThreadingHTTPServer((host, port), DemoHandler)
    # Per-server state the handlers read/write (DEMO.md §4 / server.py contract).
    server.last_run = None  # type: ignore[attr-defined]  # Optional[DemoRun]
    server.cursor = None    # type: ignore[attr-defined]  # Optional[StepCursor]
    return server


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: parse ``--host`` / ``--port`` / ``--open``, build via
    :func:`make_server`, print the sandbox banner + URL, and ``serve_forever``."""
    parser = argparse.ArgumentParser(description="Live agentic-escrow demo server (MOCK CHAIN)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8402)
    parser.add_argument("--open", action="store_true", help="open the demo page in a browser")
    args = parser.parse_args(argv)

    server = make_server(args.host, args.port)
    host, port = server.server_address[0], server.server_address[1]
    url = f"http://{host}:{port}/"

    bar = "─" * 72
    print(bar)
    print("  SWITCHBOARD — LIVE AGENTIC-ESCROW DEMO")
    print(f"  {SANDBOX_NOTE}")
    print("  Demo by Pattermesh (Patty / P. Sundaram) on kcolbchain/switchboard.")
    print(bar)
    print(f"  serving on  {url}")
    print(f"  API         POST {url}api/demo/run   GET {url}api/demo/state")
    print( "  stop        Ctrl-C")
    print(bar, flush=True)

    if args.open:  # pragma: no cover - interactive convenience only
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\n[demo] shutting down", file=sys.stderr)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
