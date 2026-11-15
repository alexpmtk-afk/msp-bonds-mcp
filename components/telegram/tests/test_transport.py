import io
import json
import unittest
import urllib.error

from msp_bonds_telegram.errors import TelegramOutcomeUnknown
from msp_bonds_telegram.transport import HttpResponse, UrllibTelegramTransport, _NoRedirectHandler


class FakeResponse:
    def __init__(self, status, body):
        self._status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self):
        return self._status

    def read(self, _limit):
        return self._body


class FakeOpener:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.request = None
        self.timeout = None

    def open(self, request, timeout):
        self.request = request
        self.timeout = timeout
        if self.error is not None:
            raise self.error
        return self.result


class TelegramTransportTests(unittest.TestCase):
    def test_posts_only_required_fields_with_timeout_and_no_redirects(self):
        opener = FakeOpener(FakeResponse(200, b'{"ok":true}'))
        handlers = []

        def factory(handler):
            handlers.append(handler)
            return opener

        transport = UrllibTelegramTransport(opener_factory=factory)
        response = transport.send_message(
            "synthetic-token",
            chat_id=-1001234567890,
            message_thread_id=42,
            text="Synthetic message",
            timeout_seconds=8.0,
        )

        self.assertEqual(response, HttpResponse(200, b'{"ok":true}'))
        self.assertIsInstance(handlers[0], _NoRedirectHandler)
        self.assertEqual(opener.request.get_method(), "POST")
        self.assertEqual(opener.request.full_url, "https://api.telegram.org/botsynthetic-token/sendMessage")
        self.assertEqual(
            json.loads(opener.request.data.decode("utf-8")),
            {"chat_id": -1001234567890, "message_thread_id": 42, "text": "Synthetic message"},
        )
        self.assertEqual(opener.timeout, 8.0)

    def test_http_429_is_returned_as_response_for_retry_policy(self):
        error = urllib.error.HTTPError(
            "https://api.telegram.org/botsynthetic-token/sendMessage",
            429,
            "rate limited",
            {},
            io.BytesIO(b'{"ok":false,"parameters":{"retry_after":3}}'),
        )
        opener = FakeOpener(error=error)
        transport = UrllibTelegramTransport(opener_factory=lambda *_handlers: opener)
        response = transport.send_message(
            "synthetic-token",
            chat_id=-1001234567890,
            message_thread_id=42,
            text="Synthetic message",
            timeout_seconds=8.0,
        )
        self.assertEqual(response.status_code, 429)
        self.assertIn(b"retry_after", response.body)

    def test_network_exception_is_sanitized_and_never_contains_tokenized_url(self):
        opener = FakeOpener(error=urllib.error.URLError("failure synthetic-token detail"))
        transport = UrllibTelegramTransport(opener_factory=lambda *_handlers: opener)
        with self.assertRaises(TelegramOutcomeUnknown) as caught:
            transport.send_message(
                "synthetic-token",
                chat_id=-1001234567890,
                message_thread_id=42,
                text="Synthetic message",
                timeout_seconds=8.0,
            )
        self.assertNotIn("synthetic-token", str(caught.exception))
        self.assertNotIn("failure", str(caught.exception))

    def test_redirect_handler_refuses_all_redirects(self):
        handler = _NoRedirectHandler()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://elsewhere.test/"))


if __name__ == "__main__":
    unittest.main()
