#!/usr/bin/env python3
"""Runnable agentic-payments demo.

    PYTHONPATH=. python examples/agentic_demo/run.py
    PYTHONPATH=. python examples/agentic_demo/run.py --swap-to LUX --json

Agent A pays Agent B for an inference job through the x402 middleware + on-chain
escrow, settles on delivery, then Agent B routes the received USDC through the
SafeSwap orchestrator into a target asset.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow ``python examples/agentic_demo/run.py`` from the repo root without
# requiring PYTHONPATH=. to be set, while still preferring an installed package.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from examples.agentic_demo.scenario import USDC, run_scenario
except ModuleNotFoundError:  # pragma: no cover - fallback when run as a script
    from scenario import USDC, run_scenario  # type: ignore


BAR = "─" * 64


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agentic A2A payment + SafeSwap demo")
    parser.add_argument("--swap-to", default="ETH", choices=["ETH", "LUX", "USDC"],
                        help="asset Agent B rebalances into via SafeSwap")
    parser.add_argument("--price", type=float, default=5.0, help="job price in USDC")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)

    result = run_scenario(
        price_units=int(args.price * USDC),
        swap_to=args.swap_to,
        verbose=not args.json,
    )

    if args.json:
        out = {
            "settled": result.settled,
            "swap_routed": result.swap_routed,
            "escrow": {
                "request_id": result.escrow_request_id,
                "state": result.escrow_state_after_settle,
            },
            "offer": {
                "amount_units": result.offer.amount_wei,
                "currency": result.offer.currency,
                "scheme": result.offer.scheme.value,
                "recipient": result.offer.recipient,
            },
            "swap": result.swap_receipt.to_dict(),
            "spend_summary": result.spend_summary,
        }
        print(json.dumps(out, indent=2))
        return 0 if (result.settled and result.swap_routed) else 1

    print()
    print(BAR)
    print("  AGENTIC PAYMENTS DEMO — A2A pay + escrow settle + SafeSwap route")
    print(BAR)
    for s in result.steps:
        print(f"  {s.step:<13} │ {s.detail}")
    print(BAR)
    r = result.swap_receipt
    print(f"  RESULT")
    print(f"    402 offer -> pay -> settle : {'OK' if result.settled else 'FAILED'} "
          f"(escrow {result.escrow_state_after_settle})")
    print(f"    agentic swap routed        : {'OK' if result.swap_routed else 'FAILED'} "
          f"({r.amount_in} USDC units -> {r.amount_out} {r.token_out} units via {' -> '.join(r.route)})")
    print(f"    total spent                : {result.spend_summary['total_spent_wei'] / USDC} USDC")
    print(BAR)
    print()

    ok = result.settled and result.swap_routed
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
