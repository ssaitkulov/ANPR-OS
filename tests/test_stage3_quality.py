"""Этап 3: регрессионные тесты и контроль качества.

Закрываем риски, найденные на этапах 0–2:

* коллизия GroupID (прошивка переиспользует идентификатор) не должна
  склеивать разные проезды в одну строку;
* проезд из архива камеры обязан попасть в SQLite и не должен задваиваться,
  если тот же проезд придёт позже вживую;
* реальная отправка, которую Telegram не принял, обязана лечь в очередь
  и уйти оттуда, когда связь вернётся — даже если камера лежит;
* частичная доставка подписчикам: неудавшийся чат ждёт в очереди, успешный
  — не получает копию;
* два добора архива одновременно не создают дублей;
* отчёт, пропущенный из-за выключенного сервиса, догоняется на следующий день;
* прогон тестов не меняет боевую базу `anpr.sqlite3` и `anpr_state.json`.

Каждый тест работает с настоящим кодом сервиса; база, состояние и файлы
подписчиков — во временном каталоге (см. `anpr_testenv`).
"""
from tests import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД

import os
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import requests

import anpr_telegram as bot


def _event(plate='ABC123', group='1048576', lane='1', direction='Approach', index='0'):
    """Событие камеры в том виде, в каком его отдаёт snapManager."""
    return {'EventBaseInfo.Code': 'TrafficJunction', 'GroupID': group, 'IndexInGroup': index,
            'TrafficCar.PlateNumber': plate, 'Lane': lane, 'Direction': direction}


def _camera_time(day='2026-11-17', hour=10, minute=0):
    """Время камеры (настенное) как unix-время записи архива.

    Поле `Time` в архиве — настенное время камеры, поэтому в записи оно
    лежит как «10:00 UTC», а `archive_time()` превращает его в 10:00+05:00.
    """
    wall = datetime.fromisoformat(f'{day}T{hour:02d}:{minute:02d}:00+00:00')
    return int(wall.timestamp())


class GroupIdCollisionTests(anpr_testenv.IsolatedTestCase):
    """GroupID переиспользуется прошивкой — он не может быть ключом проезда."""

    def _record(self, when, plate='ABC123', lane='1'):
        with patch.object(bot, 'now_local', return_value=when):
            return bot.record_event(self.state, _event(plate=plate, lane=lane), None)

    def test_repeated_passage_in_the_same_second_is_not_duplicated(self):
        """Один и тот же проезд дважды подряд — одна строка."""
        when = self.bot.now_local().replace(hour=10, minute=0, second=0, microsecond=0)
        self._record(when)
        self._record(when)
        self.assertEqual(len(self.rows(when.strftime('%Y-%m-%d'))), 1)


class BackfillStorageTests(anpr_testenv.IsolatedTestCase):
    """Добор из архива камеры: строка в БД и никаких дублей."""

    def _when(self, day='2026-11-17', hour=10, minute=0):
        return datetime.fromisoformat(f'{day}T{hour:02d}:{minute:02d}:00+05:00')

    def test_backfill_lands_in_the_database(self):
        when = self._when()
        rec = bot.record_backfill_event(self.state, '01ABC123', when, lane='1',
                                        direction='0', rec_no=184)
        self.assertIsNotNone(rec)
        rows = self.rows('2026-11-17')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['plate'], '01ABC123')
        self.assertEqual(rows[0]['backfill'], 1)
        self.assertEqual(rows[0]['rec_no'], 184)
        self.assertEqual(rows[0]['captured_at'], when.isoformat())

    def test_backfill_replaces_archive_row_when_the_same_passage_arrives_live(self):
        """Тот же проезд сначала из архива, потом вживую — одна строка, а не две."""
        when = self._when(hour=10)
        bot.record_backfill_event(self.state, '01ABC123', when, lane='1', direction='0', rec_no=184)
        with patch.object(bot, 'now_local', return_value=when + timedelta(seconds=30)):
            rec = bot.record_event(self.state, _event(plate='01ABC123'), None)
        self.assertIsNotNone(rec)
        rows = self.rows('2026-11-17')
        self.assertEqual(len(rows), 1, 'live-событие завело вторую строку')
        self.assertEqual(rows[0]['backfill'], 0)
        self.assertEqual(rows[0]['group_id'], '1048576')

    def test_archive_list_is_fetched_and_written_to_the_database(self):
        """Полный путь: recordFinder -> состояние -> SQLite -> одно сообщение."""
        when = self._when(hour=9)
        records = [{'RecNo': '184', 'Time': str(_camera_time(hour=9)), 'PlateNumber': '01ABC123',
                    'Lane': '1', 'JunctionDirection': '0', 'VehicleColor': 'White',
                    'Category': 'Car'}]
        with patch.object(bot, '_fetch_archive_records', return_value=records), \
             patch.object(bot, 'telegram_send', return_value=True) as send, \
             patch.object(bot, '_backfill_range',
                          return_value=(when - timedelta(hours=1), when + timedelta(hours=1))):
            added = bot.backfill_once(object(), self.state)
        self.assertEqual(added, 1)
        self.assertEqual(len(self.rows('2026-11-17')), 1)
        send.assert_called_once()
        self.assertIn('01ABC123', send.call_args[0][0])

    def test_duplicate_live_passage_is_not_added_again_by_backfill(self):
        """Живой проезд в окне ±120 с — добор не создаёт вторую строку."""
        when = self._when(hour=11)
        with patch.object(bot, 'now_local', return_value=when):
            bot.record_event(self.state, _event(plate='01ABC123'), None)
        self.assertEqual(len(self.rows('2026-11-17')), 1)
        self.assertIsNone(bot.record_backfill_event(self.state, '01ABC123',
                                                    when + timedelta(seconds=60),
                                                    lane='1', direction='Approach', rec_no=185))
        self.assertEqual(len(self.rows('2026-11-17')), 1)


