import unittest

from msp_bonds_telegram.config import TelegramSettings
from msp_bonds_telegram.errors import TelegramConfigurationError


VALID = {
    "MSP_TELEGRAM_CHAT_ID": "-1001234567890",
    "MSP_TELEGRAM_MESSAGE_THREAD_ID": "42",
    "MSP_TELEGRAM_TARGET_CONFIRMED": "1",
}


class TelegramSettingsTests(unittest.TestCase):
    def test_required_fixed_destination(self):
        with self.assertRaises(TelegramConfigurationError):
            TelegramSettings.from_environment({})
        with self.assertRaises(TelegramConfigurationError):
            TelegramSettings.from_environment({"MSP_TELEGRAM_CHAT_ID": VALID["MSP_TELEGRAM_CHAT_ID"]})

    def test_requires_explicit_target_confirmation(self):
        values = dict(VALID, MSP_TELEGRAM_TARGET_CONFIRMED="0")
        with self.assertRaises(TelegramConfigurationError):
            TelegramSettings.from_environment(values)

    def test_only_numeric_forum_supergroup_and_positive_topic(self):
        for chat_id in ("@somegroup", "-123456", "100123", ""):
            with self.subTest(chat_id=chat_id), self.assertRaises(TelegramConfigurationError):
                TelegramSettings.from_environment(dict(VALID, MSP_TELEGRAM_CHAT_ID=chat_id))
        for thread_id in ("0", "-1", "4.2", "general", ""):
            with self.subTest(thread_id=thread_id), self.assertRaises(TelegramConfigurationError):
                TelegramSettings.from_environment(dict(VALID, MSP_TELEGRAM_MESSAGE_THREAD_ID=thread_id))

    def test_valid_pair_is_immutable_integer_configuration(self):
        settings = TelegramSettings.from_environment(VALID)
        self.assertEqual(settings.chat_id, -1001234567890)
        self.assertEqual(settings.message_thread_id, 42)
        self.assertTrue(settings.target_confirmed)
        with self.assertRaises((AttributeError, TypeError)):
            settings.chat_id = -1009876543210


if __name__ == "__main__":
    unittest.main()
