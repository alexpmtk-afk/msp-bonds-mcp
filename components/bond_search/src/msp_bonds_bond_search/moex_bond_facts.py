"""Read-only bond facts from public MOEX ISS; values stay source-scoped and timestamped."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import re
from typing import Callable, Mapping

from .discovery import FetchDiagnostic, SourceUnavailable, safe_get

_ISIN = re.compile(r"RU[A-Z0-9]{10}\Z")
_SECID = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,31}\Z")
_SPEC_URL = "https://iss.moex.com/iss/securities/{secid}.json?iss.meta=off&iss.only=description,boards"
_MARKET_URL = (
    "https://iss.moex.com/iss/engines/stock/markets/bonds/securities/"
    "{secid}.json?iss.meta=off&iss.only=securities,marketdata"
)
_RESPONSE_BLOCKS = {
    "security_specification": ("description", "boards"),
    "market_data": ("securities", "marketdata"),
}

_FIELD_SPECS = (
    ("short_name", ("SHORTNAME",), "", "text",
     "MOEX SHORTNAME is the short display name; kept separate from SECNAME."),
    ("full_name", ("SECNAME",), "", "text",
     "MOEX SECNAME is the full instrument name; kept separate from SHORTNAME."),
    ("api_name", ("NAME",), "", "text",
     "Generic MOEX NAME field is preserved separately and is not merged with SHORTNAME or SECNAME."),
    ("issuer_name", ("ISSUER", "ISSUERNAME", "EMITENT", "EMITENTNAME"), "", "text",
     "Issuer name only when the official response exposes it."),
    ("face_value", ("FACEVALUE", "FACE_VALUE"), "MOEX ISS denomination units", "number",
     "MOEX FACEVALUE may represent the current face value after amortization."),
    ("face_currency", ("FACEUNIT", "FACEUNITCODE", "FACE_CURRENCY"), "MOEX currency code", "text",
     "Currency code is preserved as returned; no conversion is performed."),
    ("issue_date", ("ISSUEDATE", "ISSUE_DATE"), "date", "text",
     "Exchange reference field; not a substitute for the current issue document."),
    ("maturity_date", ("MATDATE", "MATURITY_DATE"), "date", "text",
     "Exchange reference field; legal terms still require the issue documents."),
    ("coupon_rate_percent", ("COUPONPERCENT", "COUPON_RATE", "COUPONRATE"), "% as reported by MOEX", "number",
     "A current exchange field does not establish all future legal coupon terms."),
    ("coupon_amount", ("COUPONVALUE", "COUPON_VALUE"), "per security; MOEX field units", "number",
     "The amount is not interpreted as a legally guaranteed future payment."),
    ("coupon_currency", ("COUPONCURRENCY", "COUPONUNIT"), "MOEX currency code", "text",
     "Currency is included only when exposed by the source."),
    ("coupon_period_days", ("COUPONPERIOD", "COUPON_PERIOD"), "days as reported by MOEX", "number",
     "Preserved as reported; no schedule is inferred."),
    ("coupon_frequency_per_year", ("COUPONFREQUENCY", "COUPON_FREQUENCY"), "as reported by MOEX", "number",
     "Included only when MOEX exposes a frequency field."),
    ("next_coupon_date", ("NEXTCOUPON", "NEXT_COUPON"), "date", "text",
     "Exchange's next-coupon field; not a complete legal payment schedule."),
)


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _present(value: object) -> bool:
    return value is not None and _text(value) not in {"", "-", "—", "null", "None"}


def _records(
    root: Mapping[str, object] | None,
    key: str,
    *,
    required: bool = False,
) -> list[dict[str, object]]:
    if root is None:
        return []
    if key not in root:
        if required:
            raise SourceUnavailable(
                "MOEX ISS response is missing a required data block",
                code="MISSING_RESPONSE_BLOCK", phase="parse",
            )
        return []
    table = root[key]
    if not isinstance(table, dict):
        raise SourceUnavailable("invalid MOEX ISS table", code="PARSE_ERROR", phase="parse")
    columns, data = table.get("columns"), table.get("data")
    if not isinstance(columns, list) or not isinstance(data, list):
        raise SourceUnavailable("invalid MOEX ISS table", code="PARSE_ERROR", phase="parse")
    if any(not isinstance(item, str) or not item for item in columns):
        raise SourceUnavailable("invalid MOEX ISS column names", code="PARSE_ERROR", phase="parse")
    names = [item.upper() for item in columns]
    if len(set(names)) != len(names):
        raise SourceUnavailable("duplicate MOEX ISS column names", code="PARSE_ERROR", phase="parse")
    records = []
    for row in data:
        if not isinstance(row, list) or len(row) != len(names):
            raise SourceUnavailable("invalid MOEX ISS row width", code="PARSE_ERROR", phase="parse")
        records.append(dict(zip(names, row)))
    return records


def _index_description(rows: list[dict[str, object]]) -> dict[str, list[object]]:
    result: dict[str, list[object]] = {}
    for row in rows:
        name = _text(row.get("NAME")).upper()
        if name and "VALUE" in row and _present(row["VALUE"]):
            result.setdefault(name, []).append(row["VALUE"])
    return result


def _pick(row: Mapping[str, object], aliases: tuple[str, ...]) -> object | None:
    for alias in aliases:
        key = alias.upper()
        if key in row and _present(row[key]):
            return row[key]
    return None


def _number(value: object) -> tuple[object | None, bool]:
    if isinstance(value, bool):
        return None, False
    if isinstance(value, int):
        return value, True
    if isinstance(value, float):
        return (value, True) if math.isfinite(value) else (None, False)
    raw = _text(value).replace(" ", "").replace(",", ".")
    try:
        number = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None, False
    if not number.is_finite():
        return None, False
    if number == number.to_integral_value():
        return int(number), True
    return float(number), True


def _canonical(value: object) -> tuple[str, str]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return "number", str(Decimal(str(value)).normalize())
        except InvalidOperation:
            pass
    normalized_text = " ".join(_text(value).split()).casefold()
    return "text", normalized_text


def _field(
    name: str,
    entries: list[tuple[object, str, datetime | None, str]],
    *,
    unit: str,
    value_type: str = "text",
    note: str = "",
    default_urls: tuple[str, ...] = (),
    default_retrieved_at: datetime | None = None,
) -> dict[str, object]:
    parsed: list[tuple[object, str, datetime | None, str]] = []
    invalid: list[tuple[str, object, str]] = []
    for raw, source_url, retrieved_at, source_field in entries:
        value = raw
        if value_type == "number":
            value, valid = _number(raw)
            if not valid:
                invalid.append((source_url, raw, source_field))
                continue
        elif value_type == "text":
            value = _text(raw)
        if _present(value):
            parsed.append((value, source_url, retrieved_at, source_field))

    urls = list(dict.fromkeys(
        [item[1] for item in parsed]
        + [url for url, _, _ in invalid]
        + list(default_urls)
    ))
    source_fields = list(dict.fromkeys(
        [item[3] for item in parsed]
        + [field for _, _, field in invalid]
    ))
    times = [item[2] for item in parsed if item[2] is not None]
    retrieved = max(times).isoformat() if times else (
        default_retrieved_at.isoformat() if default_retrieved_at else None
    )
    if invalid and not parsed:
        state = "INVALID_SOURCE_VALUE"
    elif not parsed:
        state = "NOT_AVAILABLE"
    else:
        unique = {_canonical(value) for value, _, _, _ in parsed}
        state = "CONFLICT" if len(unique) > 1 or invalid else "CONFIRMED_BY_MOEX"

    candidates = []
    if state == "CONFLICT":
        candidates = [
            {"value": value, "source_url": url, "source_field": field}
            for value, url, _, field in parsed
        ]
        candidates.extend(
            {"value": None, "source_url": url, "source_field": field, "raw_value": _text(raw)}
            for url, raw, field in invalid
        )
    value = parsed[0][0] if state == "CONFIRMED_BY_MOEX" else None
    result = {
        "claim": name,
        "value": value,
        "unit": unit,
        "state": state,
        "source": "MOEX ISS",
        "source_urls": urls,
        "source_fields": source_fields,
        "retrieved_at": retrieved,
        "note": note,
    }
    if candidates:
        result["candidates"] = candidates
    return result


def _entries(records, aliases, source_url, retrieved_at):
    result = []
    for row in records:
        for alias in aliases:
            value = row.get(alias.upper())
            if _present(value):
                result.append((value, source_url, retrieved_at, alias.upper()))
    return result


def _description_entries(index, aliases, source_url, retrieved_at):
    return [
        (value, source_url, retrieved_at, key)
        for key in aliases if key in index
        for value in index[key] if _present(value)
    ]


def _availability(fact: dict[str, object], source_errors: tuple[dict[str, object], ...]) -> None:
    if fact.get("state") != "NOT_AVAILABLE":
        return
    if source_errors:
        endpoints = ", ".join(
            f"{item.get('endpoint', 'MOEX ISS')} ({item.get('code', 'SOURCE_ERROR')})"
            for item in source_errors
        )
        fact["availability"] = {
            "reason_code": "SOURCE_RESPONSE_INCOMPLETE",
            "message": (
                "В пригодных ответах поле не найдено; часть источниковых блоков не получена "
                f"или не разобрана: {endpoints}. Это не подтверждает отсутствие поля в MOEX."
            ),
        }
    else:
        fact["availability"] = {
            "reason_code": "NO_VALUE_RETURNED",
            "message": (
                "MOEX ISS не вернул непустое значение этого поля в запрошенных блоках. "
                "Это относится только к данному ответу и не подтверждает, что значения нет "
                "на других режимах или в документах выпуска."
            ),
        }

def _identity_conflict(isin, security_id, description, security_rows, market_rows):
    expected_isin, expected_secid = isin.upper(), security_id.upper()
    for desc_isin in description.get("ISIN", []):
        normalized = _text(desc_isin).upper()
        if normalized and normalized != expected_isin:
            return True
    for desc_secid in description.get("SECID", []):
        normalized = _text(desc_secid).upper()
        if normalized and normalized != expected_secid:
            return True
    for row in security_rows + market_rows:
        row_isin = _text(row.get("ISIN")).upper()
        row_secid = _text(row.get("SECID")).upper()
        if (row_isin and row_isin != expected_isin) or (row_secid and row_secid != expected_secid):
            return True
    return False


def _market_field(claim, rows, aliases, source_url, retrieved_at, *, unit, note=""):
    return _field(
        claim, _entries(rows, aliases, source_url, retrieved_at),
        unit=unit, value_type="number", note=note, default_urls=(source_url,), default_retrieved_at=retrieved_at,
    )


def _market_data_diagnostic(market, rows, source_errors):
    error = next((item for item in source_errors if item.get("endpoint") == "market_data"), None)
    if error:
        status = "SOURCE_UNAVAILABLE"
        interpretation = (
            "Запрос рыночного блока завершился ошибкой; пустой список здесь нельзя трактовать "
            "как отсутствие торговых данных."
        )
    elif market is None:
        status = "SOURCE_UNAVAILABLE"
        interpretation = "Ответ рыночного источника не получен."
    elif "marketdata" not in market:
        status = "RESPONSE_BLOCK_MISSING"
        interpretation = "Ответ получен, но в нём отсутствует ожидаемый блок marketdata."
    elif not rows:
        status = "NO_ROWS_RETURNED"
        interpretation = (
            "Запрос завершился, но MOEX ISS вернул пустой блок marketdata. Это означает только, "
            "что в данном ответе нет рыночных строк; само по себе это не доказывает отсутствия "
            "торгов, котировок за день или исторических сделок."
        )
    else:
        recognized = ("LAST", "LASTPRICE", "ACCRUEDINT", "ACCRUED_INTEREST", "TRADEDATE", "TRADINGDATE")
        populated_rows = sum(any(_present(row.get(key)) for key in recognized) for row in rows)
        if populated_rows:
            status = "DATA_AVAILABLE"
            interpretation = "MOEX ISS вернул рыночные строки с одним или несколькими распознанными полями."
        else:
            status = "ROWS_WITHOUT_RECOGNIZED_FIELDS"
            interpretation = (
                "Строки marketdata получены, но запрошенные поля LAST, ACCRUEDINT и TRADEDATE "
                "не содержат значений."
            )
    board_ids = {
        _text(_pick(row, ("BOARDID", "BOARD_ID"))).upper() or "UNSPECIFIED"
        for row in rows
    }
    return {
        "status": status,
        "rows_received": len(rows),
        "boards_with_rows": len(board_ids),
        "endpoint_error": dict(error) if error else None,
        "interpretation": interpretation,
    }


def parse_moex_bond_facts(
    isin: str,
    security_id: str,
    *,
    specification: Mapping[str, object] | None,
    market: Mapping[str, object] | None,
    identifier_source_url: str,
    identifier_fetched_at: datetime,
    specification_source_url: str,
    specification_fetched_at: datetime | None,
    market_source_url: str,
    market_fetched_at: datetime | None,
    source_errors: tuple[dict[str, str], ...] = (),
) -> dict[str, object]:
    """Parse MOEX tables without inferring missing fields or choosing a board."""
    if not isinstance(isin, str) or not _ISIN.fullmatch(isin.upper()):
        raise ValueError("a valid ISIN is required")
    secid = security_id.upper()
    if not _SECID.fullmatch(secid):
        raise ValueError("invalid MOEX security id")

    description_rows = _records(specification, "description")
    board_rows = _records(specification, "boards")
    security_rows = _records(market, "securities")
    market_rows = _records(market, "marketdata")
    description = _index_description(description_rows)
    errors_by_endpoint = {
        str(item.get("endpoint", "")): item for item in source_errors
    }
    sources = {
        "exact_isin_lookup": {"source": "MOEX ISS", "url": identifier_source_url,
            "retrieved_at": identifier_fetched_at.isoformat(), "status": "EXACT_MATCH"},
        "security_specification": {"source": "MOEX ISS", "url": specification_source_url,
            "retrieved_at": specification_fetched_at.isoformat() if specification_fetched_at else None,
            "status": "UNAVAILABLE" if "security_specification" in errors_by_endpoint else
                      "RECEIVED" if specification is not None else "UNAVAILABLE",
            "error": dict(errors_by_endpoint["security_specification"])
                     if "security_specification" in errors_by_endpoint else None},
        "market_data": {"source": "MOEX ISS", "url": market_source_url,
            "retrieved_at": market_fetched_at.isoformat() if market_fetched_at else None,
            "status": "UNAVAILABLE" if "market_data" in errors_by_endpoint else
                      "RECEIVED" if market is not None else "UNAVAILABLE",
            "error": dict(errors_by_endpoint["market_data"])
                     if "market_data" in errors_by_endpoint else None},
    }
    if _identity_conflict(isin, secid, description, security_rows, market_rows):
        return {
            "status": "IDENTITY_CONFLICT", "isin": isin, "security_id": secid,
            "identity_state": "CONFLICT", "fields": {},
            "trading_boards": [], "market_data_by_board": [],
            "market_data_diagnostic": _market_data_diagnostic(market, market_rows, tuple(source_errors)),
            "sources": sources, "source_errors": list(source_errors),
            "reason_code": "MOEX_RESPONSE_IDENTIFIER_MISMATCH",
            "limitations": ["No field from a response with a mismatched identifier was accepted."],
        }

    fields: dict[str, object] = {
        "isin": {"claim": "isin", "value": isin, "unit": "",
            "state": "CONFIRMED_BY_EXACT_LOOKUP", "source": "MOEX ISS",
            "source_urls": [identifier_source_url],
            "retrieved_at": identifier_fetched_at.isoformat(),
            "source_fields": ["ISIN"],
            "note": "Exact ISIN lookup; confirms identity only."},
        "security_id": {"claim": "security_id", "value": secid, "unit": "",
            "state": "CONFIRMED_BY_EXACT_LOOKUP", "source": "MOEX ISS",
            "source_urls": [identifier_source_url],
            "retrieved_at": identifier_fetched_at.isoformat(),
            "source_fields": ["SECID"],
            "note": "MOEX security id linked to the exact ISIN lookup."},
    }
    for name, aliases, unit, value_type, note in _FIELD_SPECS:
        entries = _description_entries(description, aliases, specification_source_url, specification_fetched_at)
        entries.extend(_entries(security_rows, aliases, market_source_url, market_fetched_at))
        default_urls = (specification_source_url, market_source_url)
        fields[name] = _field(
            name, entries, unit=unit, value_type=value_type, note=note, default_urls=default_urls,
            default_retrieved_at=max((x for x in (specification_fetched_at, market_fetched_at) if x is not None), default=None),
        )
        _availability(fields[name], tuple(source_errors))
    fields["security_name"] = dict(fields["full_name"])
    fields["security_name"]["claim"] = "security_name"
    fields["security_name"]["note"] = "Compatibility alias of full_name; short_name is provided separately."

    boards: dict[str, dict[str, object]] = {}
    for row in board_rows:
        board_id = _text(_pick(row, ("BOARDID", "BOARD_ID"))).upper()
        if board_id:
            boards[board_id] = row
    securities_by_board: dict[str, list[dict[str, object]]] = {}
    for row in security_rows:
        board_id = _text(_pick(row, ("BOARDID", "BOARD_ID"))).upper() or "UNSPECIFIED"
        securities_by_board.setdefault(board_id, []).append(row)
        if board_id != "UNSPECIFIED":
            boards.setdefault(board_id, {})
    market_by_board: dict[str, list[dict[str, object]]] = {}
    for row in market_rows:
        board_id = _text(_pick(row, ("BOARDID", "BOARD_ID"))).upper() or "UNSPECIFIED"
        market_by_board.setdefault(board_id, []).append(row)
        if board_id != "UNSPECIFIED":
            boards.setdefault(board_id, {})

    trading_boards = []
    for board_id in sorted(boards):
        sec_rows = securities_by_board.get(board_id, [])
        board = boards[board_id]
        source_urls = []
        if board:
            source_urls.append(specification_source_url)
        if sec_rows:
            source_urls.append(market_source_url)
        trading_boards.append({
            "board_id": board_id,
            "title": _pick(board, ("TITLE", "BOARDNAME", "NAME")),
            "is_primary": _pick(board, ("IS_PRIMARY", "ISPRIMARY")),
            "is_traded": _pick(board, ("IS_TRADED", "ISTRADED")),
            "security_status": _pick(sec_rows[0], ("STATUS",)) if sec_rows else None,
            "trading_status": _pick(sec_rows[0], ("TRADINGSTATUS", "TRADING_STATUS")) if sec_rows else None,
            "source_urls": source_urls,
            "source_fields": list(dict.fromkeys(
                key for row in ([board] if board else []) + sec_rows
                for key in ("BOARDID", "BOARD_ID", "TITLE", "BOARDNAME", "NAME", "IS_PRIMARY", "ISPRIMARY", "IS_TRADED", "ISTRADED", "STATUS", "TRADINGSTATUS", "TRADING_STATUS")
                if key in row
            )),
        })

    market_data_by_board = []
    for board_id in sorted(market_by_board):
        rows = market_by_board[board_id]
        market_data_by_board.append({
            "board_id": "" if board_id == "UNSPECIFIED" else board_id,
            "last_trade_price": _market_field(
                "last_trade_price", rows, ("LAST", "LASTPRICE"), market_source_url, market_fetched_at,
                unit="MOEX ISS quotation units",
                note="Last reported trade only; not a live bid, ask, or executable price."),
            "accrued_interest": _market_field(
                "accrued_interest", rows, ("ACCRUEDINT", "ACCRUED_INTEREST"),
                market_source_url, market_fetched_at, unit="MOEX ISS field units",
                note="Reported by MOEX for this board; no currency conversion is applied."),
            "trade_date": _field(
                "trade_date", _entries(rows, ("TRADEDATE", "TRADINGDATE"), market_source_url, market_fetched_at),
                unit="date", note="Trade date as reported by MOEX.", default_urls=(market_source_url,), default_retrieved_at=market_fetched_at),
            "trade_time": _field(
                "trade_time", _entries(rows, ("SYSTIME", "TIME", "UPDATETIME"), market_source_url, market_fetched_at),
                unit="MOEX time field", note="Exchange timestamp field as reported.",
                default_urls=(market_source_url,), default_retrieved_at=market_fetched_at),
        })

    market_data_diagnostic = _market_data_diagnostic(market, market_rows, tuple(source_errors))

    has_data = any(
        isinstance(value, dict) and str(value.get("state", "")).startswith("CONFIRMED_BY_MOEX")
        for key, value in fields.items() if key not in {"isin", "security_id"}
    ) or bool(trading_boards or market_data_by_board)
    field_conflict = any(
        isinstance(item, dict) and item.get("state") == "CONFLICT" for item in fields.values()
    )
    status = ("PARTIAL_CONFLICT" if source_errors and field_conflict else
              "CONFLICT" if field_conflict else
              "PARTIAL" if source_errors and has_data else
              "UNAVAILABLE" if source_errors else "DATA_RECEIVED" if has_data else "NO_DATA")
    limitations = [
        "MOEX facts are source-specific and do not complete the 27-check due-diligence plan.",
        "Coupon fields are not a substitute for legally binding current issue documents.",
        "MOEX public market data may be delayed; no order book or executable price is asserted.",
        "Market prices and accrued interest remain separated by trading board.",
        "An absent field is reported as NOT_AVAILABLE and is never inferred.",
    ]
    for error in source_errors:
        limitations.append(
            "MOEX ISS endpoint "
            f"{error.get('endpoint', 'unknown')} failed with {error.get('code', 'SOURCE_ERROR')} "
            f"during {error.get('phase', 'unknown')} phase; dependent fields may be incomplete."
        )
    if market_data_diagnostic["status"] == "NO_ROWS_RETURNED":
        limitations.append(
            "The successful marketdata response contained no rows; this alone does not establish "
            "that no trades or historical market data exist."
        )
    elif market_data_diagnostic["status"] == "ROWS_WITHOUT_RECOGNIZED_FIELDS":
        limitations.append(
            "The marketdata response contained rows but no values in the price/accrued-interest/date fields read here."
        )
    return {
        "status": status, "isin": isin, "security_id": secid,
        "identity_state": "EXACT_LOOKUP_CONFIRMED", "fields": fields,
        "trading_boards": trading_boards, "market_data_by_board": market_data_by_board,
        "market_data_diagnostic": market_data_diagnostic,
        "sources": sources, "source_errors": list(source_errors),
        "limitations": limitations,
    }


def _decode(payload: bytes) -> dict[str, object]:
    try:
        root = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError):
        raise SourceUnavailable("invalid MOEX ISS JSON", code="PARSE_ERROR", phase="parse") from None
    if not isinstance(root, dict):
        raise SourceUnavailable("invalid MOEX ISS response", code="PARSE_ERROR", phase="parse")
    return root


def _request(url, fetch, observer):
    try:
        payload = fetch(url, observer=observer) if fetch is safe_get else fetch(url)
    except SourceUnavailable:
        raise
    except Exception:
        raise SourceUnavailable("MOEX ISS request failed", code="ADAPTER_FAILURE", phase="request") from None
    if not isinstance(payload, (bytes, bytearray)):
        raise SourceUnavailable("MOEX ISS reader returned an invalid response", code="INVALID_RESPONSE")
    return bytes(payload)


def fetch_moex_bond_facts(
    isin: str,
    security_id: str,
    *,
    identifier_source_url: str,
    identifier_fetched_at: datetime,
    fetch: Callable[[str], bytes] = safe_get,
    now: datetime | None = None,
    observer: Callable[[FetchDiagnostic], None] | None = None,
) -> dict[str, object]:
    """Fetch public MOEX specification and delayed market data for one matched security."""
    if not isinstance(isin, str) or not _ISIN.fullmatch(isin.upper()):
        raise ValueError("a valid Russian bond ISIN is required")
    if not isinstance(security_id, str) or not _SECID.fullmatch(security_id.upper()):
        raise ValueError("invalid MOEX security id")
    secid = security_id.upper()
    spec_url = _SPEC_URL.format(secid=secid)
    market_url = _MARKET_URL.format(secid=secid)
    specification = market = None
    spec_time = market_time = None
    errors: list[dict[str, str]] = []
    for endpoint, url in (("security_specification", spec_url), ("market_data", market_url)):
        try:
            payload = _request(url, fetch, observer)
            root = _decode(payload)
            for block_name in _RESPONSE_BLOCKS[endpoint]:
                _records(root, block_name, required=True)
            if endpoint == "security_specification":
                specification, spec_time = root, (now or datetime.now(timezone.utc))
            else:
                market, market_time = root, (now or datetime.now(timezone.utc))
        except SourceUnavailable as exc:
            if endpoint == "security_specification":
                spec_time = now or datetime.now(timezone.utc)
            else:
                market_time = now or datetime.now(timezone.utc)
            errors.append({
                "endpoint": endpoint,
                "code": exc.code,
                "phase": exc.phase or "request",
                "http_status": str(exc.http_status) if exc.http_status is not None else "",
                "attempts": str(exc.attempts),
            })

    return parse_moex_bond_facts(
        isin.upper(), secid, specification=specification, market=market,
        identifier_source_url=identifier_source_url,
        identifier_fetched_at=identifier_fetched_at,
        specification_source_url=spec_url, specification_fetched_at=spec_time,
        market_source_url=market_url, market_fetched_at=market_time,
        source_errors=tuple(errors),
    )
