"""Google OAuth for a personal Windows desktop app; never send tokens over MCP.

OAuth is intentionally interactive and separate from the stdio server. Tokens
live in the OS credential manager through keyring, NOT files or source control.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

APP_SERVICE = "msp-bonds-mcp-google-v1"
SCOPES = {
    "drive": ["https://www.googleapis.com/auth/drive"],
    "gmail": ["https://www.googleapis.com/auth/gmail.readonly"],
    "gmail_write": ["https://www.googleapis.com/auth/gmail.modify"],
}
ENV_OAUTH_CLIENT = "MSP_GOOGLE_OAUTH_CLIENT"


class GoogleAuthError(RuntimeError):
    pass


def _credential_name(account: str, service: str) -> str:
    if service not in SCOPES or not account or len(account) > 120:
        raise GoogleAuthError("Unknown service or empty account alias")
    return f"{account.strip().lower()}:{service}"


def _keyring():
    if os.name != "nt":
        raise GoogleAuthError("OAuth token storage requires Windows Credential Manager on HOME")
    try:
        import keyring
        # Do not silently use a plaintext or unsupported backend.
        backend = keyring.get_keyring()
        if backend.priority <= 0 or not type(backend).__module__.startswith("keyring.backends.Windows"):
            raise GoogleAuthError("Protected OS credential storage is unavailable")
        return keyring
    except ImportError as exc:
        raise GoogleAuthError("Install keyring to store OAuth tokens in Windows Credential Manager") from exc


def _load_json(account: str, service: str):
    raw = _keyring().get_password(APP_SERVICE, _credential_name(account, service))
    return json.loads(raw) if raw else None


def token_state(account: str, service: str) -> dict:
    data = _load_json(account, service)
    return {"service": service, "configured": data is not None,
            "scopes": SCOPES[service], "contains_token_values": False}


def load_credentials(account: str, service: str):
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
    except ImportError as exc:
        raise GoogleAuthError("Google authentication libraries are not installed") from exc
    info = _load_json(account, service)
    if not info:
        raise GoogleAuthError(f"Google {service} authorization missing. Run local OAuth setup first")
    try:
        creds = Credentials.from_authorized_user_info(info, scopes=SCOPES[service])
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _keyring().set_password(APP_SERVICE, _credential_name(account, service), creds.to_json())
        if not creds.valid:
            raise GoogleAuthError("OAuth token invalid or expired; reauthorize locally")
        if not set(SCOPES[service]).issubset(set(creds.scopes or [])):
            raise GoogleAuthError("OAuth token does not have required permissions")
        return creds
    except GoogleAuthError:
        raise
    except Exception as exc:
        raise GoogleAuthError(f"OAuth validation failed ({type(exc).__name__})") from exc


def interactive_login(account: str, service: str):
    if service not in SCOPES:
        raise GoogleAuthError("Service must be drive, gmail or gmail_write")
    raw_path = os.environ.get(ENV_OAUTH_CLIENT, "").strip()
    if not raw_path:
        raise GoogleAuthError(f"Set {ENV_OAUTH_CLIENT} to a local desktop OAuth credentials.json path")
    path = Path(raw_path).expanduser()
    if not path.is_file():
        raise GoogleAuthError("Local desktop OAuth credentials file not found")
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise GoogleAuthError("Google OAuth client library is missing") from exc
    flow = InstalledAppFlow.from_client_secrets_file(str(path), scopes=SCOPES[service])
    creds = flow.run_local_server(host="localhost", port=0, open_browser=True,
                                  authorization_prompt_message="Open this Google consent URL: {url}",
                                  access_type="offline", prompt="consent")
    _keyring().set_password(APP_SERVICE, _credential_name(account, service), creds.to_json())
    return {"service": service, "authorized": True, "tokens_exposed": False}


def service_client(account: str, service: str):
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise GoogleAuthError("Google API client library is missing") from exc
    creds = load_credentials(account, service)
    return build("drive", "v3", credentials=creds, cache_discovery=False) if service == "drive" else build("gmail", "v1", credentials=creds, cache_discovery=False)


def workspace_client(account: str, service: str):
    if service not in ("docs", "sheets"):
        raise GoogleAuthError("Unsupported workspace API")
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise GoogleAuthError("Google API client library is missing") from exc
    creds = load_credentials(account, "drive")
    return build("docs" if service == "docs" else "sheets", "v1" if service == "docs" else "v4", credentials=creds, cache_discovery=False)
