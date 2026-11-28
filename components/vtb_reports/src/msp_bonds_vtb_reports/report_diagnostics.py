"""Read-only VTB attachment diagnostics, restricted to structural evidence."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Mapping

from . import classifier
from .gmail_discovery import MAX_ATTACHMENT_BYTES, _potential_report, _sender_status, normalize_sender_allowlist
from .text_extraction import MAX_INPUT_BYTES, extract_document_text

# The output contains only these fixed labels, never substrings of document text.
_WORDS = {
    "REPORT": r"отчет(?:а|у|е|ом|ы|ов)?",
    "EXECUTION": r"исполн(?:ение|ения|ению|ении|ением|ен|ено)",
    "ORDER": r"поручени(?:е|я|ю|и|ем|й)",
    "INVENTORY": r"инвентарн(?:ый|ого|ому|ым|ом|ая|ой|ое|ые|ых|ыми)",
    "CORPORATE": r"корпоративн(?:ый|ого|ому|ым|ом|ая|ой|ое|ые|ых|ыми)",
    "ACTION": r"действи(?:е|я|ю|и|ем|й)",
    "NOTICE": r"уведомлени(?:е|я|ю|и|ем|й)|уведомляет|уведомляем",
    "MESSAGE": r"сообщени(?:е|я|ю|и|ем|й)|сообщает",
    "BROKER": r"брокер(?:а|у|ом|ский|ского|скому|ском|ским)?",
    "DEPOT": r"депо|депозитарный|депозитарная",
    "STATEMENT": r"выписк(?:а|и|е|у|ой)",
    "PAYMENT": r"выплат(?:а|ы|е|у|ой|ам)?",
    "TYPE": r"тип|вид",
    "ABOUT": r"о|об",
}
_PATTERNS = [(label, re.compile(pattern)) for label, pattern in _WORDS.items()]
_TRACE_KEYS = ("target_titles", "order_heading_exact", "notice_heading_short", "notice_legacy",
               "report_word", "deal_word", "cash_or_movement", "broker_fallback", "depot_account_phrase",
               "securities_phrase", "depot_movement", "depot_fallback", "payment_fallback",
               "notice_phrase", "notice_type", "notice_message", "notice_extended", "order_title_extended",
               "order_confirmation", "order_reference_title", "order_near_confirmation",
               "order_depot_confirmation", "order_extended")


def _structural_lines(text: str) -> list[dict]:
    out = []
    for number, line in enumerate(classifier._normalized(text).splitlines()[:80], 1):
        tokens = re.findall(r"[^\W\d_]+|\d+", line)[:60]
        labels = [next((label for label, pattern in _PATTERNS if pattern.fullmatch(word)), "MASKED") for word in tokens]
        if not any(x in labels for x in ("REPORT", "EXECUTION", "ORDER", "INVENTORY", "CORPORATE", "NOTICE", "STATEMENT", "PAYMENT")):
            continue
        out.append({"line": number, "signals": labels})
        if len(out) >= 20:
            break
    return out


def diagnose_bytes(data: bytes) -> dict:
    extraction = extract_document_text(data)
    info = {"status": extraction.status, "format": extraction.detected_format,
            "reason": extraction.reason, "pages": extraction.pages, "characters": len(extraction.text or "")}
    result = {"status": "OK", "diagnostic_schema": 1, "cloud_writes": 0, "local_document_writes": 0,
              "classifier_sha256": hashlib.sha256(Path(classifier.__file__).read_bytes()).hexdigest(),
              "extraction": info}
    if extraction.status != "OK" or not extraction.text:
        return dict(result, classification_status="NEEDS_REVIEW", reason_codes=["DOCUMENT_TEXT_UNAVAILABLE"], rule_trace={}, structural_lines=[])
    trace = dict.fromkeys(_TRACE_KEYS)
    text = extraction.text
    category, confidence, reason = classifier._category_from_content(text, trace)
    candidate = classifier.classify_document(text, data)
    if (category, confidence) != (candidate.category, candidate.confidence):
        return dict(result, status="ERROR", reason_codes=["TRACE_CLASSIFICATION_MISMATCH"])
    return dict(result, classification_status=candidate.status, category=category, confidence=confidence,
                reason_codes=list(candidate.reasons), category_reason=reason, rule_trace=trace,
                structural_lines=_structural_lines(text), structural_scope="first 80 lines; up to 20 relevant lines; fixed vocabulary only; null means rule not evaluated",
                account_found=bool(candidate.account_ids), period_found=bool(candidate.period_start and candidate.period_end),
                report_date_found=bool(candidate.report_date))


def diagnose_gmail_attachment(*, message_id: str, part_id: str, allowed_senders, get_message, get_attachment) -> dict:
    def fail(reason):
        return {"status": "ERROR", "reason": reason, "cloud_writes": 0, "local_document_writes": 0}
    if not isinstance(message_id, str) or not isinstance(part_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", message_id) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", part_id) or len(part_id)>64:
        return fail("INVALID_ATTACHMENT_SELECTOR")
    allowlist = normalize_sender_allowlist(allowed_senders)
    if not allowlist:
        return fail("SENDER_ALLOWLIST_NOT_CONFIGURED")
    message = get_message(message_id)
    if message.get("id") != message_id:
        return fail("MESSAGE_ID_MISMATCH")
    # Match the same configured sender rules as the normal scan.
    sender_ok, sender_reason = _sender_status(str((message.get("headers") or {}).get("from") or ""), allowlist)
    if not sender_ok:
        return fail(sender_reason)
    matches = [a for a in message.get("attachments", []) if isinstance(a, Mapping) and a.get("part_id") == part_id]
    if len(matches) != 1:
        return fail("ATTACHMENT_MISSING_OR_AMBIGUOUS")
    a = matches[0]
    if not _potential_report(a):
        return fail("UNSUPPORTED_ATTACHMENT")
    try:
        if isinstance(a.get("size"), bool):
            raise ValueError()
        size = int(a.get("size"))
    except (TypeError, ValueError):
        return fail("ATTACHMENT_SIZE_INVALID")
    limit = min(MAX_ATTACHMENT_BYTES, MAX_INPUT_BYTES)
    if not 0 < size <= limit:
        return fail("ATTACHMENT_SIZE_OUT_OF_RANGE")
    payload = get_attachment(message_id, part_id, max_bytes=limit, message_manifest=message)
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, bytes) or len(data) != size or payload.get("attachment_id") != a.get("attachment_id") or payload.get("part_id") != part_id:
        return fail("ATTACHMENT_ID_OR_SIZE_MISMATCH")
    return diagnose_bytes(data)
