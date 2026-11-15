"""Read-only bond discovery, policy-source loading and evidence routing.

This package intentionally does not register MCP tools or execute trades.
"""

__version__ = "0.1.0"

from .catalog import PolicySourceSnapshot, SnapshotError
from .pipeline import DiscoveryReport, DiscoveryService
from .routes import EvidenceRoute, plan_evidence_route

__all__ = ["PolicySourceSnapshot", "SnapshotError", "DiscoveryReport", "DiscoveryService", "EvidenceRoute", "plan_evidence_route"]
