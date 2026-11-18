"""Очередь недоставленных: доставка без камеры, архивные списки, частичная доставка."""
import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД

import threading
import time
import unittest
from unittest.mock import patch

import anpr_telegram as bot


class FakeStore:
    """Хранилище в памяти: достаточно методов очереди и проездов."""

    def __init__(self):
        self.rows = []          # (id, event_id, chat_id, body, kind, attempts)
        self.sent = []
        self.failed = []
        self.events = []

    def enqueue_text(self, event_id, chat_id, body, kind='text'):
        self.rows.append((len(self.rows) + 1, event_id, chat_id, body, kind, 0))

    def pending(self, limit=100):
        return [row for row in self.rows if row[0] not in self.sent]

    def pending_grouped(self, limit=1000):
        grouped = {}
        for row in self.pending(limit):
            grouped.setdefault(row[2], []).append(row)
        return grouped

    def mark_sent(self, row_id):
        self.sent.append(row_id)

    def mark_failed(self, row_id, error):
        self.failed.append(row_id)

    def add_event(self, key, record, source_key=None):
        self.events.append((key, record))
        return len(self.events)

    def day_passages(self, day):
        return []

    def passage_exists(self, captured_at, plate):
        return False

    def set_photo_path(self, captured_at, plate, path):
        pass


class OutboxWorkerTests(unittest.TestCase):
    def test_worker_delivers_queue_without_camera(self):
        store = FakeStore()
        store.enqueue_text(None, '42', 'ABC · 20:00', 'entry_without_photo')
        sent = threading.Event()
        stop = threading.Event()

        def _send(chat_id, text):
            sent.set()
            return True

        with patch.object(bot, 'STORE', store), patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'telegram_send_to', side_effect=_send):
            worker = threading.Thread(target=bot.outbox_worker, args=(stop,), daemon=True)
            worker.start()
            try:
                self.assertTrue(sent.wait(5), 'очередь не доставлена без камеры')
            finally:
                stop.set()
                worker.join(5)
        self.assertEqual(store.sent, [1])
        self.assertEqual(store.pending_grouped(), {})

    def test_worker_keeps_message_when_telegram_is_down(self):
        store = FakeStore()
        store.enqueue_text(None, '42', 'ABC · 20:00', 'entry_without_photo')
        stop = threading.Event()
        with patch.object(bot, 'STORE', store), patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'telegram_send_to', return_value=False):
            bot.flush_missed_messages()
        self.assertEqual(store.sent, [])
        self.assertEqual(store.failed, [1])
        self.assertEqual(len(store.pending_grouped()['42']), 1)


class BackfillOutboxTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            return None

    class FakeSession:
        def __init__(self, text):
            self.text = text

        def get(self, url, params=None, timeout=None):
            return BackfillOutboxTests.FakeResponse(self.text)

    def _record_text(self):
        stamp = int(time.time())
        return ('found=1\n'
                f'records[0].RecNo=4242\n'
                f'records[0].Time={stamp}\n'
                'records[0].PlateNumber=TEST123\n'
                'records[0].Lane=1\n'
                'records[0].JunctionDirection=0\n'
                'records[0].VehicleColor=White\n')

    def test_archive_list_is_queued_when_telegram_fails(self):
        store = FakeStore()
        state = {'last_rec_no': 0, 'days': {}, 'seen': [], 'last_event_time': ''}
        session = self.FakeSession(self._record_text())
        with patch.object(bot, 'STORE', store), patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'get_recipients', return_value=['42']), \
             patch.object(bot, 'telegram_send_to', return_value=False), \
             patch.object(bot, 'archive_time', return_value=bot.now_local()), \
             patch.object(bot, 'save_state'):
            added = bot.backfill_once(session, state)
        self.assertEqual(added, 1)
        self.assertEqual(len(store.rows), 1)
        chat_id, body, kind = store.rows[0][2], store.rows[0][3], store.rows[0][4]
        self.assertEqual(chat_id, '42')
        self.assertEqual(kind, 'backfill')
        self.assertIn('TEST123', body)

    def test_archive_list_is_not_queued_when_delivered(self):
        store = FakeStore()
        state = {'last_rec_no': 0, 'days': {}, 'seen': [], 'last_event_time': ''}
        session = self.FakeSession(self._record_text())
        with patch.object(bot, 'STORE', store), patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'get_recipients', return_value=['42']), \
             patch.object(bot, 'telegram_send_to', return_value=True), \
             patch.object(bot, 'archive_time', return_value=bot.now_local()), \
             patch.object(bot, 'save_state'):
            added = bot.backfill_once(session, state)
        self.assertEqual(added, 1)
        self.assertEqual(store.rows, [])


