import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import setup_anpr as setup
import anpr_telegram as bot


class SettingsTests(unittest.TestCase):
    def values(self):
        return dict(CAMERA_NAME='Ворота', CAMERA_URL='http://192.168.100.249/',
                    DAHUA_USERNAME='admin', DAHUA_PASSWORD='test-only',
                    TELEGRAM_BOT_TOKEN='123:example', TELEGRAM_CHAT_ID='123',
                    ALLOWED_CHAT_IDS='123', TIMEZONE='Asia/Tashkent')

    def test_preserves_other_settings_and_replaces_duplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('# comment\nCUSTOM=keep\nDAHUA_PASSWORD=old\nDAHUA_PASSWORD=older\n', encoding='utf-8')
            setup.save_settings(self.values(), path)
            self.assertEqual(setup.read_settings(path)['CUSTOM'], 'keep')
            self.assertEqual(path.read_text(encoding='utf-8').count('DAHUA_PASSWORD='), 1)
            self.assertEqual(setup.read_settings(path)['DAHUA_PORT'], '80')

    def test_invalid_values_do_not_overwrite_file(self):
        for key, value in [('CAMERA_URL', 'http://user:password@192.168.1.1/'),
                           ('ALLOWED_CHAT_IDS', ''), ('TELEGRAM_CHAT_ID', '456'),
                           ('CAMERA_NAME', 'name\nINJECT=1'), ('TIMEZONE', 'wrong/zone')]:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / '.env'
                path.write_text('UNCHANGED=yes', encoding='utf-8')
                values = self.values()
                values[key] = value
                with self.assertRaises(ValueError):
                    setup.save_settings(values, path)
                self.assertEqual(path.read_text(encoding='utf-8'), 'UNCHANGED=yes')

    def test_fail_closed_and_filter_preexisting_subscribers(self):
        with patch.object(bot, 'ALLOWED_CHAT_IDS', ''):
            self.assertFalse(bot._allowed('123'))
            self.assertEqual(bot.get_recipients(), [])
        with patch.object(bot, 'ALLOWED_CHAT_IDS', '123'), patch.object(bot, 'TELEGRAM_CHAT_ID', '456'), patch.object(bot, 'BROADCAST_ENABLED', True), patch.object(bot, 'load_subscribers', return_value=['123', '789']):
            self.assertEqual(bot.get_recipients(), ['123'])


if __name__ == '__main__':
    unittest.main()
