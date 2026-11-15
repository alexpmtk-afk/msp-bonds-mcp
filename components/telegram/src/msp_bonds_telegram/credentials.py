"""Read the bot token only from the Windows Credential Locker keyring backend."""

from __future__ import annotations

import importlib
import os
from typing import Any

from .errors import TelegramCredentialBackendError, TelegramCredentialUnavailable

KEYRING_SERVICE = "msp-bonds-telegram"
KEYRING_USERNAME = "bot-token"


class WindowsCredentialProvider:
    """Fail closed unless keyring selected the native WinVaultKeyring backend."""

    def __init__(
        self,
        *,
        keyring_api: Any | None = None,
        backend_type: type | None = None,
        platform_name: str | None = None,
    ) -> None:
        self._keyring_api = keyring_api
        self._backend_type = backend_type
        self._platform_name = os.name if platform_name is None else platform_name

    def _load_dependencies(self) -> tuple[Any, type]:
        if self._keyring_api is not None and self._backend_type is not None:
            return self._keyring_api, self._backend_type
        try:
            api = self._keyring_api or importlib.import_module("keyring")
            backend_type = self._backend_type
            if backend_type is None:
                module = importlib.import_module("keyring.backends.Windows")
                backend_type = module.WinVaultKeyring
            return api, backend_type
        except Exception:
            raise TelegramCredentialBackendError("Windows Credential Locker backend is unavailable.") from None

    def get_token(self) -> str:
        if self._platform_name != "nt":
            raise TelegramCredentialBackendError("Telegram credentials are supported only through Windows Credential Locker.")

        keyring_api, expected_backend = self._load_dependencies()
        try:
            active_backend = keyring_api.get_keyring()
        except Exception:
            raise TelegramCredentialBackendError("Windows Credential Locker backend could not be verified.") from None

        if type(active_backend) is not expected_backend:
            raise TelegramCredentialBackendError("Active keyring is not the native Windows Credential Locker backend.")

        try:
            token = keyring_api.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
        except Exception:
            raise TelegramCredentialUnavailable("Telegram token could not be read from Windows Credential Locker.") from None

        if (
            not isinstance(token, str)
            or not token
            or len(token) > 1024
            or token != token.strip()
            or any(ord(char) < 33 or ord(char) == 127 for char in token)
        ):
            raise TelegramCredentialUnavailable("No usable Telegram token is stored in Windows Credential Locker.")
        return token
