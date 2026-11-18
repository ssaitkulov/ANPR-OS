import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import anpr_telegram as bot
from app.storage.database import Store


class StorageTests(unittest.TestCase):
    def test_event_metadata_and_outbox_survive_reopen_without_images(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'events.sqlite3'
            store = Store(path)
            event_id = store.add_event('event-1', {
                'iso': '2026-11-16T20:00:00+05:00', 'plate': '01A123BC',
                'direction': 'Obverse', 'lane': '1', 'color': 'White',
            })
            store.enqueue_text(event_id, '42', '01A123BC · 20:00', 'entry_without_photo')
            store.close()
            reopened = Store(path)
            self.assertEqual(reopened.stats(), {'events': 1, 'pending_messages': 1})
            self.assertEqual(reopened.pending()[0][3], '01A123BC · 20:00')
            self.assertNotIn('photo', {row[4] for row in reopened.pending()})
            reopened.close()

    def test_failed_message_stays_in_queue_with_backoff(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'events.sqlite3')
            store.enqueue_text(None, '42', 'missed', 'entry_without_photo')
            row_id = store.pending()[0][0]
            status = store.mark_failed(row_id, 'offline')
            self.assertEqual(status, 'pending')
            attempts, next_at = store.conn.execute(
                'SELECT attempts, next_attempt_at FROM outbox WHERE id=?', (row_id,)).fetchone()
            self.assertEqual(attempts, 1)
            self.assertGreater(next_at, time.time())  # пауза перед повтором
            self.assertEqual(store.pending(), [])       # до срока не повторяем
            self.assertEqual(store.stats()['pending_messages'], 1)  # но запись жива
            store.close()


class OutboxTests(unittest.TestCase):
    def test_failed_photo_delivery_queues_text_only(self):
        class FakeStore:
            def __init__(self): self.rows = []
            def enqueue_text(self, event_id, chat_id, body, kind): self.rows.append((chat_id, body, kind))
        fake = FakeStore()
        with patch.object(bot, 'STORE', fake), patch.object(bot, 'get_recipients', return_value=['42']), \
             patch.object(bot, 'telegram_send_media_group_to', return_value=False), \
             patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'):
            bot.send_entry_notification({'plate': 'ABC123', 'iso': '2026-11-16T20:00:00+05:00', 'time': '20:00:00'}, [b'car', b'plate'])
        self.assertEqual(len(fake.rows), 1)
        self.assertEqual(fake.rows[0][2], 'entry_without_photo')
        self.assertNotIn('car', fake.rows[0][1])
        self.assertNotIn('plate', fake.rows[0][1])

    def test_pending_events_are_sent_as_one_list_per_chat(self):
        class FakeStore:
            def __init__(self): self.sent = []; self.failed = []
            def pending_grouped(self):
                return {'42': [(1, None, '42', 'ABC · 20:00', 'entry_without_photo', 0),
                               (2, None, '42', 'XYZ · 20:01', 'entry_without_photo', 0)]}
            def mark_sent(self, row_id): self.sent.append(row_id)
            def mark_failed(self, row_id, error): self.failed.append(row_id)
        fake = FakeStore()
        with patch.object(bot, 'STORE', fake), patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'telegram_send_to', return_value=True) as send:
            self.assertTrue(bot.flush_missed_messages())
        self.assertEqual(send.call_count, 1)
        body = send.call_args.args[1]
        self.assertIn('ABC · 20:00', body)
        self.assertIn('XYZ · 20:01', body)
        self.assertEqual(fake.sent, [1, 2])

    def test_missed_list_explains_why_photos_are_missing(self):
        class FakeStore:
            def pending_grouped(self):
                return {'42': [(1, None, '42', 'ABC · 20:00', 'entry_without_photo', 0)]}
            def mark_sent(self, row_id): pass
            def mark_failed(self, row_id, error): pass
        with patch.object(bot, 'STORE', FakeStore()), patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), patch.object(bot, 'telegram_send_to', return_value=True) as send:
            bot.flush_missed_messages()
        self.assertIn('Фотографии этих проездов недоступны', send.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