class OutboxDeliveryTests(anpr_testenv.IsolatedTestCase):
    """Очередь недоставленных: реальная отправка, сбой связи, частичный успех."""

    def test_failed_real_send_goes_to_the_outbox(self):
        """Telegram недоступен — сообщение не потеряно, оно в очереди."""
        with patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'ALLOWED_CHAT_IDS', '42'), \
             patch.object(bot, 'get_recipients', return_value=['42']), \
             patch.object(bot.requests, 'post', side_effect=requests.ConnectionError('telegram недоступен')):
            ok = bot.telegram_send('ВЪЕЗД ABC123', kind='text')
        self.assertFalse(ok)
        rows = self.outbox_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], '42')
        self.assertEqual(rows[0][4], 'pending')
        self.assertIn('ABC123', rows[0][2])

    def test_successful_real_send_leaves_the_outbox_empty(self):
        """Доставленное сообщение в очередь не попадает."""
        with patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'ALLOWED_CHAT_IDS', '42'), \
             patch.object(bot, 'get_recipients', return_value=['42']), \
             patch.object(bot.requests, 'post') as post:
            post.return_value.raise_for_status.return_value = None
            ok = bot.telegram_send('ВЪЕЗД ABC123', kind='text')
        self.assertTrue(ok)
        self.assertEqual(self.outbox_rows(), [])

    def test_pending_message_is_flushed_when_telegram_returns(self):
        """Камера лежит — очередь уходит, как только Telegram снова доступен (P0)."""
        with patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'ALLOWED_CHAT_IDS', '42'), \
             patch.object(bot, 'get_recipients', return_value=['42']), \
             patch.object(bot.requests, 'post', side_effect=requests.ConnectionError('down')):
            bot.telegram_send('ВЪЕЗД ABC123', kind='text')
        self.assertEqual(len(self.outbox_rows()), 1)

        sent = []

        def fake_post(url, **kwargs):
            sent.append(url)
            response = requests.Response()
            response.status_code = 200
            return response

        with patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot.requests, 'post', side_effect=fake_post):
            delivered = bot.flush_missed_messages()
        self.assertTrue(delivered)
        self.assertTrue(sent and all('api.telegram.org' in url for url in sent),
                        'очередь ушла не в Telegram')
        rows = self.outbox_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][4], 'sent')

    def test_partial_success_keeps_only_the_failed_chat_in_the_outbox(self):
        """Одному подписчику доставлено, другому — нет: ждёт только он."""
        def fake_send_to(chat_id, text):
            return chat_id == '11'

        with patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'ALLOWED_CHAT_IDS', '11,22'), \
             patch.object(bot, 'get_recipients', return_value=['11', '22']), \
             patch.object(bot, 'telegram_send_to', side_effect=fake_send_to):
            ok = bot.telegram_send('ВЪЕЗД ABC123', kind='text')
        self.assertTrue(ok)
        self.assertEqual(bot._LAST_DELIVERY['delivered'], ['11'])
        self.assertEqual(bot._LAST_DELIVERY['failed'], ['22'])
        rows = self.outbox_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], '22')

