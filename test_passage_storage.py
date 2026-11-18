"""Storage of passages: one row per passage, live and archive alike."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import anpr_telegram as bot
from storage import Store, passage_key, backfill_key


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.store = Store(self.folder / 'events.sqlite3')
        self.addCleanup(self.store.close)
        store_patch = patch.object(bot, 'STORE', self.store)
        store_patch.start()
        self.addCleanup(store_patch.stop)


class PassageStorageTests(StoreTestCase):
    def test_same_group_id_keeps_every_passage(self):
        """Прошивка переиспользует GroupID — каждый проезд должен попасть в БД."""
        event = {'EventBaseInfo.Code': 'TrafficJunction', 'GroupID': '1048576',
                 'IndexInGroup': '1', 'TrafficCar.PlateNumber': 'RR', 'Lane': '1'}
        state = {'days': {}, 'seen': []}
        start = datetime(2026, 11, 16, 20, 9, tzinfo=timezone.utc)
        with patch.object(bot, 'save_state'), patch.object(bot, 'ALLOWED_LANES', ''):
            with patch.object(bot, 'now_local', return_value=start):
                self.assertIsNotNone(bot.record_event(state, event, None))
            with patch.object(bot, 'now_local', return_value=start + timedelta(seconds=20)):
                self.assertIsNotNone(bot.record_event(state, event, None))
        rows = self.store.day_passages('2026-11-16')
        self.assertEqual(len(rows), 2)
        self.assertEqual({row['plate'] for row in rows}, {'RR'})
        self.assertEqual(rows[0]['source_key'], '1048576:1:RR:1')

    def test_backfill_event_is_stored_with_archive_flag(self):
        state = {'days': {}, 'seen': []}
        when = datetime(2026, 11, 16, 20, 9, tzinfo=timezone.utc)
        with patch.object(bot, 'save_state'):
            rec = bot.record_backfill_event(state, 'RR', when, lane='1', direction='Approach', rec_no=4242)
        self.assertIsNotNone(rec)
        rows = self.store.day_passages('2026-11-16')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['backfill'], 1)
        self.assertEqual(rows[0]['plate'], 'RR')
        self.assertEqual(rows[0]['source_key'], None)

    def test_archive_record_is_not_duplicated_by_a_live_one(self):
        state = {'days': {}, 'seen': []}
        when = datetime(2026, 11, 16, 20, 9, tzinfo=timezone.utc)
        with patch.object(bot, 'save_state'), patch.object(bot, 'now_local', return_value=when):
            live = bot.record_event(state, {'EventBaseInfo.Code': 'TrafficJunction', 'GroupID': '1',
                                             'IndexInGroup': '1', 'TrafficCar.PlateNumber': 'RR',
                                             'Lane': '1'}, None)
        self.assertIsNotNone(live)
        with patch.object(bot, 'save_state'):
            self.assertIsNone(bot.record_backfill_event(state, 'RR', when, lane='1'))
        self.assertEqual(len(self.store.day_passages('2026-11-16')), 1)

    def test_live_event_promotes_the_archive_row(self):
        """Если проезд сначала пришёл из архива, live-событие обновляет ту же строку."""
        state = {'days': {}, 'seen': []}
        when = datetime(2026, 11, 16, 20, 9, tzinfo=timezone.utc)
        with patch.object(bot, 'save_state'):
            self.assertIsNotNone(bot.record_backfill_event(state, 'RR', when, lane='1', rec_no=7))
        with patch.object(bot, 'save_state'), patch.object(bot, 'now_local', return_value=when):
            live = bot.record_event(state, {'EventBaseInfo.Code': 'TrafficJunction', 'GroupID': '5',
                                             'IndexInGroup': '1', 'TrafficCar.PlateNumber': 'RR',
                                             'Lane': '1'}, None)
        self.assertIsNotNone(live)
        rows = self.store.day_passages('2026-11-16')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['backfill'], 0)
        self.assertEqual(rows[0]['source_key'], '5:1:RR:1')
        self.assertEqual(len(state['days']['2026-11-16']), 1)
        self.assertFalse(state['days']['2026-11-16'][0].get('backfill'))

    def test_same_passage_is_stored_once(self):
        record = {'iso': '2026-11-16T20:09:00.000000+05:00', 'plate': 'RR', 'lane': '1'}
        key = passage_key(record['iso'], record['plate'], record['lane'])
        first = self.store.add_event(key, record)
        second = self.store.add_event(key, record)
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.day_passages('2026-11-16')), 1)

    def test_missing_id_is_reported_instead_of_a_stale_rowid(self):
        record = {'iso': '2026-11-16T20:09:00.000000+05:00', 'plate': 'RR', 'lane': '1'}
        key = passage_key(record['iso'], record['plate'], record['lane'])
        self.store.add_event(key, record)
        other = {'iso': '2026-11-16T20:10:00.000000+05:00', 'plate': 'RR', 'lane': '1'}
        self.assertNotEqual(self.store.add_event('other-key', other), None)
        self.assertEqual(len(self.store.day_passages('2026-11-16')), 2)


class LegacyMigrationTests(unittest.TestCase):
    def test_legacy_unique_firmware_key_is_rebuilt(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / 'legacy.sqlite3'
        conn = sqlite3.connect(path)
        conn.executescript('''
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT UNIQUE,
                day TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                plate TEXT NOT NULL,
                direction TEXT,
                lane TEXT,
                speed TEXT,
                vehicle_color TEXT,
                plate_type TEXT,
                confidence TEXT,
                backfill INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        rows = [
            ('7:1:RR:1', '2026-11-16', '2026-11-16T20:09:00+05:00', 'RR', '1'),
            ('8:1:RR:1', '2026-11-16', '2026-11-16T20:12:00+05:00', 'RR', '1'),
        ]
        conn.executemany('''INSERT INTO events (event_key, day, captured_at, plate, lane)
                             VALUES (?,?,?,?,?)''', rows)
        conn.commit()
        conn.close()

        store = Store(path)
        self.addCleanup(store.close)
        passages = store.day_passages('2026-11-16')
        self.assertEqual(len(passages), 2)
        self.assertEqual(len({row['event_key'] for row in passages}), 2)
        self.assertEqual({row['source_key'] for row in passages}, {'7:1:RR:1', '8:1:RR:1'})
        # Миграция идемпотентна: повторное открытие ничего не ломает.
        reopened = Store(path)
        self.addCleanup(reopened.close)
        self.assertEqual(len(reopened.day_passages('2026-11-16')), 2)


class ReconcileTests(StoreTestCase):
    def test_json_only_passages_are_imported(self):
        state = {'days': {'2026-11-16': [
            {'plate': 'RR', 'iso': '2026-11-16T20:09:00+05:00', 'lane': '1', 'direction': 'Approach'},
            {'plate': 'UU', 'iso': '2026-11-16T20:10:00+05:00', 'lane': '1', 'direction': 'Approach',
             'backfill': True},
        ]}}
        imported = bot.reconcile_state_with_store(state)
        self.assertEqual(imported, 2)
        self.assertEqual(len(self.store.day_passages('2026-11-16')), 2)
        self.assertEqual(bot.reconcile_state_with_store(state), 0)

    def test_report_is_built_from_the_database(self):
        state = {'days': {'2026-11-16': []}}
        self.store.add_event(passage_key('2026-11-16T20:09:00+05:00', 'RR', '1'),
                             {'iso': '2026-11-16T20:09:00+05:00', 'plate': 'RR', 'lane': '1'})
        report = bot.build_report(state, '2026-11-16')
        self.assertIn('RR', report)
        self.assertIn('Всего проездов: <b>1</b>', report)


class KeyTests(unittest.TestCase):
    def test_keys_differ_for_the_same_firmware_identifiers(self):
        first = passage_key('2026-11-16T20:09:00.000001+05:00', 'RR', '1')
        second = passage_key('2026-11-16T20:09:01.000001+05:00', 'RR', '1')
        self.assertNotEqual(first, second)

    def test_archive_key_uses_record_number(self):
        self.assertEqual(backfill_key(17, '2026-11-16T20:09:00+05:00', 'RR', '1'), 'backfill:rec:17')
        self.assertTrue(backfill_key(None, '2026-11-16T20:09:00+05:00', 'RR', '1').startswith('backfill:'))


if __name__ == '__main__':
    unittest.main()
