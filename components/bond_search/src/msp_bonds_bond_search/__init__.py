"""Read-only bond discovery, policy interpretation and evidence verification.

The package does not register MCP tools, run schedules, modify portfolios or trade.
"""

__version__ = "0.2.0"

from .catalog import PolicySourceSnapshot, SnapshotError
from .evidence import SequentialEvidenceVerifier, VerificationReport
from .google_source import load_from_existing_workspace, load_policy_snapshot
from .official_disclosures import (
    OfficialDisclosureCapture,
    OfficialDisclosureUnavailable,
    capture_interfax_disclosure_html,
    fetch_interfax_disclosure_html,
)
from .pipeline import DiscoveryReport, DiscoveryService
from .policy import CandidateFacts, PolicyAssessment, SelectionPolicy
from .routes import EvidenceRoute, plan_evidence_route
from .yield_calc import CashFlow, bond_xirr, full_entry_cost, xirr

__all__ = [
    "PolicySourceSnapshot", "SnapshotError", "DiscoveryReport", "DiscoveryService",
    "EvidenceRoute", "plan_evidence_route", "SelectionPolicy", "CandidateFacts",
    "PolicyAssessment", "load_policy_snapshot", "load_from_existing_workspace",
    "SequentialEvidenceVerifier", "VerificationReport", "CashFlow", "full_entry_cost",
    "bond_xirr", "xirr",
    "OfficialDisclosureCapture", "OfficialDisclosureUnavailable",
    "capture_interfax_disclosure_html", "fetch_interfax_disclosure_html",
]