class PartialDeliveryTests(unittest.TestCase):
    def test_only_failed_chat_gets_the_message_queued(self):
        store = FakeStore()
        with patch.object(bot, 'STORE', store), patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'get_recipients', return_value=['1', '2']), \
             patch.object(bot, 'telegram_send_media_group_to', side_effect=lambda cid, items: cid == '1'):
            ok = bot.telegram_send_media_group([(b'car', 'caption'), (b'plate', '')])
        self.assertTrue(ok)
        self.assertEqual([(row[2], row[4]) for row in store.rows], [('2', 'entry_without_photo')])

    def test_failed_photo_queues_text_for_that_chat(self):
        store = FakeStore()
        with patch.object(bot, 'STORE', store), patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'get_recipients', return_value=['1', '2']), \
             patch.object(bot, 'telegram_send_photo_to', side_effect=lambda cid, image, caption: cid == '1'):
            ok = bot.telegram_send_photo(b'car', 'caption')
        self.assertTrue(ok)
        self.assertEqual([row[2] for row in store.rows], ['2'])

    def test_failed_text_delivery_is_queued(self):
        store = FakeStore()
        with patch.object(bot, 'STORE', store), patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'get_recipients', return_value=['1', '2']), \
             patch.object(bot, 'telegram_send_to', side_effect=lambda cid, text: cid == '1'):
            ok = bot.telegram_send('дневной отчёт', kind='report')
        self.assertTrue(ok)
        self.assertEqual([(row[2], row[4]) for row in store.rows], [('2', 'report')])


class BackfillConcurrencyTests(unittest.TestCase):
    def test_second_backfill_is_skipped_while_first_runs(self):
        started = threading.Event()
        release = threading.Event()

        def _slow_backfill(sess, state, dry_run=False):
            started.set()
            release.wait(5)
            return 0

        with patch.object(bot, 'backfill_once', side_effect=_slow_backfill):
            worker = threading.Thread(target=bot.run_backfill, args=({},), daemon=True)
            worker.start()
            try:
                self.assertTrue(started.wait(5))
                self.assertEqual(bot.run_backfill({}), 0)
            finally:
                release.set()
                worker.join(5)


class PhotoArchiveTests(unittest.TestCase):
    def test_photo_is_not_written_when_saving_is_disabled(self):
        store = FakeStore()
        record = {'iso': '2026-11-17T12:00:00+05:00', 'plate': 'A', 'lane': '1', 'speed': '0', 'time': '12:00:00'}
        with patch.object(bot, 'STORE', store), patch.object(bot, 'PHOTOS'), \
             patch.object(bot, 'record_event', return_value=record), \
             patch.object(bot, 'prepare_event_images', return_value=[b'jpeg']), \
             patch.object(bot, 'send_entry_notification'), patch.object(bot, 'EVENTS'), \
             patch.object(bot, 'save_photo', return_value='/tmp/a.jpg') as save:
            with patch.object(bot, 'SAVE_PHOTOS', False):
                bot.flush_pending_events({}, {0: {}}, [b'jpeg'])
        save.assert_not_called()

    def test_photo_is_written_when_saving_is_enabled(self):
        store = FakeStore()
        record = {'iso': '2026-11-17T12:00:00+05:00', 'plate': 'A', 'lane': '1', 'speed': '0', 'time': '12:00:00'}
        with patch.object(bot, 'STORE', store), patch.object(bot, 'PHOTOS'), \
             patch.object(bot, 'record_event', return_value=record), \
             patch.object(bot, 'prepare_event_images', return_value=[b'jpeg']), \
             patch.object(bot, 'send_entry_notification'), patch.object(bot, 'EVENTS'), \
             patch.object(bot, 'save_photo', return_value='/tmp/a.jpg') as save:
            with patch.object(bot, 'SAVE_PHOTOS', True):
                bot.flush_pending_events({}, {0: {}}, [b'jpeg'])
        save.assert_called_once()


if __name__ == '__main__':
    unittest.main()