class DeliveryCounterTests(anpr_testenv.IsolatedTestCase):
    """Счётчики доставки в `/health` показывают результат любой рассылки.

    Рассылка идёт тремя путями — въезд, добор из архива, отчёт, — и счётчик
    должен отражать результат любой из них: иначе панель пишет «Нет соединения»,
    хотя сообщения доставлялись.
    """

    def _reset_counter(self):
        bot.RUNTIME['telegram_delivery'] = {'ok': False, 'delivered': 0, 'failed': 0, 'at': ''}
        bot.RUNTIME['telegram_message'] = 'ожидаю первую отправку.'

    def test_plain_broadcast_publishes_the_delivery_counters(self):
        """Рассылка не из `/entries` (добор, отчёт) — тоже видна в `/health`."""
        self._reset_counter()
        with patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'ALLOWED_CHAT_IDS', '42'), \
             patch.object(bot, 'get_recipients', return_value=['42']), \
             patch.object(bot, 'telegram_send_to', return_value=True):
            ok = bot.telegram_send('📥 Проезды из архива камеры: 3', kind='backfill')
        self.assertTrue(ok)
        delivery = bot.RUNTIME['telegram_delivery']
        self.assertTrue(delivery['ok'])
        self.assertEqual(delivery['delivered'], 1)
        self.assertEqual(delivery['failed'], 0)
        self.assertTrue(delivery['at'])
        self.assertIn('доставлено', bot.RUNTIME['telegram_message'])

    def test_failed_broadcast_publishes_the_failure(self):
        """Доставка не прошла — счётчик показывает ошибку, а не «ожидание»."""
        self._reset_counter()
        with patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'ALLOWED_CHAT_IDS', '42'), \
             patch.object(bot, 'get_recipients', return_value=['42']), \
             patch.object(bot, 'telegram_send_to', return_value=False):
            ok = bot.telegram_send('📥 Проезды из архива камеры: 3', kind='backfill')
        self.assertFalse(ok)
        delivery = bot.RUNTIME['telegram_delivery']
        self.assertFalse(delivery['ok'])
        self.assertEqual(delivery['delivered'], 0)
        self.assertEqual(delivery['failed'], 1)
        self.assertTrue(delivery['at'])
        self.assertIn('не удалось', bot.RUNTIME['telegram_message'])

    def test_no_recipients_keeps_the_previous_counters(self):
        """Получателей нет — отправки не было, прежнее значение сохраняется."""
        self._reset_counter()
        with patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'get_recipients', return_value=[]):
            bot.telegram_send('📥 Проезды из архива камеры: 3', kind='backfill')
        self.assertEqual(bot.RUNTIME['telegram_delivery'],
                         {'ok': False, 'delivered': 0, 'failed': 0, 'at': ''})
        self.assertEqual(bot.RUNTIME['telegram_message'], 'ожидаю первую отправку.')


