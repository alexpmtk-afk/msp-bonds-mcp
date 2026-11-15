"""Independent Telegram notification component; not registered with the MCP server."""

from .config import TelegramSettings
from .credentials import WindowsCredentialProvider
from .sender import NotificationOutcome, TelegramSender, notify_best_effort

__all__ = [
    "NotificationOutcome",
    "TelegramSender",
    "TelegramSettings",
    "WindowsCredentialProvider",
    "notify_best_effort",
]

__version__ = "0.1.0"
