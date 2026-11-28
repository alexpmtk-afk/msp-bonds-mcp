"""Read-only, bounded Gmail discovery for VTB report attachments.

The pipeline is dependency-injected for synthetic offline tests. It never
persists message state, attachment bytes, documents, or import-journal entries.
"""
from __future__ import annotations

from collections import Counter
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
import json
import re

from .archive_reconciliation import match_archive_candidate, resolve_target_folder
from .classifier import ClassificationResult, classify_document
from .mail_routing import route_message_labels
from .text_extraction import MAX_INPUT_BYTES, extract_document_text

MAX_PAGE_SIZE = 100
MAX_SCAN_PAGES = 20
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
_SUPPORTED_EXTENSIONS = {".pdf", ".xlsx"}
_SUPPORTED_MIME_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/octet-stream",
}
_OUTCOMES = ("ALREADY_ARCHIVED", "NEW", "NEEDS_REVIEW", "ERROR")


def normalize_sender_allowlist(values: str | Iterable[str]) -> frozenset[str]:
    """Normalize exact sender addresses; domains and display names are not trusted."""
    if isinstance(values, str):
        source = [item.strip() for item in values.replace(";", ",").split(",") if item.strip()]
    else:
        source = [str(item).strip() for item in values if str(item).strip()]
    addresses = set()
    for item in source:
        addresses.update(
            address.casefold().strip()
            for _, address in getaddresses([item])
            if (
                address
                and address.count("@") == 1
                and "." in address.rsplit("@", 1)[1]
                and not any(character.isspace() for character in address)
            )
        )
    return frozenset(addresses)


def _sender_status(header: str, allowlist: frozenset[str]) -> tuple[bool, str]:
    if not allowlist:
        return False, "SENDER_ALLOWLIST_NOT_CONFIGURED"
    senders = {address.casefold().strip() for _, address in getaddresses([header]) if address}
    if senders & allowlist:
        return True, ""
    return False, "SENDER_NOT_ALLOWLISTED"


def _potential_report(attachment: Mapping[str, Any]) -> bool:
    filename = str(attachment.get("filename") or "")
    mime_type = str(attachment.get("mime_type") or "").casefold().split(";", 1)[0].strip()
    extension = Path(filename).suffix.casefold()
    return extension in _SUPPORTED_EXTENSIONS or mime_type in _SUPPORTED_MIME_TYPES