class TelegramConnectionTests(anpr_testenv.IsolatedTestCase):
    """Связь с Telegram — отдельный диагноз от доставки.

    Успешная отправка не доказывает, что Telegram доступен сейчас: сеть
    может лечь в любой момент. Поэтому связь отслеживается по факту
    обращения к API, а доставка считается отдельно.
    """

    def setUp(self):
        super().setUp()
        bot.RUNTIME['telegram_connection'] = {'ok': False, 'at': '', 'failed_at': '', 'error': ''}
        bot.RUNTIME['telegram_delivery'] = {'ok': False, 'delivered': 0, 'failed': 0, 'at': ''}
        bot.RUNTIME['telegram_message'] = 'ожидаю первую отправку.'

    def test_successful_call_marks_the_connection_alive(self):
        """Ответ Telegram API — связь есть."""
        with patch.object(bot.requests, 'post', return_value=type('R', (), {
                'raise_for_status': lambda self: None, 'status_code': 200})()):
            self.assertTrue(bot.telegram_send_to('42', 'привет'))
        connection = bot.RUNTIME['telegram_connection']
        self.assertTrue(connection['ok'])
        self.assertTrue(connection['at'])
        self.assertEqual(connection['error'], '')

    def test_network_error_marks_the_connection_lost(self):
        """Сетевая ошибка — связи нет, даже если счётчики доставки удали."""
        bot.RUNTIME['telegram_delivery'] = {'ok': True, 'delivered': 3, 'failed': 0, 'at': 'x'}
        with patch.object(bot.requests, 'post', side_effect=requests.ConnectionError('сеть недоступна')):
            self.assertFalse(bot.telegram_send_to('42', 'привет'))
        connection = bot.RUNTIME['telegram_connection']
        self.assertFalse(connection['ok'])
        self.assertTrue(connection['failed_at'])
        self.assertIn('сеть недоступна', connection['error'])
        # Счётчики доставки — про доставку, их сеть не меняет.
        self.assertEqual(bot.RUNTIME['telegram_delivery']['delivered'], 3)

    def test_long_poll_failure_keeps_the_previous_state(self):
        """Таймаут долгого опроса — это «не дождались ответа», а не обрыв связи.

        Иначе одна медленная сеть мигала бы индикатором впустую.
        """
        bot._note_telegram_contact(True)
        with patch.object(bot.requests, 'get', side_effect=requests.ReadTimeout('read timeout=40')):
            with self.assertRaises(requests.ReadTimeout):
                bot._telegram_call('get', 'https://api.telegram.org/botX/getUpdates',
                                   affects_connection=False, timeout=1)
        connection = bot.RUNTIME['telegram_connection']
        self.assertTrue(connection['ok'])
        self.assertIn('read timeout', connection['error'])

    def test_long_poll_silence_breaks_the_connection(self):
        """Telegram молчит дольше CONNECTION_STALE_SEC — связь считается потерянной.

        Иначе при отсутствии проездов обрыв связи не был бы виден вовсе.
        """
        bot._note_telegram_contact(True)
        with patch.object(bot.requests, 'get', side_effect=requests.ReadTimeout('read timeout=40')):
            with self.assertRaises(requests.ReadTimeout):
                bot._telegram_call('get', 'https://api.telegram.org/botX/getUpdates',
                                   affects_connection=False, timeout=1)
        self.assertTrue(bot.RUNTIME['telegram_connection']['ok'])
        state = bot.RUNTIME['telegram_connection']
        state['at'] = (datetime.now(bot.TZ) - timedelta(seconds=bot.CONNECTION_STALE_SEC + 10)).isoformat()
        with patch.object(bot.requests, 'get', side_effect=requests.ReadTimeout('read timeout=40')):
            with self.assertRaises(requests.ReadTimeout):
                bot._telegram_call('get', 'https://api.telegram.org/botX/getUpdates',
                                   affects_connection=False, timeout=1)
        self.assertFalse(bot.RUNTIME['telegram_connection']['ok'])

    def test_error_text_hides_the_bot_token(self):
        """Токен бота не должен попадать в текст ошибки: он есть в адресе запроса."""
        with patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:SECRET-TOKEN'):
            text = bot._short_error(Exception(
                "HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded "
                "with url: /bot123:SECRET-TOKEN/getUpdates"))
        self.assertNotIn('SECRET-TOKEN', text)
        self.assertIn('***', text)

    def test_delivery_success_does_not_imply_connection(self):
        """Отправка, обходящая API, не подтверждает связь."""
        with patch.object(bot, 'TELEGRAM_BOT_TOKEN', '123:token'), \
             patch.object(bot, 'ALLOWED_CHAT_IDS', '42'), \
             patch.object(bot, 'get_recipients', return_value=['42']), \
             patch.object(bot, 'telegram_send_to', return_value=True):
            self.assertTrue(bot.telegram_send('проверка доставки', kind='backfill'))
        self.assertTrue(bot.RUNTIME['telegram_delivery']['ok'])
        self.assertFalse(bot.RUNTIME['telegram_connection']['at'])

    def test_connection_and_delivery_do_not_bleed_into_each_other(self):
        """Обрыв связи не должен превращаться в «ошибку доставки»."""
        with patch.object(bot.requests, 'post', side_effect=requests.ConnectionError('сеть недоступна')):
            bot._telegram_post_message('42', 'привет')
        self.assertFalse(bot.RUNTIME['telegram_connection']['ok'])
        self.assertEqual(bot.RUNTIME['telegram_delivery'],
                         {'ok': False, 'delivered': 0, 'failed': 0, 'at': ''})


