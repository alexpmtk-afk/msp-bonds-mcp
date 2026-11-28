"""One-shot, read-only Gmail profile diagnostic and local command.

The command reads the existing Gmail credential record without refreshing or
writing it. It only reports PASS or ERROR and never exposes profile data.
"""
from __future__ import annotations

from datetime import datetime, timezone
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
REQUEST_TIMEOUT_SECONDS = 5.0
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
PASS = "PASS"
ERROR = "ERROR"
EXIT_PASS = 0
EXIT_CREDENTIAL_STORE_ERROR = 2
EXIT_CREDENTIAL_RECORD_MISSING = 3
EXIT_SCOPE_MISMATCH = 4
EXIT_TOKEN_INVALID = 5
EXIT_TOKEN_EXPIRED = 6
EXIT_GMAIL_HTTP_401 = 11
EXIT_GMAIL_HTTP_403 = 12
EXIT_GMAIL_HTTP_OTHER = 13
EXIT_NETWORK_TIMEOUT = 14
EXIT_NETWORK_ERROR = 15


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Fail closed instead of forwarding an authorization header elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def gmail_profile_status(
    access_token: str | None,
    *,
    credentials_valid: bool,
    credentials_expired: bool,
    opener_factory: Callable[..., Any] | None = None,
) -> str:
    """Make at most one fixed Gmail profile request; return only PASS or ERROR.

    The token is used only in an in-memory Authorization header. Missing,
    expired, invalid, or malformed credentials are rejected before networking.
    The response body is never read, returned, logged, or persisted.
    """
    return _gmail_profile_result(
        access_token,
        credentials_valid=credentials_valid,
        credentials_expired=credentials_expired,
        opener_factory=opener_factory,
    )[0]


def _gmail_profile_result(
    access_token: str | None,
    *,
    credentials_valid: bool,
    credentials_expired: bool,
    opener_factory: Callable[..., Any] | None = None,
) -> tuple[str, int]:
    if credentials_expired is True:
        return ERROR, EXIT_TOKEN_EXPIRED
    if credentials_valid is not True:
        return ERROR, EXIT_TOKEN_INVALID
    if (
        not isinstance(access_token, str)
        or not access_token
        or access_token != access_token.strip()
        or any(ord(char) < 33 or ord(char) > 126 for char in access_token)
    ):
        return ERROR, EXIT_TOKEN_INVALID

    try:
        request = urllib.request.Request(
            PROFILE_URL,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
            method="GET",
        )
        factory = opener_factory or urllib.request.build_opener
        opener = factory(_NoRedirectHandler())
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            status_code = response.getcode()
            if status_code == 200:
                return PASS, EXIT_PASS
            if status_code == 401:
                return ERROR, EXIT_GMAIL_HTTP_401
            if status_code == 403:
                return ERROR, EXIT_GMAIL_HTTP_403
            return ERROR, EXIT_GMAIL_HTTP_OTHER
    except urllib.error.HTTPError as error:
        # Do not read or stringify the response; it may contain profile data.
        status_code = error.code
        try:
            error.close()
        except Exception:
            pass
        if status_code == 401:
            return ERROR, EXIT_GMAIL_HTTP_401
        if status_code == 403:
            return ERROR, EXIT_GMAIL_HTTP_403
        return ERROR, EXIT_GMAIL_HTTP_OTHER
    except TimeoutError:
        return ERROR, EXIT_NETWORK_TIMEOUT
    except urllib.error.URLError as error:
        if isinstance(getattr(error, "reason", None), TimeoutError):
            return ERROR, EXIT_NETWORK_TIMEOUT
        return ERROR, EXIT_NETWORK_ERROR
    except Exception:
        # Network and timeout details are deliberately not surfaced.
        return ERROR, EXIT_NETWORK_ERROR


def main() -> int:
    """Read the stored Gmail token without refresh, then make one profile call."""
    try:
        credentials = _load_saved_gmail_credentials()
    except Exception:
        # Never reveal the account, credential-store, or transport error.
        result, exit_code = ERROR, EXIT_CREDENTIAL_STORE_ERROR
    else:
        access_token, exit_code = _validated_access_token(credentials)
        result = ERROR
        if access_token is not None:
            result, exit_code = _gmail_profile_result(
                access_token,
                credentials_valid=True,
                credentials_expired=False,
            )
    print(result)
    return exit_code


def _load_saved_gmail_credentials() -> Any:
    """Read the current Gmail credential record; never refresh or write it."""
    from .auth import _load_json
    from .workspace import account_alias

    return _load_json(account_alias(), "gmail")


def _usable_access_token(credentials: Any, *, now: datetime | None = None) -> str | None:
    """Return an unexpired token with exactly the Gmail read-only scope."""
    return _validated_access_token(credentials, now=now)[0]


def _validated_access_token(
    credentials: Any,
    *,
    now: datetime | None = None,
) -> tuple[str | None, int]:
    if not isinstance(credentials, dict):
        return None, EXIT_CREDENTIAL_RECORD_MISSING
    token = credentials.get("token")
    scopes = credentials.get("scopes")
    expiry_text = credentials.get("expiry")
    if not isinstance(scopes, list) or set(scopes) != {GMAIL_READONLY_SCOPE}:
        return None, EXIT_SCOPE_MISMATCH
    if not isinstance(token, str) or not token or not isinstance(expiry_text, str) or not expiry_text:
        return None, EXIT_TOKEN_INVALID
    if token != token.strip() or any(ord(char) < 33 or ord(char) > 126 for char in token):
        return None, EXIT_TOKEN_INVALID
    try:
        normalized_expiry = expiry_text[:-1] + "+00:00" if expiry_text.endswith("Z") else expiry_text
        expiry = datetime.fromisoformat(normalized_expiry)
        if expiry.tzinfo is None:
            return None, EXIT_TOKEN_INVALID
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            return None, EXIT_TOKEN_INVALID
        if expiry.astimezone(timezone.utc) <= current.astimezone(timezone.utc):
            return None, EXIT_TOKEN_EXPIRED
    except (TypeError, ValueError, OverflowError):
        return None, EXIT_TOKEN_INVALID
    return token, EXIT_PASS


if __name__ == "__main__":
    raise SystemExit(main())
