"""Этап 4: тесты инструментов приёмки и проверки зависимостей.

Инструменты приёмки (`tools/acceptance_check.py`) и проверки файлов
зависимостей (`tools/check_lock.py`) — часть поставки: по ним принимают
работу в бой. Поэтому их поведение проверяется тестами, а не «руками».
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR / 'tools'))

import acceptance_check  # noqa: E402
import check_lock  # noqa: E402

SCHEMA = '''
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT UNIQUE, day TEXT, captured_at TEXT, plate TEXT,
    direction TEXT, lane TEXT, backfill INTEGER DEFAULT 0
);
CREATE TABLE passage_photo_paths (
    captured_at TEXT, plate TEXT, path TEXT,
    PRIMARY KEY (captured_at, plate)
);
CREATE TABLE outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER, chat_id TEXT, body TEXT, kind TEXT DEFAULT 'text',
    status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0,
    last_error TEXT, next_attempt_at REAL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    sent_at TEXT
);
'''


class LockFileTests(unittest.TestCase):
    """Файлы зависимостей не должны разъезжаться между собой."""

    def test_requirements_lock_and_pyproject_agree(self):
        report = check_lock.check()
        self.assertTrue(report['ok'], msg=json.dumps(report, ensure_ascii=False))
        self.assertTrue(report['requirements'], 'в requirements.txt нет зависимостей')

    def test_transitive_packages_stay_in_the_lock_file_only(self):
        report = check_lock.check()
        self.assertTrue(report['transitive_only_in_lock'],
                        'lock-файл должен содержать и транзитивные зависимости')


class AcceptanceToolTests(unittest.TestCase):
    """Проверки приёмки должны ловить нарушения, а не молчать."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory(prefix='anpr-acceptance-')
        self.root = Path(self._folder.name)
        self.conn = sqlite3.connect(self.root / 'events.sqlite3')
        self.conn.executescript(SCHEMA)
        self.report = acceptance_check.Report()

    def tearDown(self):
        self.conn.close()
        self._folder.cleanup()

    def _event(self, day, captured_at, plate, backfill=0):
        self.conn.execute(
            'INSERT INTO events (event_key, day, captured_at, plate, backfill) VALUES (?,?,?,?,?)',
            (f'{captured_at}-{plate}-{backfill}', day, captured_at, plate, backfill))
        self.conn.commit()

    def test_clean_outbox_passes(self):
        acceptance_check.check_outbox(self.report, self.conn, 30)
        statuses = {check['status'] for check in self.report.checks}
        self.assertEqual(statuses, {acceptance_check.OK})

    def test_dead_message_is_blocking(self):
        self.conn.execute("INSERT INTO outbox (chat_id, body, status, created_at) "
                          "VALUES ('1', 'текст', 'dead', datetime('now','-1 day'))")
        self.conn.commit()
        acceptance_check.check_outbox(self.report, self.conn, 30)
        self.assertTrue(any(check['status'] == acceptance_check.FAIL
                            for check in self.report.checks))

    def test_stale_pending_message_is_a_warning(self):
        old = (datetime.now() - timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S')
        self.conn.execute("INSERT INTO outbox (chat_id, body, status, created_at) "
                          "VALUES ('1', 'текст', 'pending', ?)", (old,))
        self.conn.commit()
        acceptance_check.check_outbox(self.report, self.conn, 30)
        self.assertTrue(any(check['status'] == acceptance_check.WARN
                            for check in self.report.checks))
        self.assertEqual(self.report.failed, 0)

    def test_missing_report_for_a_past_day_is_blocking(self):
        day = '2026-11-01'
        self._event(day, f'{day}T10:00:00+05:00', '01ABC123')
        self.conn.commit()
        acceptance_check.check_report(self.report, self.conn, {}, day,
                                      datetime(2026, 11, 2, 12, tzinfo=None),
                                      '20:00', {})
        self.assertEqual(self.report.failed, 1)

    def test_report_before_report_time_is_not_blocking(self):
        day = '2026-11-18'
        self._event(day, f'{day}T10:00:00+05:00', '01ABC123')
        self.conn.commit()
        acceptance_check.check_report(self.report, self.conn, {}, day,
                                      datetime(2026, 11, 18, 12, tzinfo=None),
                                      '20:00', {})
        self.assertEqual(self.report.failed, 0)

    def test_sent_report_in_state_passes(self):
        day = '2026-11-01'
        self._event(day, f'{day}T10:00:00+05:00', '01ABC123')
        self.conn.commit()
        acceptance_check.check_report(self.report, self.conn, {'reported_days': [day]}, day,
                                      datetime(2026, 11, 2, 12, tzinfo=None),
                                      '20:00', {})
        self.assertEqual(self.report.failed, 0)

    def test_photo_archive_missing_file_is_blocking(self):
        day = '2026-11-18'
        self._event(day, f'{day}T10:00:00+05:00', '01ABC123', backfill=0)
        self.conn.execute('INSERT INTO passage_photo_paths VALUES (?,?,?)',
                          (f'{day}T10:00:00+05:00', '01ABC123', str(self.root / 'нет.jpg')))
        self.conn.commit()
        acceptance_check.check_photo_archive(self.report, self.conn, day, self.root)
        self.assertEqual(self.report.failed, 1)

    def test_photo_archive_with_file_passes(self):
        day = '2026-11-18'
        photo = self.root / 'есть.jpg'
        photo.write_bytes(b'jpeg')
        self._event(day, f'{day}T10:00:00+05:00', '01ABC123', backfill=0)
        self.conn.execute('INSERT INTO passage_photo_paths VALUES (?,?,?)',
                          (f'{day}T10:00:00+05:00', '01ABC123', str(photo)))
        self.conn.commit()
        acceptance_check.check_photo_archive(self.report, self.conn, day, self.root)
        self.assertEqual(self.report.failed, 0)

    def test_recipients_are_counted_from_env_and_subscribers(self):
        subscribers = self.root / 'subscribers.json'
        subscribers.write_text(json.dumps(['111', '222']), encoding='utf-8')
        env = {'TELEGRAM_CHAT_ID': '999', 'SUBSCRIBERS_FILE': str(subscribers),
               'ALLOWED_CHAT_IDS': ''}
        expected = acceptance_check.expected_recipients(env)
        self.assertEqual(expected, ['111', '222', '999'])

    def test_allowed_list_filters_recipients(self):
        subscribers = self.root / 'subscribers.json'
        subscribers.write_text(json.dumps(['111', '222']), encoding='utf-8')
        env = {'TELEGRAM_CHAT_ID': '999', 'SUBSCRIBERS_FILE': str(subscribers),
               'ALLOWED_CHAT_IDS': '111,999'}
        expected = acceptance_check.expected_recipients(env)
        self.assertEqual(expected, ['111', '999'])

    def test_failed_delivery_is_reported_from_health(self):
        env = {'TELEGRAM_CHAT_ID': '999', 'SUBSCRIBERS_FILE': str(self.root / 'нет.json'),
               'ALLOWED_CHAT_IDS': ''}
        health = {'telegram_delivery': {'delivered': 0, 'failed': 1, 'at': '2026-11-18T10:00:00'}}
        acceptance_check.check_recipients(self.report, self.conn, env, health)
        self.assertTrue(any(check['status'] == acceptance_check.WARN
                            for check in self.report.checks))
        self.assertEqual(self.report.failed, 0)


if __name__ == '__main__':
    unittest.main()
