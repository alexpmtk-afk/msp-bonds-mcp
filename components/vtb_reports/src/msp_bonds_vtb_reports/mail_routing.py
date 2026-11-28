"""Pure label-routing policy for a future importer; no Gmail connection."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class MessageRoute:
    action: str
    has_broker_label: bool
    has_corporate_action_label: bool
    reason: str


def route_message_labels(
    label_ids: Iterable[str],
    *,
    broker_label_id: str,
    corporate_action_label_id: str,
) -> MessageRoute:
    """Only the broker label makes a message a scan candidate.

    A dual-labeled message remains a candidate, but its attachment still must pass
    content classification. A notification-only message is never treated as a report.
    """
    labels = set(label_ids)
    broker = broker_label_id in labels
    corporate = corporate_action_label_id in labels
    if broker:
        return MessageRoute(
            "SCAN_CANDIDATE", True, corporate,
            "Broker label present; classify attachment content before import",
        )
    if corporate:
        return MessageRoute(
            "SKIP_NOTIFICATION_ONLY", False, True,
            "Corporate-action notification label without broker label",
        )
    return MessageRoute("SKIP_UNRELATED", False, False, "Neither target label is present")
