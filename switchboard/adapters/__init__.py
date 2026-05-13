"""Adapters that bridge Switchboard payment envelopes to external protocols."""

from .a2a_x402 import from_a2a_response, to_a2a_request

__all__ = ["from_a2a_response", "to_a2a_request"]
