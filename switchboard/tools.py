"""Tool registry — single source of truth for switchboard callable tools.

Unit ⑰ of the agent-wallet-multitoken-settlement plan.

This module defines every tool that agents may call, with:
  - ``name``        — stable identifier used by MCP and CLI alike
  - ``description`` — human/agent-readable explanation
  - ``schema``      — JSON-Schema object describing the input parameters
  - ``op``          — which wallet/escrow operation the tool maps to
  - ``policy``      — access-policy constraints (used by the access-policy engine)

Both ``mcp_server.py`` and ``cli.py`` read from :func:`get_registry` — they do
NOT duplicate schemas or policy rules.  This is the DRY source of truth.

Extending the registry
-----------------------
Add an entry to :data:`TOOL_DEFINITIONS` and (optionally) add it to
``switchboard/registry.json`` under the ``"tools"`` key.  The MCP server and
CLI pick it up automatically.

Access-policy interface
------------------------
The access-policy engine (Unit ⑲, ``switchboard/access_policy.py``) is built
in parallel and is **not** present in this tree.  We define the thin interface
we expect here so the caller can wire the real implementation at integration
time.

Expected interface::

    from switchboard.access_policy import AccessPolicy, Decision

    policy_engine: AccessPolicy   # passed in at server/CLI construction time
    decision: Decision = policy_engine.check(agent_id="0xAgent", action="pay")
    if decision.denied:
        raise PermissionError(decision.reason)

``AccessPolicy`` is a ``typing.Protocol``::

    class AccessPolicy(Protocol):
        def check(self, agent_id: str, action: str) -> Decision: ...

``Decision`` is a dataclass::

    @dataclass(frozen=True)
    class Decision:
        denied: bool
        reason: str | None = None   # machine-readable reason when denied

If no ``AccessPolicy`` is provided, a permissive stub (``AllowAllPolicy``) is
used so the server/CLI work standalone.  Wire the real one by passing it at
construction::

    server = MCPServer(wallet=wallet, delegation=delegation, access_policy=real_engine)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Access-policy interface (thin seam; real impl arrives in Unit ⑲)
# ---------------------------------------------------------------------------

class AccessPolicy:
    """Protocol that the access-policy engine must satisfy.

    The real implementation (Unit ⑲) replaces this at integration time.
    Caller passes it as ``access_policy=`` to ``MCPServer`` and ``CLI``.
    """

    def check(self, agent_id: str, action: str) -> "Decision":  # noqa: F821
        raise NotImplementedError


@dataclass(frozen=True)
class Decision:
    """Result of an access-policy check."""

    denied: bool
    reason: Optional[str] = None


class AllowAllPolicy:
    """Permissive stub — used when no real policy engine is wired in.

    Every action is allowed; integration replaces this with the Unit ⑲ impl.
    """

    def check(self, agent_id: str, action: str) -> Decision:
        return Decision(denied=False, reason=None)


# ---------------------------------------------------------------------------
# Tool definition dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolDef:
    """Describes one callable tool.

    Parameters
    ----------
    name:
        Stable, snake_case identifier.  Used as the MCP tool name and the
        CLI sub-command name.
    description:
        Short human/agent-readable explanation of what the tool does.
    schema:
        JSON-Schema ``"object"`` describing the tool's input parameters.
        The ``"required"`` list must enumerate every non-optional field.
    op:
        The logical wallet/escrow operation this tool maps to.  Used by
        the dispatcher to route calls to the right method.
    policy:
        Access-policy metadata consumed by the access-policy engine.
        ``required_tier`` — minimum tier (default ``"standard"``).
        ``rate_class``    — rate-limiting bucket (default ``"default"``).
    """

    name: str
    description: str
    schema: Dict[str, Any]
    op: str
    policy: Dict[str, str] = field(default_factory=lambda: {
        "required_tier": "standard",
        "rate_class": "default",
    })


# ---------------------------------------------------------------------------
# Canonical tool definitions
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: List[ToolDef] = [
    ToolDef(
        name="wallet_balance",
        description=(
            "Return the wallet's token balances on a given chain. "
            "Reports both gross balance and spendable (net of reserve) for "
            "every token the treasury tracks on that chain."
        ),
        schema={
            "type": "object",
            "properties": {
                "session_key": {
                    "type": "string",
                    "description": "Session key ID issued by grant().",
                },
                "chain_id": {
                    "type": "integer",
                    "description": "EVM chain ID (e.g. 1 for mainnet, 84532 for Base Sepolia).",
                },
                "token": {
                    "type": "string",
                    "description": (
                        "Token EVM address (address(0) = native ETH). "
                        "If omitted, returns all tokens on the chain."
                    ),
                },
            },
            "required": ["session_key", "chain_id"],
        },
        op="wallet_balance",
        policy={"required_tier": "standard", "rate_class": "read"},
    ),
    ToolDef(
        name="pay",
        description=(
            "Execute a payment from the agent wallet to a payee. "
            "Enforces the active SpendPolicy (token allowlist, per-tx cap, "
            "daily cap, counterparty allowlist) before co-signing."
        ),
        schema={
            "type": "object",
            "properties": {
                "session_key": {
                    "type": "string",
                    "description": "Session key ID authorising the payment.",
                },
                "chain_id": {"type": "integer", "description": "EVM chain ID."},
                "token": {
                    "type": "string",
                    "description": "Token address (address(0) = ETH).",
                },
                "amount": {
                    "type": "integer",
                    "description": "Amount in token base units (wei / USDC decimals).",
                },
                "payee": {"type": "string", "description": "Payee EVM address."},
                "metadata": {
                    "type": "object",
                    "description": "Optional key-value metadata attached to the payment.",
                },
            },
            "required": ["session_key", "chain_id", "token", "amount", "payee"],
        },
        op="pay",
        policy={"required_tier": "standard", "rate_class": "write"},
    ),
    ToolDef(
        name="create_escrow",
        description=(
            "Create a new on-chain escrow entry for a payment. "
            "Returns an escrow_id the payee uses to confirm or release funds."
        ),
        schema={
            "type": "object",
            "properties": {
                "session_key": {"type": "string"},
                "chain_id": {"type": "integer"},
                "token": {"type": "string"},
                "amount": {"type": "integer"},
                "payee": {"type": "string"},
                "metadata": {"type": "object"},
            },
            "required": ["session_key", "chain_id", "token", "amount", "payee"],
        },
        op="create_escrow",
        policy={"required_tier": "standard", "rate_class": "write"},
    ),
    ToolDef(
        name="confirm_payment",
        description=(
            "Confirm and release an in-flight escrow once the payee has "
            "delivered the agreed service. The escrow is released to the payee."
        ),
        schema={
            "type": "object",
            "properties": {
                "session_key": {"type": "string"},
                "escrow_id": {
                    "type": "string",
                    "description": "The escrow_id returned by create_escrow.",
                },
            },
            "required": ["session_key", "escrow_id"],
        },
        op="confirm_payment",
        policy={"required_tier": "standard", "rate_class": "write"},
    ),
    ToolDef(
        name="request_refund",
        description=(
            "Request a refund of an escrowed payment. Valid only when the "
            "escrow is in the Locked state and the challenge period has passed, "
            "or the payee has agreed to the refund."
        ),
        schema={
            "type": "object",
            "properties": {
                "session_key": {"type": "string"},
                "escrow_id": {"type": "string"},
                "reason": {
                    "type": "string",
                    "description": "Human-readable reason for the refund request.",
                },
            },
            "required": ["session_key", "escrow_id"],
        },
        op="request_refund",
        policy={"required_tier": "standard", "rate_class": "write"},
    ),
    ToolDef(
        name="policy_status",
        description=(
            "Return the current spend-policy status for a session key: "
            "remaining per-tx cap, remaining daily cap, expiry time, "
            "and whether the key is still active."
        ),
        schema={
            "type": "object",
            "properties": {
                "session_key": {"type": "string"},
            },
            "required": ["session_key"],
        },
        op="policy_status",
        policy={"required_tier": "standard", "rate_class": "read"},
    ),
    ToolDef(
        name="escrow_metrics",
        description=(
            "Return aggregated escrow-fulfilment metrics: fill rate, "
            "average time-to-release, timeout rate, refund rate, "
            "challenge rate, and current pending count."
        ),
        schema={
            "type": "object",
            "properties": {
                "session_key": {"type": "string"},
                "chain_id": {
                    "type": "integer",
                    "description": "Filter metrics to this chain. Omit for all chains.",
                },
            },
            "required": ["session_key"],
        },
        op="escrow_metrics",
        policy={"required_tier": "standard", "rate_class": "read"},
    ),
]


# ---------------------------------------------------------------------------
# Registry accessors
# ---------------------------------------------------------------------------

def get_registry() -> List[ToolDef]:
    """Return the canonical list of all registered tools.

    Both ``mcp_server.py`` and ``cli.py`` call this — do not duplicate the
    list in either place.
    """
    return list(TOOL_DEFINITIONS)


def get_tool(name: str) -> Optional[ToolDef]:
    """Look up a single tool by name; return ``None`` if not found."""
    for tool in TOOL_DEFINITIONS:
        if tool.name == name:
            return tool
    return None


def registry_as_json() -> str:
    """Serialise the full registry to a JSON string (useful for debugging)."""
    return json.dumps(
        [
            {
                "name": t.name,
                "description": t.description,
                "schema": t.schema,
                "op": t.op,
                "policy": t.policy,
            }
            for t in TOOL_DEFINITIONS
        ],
        indent=2,
    )


# ---------------------------------------------------------------------------
# Sync registry.json with the tools section
# ---------------------------------------------------------------------------

def sync_registry_json(registry_path: Optional[Path] = None) -> None:
    """Write the ``"tools"`` key in ``switchboard/registry.json``.

    Called once at build/dev time to keep the JSON file in sync with the
    Python definitions.  The JSON file is checked into the repo so that
    non-Python clients (frontend, docs) can read it without importing Python.
    """
    if registry_path is None:
        registry_path = Path(__file__).parent / "registry.json"

    with open(registry_path) as fh:
        data = json.load(fh)

    data["tools"] = [
        {
            "name": t.name,
            "description": t.description,
            "schema": t.schema,
            "op": t.op,
            "policy": t.policy,
        }
        for t in TOOL_DEFINITIONS
    ]

    with open(registry_path, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
