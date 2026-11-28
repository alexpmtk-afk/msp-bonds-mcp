"""Bounded preparation of VTB imports; no cloud mutation or approval bypass."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Callable, Any

from .vtb_mail_intake import _locked_queue, _read_state, _write_state, _now, merge_scan_into_queue


def prepare_import_page(*, queue_path: Path, stage_root: Path,
                        scan_page: Callable[[], dict],
                        load_registry: Callable[[], tuple[list[dict], bool, str]],
                        stage: Callable[[str, str], dict]) -> dict[str, Any]:
    """Persist a bounded page of verified candidates for existing local approval."""
    with _locked_queue(queue_path):
        state = _read_state(queue_path)
        records, complete, _ = load_registry()
        if not complete:
            return {"status": "BLOCKED", "reason": "ARCHIVE_REGISTRY_UNVERIFIED"}
        scan = scan_page()
        if scan.get("status") not in {"OK", "PARTIAL"} or not scan.get("archive_registry_complete"):
            return {"status": "BLOCKED", "reason": "SCAN_OR_REGISTRY_UNVERIFIED",
                    "scan_status": scan.get("status"),
                    "retry_page_token": scan.get("retry_page_token"),
                    "scan_errors": len(scan.get("errors", []))}
        after, after_complete, _ = load_registry()
        if not after_complete or after != records:
            return {"status": "BLOCKED", "reason": "ARCHIVE_REGISTRY_CHANGED_DURING_SCAN"}
        # On an incomplete/failed page do not lose the continuation or claim all mail scanned.
        merged = merge_scan_into_queue(state=state, scan=scan, archive_records=records,
                                      stage=stage, stage_root=stage_root)
        state["scanned_at"] = _now()
        state["last_scan"] = {"status": scan["status"], "counts": scan.get("result_counts", {}), **merged}
        _write_state(queue_path, state)
        queue_counts = dict(Counter(item["state"] for item in state["items"]))
        return {"status": "PREPARED_PARTIAL" if scan["status"] != "OK" or merged["problems"] else "PREPARED",
                "cloud_writes": 0, "local_approval_required": True,
                "messages_seen": scan.get("messages_seen", 0),
                "attachments_checked": scan.get("attachments_checked", 0),
                "result_counts": scan.get("result_counts", {}),
                "reason_counts": scan.get("reason_counts", {}),
                "skipped_notification_attachments": scan.get("skipped_notification_attachments", 0),
                "skipped_non_target_attachments": scan.get("skipped_non_target_attachments", 0),
                "next_page_token": scan.get("next_page_token", ""),
                "retry_page_token": scan.get("retry_page_token"),
                "scan_errors": len(scan.get("errors", [])),
                "queue_counts": queue_counts, **merged}
