"""Надёжность доставки: backoff, тупик (dead), доставка по чатам, фоновый поток.

Сценарии проверяют критерии этапа «доставка не теряется»:
Telegram выключен на десять минут → включён: все накопленные сообщения доставлены
ровно по одному разу; один чат недоступен — его запись докатывается отдельно.
"""
from tests import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import anpr_telegram as bot
from app.storage import database as storage_module
from app.storage.database import Store


class StoreCase(unittest.TestCase):
    """Общая заготовка: временная база и патч бота на неё."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.store = Store(Path(self._folder.name) / 'outbox.sqlite3')

    def tearDown(self):
        self.store.close()
        self._folder.cleanup()

    def _make_due(self):
        """Снять паузу backoff — имитация «прошло 10 минут»."""
        with self.store.lock:
            self.store.conn.execute('UPDATE outbox SET next_attempt_at=0')
            self.store.conn.commit()

    def outbox_rows(self):
        with self.store.lock:
            return self.store.conn.execute(
                'SELECT id, chat_id, body, kind, status, attempts FROM outbox ORDER BY id').fetchall()

    def outbox_stats(self):
        return self.store.outbox_stats()


class TelegramOfflineScenarioTests(StoreCase):
    def test_messages_accumulated_while_telegram_was_down_are_delivered_once(self):
        bodies = [f'проезд №{i} · 12:0{i}' for i in range(3)]
        for body in bodies:
            self.store.enqueue_text(None, '42', body, 'entry_without_photo')

        sent = []
        with patch.object(bot, 'STORE', self.store), \
             patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'telegram_send_to', return_value=False):
            self.assertFalse(bot.flush_missed_messages())

        # Telegram выключен: ничего не доставлено, но всё сохранено в очереди.
        self.assertEqual(sent, [])
        self.assertEqual(self.store.stats()['pending_messages'], 3)
        self.assertEqual(self.store.pending(), [])          # ждём паузу перед повтором
        self.assertEqual(self.outbox_stats()['pending'], 3)

        # Telegram включили — прошло 10 минут, повтор разрешён.
        self._make_due()
        with patch.object(bot, 'STORE', self.store), \
             patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'telegram_send_to', side_effect=lambda chat, text: sent.append((chat, text)) or True):
            self.assertTrue(bot.flush_missed_messages())

        # Всё доставлено, каждое сообщение — ровно один раз.
        self.assertEqual(len(sent), 1)
        body = sent[0][1]
        for text in bodies:
            self.assertEqual(body.count(text), 1)
        self.assertEqual(self.store.stats()['pending_messages'], 0)

        # Повторный flush ничего не отправляет — доставка идемпотентна.
        with patch.object(bot, 'STORE', self.store), \
             patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'telegram_send_to', side_effect=lambda chat, text: sent.append((chat, text)) or True):
            self.assertFalse(bot.flush_missed_messages())
        self.assertEqual(len(sent), 1)

    def test_queue_survives_restart(self):
        self.store.enqueue_text(None, '42', 'пережить рестарт', 'entry_without_photo')
        path = self.store.path
        self.store.close()
        reopened = Store(path)
        try:
            self.assertEqual(len(reopened.pending()), 1)
            self.assertEqual(reopened.pending()[0][3], 'пережить рестарт')
        finally:
            reopened.close()
        self.store = Store(path)  # для tearDown

    def test_delivery_after_restart_reaches_the_chat(self):
        self.store.enqueue_text(None, '42', 'после рестарта', 'entry_without_photo')
        path = self.store.path
        self.store.close()
        reopened = Store(path)
        sent = []
        try:
            with patch.object(bot, 'STORE', reopened), \
                 patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
                 patch.object(bot, 'telegram_send_to', side_effect=lambda chat, text: sent.append(text) or True):
                bot.flush_missed_messages()
        finally:
            reopened.close()
        self.assertEqual(len(sent), 1)
        self.assertIn('после рестарта', sent[0])
        self.store = Store(path)


class PerChatDeliveryTests(StoreCase):
    def test_one_unavailable_chat_is_queued_and_retried_separately(self):
        sent = []

        def _send(chat_id, text):
            sent.append(chat_id)
            return chat_id == '1'

        with patch.object(bot, 'STORE', self.store), \
             patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'get_recipients', return_value=['1', '2']), \
             patch.object(bot, 'telegram_send_to', side_effect=_send):
            self.assertTrue(bot.telegram_send('проезд', kind='entry_without_photo'))

        # В очередь попал только недоступный чат — с его chat_id.
        rows = self.outbox_rows()
        self.assertEqual([row[1] for row in rows], ['2'])
        self.assertEqual(rows[0][3], 'entry_without_photo')

        # Чаты, где доставка прошла, в очередь не попадают.
        self.assertEqual(sent, ['1', '2'])

        # Telegram ожил: докатывается только запись чата '2'.
        sent.clear()
        self._make_due()
        with patch.object(bot, 'STORE', self.store), \
             patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'telegram_send_to', side_effect=lambda chat, text: sent.append(chat) or True):
            bot.flush_missed_messages()
        self.assertEqual(sent, ['2'])
        self.assertEqual(self.store.stats()['pending_messages'], 0)

    def test_message_without_recipients_waits_for_subscribers(self):
        with patch.object(bot, 'STORE', self.store), \
             patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'get_recipients', return_value=[]):
            self.assertFalse(bot.telegram_send('проезд', kind='entry_without_photo'))

        rows = self.outbox_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], bot.BROADCAST_CHAT)

        sent = []
        with patch.object(bot, 'STORE', self.store), \
             patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'get_recipients', return_value=['42']), \
             patch.object(bot, 'telegram_send_to', side_effect=lambda chat, text: sent.append((chat, text)) or True):
            bot.flush_missed_messages()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], '42')
        self.assertEqual(self.store.stats()['pending_messages'], 0)


class PartialDeliveryTests(StoreCase):
    def test_long_message_is_not_duplicated_after_partial_failure(self):
        sent = []
        state = {'calls': 0}

        def _post(chat_id, text):
            state['calls'] += 1
            if state['calls'] == 1:      # первая часть ушла, вторая — нет
                sent.append(text)
                return True
            return False

        text = 'A' * 5000
        with patch.object(bot, 'STORE', self.store), \
             patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'get_recipients', return_value=['42']), \
             patch.object(bot, '_telegram_post_message', side_effect=_post):
            self.assertFalse(bot.telegram_send(text, kind='text'))

        # Доставлена первая часть; в очередь ушёл только недоставленный хвост.
        self.assertEqual(len(sent), 1)
        rows = self.outbox_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0][2]), 1000)

        # Хвост докатывается — первая часть не повторяется.
        sent.clear()
        with patch.object(bot, 'STORE', self.store), \
             patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'telegram_send_to', side_effect=lambda chat, body: sent.append(body) or True):
            bot.flush_missed_messages()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].count('A'), 1000)      # только хвост
        self.assertNotIn('A' * 4000, sent[0])            # доставленная часть не повторена
        self.assertEqual(self.store.stats()['pending_messages'], 0)


class BackoffAndDeadLetterTests(StoreCase):
    def test_retry_delay_grows_and_message_ends_dead(self):
        self.store.enqueue_text(None, '42', 'недоставимо', 'entry_without_photo')
        row_id = self.store.pending()[0][0]
        delays = []
        status = 'pending'
        for attempt in range(1, 4):
            status = self.store.mark_failed(row_id, 'Telegram недоступен', max_attempts=3)
            delays.append(storage_module.backoff_delay(attempt))
            self._make_due()
        self.assertEqual(status, 'dead')
        self.assertEqual(delays, [30.0, 60.0, 120.0])   # экспонента
        self.assertEqual(self.store.pending(), [])       # dead больше не отправляется
        self.assertEqual(self.outbox_stats()['dead'], 1)

    def test_dead_message_is_reported_to_events(self):
        self.store.enqueue_text(None, '42', 'недоставимо', 'entry_without_photo')
        with patch.object(storage_module, 'DEFAULT_MAX_ATTEMPTS', 2), \
             patch.object(bot, 'STORE', self.store), \
             patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'telegram_send_to', return_value=False):
            bot.flush_missed_messages()          # первая попытка
            self._make_due()
            bot.flush_missed_messages()          # вторая — исчерпали лимит
        kinds = [event['kind'] for event in bot.EVENTS.snapshot()['events']]
        self.assertIn('telegram_dead', kinds)
        self.assertEqual(self.store.outbox_stats()['dead'], 1)

    def test_dead_message_is_not_retried(self):
        self.store.enqueue_text(None, '42', 'недоставимо', 'entry_without_photo')
        with patch.object(storage_module, 'DEFAULT_MAX_ATTEMPTS', 1), \
             patch.object(bot, 'STORE', self.store), \
             patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'telegram_send_to', return_value=False):
            bot.flush_missed_messages()
            self._make_due()
            sent = []
            with patch.object(bot, 'telegram_send_to', side_effect=lambda chat, text: sent.append(text) or True):
                self.assertFalse(bot.flush_missed_messages())
        self.assertEqual(sent, [])


class BackgroundWorkerTests(StoreCase):
    def test_worker_delivers_when_telegram_returns(self):
        self.store.enqueue_text(None, '42', 'накопленное', 'entry_without_photo')
        online = {'value': False}
        sent = []

        def _send(chat_id, text):
            if not online['value']:
                return False
            sent.append(text)
            return True

        stop = threading.Event()
        with patch.object(bot, 'STORE', self.store), \
             patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'OUTBOX_FLUSH_INTERVAL', 1), \
             patch.object(bot, 'telegram_send_to', side_effect=_send):
            worker = threading.Thread(target=bot.outbox_worker, args=(stop,), daemon=True)
            worker.start()
            try:
                time.sleep(0.5)
                self.assertEqual(sent, [])          # Telegram выключен
                online['value'] = True
                deadline = time.time() + 15
                while time.time() < deadline and not sent:
                    time.sleep(0.2)
                    self._make_due()                 # имитация прошедших 10 минут
            finally:
                stop.set()
                worker.join(5)
        self.assertEqual(len(sent), 1)
        self.assertIn('накопленное', sent[0])
        self.assertEqual(self.store.stats()['pending_messages'], 0)


class StateSnapshotTests(unittest.TestCase):
    """HTTP-эндпоинты не должны читать живые словари потоков."""

    def test_snapshot_runtime_copies_nested_dicts(self):
        snapshot = bot.snapshot_runtime()
        snapshot['telegram_delivery']['ok'] = True
        self.assertFalse(bot.RUNTIME['telegram_delivery']['ok'])

    def test_snapshot_state_is_a_copy(self):
        state = {'last_rec_no': 7}
        snapshot = bot.snapshot_state(state)
        snapshot['last_rec_no'] = 9
        self.assertEqual(state['last_rec_no'], 7)


class NonBlockingOutboxTests(StoreCase):
    """Камера не должна ждать сетевой таймаут чужой доставки."""

    def test_nonblocking_flush_skips_while_another_flush_holds_the_lock(self):
        with patch.object(bot, '_flush_missed_messages', return_value=True) as flush:
            with bot._outbox_lock:
                self.assertFalse(bot.flush_missed_messages_nonblocking())
                flush.assert_not_called()
            self.assertTrue(bot.flush_missed_messages_nonblocking())
            flush.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
