import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo
import anpr_telegram as bot


class ArchiveTimeTests(unittest.TestCase):
    def test_old_archive_entry_with_wrong_timezone_does_not_hide_new_passage(self):
        when = datetime.fromisoformat('2026-11-16T21:43:02+05:00')
        state = {'days': {'2026-11-16': [{'plate': '010047MV', 'iso': '2026-11-16T19:43:54+03:00', 'direction': '0', 'backfill': True}]}}
        self.assertFalse(bot.is_duplicate_backfill(state, '010047MV', when, '0'))

    def test_local_archive_matches_live_across_original_date_error(self):
        stamp = int(datetime(2026, 11, 16, 20, 3, 6, tzinfo=timezone.utc).timestamp())
        with patch.object(bot, 'BACKFILL_TIMESTAMP_MODE', 'local'), patch.object(bot, 'TZ', ZoneInfo('Asia/Tashkent')):
            result = bot.archive_time(stamp)
            self.assertEqual(result.isoformat(), '2026-11-16T20:03:06+05:00')
            state = {'days': {'2026-11-16': [{'plate': '00047MV', 'iso': '2026-11-16T20:03:08+05:00'}]}}
            self.assertTrue(bot.is_duplicate_backfill(state, '00047MV', result))

    def test_utc_devices_keep_utc_conversion(self):
        with patch.object(bot, 'BACKFILL_TIMESTAMP_MODE', 'utc'), patch.object(bot, 'TZ', ZoneInfo('Asia/Tashkent')):
            stamp = int(datetime(2026, 11, 16, tzinfo=timezone.utc).timestamp())
            self.assertEqual(bot.archive_time(stamp).hour, 5)


if __name__ == '__main__':
    unittest.main()
