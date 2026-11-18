"""Этап 2: эксплуатационная устойчивость.

Проверяем то, что должно выдерживать сервис в работе: один поток добора,
отчёт по времени с догоняющим отправкой, ротацию журналов, ротацию фотографий,
индексы и чекпоинт базы, мягкую остановку.
"""
import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД

import sys
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import requests

import anpr_telegram as bot
from app.services import control as service_control
from app.services import logs as service_logs
from app.storage import database as storage
from app.storage.photo_archive import cleanup_old_photos


def _state():
    return {'days': {}, 'seen': [], 'last_report_date': '', 'reported_days': [],
            'last_event_time': '', 'last_rec_no': 0}


class LogRotationTests(unittest.TestCase):
    def test_file_rotates_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'service.stdout.log'
            writer = service_logs.RotatingFileWriter(path, max_bytes=1024, backups=2)
            try:
                for _ in range(30):
                    writer.write('строка журнала\n' * 10)
            finally:
                writer.close()
            self.assertTrue(path.exists())
            self.assertTrue((Path(tmp) / 'service.stdout.log.1').exists())
            self.assertLessEqual(path.stat().st_size, 2048)

    def test_write_failure_does_not_break_the_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'service.stdout.log'
            writer = service_logs.RotatingFileWriter(path, max_bytes=1024, backups=1)
            try:
                writer.close()
                writer.write('после закрытия\n')  # не должно поднимать исключение
            finally:
                writer.close()

    def test_install_redirects_print_into_the_log_file(self):
        """Весь вывод сервиса попадает в файл, а не теряется в консоли."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            old_out, old_err = sys.stdout, sys.stderr
            installed = service_logs._installed
            try:
                service_logs._installed = False
                service_logs.install(base)
                print('строка в журнале')
                sys.stderr.write('ошибка в журнале\n')
            finally:
                sys.stdout, sys.stderr = old_out, old_err
                service_logs._installed = installed
                for name in ('service.stdout.log', 'service.stderr.log'):
                    service_logs.writer_for(base / name).close()
            out = (base / 'service.stdout.log').read_text(encoding='utf-8')
            err = (base / 'service.stderr.log').read_text(encoding='utf-8')
        self.assertIn('строка в журнале', out)
        self.assertIn('ошибка в журнале', err)

    def test_uvicorn_log_config_uses_the_same_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = service_logs.uvicorn_log_config(tmp)
            handlers = config['handlers']
            self.assertIn('app.services.logs.BridgeHandler', handlers['app_out']['class'])
            self.assertTrue(handlers['app_out']['filename'].endswith('service.stdout.log'))
            self.assertTrue(handlers['app_err']['filename'].endswith('service.stderr.log'))


class ReportWindowTests(unittest.TestCase):
    """Отчёт уходит по времени, а не в ту минуту, когда повезло."""

    def _now(self, hour, minute):
        return bot.now_local().replace(hour=hour, minute=minute, second=0, microsecond=0)

    def test_before_report_time_nothing_is_due(self):
        with patch.object(bot, 'REPORT_TIME', '20:00'), patch.object(bot, 'has_passages', return_value=False):
            self.assertEqual(bot.due_report_days(_state(), self._now(10, 0)), [])

    def test_after_report_time_today_is_due(self):
        with patch.object(bot, 'REPORT_TIME', '20:00'), patch.object(bot, 'has_passages', return_value=False):
            self.assertEqual(bot.due_report_days(_state(), self._now(20, 5)), ['%s' % self._now(20, 5).strftime('%Y-%m-%d')])

    def test_report_stays_due_inside_the_window(self):
        """Сервис перезапустили через час после отчёта — отчёт всё равно уходит."""
        now = self._now(21, 30)
        with patch.object(bot, 'REPORT_TIME', '20:00'), patch.object(bot, 'REPORT_WINDOW_MINUTES', 240), \
             patch.object(bot, 'has_passages', return_value=False):
            self.assertIn(now.strftime('%Y-%m-%d'), bot.due_report_days(_state(), now))

    def test_window_closes_the_report(self):
        now = self._now(23, 59)
        with patch.object(bot, 'REPORT_TIME', '20:00'), patch.object(bot, 'REPORT_WINDOW_MINUTES', 60), \
             patch.object(bot, 'has_passages', return_value=False):
            self.assertNotIn(now.strftime('%Y-%m-%d'), bot.due_report_days(_state(), now))

    def test_missed_report_of_yesterday_is_caught_up(self):
        now = self._now(9, 0)
        yesterday = (now - timedelta(days=1)).strftime('%Y-%m-%d')
        with patch.object(bot, 'REPORT_TIME', '20:00'), patch.object(bot, 'has_passages', return_value=True):
            self.assertIn(yesterday, bot.due_report_days(_state(), now))

    def test_empty_day_is_not_caught_up(self):
        now = self._now(9, 0)
        yesterday = (now - timedelta(days=1)).strftime('%Y-%m-%d')
        with patch.object(bot, 'REPORT_TIME', '20:00'), patch.object(bot, 'has_passages', return_value=False):
            self.assertNotIn(yesterday, bot.due_report_days(_state(), now))

    def test_reported_day_is_not_sent_twice(self):
        now = self._now(21, 0)
        state = _state()
        state['reported_days'] = [now.strftime('%Y-%m-%d')]
        with patch.object(bot, 'REPORT_TIME', '20:00'), patch.object(bot, 'has_passages', return_value=True):
            self.assertNotIn(now.strftime('%Y-%m-%d'), bot.due_report_days(state, now))

    def test_mark_reported_keeps_last_report_date(self):
        state = _state()
        bot.mark_reported(state, '2026-11-18')
        self.assertEqual(state['last_report_date'], '2026-11-18')
        self.assertIn('2026-11-18', state['reported_days'])


class ReportWorkerTests(unittest.TestCase):
    def test_worker_sends_report_once_and_stops(self):
        sent = []
        stop = threading.Event()
        state = _state()
        now = bot.now_local().replace(hour=20, minute=5, second=0, microsecond=0)

        def fake_send(text, kind=''):
            sent.append((text, kind))
            stop.set()
            return True

        with patch.object(bot, 'telegram_send', side_effect=fake_send), \
             patch.object(bot, 'now_local', return_value=now), \
             patch.object(bot, 'build_report', return_value='отчёт'), \
             patch.object(bot, 'has_passages', return_value=False), \
             patch.object(bot, 'save_state'):
            bot.report_worker(state, stop)
            stop.set()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1], 'report')
        self.assertIn(now.strftime('%Y-%m-%d'), state['reported_days'])


class BackfillThreadTests(unittest.TestCase):
    def test_worker_starts_immediately_and_repeats(self):
        calls = []
        stop = threading.Event()
        with patch.object(bot, 'run_backfill', side_effect=lambda *a, **k: calls.append(1)), \
             patch.object(bot, 'BACKFILL_INTERVAL', 0.05), \
             patch.object(bot, 'reconcile_state_with_store', return_value=0):
            thread = threading.Thread(target=bot.backfill_worker, args=(_state(), stop), daemon=True)
            thread.start()
            deadline = time.time() + 5
            while len(calls) < 2 and time.time() < deadline:
                time.sleep(0.02)
            stop.set()
            thread.join(5)
        self.assertGreaterEqual(len(calls), 2)

    def test_only_one_thread_per_role(self):
        started = []

        def worker(*args, **kwargs):
            started.append(1)
            time.sleep(0.3)

        try:
            first = bot.start_worker('test-role', worker)
            second = bot.start_worker('test-role', worker)
            self.assertIs(first, second)
            self.assertEqual(len(started), 1)
        finally:
            time.sleep(0.4)

    def test_camera_is_not_blocked_while_archive_is_downloaded(self):
        """Добор не держит общий замок состояния во время сетевого запроса."""
        release = threading.Event()
        downloading = threading.Event()
        state = _state()

        def fake_fetch(sess):
            downloading.set()
            release.wait(5)
            return []

        with patch.object(bot, '_fetch_archive_records', side_effect=fake_fetch), \
             patch.object(bot, 'telegram_send', return_value=True):
            thread = threading.Thread(target=bot.backfill_once, args=(object(), state), daemon=True)
            thread.start()
            self.assertTrue(downloading.wait(5), 'добор не начался')
            free = bot.STATE_LOCK.acquire(blocking=False)
            if free:
                bot.STATE_LOCK.release()
            self.assertTrue(free, 'камера заблокирована на время скачивания архива')
            release.set()
            thread.join(5)


class ImageRetentionTests(unittest.TestCase):
    def test_old_day_folders_are_removed_and_others_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / '2020-01-01'
            old.mkdir()
            (old / 'photo.jpg').write_bytes(b'x')
            fresh = root / bot.now_local().strftime('%Y-%m-%d')
            fresh.mkdir()
            diagnostic = root / 'diagnostic'
            diagnostic.mkdir()
            removed = cleanup_old_photos(root, keep_days=30)
            self.assertEqual(removed, 1)
            self.assertFalse(old.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(diagnostic.exists())

    def test_missing_folder_is_not_an_error(self):
        self.assertEqual(cleanup_old_photos(Path('/no/such/folder'), keep_days=30), 0)


class DatabaseGrowthTests(unittest.TestCase):
    def test_indexes_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'events.sqlite3'
            store = storage.Store(db)
            try:
                names = {row[0] for row in store.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'")}
            finally:
                store.close()
            self.assertIn('idx_events_day', names)
            self.assertIn('idx_events_captured_at', names)

    def test_wal_checkpoint_truncates_the_wal_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'events.sqlite3'
            store = storage.Store(db)
            wal = db.with_name(db.name + '-wal')
            try:
                store.add_event('key-1', {'iso': '2026-11-18T10:00:00+05:00', 'plate': 'A'})
                self.assertTrue(store.checkpoint())
                self.assertTrue(wal.exists())
                self.assertEqual(wal.stat().st_size, 0)
            finally:
                store.close()

    def test_count_day_counts_passages(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'events.sqlite3'
            store = storage.Store(db)
            try:
                store.add_event('key-2', {'iso': '2026-11-18T10:00:00+05:00', 'plate': 'B'})
                self.assertEqual(store.count_day('2026-11-18'), 1)
                self.assertEqual(store.count_day('2026-11-17'), 0)
            finally:
                store.close()


class GracefulShutdownTests(unittest.TestCase):
    def test_shutdown_saves_state_and_closes_the_store(self):
        from app.services import http_api as app
        done = {}

        class FakeStore:
            def close(self):
                done['closed'] = True

        try:
            with patch.object(app.M, 'STORE', FakeStore()), \
                 patch.object(app.M, 'save_state', side_effect=lambda st: done.update(state=st)), \
                 patch.object(app, '_worker_started', True), \
                 patch.object(app, '_worker', None):
                app.shutdown_service()
        finally:
            bot._stop_event.clear()
        self.assertTrue(done.get('closed'))
        self.assertIn('state', done)

    def test_service_stops_through_the_shutdown_endpoint(self):
        calls = []

        class FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, url, timeout=None):
                calls.append(url)
                return object()

            def get(self, url, timeout=None):
                raise requests.ConnectionError()

        with patch.object(service_control.requests, 'Session', return_value=FakeSession()), \
             patch.object(service_control, 'health', side_effect=requests.ConnectionError()), \
             patch.object(service_control.subprocess, 'run') as kill:
            service_control.stop_gracefully(4242)
        self.assertTrue(any(url.endswith('/shutdown') for url in calls))
        kill.assert_not_called()

    def test_force_kill_only_as_a_last_resort(self):
        class FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, url, timeout=None):
                return object()

        with patch.object(service_control.requests, 'Session', return_value=FakeSession()), \
             patch.object(service_control, 'health', return_value={'pid': 4242}), \
             patch.object(service_control, 'STOP_TIMEOUT', 0.1), \
             patch.object(service_control.subprocess, 'run') as kill:
            service_control.stop_gracefully(4242)
        kill.assert_called_once()


if __name__ == '__main__':
    unittest.main()
