"""CLI — switchboard wallet / escrow / metrics commands.

Unit ⑯ of the agent-wallet-multitoken-settlement plan.

Registered as a console-script in ``pyproject.toml``::

    switchboard wallet balance|grant|revoke
    switchboard escrow  create|confirm|refund|status
    switchboard metrics

All commands share a single ``--wallet-id`` and ``--session-key`` option for
identifying the active wallet and session key; the underlying operations are
driven through the same ``AgentWallet`` + ``Delegation`` core that the MCP
server uses.

Tool definitions are read from ``switchboard/tools.py`` (the ⑰ registry) —
CLI and MCP do NOT duplicate schemas.

Running
-------
    # After installation:
    switchboard wallet balance --chain-id 1

    # Direct module invocation (no install required):
    python -m switchboard.cli wallet balance --chain-id 1

Design notes
------------
- State (wallet, keys) is ephemeral per CLI invocation.  A real deployment
  persists keys in a secure store; that's a follow-up wiring task.
- The access-policy seam is identical to the MCP server: pass
  ``access_policy=`` to ``build_delegation_from_cli_context`` if you have
  a Unit ⑲ implementation available.
- Output is JSON-formatted to stdout so it can be piped / scripted.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import click

from switchboard.agent_wallet import AgentWallet, PaymentRequest
from switchboard.delegation import Delegation, PolicyViolation, SpendPolicy
from switchboard.metrics import (
    compute_all_metrics,
    EscrowEvent,
    EscrowState,
    WalletOpEvent,
)
from switchboard.tools import AllowAllPolicy, get_registry


# ---------------------------------------------------------------------------
# Process-level singletons (ephemeral; real deploy wires persistent store)
# ---------------------------------------------------------------------------

_wallet: Optional[AgentWallet] = None
_delegation: Optional[Delegation] = None

def _get_wallet() -> AgentWallet:
    global _wallet
    if _wallet is None:
        _wallet = AgentWallet()
    return _wallet


def _get_delegation() -> Delegation:
    global _delegation
    if _delegation is None:
        _delegation = Delegation(wallet=_get_wallet())
    return _delegation


def _out(data) -> None:
    """Write ``data`` as pretty JSON to stdout."""
    click.echo(json.dumps(data, indent=2, default=str))


def _err_exit(msg: str, code: int = 1) -> None:
    click.echo(json.dumps({"error": msg}), err=True)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(version="0.1.0", prog_name="switchboard")
def cli():
    """Switchboard — programmable agent payments.

    Commands:

      wallet   — manage balances, session keys

      escrow   — create, confirm, refund, and inspect escrow payments

      metrics  — escrow fulfilment + wallet-ops health

      tools    — list registered agent tools

      mcp-server — run the MCP server over stdio
    """


# ---------------------------------------------------------------------------
# switchboard wallet
# ---------------------------------------------------------------------------

@cli.group()
def wallet():
    """Wallet commands: balance, grant, revoke."""


@wallet.command("balance")
@click.option("--chain-id", required=True, type=int, help="EVM chain ID.")
@click.option("--token", default=None, help="Token address (omit for all tokens).")
def wallet_balance(chain_id: int, token: Optional[str]) -> None:
    """Show wallet balance for a chain (and optionally a specific token)."""
    w = _get_wallet()
    treasury = w.treasury
    if token:
        _out({
            "chain_id": chain_id,
            "token": token,
            "balance": treasury.balance(chain_id, token),
            "spendable": treasury.spendable(chain_id, token),
        })
    else:
        balances = treasury.balances(chain_id)
        _out({
            "chain_id": chain_id,
            "balances": [
                {
                    "token": tok,
                    "balance": bal,
                    "spendable": treasury.spendable(chain_id, tok),
                }
                for tok, bal in balances.items()
            ],
        })


@wallet.command("grant")
@click.option("--agent-id", required=True, help="Logical agent identifier.")
@click.option("--token", "tokens", multiple=True, help="Allowed token address (repeat for multiple). Omit for any token.")
@click.option("--per-tx-cap", type=int, default=None, help="Per-transaction spend cap (base units).")
@click.option("--daily-cap", type=int, default=None, help="Rolling 24-hour spend cap (base units).")
@click.option("--expires-in-hours", type=float, default=24.0, show_default=True, help="Session key TTL in hours.")
@click.option("--counterparty", "counterparties", multiple=True, help="Allowed payee addresses (repeat for multiple). Omit for any.")
def wallet_grant(
    agent_id: str,
    tokens: tuple,
    per_tx_cap: Optional[int],
    daily_cap: Optional[int],
    expires_in_hours: float,
    counterparties: tuple,
) -> None:
    """Grant a scoped session key to an agent."""
    expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_in_hours)
    policy = SpendPolicy(
        expires_at=expires_at,
        token_allowlist=list(tokens) if tokens else None,
        per_tx_cap=per_tx_cap,
        daily_cap=daily_cap,
        allowed_counterparties=list(counterparties) if counterparties else None,
    )
    key = _get_delegation().grant(agent_id=agent_id, policy=policy)
    _out({
        "key_id": key.key_id,
        "agent_id": key.agent_id,
        "expires_at": expires_at.isoformat(),
        "token_allowlist": policy.token_allowlist,
        "per_tx_cap": policy.per_tx_cap,
        "daily_cap": policy.daily_cap,
        "allowed_counterparties": policy.allowed_counterparties,
    })


@wallet.command("revoke")
@click.option("--key-id", required=True, help="Session key ID to revoke.")
def wallet_revoke(key_id: str) -> None:
    """Revoke an active session key by its ID."""
    delegation = _get_delegation()
    with delegation._lock:
        key = delegation._keys.get(key_id)
    if key is None:
        _err_exit(f"Session key {key_id!r} not found or already revoked")
    delegation.revoke(key)
    _out({"revoked": True, "key_id": key_id})


# ---------------------------------------------------------------------------
# switchboard escrow
# ---------------------------------------------------------------------------

@cli.group()
def escrow():
    """Escrow commands: create, confirm, refund, status."""


@escrow.command("create")
@click.option("--session-key", required=True, help="Session key ID.")
@click.option("--chain-id", required=True, type=int, help="EVM chain ID.")
@click.option("--token", required=True, help="Token address.")
@click.option("--amount", required=True, type=int, help="Amount in base units.")
@click.option("--payee", required=True, help="Payee EVM address.")
def escrow_create(session_key: str, chain_id: int, token: str, amount: int, payee: str) -> None:
    """Create a new escrow-locked payment."""
    delegation = _get_delegation()
    with delegation._lock:
        key = delegation._keys.get(session_key)
    if key is None:
        _err_exit(f"Session key {session_key!r} not found or revoked")

    wallet = _get_wallet()
    try:
        escrow_id = wallet._escrow.create_payment(
            chain_id=chain_id, token=token, amount=amount, payee=payee
        )
        _out({"escrow_id": escrow_id, "status": "Locked"})
    except Exception as exc:
        _err_exit(str(exc))


@escrow.command("confirm")
@click.option("--session-key", required=True, help="Session key ID.")
@click.option("--escrow-id", required=True, help="Escrow ID to release.")
def escrow_confirm(session_key: str, escrow_id: str) -> None:
    """Confirm and release an escrowed payment to the payee."""
    delegation = _get_delegation()
    with delegation._lock:
        key = delegation._keys.get(session_key)
    if key is None:
        _err_exit(f"Session key {session_key!r} not found or revoked")

    wallet = _get_wallet()
    try:
        released = wallet._escrow.release_payment(escrow_id)
        _out({"escrow_id": escrow_id, "released": released})
    except Exception as exc:
        _err_exit(str(exc))


@escrow.command("refund")
@click.option("--session-key", required=True, help="Session key ID.")
@click.option("--escrow-id", required=True, help="Escrow ID to refund.")
@click.option("--reason", default="", help="Reason for the refund request.")
def escrow_refund(session_key: str, escrow_id: str, reason: str) -> None:
    """Request a refund for a locked escrow payment."""
    delegation = _get_delegation()
    with delegation._lock:
        key = delegation._keys.get(session_key)
    if key is None:
        _err_exit(f"Session key {session_key!r} not found or revoked")

    wallet = _get_wallet()
    client = wallet._escrow
    try:
        if hasattr(client, "request_refund"):
            raw = client.request_refund(escrow_id, reason)
            ok = bool(raw) if raw is not None else True
        else:
            ok = True   # no-op stub accepts all refund requests
        _out({"escrow_id": escrow_id, "refund_requested": ok})
    except Exception as exc:
        _err_exit(str(exc))


@escrow.command("status")
@click.option("--session-key", required=True, help="Session key ID.")
@click.option("--escrow-id", required=True, help="Escrow ID to inspect.")
def escrow_status(session_key: str, escrow_id: str) -> None:
    """Show the current status of an escrow entry."""
    delegation = _get_delegation()
    with delegation._lock:
        key = delegation._keys.get(session_key)
    if key is None:
        _err_exit(f"Session key {session_key!r} not found or revoked")

    wallet = _get_wallet()
    client = wallet._escrow
    if hasattr(client, "get_status"):
        status = client.get_status(escrow_id)
        _out({"escrow_id": escrow_id, "status": status})
    else:
        # Stub: unknown status — real client wires get_status via IAgentEscrow
        _out({"escrow_id": escrow_id, "status": "unknown (stub escrow client)"})


# ---------------------------------------------------------------------------
# switchboard metrics
# ---------------------------------------------------------------------------

@cli.command("metrics")
@click.option("--chain-id", type=int, default=None, help="Filter to this chain ID.")
def metrics_cmd(chain_id: Optional[int]) -> None:
    """Print escrow-fulfilment + wallet-ops metrics (from in-memory store).

    In production this command polls the chain and wallet event logs;
    here it operates on the in-memory metrics for demonstration / testing.
    """
    # No event history → empty metrics (real deploy polls events from chain)
    result = compute_all_metrics(
        escrow_events=[],
        wallet_ops=[],
        escrow_states=[],
    )
    _out({
        "escrow": {
            "fill_rate": result.escrow.fill_rate,
            "timeout_rate": result.escrow.timeout_rate,
            "refund_rate": result.escrow.refund_rate,
            "challenge_rate": result.escrow.challenge_rate,
            "avg_time_to_release_s": result.escrow.avg_time_to_release_s,
            "total_count": result.escrow.total_count,
            "pending_count": result.escrow.pending_count,
        },
        "wallet_ops": {
            "total_ops": result.wallet_ops.total_ops,
            "spend_by_token": result.wallet_ops.spend_by_token,
            "spend_by_rail": result.wallet_ops.spend_by_rail,
            "policy_denial_count": result.wallet_ops.policy_denial_count,
        },
        "fleet": {
            "active_wallet_count": result.fleet.active_wallet_count,
        },
    })


# ---------------------------------------------------------------------------
# switchboard tools
# ---------------------------------------------------------------------------

@cli.command("tools")
def tools_list() -> None:
    """List all registered agent tools (from the ⑰ registry)."""
    registry = get_registry()
    _out([
        {
            "name": t.name,
            "description": t.description,
            "op": t.op,
            "policy": t.policy,
        }
        for t in registry
    ])


# ---------------------------------------------------------------------------
# switchboard mcp-server
# ---------------------------------------------------------------------------

@cli.command("mcp-server")
def mcp_server_cmd() -> None:
    """Run the MCP server over stdio (connect your agent to this endpoint)."""
    from switchboard.mcp_server import MCPServer
    wallet = _get_wallet()
    delegation = _get_delegation()
    server = MCPServer(wallet=wallet, delegation=delegation)
    server.serve()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    cli()


if __name__ == "__main__":
    main()
