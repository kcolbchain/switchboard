"""MCP server over stdio — the "connect-your-agent" surface.

Unit ⑮ of the agent-wallet-multitoken-settlement plan.

Implements the Model Context Protocol (MCP) JSON-RPC 2.0 transport over
stdio.  Exposes seven tools read from the ⑰ registry:

    wallet_balance, pay, create_escrow, confirm_payment,
    request_refund, policy_status, escrow_metrics

Every call is gated by:
  1. Session-key lookup (``Delegation`` resolves key_id → ``SessionKey``).
  2. Access-policy check via the ``AccessPolicy`` seam (Unit ⑲).
     If not wired, ``AllowAllPolicy`` is used (see ``switchboard/tools.py``).

MCP wire format
---------------
Requests arrive as newline-delimited JSON objects on stdin::

    {"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}
    {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"pay","arguments":{...}}}

Responses are written to stdout, one JSON object per line.

Initialisation handshake
------------------------
MCP requires an ``initialize`` / ``initialized`` handshake before tools may
be called.  The server responds to ``initialize`` with its capabilities and
marks itself as ready; ``initialized`` is a notification (no response).

Running the server
------------------
    python -m switchboard.mcp_server

or (after console-script registration)::

    switchboard mcp-server

The server reads until EOF, then exits.

Access-policy seam
------------------
See ``switchboard/tools.py`` for the ``AccessPolicy`` / ``Decision`` interface.
Wire the real engine at construction::

    from switchboard.access_policy import RealAccessPolicy
    server = MCPServer(wallet=wallet, delegation=delegation,
                       access_policy=RealAccessPolicy())
    server.serve()
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from switchboard.agent_wallet import AgentWallet, PaymentRequest
from switchboard.delegation import Delegation, PolicyViolation
from switchboard.metrics import (
    AllMetrics,
    EscrowEvent,
    EscrowState,
    WalletOpEvent,
    compute_all_metrics,
)
from switchboard.tools import AllowAllPolicy, Decision, get_registry, get_tool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(req_id: Any, result: Any) -> Dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str, data: Any = None) -> Dict:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


# JSON-RPC error codes
_PARSE_ERROR      = -32700
_INVALID_REQUEST  = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS   = -32602
_INTERNAL_ERROR   = -32603
# Application-level codes (above -32000)
_POLICY_DENIED    = -32001
_SESSION_INVALID  = -32002
_INSUFFICIENT_BAL = -32003


# ---------------------------------------------------------------------------
# MCPServer
# ---------------------------------------------------------------------------

class MCPServer:
    """MCP server that wraps the agent wallet and delegation layer.

    Parameters
    ----------
    wallet:
        The ``AgentWallet`` instance.  If ``None``, a fresh wallet with no
        pre-funded treasury is used (useful in tests).
    delegation:
        The ``Delegation`` layer that manages session keys.  If ``None``,
        a fresh ``Delegation(wallet=wallet)`` is constructed.
    access_policy:
        The access-policy engine (Unit ⑲).  Defaults to ``AllowAllPolicy``.
        Pass the real implementation at integration time.
    metrics_store:
        Optional pre-populated metrics fixture.  In production the server
        would poll the chain; in tests inject a static ``AllMetrics``.
    in_stream / out_stream:
        Override stdin/stdout for testing.
    """

    SERVER_INFO = {
        "name": "switchboard-mcp",
        "version": "0.1.0",
    }

    def __init__(
        self,
        wallet: Optional[AgentWallet] = None,
        delegation: Optional[Delegation] = None,
        access_policy: Optional[Any] = None,
        metrics_store: Optional[AllMetrics] = None,
        in_stream=None,
        out_stream=None,
    ) -> None:
        self._wallet = wallet if wallet is not None else AgentWallet()
        self._delegation = (
            delegation if delegation is not None else Delegation(wallet=self._wallet)
        )
        self._policy_engine = access_policy if access_policy is not None else AllowAllPolicy()
        self._metrics_store: Optional[AllMetrics] = metrics_store
        self._in = in_stream if in_stream is not None else sys.stdin
        self._out = out_stream if out_stream is not None else sys.stdout
        self._initialized = False

    # ------------------------------------------------------------------
    # I/O helpers
    # ------------------------------------------------------------------

    def _write(self, obj: Dict) -> None:
        self._out.write(json.dumps(obj) + "\n")
        self._out.flush()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def serve(self) -> None:
        """Read newline-delimited JSON from stdin; write responses to stdout."""
        for raw in self._in:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError as exc:
                self._write(_err(None, _PARSE_ERROR, f"Parse error: {exc}"))
                continue

            resp = self._dispatch(msg)
            if resp is not None:
                self._write(resp)

    def handle_message(self, msg: Dict) -> Optional[Dict]:
        """Process a single parsed message; return response or None.

        Exposed for unit-testing individual messages without the I/O loop.
        """
        return self._dispatch(msg)

    # ------------------------------------------------------------------
    # Dispatcher
    # ------------------------------------------------------------------

    def _dispatch(self, msg: Dict) -> Optional[Dict]:
        req_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}

        # Notifications (no id) get no response
        is_notification = "id" not in msg

        if method == "initialize":
            return self._handle_initialize(req_id, params)

        if method == "initialized":
            # Client notification — no response
            self._initialized = True
            return None

        if method == "tools/list":
            return self._handle_tools_list(req_id)

        if method == "tools/call":
            if not self._initialized:
                return _err(req_id, _INVALID_REQUEST, "Server not yet initialized")
            return self._handle_tools_call(req_id, params)

        if method == "ping":
            return _ok(req_id, {})

        if is_notification:
            return None

        return _err(req_id, _METHOD_NOT_FOUND, f"Method not found: {method!r}")

    # ------------------------------------------------------------------
    # MCP lifecycle handlers
    # ------------------------------------------------------------------

    def _handle_initialize(self, req_id: Any, params: Dict) -> Dict:
        self._initialized = True
        return _ok(req_id, {
            "protocolVersion": "2024-11-05",
            "serverInfo": self.SERVER_INFO,
            "capabilities": {
                "tools": {},
            },
        })

    def _handle_tools_list(self, req_id: Any) -> Dict:
        tools_payload = [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.schema,
            }
            for t in get_registry()
        ]
        return _ok(req_id, {"tools": tools_payload})

    # ------------------------------------------------------------------
    # tools/call dispatcher
    # ------------------------------------------------------------------

    def _handle_tools_call(self, req_id: Any, params: Dict) -> Dict:
        tool_name = params.get("name")
        arguments: Dict = params.get("arguments") or {}

        tool_def = get_tool(tool_name)
        if tool_def is None:
            return _err(req_id, _METHOD_NOT_FOUND, f"Unknown tool: {tool_name!r}")

        # Resolve session key (all tools require one)
        key_id = arguments.get("session_key")
        if not key_id:
            return _err(req_id, _INVALID_PARAMS, "Missing required field: session_key")

        key = self._resolve_key(key_id)
        if key is None:
            return _err(req_id, _SESSION_INVALID, f"Session key {key_id!r} not found or revoked")

        # Access-policy gate
        decision: Decision = self._policy_engine.check(
            agent_id=key.agent_id, action=tool_def.op
        )
        if decision.denied:
            return _err(
                req_id, _POLICY_DENIED,
                f"Access denied for action {tool_def.op!r}: {decision.reason}",
            )

        # Dispatch to the concrete handler
        try:
            return self._call_tool(req_id, tool_def.op, key, arguments)
        except PolicyViolation as exc:
            return _err(req_id, _POLICY_DENIED, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _err(req_id, _INTERNAL_ERROR, str(exc))

    # ------------------------------------------------------------------
    # Concrete tool handlers
    # ------------------------------------------------------------------

    def _resolve_key(self, key_id: str):
        """Find the SessionKey by key_id across the Delegation's key store."""
        # Delegation stores keys in _keys dict; we need to look up by key_id.
        with self._delegation._lock:
            return self._delegation._keys.get(key_id)

    def _call_tool(self, req_id: Any, op: str, key, args: Dict) -> Dict:
        if op == "wallet_balance":
            return self._op_wallet_balance(req_id, key, args)
        if op == "pay":
            return self._op_pay(req_id, key, args)
        if op == "create_escrow":
            return self._op_create_escrow(req_id, key, args)
        if op == "confirm_payment":
            return self._op_confirm_payment(req_id, key, args)
        if op == "request_refund":
            return self._op_request_refund(req_id, key, args)
        if op == "policy_status":
            return self._op_policy_status(req_id, key, args)
        if op == "escrow_metrics":
            return self._op_escrow_metrics(req_id, key, args)
        return _err(req_id, _METHOD_NOT_FOUND, f"No handler for op: {op!r}")

    def _op_wallet_balance(self, req_id, key, args: Dict) -> Dict:
        chain_id = args.get("chain_id")
        if chain_id is None:
            return _err(req_id, _INVALID_PARAMS, "Missing required field: chain_id")
        token: Optional[str] = args.get("token")

        treasury = self._wallet.treasury
        if token is not None:
            result = {
                "chain_id": chain_id,
                "token": token,
                "balance": treasury.balance(chain_id, token),
                "spendable": treasury.spendable(chain_id, token),
            }
        else:
            # Return all tokens on the chain
            balances = treasury.balances(chain_id)
            result = {
                "chain_id": chain_id,
                "balances": [
                    {
                        "token": tok,
                        "balance": bal,
                        "spendable": treasury.spendable(chain_id, tok),
                    }
                    for tok, bal in balances.items()
                ],
            }
        return _ok(req_id, {"content": [{"type": "text", "text": json.dumps(result)}]})

    def _op_pay(self, req_id, key, args: Dict) -> Dict:
        for field in ("chain_id", "token", "amount", "payee"):
            if field not in args:
                return _err(req_id, _INVALID_PARAMS, f"Missing required field: {field}")

        request = PaymentRequest(
            chain_id=args["chain_id"],
            token=args["token"],
            amount_wei=args["amount"],
            payee=args["payee"],
            metadata=args.get("metadata") or {},
        )
        receipt = self._delegation.pay_with_key(key, request)
        result = {
            "tx_id": receipt.tx_id,
            "chain_id": receipt.chain_id,
            "token": receipt.token,
            "amount": receipt.amount,
            "payee": receipt.payee,
            "escrow_id": receipt.escrow_id,
        }
        return _ok(req_id, {"content": [{"type": "text", "text": json.dumps(result)}]})

    def _op_create_escrow(self, req_id, key, args: Dict) -> Dict:
        for field in ("chain_id", "token", "amount", "payee"):
            if field not in args:
                return _err(req_id, _INVALID_PARAMS, f"Missing required field: {field}")

        # create_escrow creates but does NOT immediately release
        escrow_id = self._wallet._escrow.create_payment(
            chain_id=args["chain_id"],
            token=args["token"],
            amount=args["amount"],
            payee=args["payee"],
        )
        result = {"escrow_id": escrow_id, "status": "Locked"}
        return _ok(req_id, {"content": [{"type": "text", "text": json.dumps(result)}]})

    def _op_confirm_payment(self, req_id, key, args: Dict) -> Dict:
        escrow_id = args.get("escrow_id")
        if not escrow_id:
            return _err(req_id, _INVALID_PARAMS, "Missing required field: escrow_id")

        released = self._wallet._escrow.release_payment(escrow_id)
        result = {"escrow_id": escrow_id, "released": released}
        return _ok(req_id, {"content": [{"type": "text", "text": json.dumps(result)}]})

    def _op_request_refund(self, req_id, key, args: Dict) -> Dict:
        escrow_id = args.get("escrow_id")
        if not escrow_id:
            return _err(req_id, _INVALID_PARAMS, "Missing required field: escrow_id")

        # Seam: if the escrow client supports refund(), call it; otherwise
        # fall back to a "refund requested" status (the real client wires in
        # a proper refund path via IAgentEscrow.requestRefund).
        client = self._wallet._escrow
        if hasattr(client, "request_refund"):
            raw = client.request_refund(escrow_id, args.get("reason", ""))
            ok = bool(raw) if raw is not None else True
        else:
            # No-op stub or mocked client: treat as accepted
            ok = True
        result = {"escrow_id": escrow_id, "refund_requested": ok}
        return _ok(req_id, {"content": [{"type": "text", "text": json.dumps(result)}]})

    def _op_policy_status(self, req_id, key, args: Dict) -> Dict:
        now_utc = datetime.now(timezone.utc)
        expires = key.policy.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)

        active = self._delegation.is_active(key)
        result = {
            "key_id": key.key_id,
            "agent_id": key.agent_id,
            "active": active,
            "expires_at": expires.isoformat(),
            "expired": now_utc >= expires,
            "token_allowlist": key.policy.token_allowlist,
            "per_tx_cap": key.policy.per_tx_cap,
            "daily_cap": key.policy.daily_cap,
            "allowed_counterparties": key.policy.allowed_counterparties,
        }
        return _ok(req_id, {"content": [{"type": "text", "text": json.dumps(result)}]})

    def _op_escrow_metrics(self, req_id, key, args: Dict) -> Dict:
        if self._metrics_store is not None:
            m = self._metrics_store.escrow
            result = {
                "fill_rate": m.fill_rate,
                "timeout_rate": m.timeout_rate,
                "refund_rate": m.refund_rate,
                "challenge_rate": m.challenge_rate,
                "avg_time_to_release_s": m.avg_time_to_release_s,
                "total_count": m.total_count,
                "pending_count": m.pending_count,
            }
        else:
            # No metrics store provided — return empty/zeroed metrics
            result = {
                "fill_rate": None,
                "timeout_rate": None,
                "refund_rate": None,
                "challenge_rate": None,
                "avg_time_to_release_s": None,
                "total_count": 0,
                "pending_count": 0,
            }
        return _ok(req_id, {"content": [{"type": "text", "text": json.dumps(result)}]})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Launch the MCP server against a fresh wallet (CLI / console-script)."""
    wallet = AgentWallet()
    delegation = Delegation(wallet=wallet)
    server = MCPServer(wallet=wallet, delegation=delegation)
    server.serve()


if __name__ == "__main__":
    main()
