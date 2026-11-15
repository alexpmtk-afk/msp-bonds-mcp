"""Verify identity alignment for Drive and Gmail without revealing account emails."""
from __future__ import annotations

from .auth import service_client
from .workspace import account_alias
from .utils import InputError


def _account_emails() -> tuple[str, str]:
    alias = account_alias()
    drive = service_client(alias, "drive")
    gmail = service_client(alias, "gmail")
    drive_record = drive.about().get(fields="user(emailAddress)").execute()
    gmail_record = gmail.users().getProfile(userId="me").execute()
    drive_email = str((drive_record.get("user") or {}).get("emailAddress", "")).strip().casefold()
    gmail_email = str(gmail_record.get("emailAddress", "")).strip().casefold()
    return drive_email, gmail_email


def connection_health() -> dict:
    """Read-only, live Google API probe. Returns no account addresses or tokens."""
    drive_email, gmail_email = _account_emails()
    if not drive_email or not gmail_email:
        return {"status": "FAIL", "drive_reachable": bool(drive_email),
                "gmail_reachable": bool(gmail_email), "same_account": False,
                "details": "Google did not return account identity for both services"}
    same = drive_email == gmail_email
    return {"status": "PASS" if same else "FAIL", "drive_reachable": True,
            "gmail_reachable": True, "same_account": same,
            "details": "Account identity matched" if same else "Drive and Gmail accounts differ; transfers blocked"}


def assert_same_account() -> None:
    result = connection_health()
    if result["status"] != "PASS":
        raise InputError("Drive/Gmail account consistency check failed; Gmail -> Drive upload blocked")
