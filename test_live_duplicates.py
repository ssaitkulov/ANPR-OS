import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import anpr_telegram as bot
from app.storage.database import Store


class DuplicateTests(unittest.TestCase):
    def setUp(self):
        direction = patch.object(bot, 'DIRECTION', '')
        direction.start()
        self.addCleanup(direction.stop)
        # Тесты не должны писать в рабочую БД.
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = Store(Path(folder.name) / 'test.sqlite3')
        self.addCleanup(store.close)
        store_patch = patch.object(bot, 'STORE', store)
        store_patch.start()
        self.addCleanup(store_patch.stop)

    def test_reused_camera_id_does_not_block_later_passage(self):
        event = {'EventBaseInfo.Code': 'TrafficJunction', 'GroupID': '1048576',
                 'IndexInGroup': '1', 'TrafficCar.PlateNumber': '1', 'Lane': '1'}
        state = {'days': {}, 'seen': ['1048576:1:1']}
        start = datetime(2026, 11, 16, 20, tzinfo=timezone.utc)
        with patch.object(bot, 'save_state'), patch.object(bot, 'ALLOWED_LANES', ''), patch.object(bot, 'SAVE_PHOTOS', False):
            with patch.object(bot, 'now_local', return_value=start):
                self.assertIsNotNone(bot.record_event(state, event, None))
                self.assertIsNone(bot.record_event(state, event, None))
            with patch.object(bot, 'now_local', return_value=start + timedelta(seconds=16)):
                self.assertIsNotNone(bot.record_event(state, event, None))
        self.assertEqual(len(state['days']['2026-11-16']), 2)

    def test_repeated_recognition_slides_window_but_different_lane_passes(self):
        event = {'EventBaseInfo.Code': 'TrafficJunction', 'TrafficCar.PlateNumber': 'ABC', 'Lane': '1'}
        state = {'days': {}, 'seen': []}
        start = datetime(2026, 11, 16, 20, tzinfo=timezone.utc)
        with patch.object(bot, 'save_state'), patch.object(bot, 'ALLOWED_LANES', ''), patch.object(bot, 'SAVE_PHOTOS', False), patch.object(bot, 'DEDUP_WINDOW_SECONDS', 15):
            for seconds, expected in [(0, True), (10, False), (20, False), (36, True)]:
                with patch.object(bot, 'now_local', return_value=start + timedelta(seconds=seconds)):
                    self.assertEqual(bot.record_event(state, event, None) is not None, expected)
            with patch.object(bot, 'now_local', return_value=start + timedelta(seconds=37)):
                self.assertIsNotNone(bot.record_event(state, dict(event, Lane='2'), None))

    def test_identical_photo_after_restart_is_not_a_new_passage(self):
        event = {'EventBaseInfo.Code': 'TrafficJunction', 'TrafficCar.PlateNumber': 'ABC', 'Lane': '1'}
        state = {'days': {}, 'seen': []}
        with patch.object(bot, 'save_state'), patch.object(bot, 'ALLOWED_LANES', ''), patch.object(bot, 'SAVE_PHOTOS', False):
            self.assertIsNotNone(bot.record_event(state, event, b'same photo'))
            state['recent_live'] = {}
            self.assertIsNone(bot.record_event(state, event, b'same photo'))
            self.assertIsNotNone(bot.record_event(state, event, b'new photo'))


if __name__ == '__main__':
    unittest.main()
