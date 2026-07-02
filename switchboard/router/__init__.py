"""switchboard.router — pluggable routing strategies for the AgentWallet.

Unit ⑩-⑬ of the agent-wallet-multitoken-settlement spec.

Exports
-------
Router      The composing entry-point: Router.route(request) -> Plan.
Plan        The routing decision: token, rail, wallet.
"""

from switchboard.router.token_selector import TokenSelector, TokenCandidate
from switchboard.router.rail_selector import RailSelector
from switchboard.router.fleet_balancer import FleetBalancer
from switchboard.router.rebalancer import Rebalancer, RebalanceTarget, SwapIntent
from switchboard.router.router import Router, Plan

__all__ = [
    "Router",
    "Plan",
    "TokenSelector",
    "TokenCandidate",
    "RailSelector",
    "FleetBalancer",
    "Rebalancer",
    "RebalanceTarget",
    "SwapIntent",
]
