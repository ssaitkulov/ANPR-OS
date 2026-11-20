"""HTTP-эндпоинты локального сервиса ANPR.

Проверяется то, чем пользуется панель управления и кто угодно ещё с
локального узла:

* `/health` — состояние сервиса и требование токена;
* `/events` — журнал операций;
* `/passages` — последние проезды с-human-readable номерами;
* `/passages/{id}/photo` — снимок проезда (и 404, если снимка нет);
* `/status` — счётчики за сегодня и состояние очереди;
* `/entries/today` — проезды за сегодня;
* `/report-now` и `/backfill-now` — ручные команды запускают действие.

Сервис не запускается: `lifespan` не выполняется, камера не
подключается. Отправка в Telegram и добор из архива подменены — тест не
должен трогать сеть и боевую базу.
"""
from tests import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import anpr_telegram as bot
from app.services import http_api


class HttpApiTests(unittest.TestCase):
    """Ответы локального HTTP-сервиса."""

    def setUp(self):
        # TestClient без контекстного менеджера не выполняет `lifespan`:
        # поток подписки на камеру не поднимается.
        self.client = TestClient(http_api.app, raise_server_exceptions=False)

    # ------------------------------------------------------------------
    def test_health_reports_the_service_state(self):
        response = self.client.get('/health')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['service'], 'anpr-fayz')
        self.assertEqual(data['shutdown_auth'], 'token')
        self.assertIn(data['status'], ('ok', 'worker_stopped'))

    def test_events_return_the_operation_log(self):
        response = self.client.get('/events')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['service'], 'anpr-fayz')
        self.assertIn('events', data)

    def test_passages_are_returned_with_readable_plates(self):
        key = 'http-api-probe'
        bot.STORE.add_event(key, {'iso': bot.now_local().isoformat(),
                                  'plate': '01D555AA', 'direction': 'entry'})
        response = self.client.get('/passages')
        self.assertEqual(response.status_code, 200)
        rows = response.json()['passages']
        # Ищем свой проезд, а не первый в списке: база общая у всех тестов.
        row = next((item for item in rows
                    if item['plate'].replace(' ', '') == '01D555AA'), None)
        self.assertIsNotNone(row, 'проезд не попал в ответ')
        self.assertEqual(row['plate'], '01 D 555 AA')
        self.assertEqual(row['direction'], 'Въезд')

    def test_photo_of_a_passage_without_a_snapshot_is_not_found(self):
        bot.STORE.add_event('http-api-photo-probe',
                            {'iso': bot.now_local().isoformat(), 'plate': '01D555AA'})
        rows = self.client.get('/passages').json()['passages']
        row = next(item for item in rows
                   if item['plate'].replace(' ', '') == '01D555AA')
        response = self.client.get(f"/passages/{row['id']}/photo")
        self.assertEqual(response.status_code, 404)

    def test_status_reports_counters_and_the_outbox(self):
        response = self.client.get('/status')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['today'], bot.today_str())
        for key in ('entries', 'unique_plates', 'subscribers', 'outbox', 'last_rec_no'):
            self.assertIn(key, data)
        self.assertIn('pending', data['outbox'])

    def test_entries_today_list_the_passages_of_the_day(self):
        response = self.client.get('/entries/today')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['date'], bot.today_str())
        self.assertIsInstance(data['events'], list)

    def test_report_now_sends_the_report(self):
        with patch.object(bot, 'telegram_send') as send:
            response = self.client.post('/report-now')
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()['text'])
            self.assertTrue(send.called, 'отчёт не отправлен')

    def test_backfill_now_starts_the_archive_pass(self):
        with patch.object(bot, 'run_backfill') as backfill:
            response = self.client.post('/backfill-now')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['status'], 'backfill queued')
            self.assertTrue(backfill.called, 'добор из архива не запущен')


if __name__ == '__main__':
    unittest.main()
