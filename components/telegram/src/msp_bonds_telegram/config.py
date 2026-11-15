"""Strict settings for one owner-approved Telegram forum topic."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

from .errors import TelegramConfigurationError

_CHAT_ID = re.compile(r"-100[0-9]+\Z")
_POSITIVE_INTEGER = re.compile(r"[1-9][0-9]*\Z")
_BOT_API_MAX_SAFE_ID = 2**52


@dataclass(frozen=True, slots=True)
class TelegramSettings:
    """A single immutable destination; send_event never accepts recipient fields."""

    chat_id: int
    message_thread_id: int
    target_confirmed: bool

    def __post_init__(self) -> None:
        if isinstance(self.chat_id, bool) or not isinstance(self.chat_id, int):
            raise TelegramConfigurationError("Telegram chat_id must be a numeric supergroup ID.")
        if not _CHAT_ID.fullmatch(str(self.chat_id)) or abs(self.chat_id) > _BOT_API_MAX_SAFE_ID:
            raise TelegramConfigurationError("Telegram chat_id must identify a numeric forum supergroup.")
        if isinstance(self.message_thread_id, bool) or not isinstance(self.message_thread_id, int):
            raise TelegramConfigurationError("Telegram message_thread_id must be a positive integer.")
        if self.message_thread_id <= 0:
            raise TelegramConfigurationError("Telegram message_thread_id must be a positive integer.")
        if self.target_confirmed is not True:
            raise TelegramConfigurationError("The owner must confirm the fixed Telegram target before sending.")

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "TelegramSettings":
        """Read only fixed routing settings; dotenv files and token env vars are not read."""

        values = os.environ if environ is None else environ
        raw_chat_id = values.get("MSP_TELEGRAM_CHAT_ID", "").strip()
        raw_thread_id = values.get("MSP_TELEGRAM_MESSAGE_THREAD_ID", "").strip()
        confirmed = values.get("MSP_TELEGRAM_TARGET_CONFIRMED", "") == "1"

        if not raw_chat_id:
            raise TelegramConfigurationError("MSP_TELEGRAM_CHAT_ID is required.")
        if not raw_thread_id:
            raise TelegramConfigurationError("MSP_TELEGRAM_MESSAGE_THREAD_ID is required.")
        if not _CHAT_ID.fullmatch(raw_chat_id):
            raise TelegramConfigurationError("MSP_TELEGRAM_CHAT_ID must be a numeric -100 forum ID.")
        if not _POSITIVE_INTEGER.fullmatch(raw_thread_id):
            raise TelegramConfigurationError("MSP_TELEGRAM_MESSAGE_THREAD_ID must be a positive integer.")
        if not confirmed:
            raise TelegramConfigurationError("MSP_TELEGRAM_TARGET_CONFIRMED must be 1 after owner verification.")

        chat_id = int(raw_chat_id)
        thread_id = int(raw_thread_id)
        if abs(chat_id) > _BOT_API_MAX_SAFE_ID:
            raise TelegramConfigurationError("MSP_TELEGRAM_CHAT_ID is outside the Bot API safe integer range.")
        return cls(chat_id, thread_id, target_confirmed=True)