def load_archive_registry(path: str | Path) -> tuple[list[dict[str, Any]], bool, str]:
    """Read the private registry; only a complete, fully verified registry can prove NEW."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        records = payload.get("records")
        if not isinstance(records, list):
            return [], False, "ARCHIVE_REGISTRY_INVALID"
        valid_rows = [row for row in records if isinstance(row, dict)]
        complete = (
            payload.get("status") == "OK"
            and not payload.get("errors")
            and payload.get("total") == len(valid_rows)
            and len(valid_rows) > 0
            and all(
                row.get("drive_verification") == "MATCH"
                and row.get("drive_file_id")
                and row.get("drive_parent_folder_id")
                for row in valid_rows
            )
        )
        return valid_rows, complete, "" if complete else "ARCHIVE_REGISTRY_UNVERIFIED"
    except (OSError, ValueError, TypeError, AttributeError):
        return [], False, "ARCHIVE_REGISTRY_UNAVAILABLE"


def _base_result(message_id: str, attachment: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "gmail_message_id": message_id,
        "gmail_attachment_id": attachment.get("attachment_id") or None,
        "part_id": attachment.get("part_id") or None,
        "filename": attachment.get("filename") or "attachment.bin",
        "size_bytes": attachment.get("size"),
        "mime_type": attachment.get("mime_type") or "",
        "status": "NEEDS_REVIEW",
        "category": None,
        "account_ids": [],
        "period_start": None,
        "period_end": None,
        "report_date": None,
        "sha256": None,
        "confidence": 0.0,
        "reason_codes": [],
        "archive_refs": [],
        "target_folder_id": None,
    }


def _result_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts = {status: 0 for status in _OUTCOMES}
    for result in results:
        status = result.get("status")
        if status in counts:
            counts[status] += 1
    return counts


def scan_gmail_reports(
    *,
    search_page: Callable[..., Mapping[str, Any]],
    get_message: Callable[[str], Mapping[str, Any]],
    get_attachment: Callable[..., Any],
    archive_records: Iterable[Mapping[str, Any]],
    archive_complete: bool,
    allowed_senders: Iterable[str],
    broker_label_id: str,
    corporate_action_label_id: str,
    page_size: int = 50,
    max_pages: int = 10,
    page_token: str = "",
    scan_sender_mailbox: bool = False,
) -> dict[str, Any]:
    """Scan Gmail by label or trusted senders; verify sender and attachment contents."""
    if not isinstance(page_size, int) or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be from 1 to {MAX_PAGE_SIZE}")
    if not isinstance(max_pages, int) or not 1 <= max_pages <= MAX_SCAN_PAGES:
        raise ValueError(f"max_pages must be from 1 to {MAX_SCAN_PAGES}")
    if not broker_label_id or not corporate_action_label_id:
        raise ValueError("Both Gmail label IDs must be verified")
    records = [dict(row) for row in archive_records]
    trusted_senders = normalize_sender_allowlist(allowed_senders)
    if scan_sender_mailbox and not trusted_senders:
        raise ValueError("SENDER_ALLOWLIST_NOT_CONFIGURED")
    if scan_sender_mailbox and any(
        re.fullmatch(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", addr) is None
        for addr in trusted_senders
    ):
        raise ValueError("SENDER_ADDRESS_INVALID_FOR_SEARCH")
    sender_query = (
        "has:attachment " + (
            "from:" + next(iter(trusted_senders)) if len(trusted_senders) == 1
            else "{" + " ".join("from:" + addr for addr in sorted(trusted_senders)) + "}"
        )
    ) if scan_sender_mailbox else ""
    seen_message_ids: set[str] = set()
    seen_attachments: set[tuple[str, str]] = set()
    seen_hashes: dict[str, str] = {}
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    category_counts: Counter[str] = Counter()
    skipped_notification = 0
    skipped_notification_attachments = 0
    skipped_non_target_attachments = 0
    retry_page_token = None
    skipped_unrelated = 0
    duplicate_messages = 0
    messages_seen = 0
    attachments_checked = 0
    current_token = page_token
    token_history: set[str] = set()
    next_page_token = ""

    for _ in range(max_pages):
        if current_token and current_token in token_history:
            errors.append({"error_type": "PaginationError", "reason_code": "REPEATED_PAGE_TOKEN"})
            next_page_token = ""
            break
        if current_token:
            token_history.add(current_token)
        try:
            page = (search_page(query=sender_query, label_id="", page_size=page_size,
                                page_token=current_token)
                    if scan_sender_mailbox else
                    search_page(label_id=broker_label_id, page_size=page_size,
                                page_token=current_token))
        except Exception as exc:
            errors.append({"error_type": type(exc).__name__, "reason_code": "GMAIL_SEARCH_FAILED"})
            retry_page_token = current_token
            break
        message_rows = page.get("messages") or []
        next_page_token = str(page.get("next_page_token") or "")
        for row in message_rows:
            message_id = str((row or {}).get("id") or "")
            if not message_id:
                errors.append({"error_type": "MessageMetadataError", "reason_code": "MESSAGE_ID_MISSING"})
                continue
            if message_id in seen_message_ids:
                duplicate_messages += 1
                continue
            seen_message_ids.add(message_id)
            messages_seen += 1
            try:
                message = get_message(message_id)
            except Exception as exc:
                results.append({
                    "gmail_message_id": message_id,
                    "gmail_attachment_id": None,
                    "part_id": None,
                    "filename": None,
                    "size_bytes": None,
                    "mime_type": None,
                    "status": "ERROR",
                    "category": None,
                    "account_ids": [],
                    "period_start": None,
                    "period_end": None,
                    "report_date": None,
                    "sha256": None,
                    "confidence": 0.0,
                    "reason_codes": ["GMAIL_MESSAGE_READ_FAILED", type(exc).__name__],
                    "archive_refs": [],
                    "target_folder_id": None,
                })
                continue

            route = route_message_labels(
                message.get("labels") or [],
                broker_label_id=broker_label_id,
                corporate_action_label_id=corporate_action_label_id,
            )
            if route.action == "SKIP_NOTIFICATION_ONLY":
                skipped_notification += 1
                continue
            # A trusted VTB sender may have no broker label at all.
            # The sender header is checked independently of Gmail search results.
            from_trusted_sender = _sender_status(
                str((message.get("headers") or {}).get("from") or ""),
                trusted_senders,
            )[0]
            if route.action != "SCAN_CANDIDATE" and not (
                scan_sender_mailbox and route.action == "SKIP_UNRELATED"
                and from_trusted_sender
            ):
                skipped_unrelated += 1
                continue

            attachments = message.get("attachments") or []
            if not attachments:
                continue
            sender_ok, sender_reason = _sender_status(
                str((message.get("headers") or {}).get("from") or ""),
                trusted_senders,
            )
            for attachment in attachments:
                if not isinstance(attachment, Mapping) or not _potential_report(attachment):
                    continue
                part_id = str(attachment.get("part_id") or "")
                attachment_id = str(attachment.get("attachment_id") or part_id)
                if not part_id:
                    item = _base_result(message_id, attachment)
                    item["status"] = "ERROR"
                    item["reason_codes"] = ["ATTACHMENT_PART_ID_MISSING"]
                    results.append(item)
                    attachments_checked += 1
                    continue
                attachment_key = (message_id, attachment_id)
                if attachment_key in seen_attachments:
                    continue
                seen_attachments.add(attachment_key)
                attachments_checked += 1
                item = _base_result(message_id, attachment)
                if not sender_ok:
                    item["reason_codes"] = [sender_reason]
                    results.append(item)
                    continue
                raw_size = attachment.get("size")
                if isinstance(raw_size, bool):
                    item["reason_codes"] = ["ATTACHMENT_SIZE_INVALID"]
                    results.append(item)
                    continue
                try:
                    declared_size = int(raw_size)
                except (TypeError, ValueError):
                    item["reason_codes"] = ["ATTACHMENT_SIZE_UNAVAILABLE"]
                    results.append(item)
                    continue
                if declared_size < 0:
                    item["reason_codes"] = ["ATTACHMENT_SIZE_INVALID"]
                    results.append(item)
                    continue
                if declared_size > MAX_ATTACHMENT_BYTES or declared_size > MAX_INPUT_BYTES:
                    item["reason_codes"] = ["ATTACHMENT_SIZE_LIMIT_EXCEEDED"]
                    results.append(item)
                    continue
                try:
                    downloaded = get_attachment(message_id, part_id, MAX_ATTACHMENT_BYTES)
                    content = downloaded.get("data") if isinstance(downloaded, Mapping) else downloaded
                    if not isinstance(content, bytes):
                        raise TypeError("Attachment reader did not return bytes")
                    if len(content) != declared_size:
                        raise ValueError("Attachment size differs from Gmail metadata")
                    if len(content) > MAX_ATTACHMENT_BYTES or len(content) > MAX_INPUT_BYTES:
                        raise ValueError("Attachment exceeds configured processing limit")
                except Exception as exc:
                    item["status"] = "ERROR"
                    item["reason_codes"] = ["GMAIL_ATTACHMENT_READ_FAILED", type(exc).__name__]
                    results.append(item)
                    continue

                item["size_bytes"] = len(content)
                extraction = extract_document_text(
                    content, filename=item["filename"], mime_type=item["mime_type"]
                )
                if extraction.status != "OK" or not extraction.text:
                    item["reason_codes"] = ["DOCUMENT_TEXT_UNAVAILABLE"]
                    results.append(item)
                    continue

                candidate: ClassificationResult = classify_document(
                    extraction.text,
                    content,
                    filename=item["filename"],
                    mime_type=item["mime_type"],
                )
                item.update({
                    "category": candidate.category,
                    "account_ids": list(candidate.account_ids),
                    "period_start": candidate.period_start,
                    "period_end": candidate.period_end,
                    "report_date": candidate.report_date,
                    "sha256": candidate.sha256,
                    "confidence": candidate.confidence,
                    "reason_codes": list(candidate.reasons),
                })
                if candidate.category:
                    category_counts[candidate.category] += 1

                archive_match = match_archive_candidate(candidate, records)
                target = resolve_target_folder(candidate, records)
                item["archive_refs"] = archive_match.get("archive_refs", [])
                if (
                    archive_match.get("status") == "EXACT_DUPLICATE"
                    or target.get("status") == "EXACT_DUPLICATE"
                ):
                    item["status"] = "ALREADY_ARCHIVED"
                    item["archive_refs"] = (
                        target.get("archive_refs", []) or item["archive_refs"]
                    )
                    if target.get("status") != "EXACT_DUPLICATE":
                        item["reason_codes"] = ["ARCHIVED_LOCATION_NOT_VERIFIED"]
                    results.append(item)
                    continue
                if archive_match.get("status") == "EXACT_HASH_UNVERIFIED":
                    item["reason_codes"] = ["ARCHIVE_HASH_NOT_VERIFIED"]
                    results.append(item)
                    continue
                # An explicit notification classification is not a target report.
                # Keep exact-hash archive evidence above; never skip ambiguous titles.
                if candidate.category is None and "CORPORATE_ACTION_NOTIFICATION" in candidate.reasons:
                    skipped_notification_attachments += 1
                    continue
                if candidate.category is None and "ORDER_EXECUTION_REPORT" in candidate.reasons:
                    skipped_non_target_attachments += 1
                    continue
                if archive_match.get("status") == "POSSIBLE_VERSION":
                    item["reason_codes"] = ["POSSIBLE_VERSION_CONFLICT"]
                    results.append(item)
                    continue
                if candidate.status != "CLASSIFIED":
                    item["reason_codes"] = list(candidate.reasons) or ["CLASSIFICATION_NEEDS_REVIEW"]
                    results.append(item)
                    continue
                if not archive_complete:
                    item["reason_codes"] = ["ARCHIVE_REGISTRY_UNVERIFIED"]
                    results.append(item)
                    continue
                if target.get("status") != "READY" or not target.get("target_folder_id"):
                    item["reason_codes"] = [str(target.get("reason") or "TARGET_FOLDER_UNRESOLVED")]
                    results.append(item)
                    continue
                prior_message = seen_hashes.get(candidate.sha256)
                if prior_message:
                    item["reason_codes"] = ["SAME_DOCUMENT_IN_MULTIPLE_MESSAGES_IN_THIS_SCAN"]
                    item["duplicate_of_message_id"] = prior_message
                    results.append(item)
                    continue
                seen_hashes[candidate.sha256] = message_id
                item["status"] = "NEW"
                item["target_folder_id"] = target["target_folder_id"]
                item["reason_codes"] = (
                    ["OVERLAPPING_PERIOD_IS_NOT_A_DUPLICATE"]
                    if archive_match.get("status") == "OVERLAPPING_PERIOD" else []
                )
                results.append(item)

        if not next_page_token:
            current_token = ""
            break
        current_token = next_page_token

    partial = bool(current_token and next_page_token)
    if partial:
        next_page_token = current_token
    elif not partial:
        next_page_token = ""
    has_item_errors = any(result.get("status") == "ERROR" for result in results)
    status = "ERROR" if errors and not results else (
        "PARTIAL" if errors or next_page_token or has_item_errors else "OK"
    )
    return {
        "status": status,
        "messages_seen": messages_seen,
        "duplicate_messages_skipped": duplicate_messages,
        "attachments_checked": attachments_checked,
        "category_counts": {category: int(category_counts[category]) for category in (
            "Брокерский отчёт",
            "Выписка по счёту депо",
            "Отчёт о выплатах по ценным бумагам",
        )},
        "result_counts": _result_counts(results),
        "skipped_notification_only": skipped_notification,
        "skipped_notification_attachments": skipped_notification_attachments,
        "skipped_non_target_attachments": skipped_non_target_attachments,
        "reason_counts": dict(Counter(reason for row in results for reason in row.get("reason_codes", []))),
        "retry_page_token": retry_page_token,
        "skipped_unrelated": skipped_unrelated,
        "results": results,
        "errors": errors,
        "next_page_token": next_page_token,
        "archive_registry_complete": archive_complete,
    }
