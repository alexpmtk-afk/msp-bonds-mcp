"""Small HTTPS transport for Telegram Bot API sendMessage."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .errors import TelegramOutcomeUnknown

MAX_RESPONSE_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    body: bytes


class TelegramTransport(Protocol):
    def send_message(
        self,
        token: str,
        *,
        chat_id: int,
        message_thread_id: int,
        text: str,
        timeout_seconds: float,
    ) -> HttpResponse: ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent forwarding a URL containing the bot token to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibTelegramTransport:
    """POST JSON over HTTPS; no redirects, raw errors, or logging."""

    def __init__(self, opener_factory=urllib.request.build_opener) -> None:
        self._opener_factory = opener_factory

    def send_message(
        self,
        token: str,
        *,
        chat_id: int,
        message_thread_id: int,
        text: str,
        timeout_seconds: float,
    ) -> HttpResponse:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = json.dumps(
            {
                "chat_id": chat_id,
                "message_thread_id": message_thread_id,
                "text": text,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        opener = self._opener_factory(_NoRedirectHandler())

        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                status = int(response.getcode())
                response_body = response.read(MAX_RESPONSE_BYTES + 1)
                return HttpResponse(status, response_body[:MAX_RESPONSE_BYTES])
        except urllib.error.HTTPError as error:
            # HTTPError is a definitive HTTP response (including 429). Never stringify it:
            # its URL contains the bot token.
            try:
                response_body = error.read(MAX_RESPONSE_BYTES + 1)
            except Exception:
                response_body = b""
            finally:
                error.close()
            return HttpResponse(int(error.code), response_body[:MAX_RESPONSE_BYTES])
        except Exception:
            # A connection/read failure can happen after Telegram accepted the message.
            # Suppress the original exception because it can include the tokenized URL.
            raise TelegramOutcomeUnknown() from None
