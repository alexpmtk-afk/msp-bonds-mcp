"""Read-only bond discovery, policy-source loading and evidence routing.

This package intentionally does not register MCP tools or execute trades.
"""

__version__ = "0.1.0"

from .catalog import PolicySourceSnapshot, SnapshotError
from .pipeline import DiscoveryReport, DiscoveryService

__all__ = ["PolicySourceSnapshot", "SnapshotError", "DiscoveryReport", "DiscoveryService"]