class ConcurrentBackfillTests(anpr_testenv.IsolatedTestCase):
    """Два добора архива одновременно не должны мешать друг другу."""

    def test_only_one_backfill_session_runs_at_a_time(self):
        calls = []
        started = threading.Event()

        def fake_once(sess, state, dry_run=False):
            calls.append(1)
            started.set()
            time.sleep(0.4)
            return 0

        with patch.object(bot, 'backfill_once', side_effect=fake_once), \
             patch.object(bot.requests, 'Session'):
            threads = [threading.Thread(target=bot.run_backfill, args=(self.state,))
                       for _ in range(4)]
            for thread in threads:
                thread.start()
            self.assertTrue(started.wait(5), 'добор не начался')
            for thread in threads:
                thread.join(10)
        self.assertEqual(len(calls), 1, 'параллельных доборов больше одного')

    def test_concurrent_backfill_does_not_duplicate_rows(self):
        record = {'RecNo': '184', 'Time': str(_camera_time(hour=10)), 'PlateNumber': '01ABC123',
                  'Lane': '1', 'JunctionDirection': '0', 'VehicleColor': 'White',
                  'Category': 'Car'}
        when = datetime.fromisoformat('2026-11-17T10:00:00+05:00')
        results = []

        def worker():
            # Через run_backfill: сеансы добора сериализуются замком, иначе
            # результат зависел бы от того, как потоки успели разойтись.
            results.append(bot.run_backfill(self.state))

        # Подмены делаются в главном потоке: патчить модуль из потоков нельзя —
        # подмены наезжают друг на друга и оставляют после себя моки.
        with patch.object(bot, '_fetch_archive_records', return_value=[dict(record)]), \
             patch.object(bot, 'telegram_send', return_value=True), \
             patch.object(bot, '_backfill_range',
                          return_value=(when - timedelta(hours=1), when + timedelta(hours=1))):
            threads = [threading.Thread(target=worker) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(15)
        self.assertEqual(len(self.rows('2026-11-17')), 1, 'параллельный добор завёл дубли')
        self.assertEqual(sum(results), 1, 'добор прошёл больше одного раза')


class ReportCatchUpTests(anpr_testenv.IsolatedTestCase):
    """Отчёт, пропущенный из-за выключенного сервиса, догоняется."""

    def setUp(self):
        super().setUp()
        self.yesterday = (self.bot.now_local() - timedelta(days=1)).strftime('%Y-%m-%d')
        self.today = self.bot.now_local().strftime('%Y-%m-%d')
        self.store.add_event('yesterday-key', {'iso': f'{self.yesterday}T09:00:00+05:00',
                                                'plate': '01ABC123', 'direction': 'Approach'})

    def test_report_of_yesterday_is_sent_after_the_outage(self):
        now = self.bot.now_local().replace(hour=9, minute=0, second=0, microsecond=0)
        with patch.object(bot, 'REPORT_TIME', '20:00'):
            due = bot.due_report_days(self.state, now)
        self.assertIn(self.yesterday, due)
        self.assertNotIn(self.today, due, 'сегодняшний отчёт до 20:00 не должен уходить')

        sent = []
        with patch.object(bot, 'telegram_send', side_effect=lambda text, kind='': sent.append(kind) or True), \
             patch.object(bot, 'now_local', return_value=now):
            self.assertTrue(bot.maybe_send_report(self.state))
        self.assertEqual(sent, ['report'])
        self.assertIn(self.yesterday, self.state['reported_days'])

    def test_caught_up_report_is_not_sent_twice(self):
        now = self.bot.now_local().replace(hour=9, minute=0, second=0, microsecond=0)
        sent = []
        with patch.object(bot, 'telegram_send', side_effect=lambda text, kind='': sent.append(kind) or True), \
             patch.object(bot, 'now_local', return_value=now):
            bot.maybe_send_report(self.state)
            bot.maybe_send_report(self.state)
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.state['reported_days'].count(self.yesterday), 1)

    def test_report_text_contains_the_passages_of_the_day(self):
        text = bot.build_report(self.state, self.yesterday)
        self.assertIn(self.yesterday, text)
        self.assertIn('01ABC123', text)


class WorkingDatabaseIsolationTests(anpr_testenv.IsolatedTestCase):
    """Прогон тестов не должен трогать боевую базу и JSON-состояние."""

    def test_state_is_written_to_the_temporary_file(self):
        """JSON-зеркало пишется во временный каталог, боевой файл не трогаем."""
        working_before = anpr_testenv.fingerprint(anpr_testenv.WORKING_FILES['state'])
        self.state['last_rec_no'] = 7
        self.bot.save_state(self.state)
        self.assertTrue(self.state_path.exists())
        self.assertIn('"last_rec_no": 7', self.state_path.read_text(encoding='utf-8'))
        if working_before is None:
            self.assertIsNone(anpr_testenv.fingerprint(anpr_testenv.WORKING_FILES['state']),
                              'тест создал боевой anpr_state.json')


if __name__ == '__main__':
    unittest.main()
