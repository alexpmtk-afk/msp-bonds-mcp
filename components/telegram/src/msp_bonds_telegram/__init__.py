"""Independent Telegram notification component and safe MCP-callable adapter."""

from .config import TelegramSettings
from .credentials import WindowsCredentialProvider
from .mcp_tool import create_telegram_notification_handler, send_telegram_notification
from .sender import NotificationOutcome, TelegramSender, notify_best_effort

__all__ = [
    "NotificationOutcome",
    "TelegramSender",
    "TelegramSettings",
    "WindowsCredentialProvider",
    "create_telegram_notification_handler",
    "notify_best_effort",
    "send_telegram_notification",
]

__version__ = "0.1.0"
