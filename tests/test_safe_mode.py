"""Безопасный тестовый режим: доказательства, что тест не трогает рабочую систему.

Проверяется именно то, о чём просил этап 2.2:

* режим включается по умолчанию при любом тестовом прогоне;
* рабочие данные (`.env`, база, состояние, фотографии, PID-файл) изолированы;
* запуск/остановка сервиса, HTTP-запросы к рабочему сервису и камере,
  RTSP-подключения и отправка в Telegram невозможны;
* фоновые потоки завершаются до снятия тестовых подмен;
* боевые файлы после прогона остаются нетронутыми.

Тесты не обращаются к живой системе: любой такой запрос блокируется на
уровне сокета, ещё до сетевого вызова.
"""
from tests import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД

import socket
import subprocess
import sys
import threading
import unittest
import os
from pathlib import Path

import requests

import anpr_telegram as bot
from app.core import safe_mode
from app.core.paths import PROJECT_ROOT
from app.services import control as service_control

#: Порт рабочего сервиса ANPR.
LIVE_PORT = 18749


class SafeModeStateTests(unittest.TestCase):
    """Режим включён по умолчанию и держит рабочие данные в изоляции."""

    def test_mode_is_active_by_default(self):
        self.assertTrue(safe_mode.is_active(), 'безопасный режим не включён при тестовом прогоне')
        self.assertTrue(safe_mode.reason())

    def test_detection_covers_pytest_and_unittest(self):
        """Режим включается и при pytest, и при `python -m unittest`."""
        self.assertTrue(safe_mode.detect_test_run())

    def test_plain_start_is_not_detected_as_a_test_run(self):
        """Обычный запуск приложения тестовым не считается.

        Проверяется поведение детектора, а не текст в его исходнике:
        переименование переменной не должно ронять проверку.
        """
        main = sys.modules.get('__main__')
        saved_pytest = sys.modules.pop('pytest', None)
        saved_argv = list(sys.argv)
        saved_env = os.environ.pop('PYTEST_CURRENT_TEST', None)
        saved_package = getattr(main, '__package__', None)
        try:
            sys.argv = ['anpr_telegram.py']
            if main is not None:
                main.__package__ = ''
            self.assertFalse(safe_mode.detect_test_run())
        finally:
            if saved_pytest is not None:
                sys.modules['pytest'] = saved_pytest
            sys.argv = saved_argv
            if saved_env is not None:
                os.environ['PYTEST_CURRENT_TEST'] = saved_env
            if main is not None and saved_package is not None:
                main.__package__ = saved_package

    def test_database_state_and_photos_are_isolated(self):
        isolation = str(safe_mode.isolation_dir())
        self.assertTrue(str(bot.DB_FILE).startswith(isolation))
        self.assertTrue(str(bot.STATE_FILE).startswith(isolation))
        self.assertTrue(str(bot.IMAGE_DIR).startswith(isolation))
        self.assertTrue(str(bot.SUBSCRIBERS_FILE).startswith(isolation))
        self.assertTrue(str(service_control.pid_file()).startswith(isolation))

    def test_working_files_are_not_used(self):
        working = anpr_testenv.WORKING_FILES
        self.assertNotEqual(str(bot.DB_FILE), str(working['db']))
        self.assertNotEqual(str(bot.STATE_FILE), str(working['state']))
        self.assertNotEqual(str(service_control.pid_file()),
                            str(PROJECT_ROOT / 'service.pid'))

    def test_env_file_is_isolated_and_secrets_are_cleared(self):
        """Боевой `.env` не читается: токен бота и пароль камеры недоступны."""
        self.assertNotEqual(str(PROJECT_ROOT / '.env'), str(safe_mode.isolation_dir() / '.env'))
        self.assertEqual(bot.TELEGRAM_BOT_TOKEN, '')
        self.assertEqual(bot.TELEGRAM_CHAT_ID, '')
        self.assertEqual(bot.ALLOWED_CHAT_IDS, '')
        self.assertEqual(bot.DAHUA_PASSWORD, '')

    def test_configuration_does_not_depend_on_the_working_env(self):
        """Направление и полосы заданы явно — результат теста не зависит от `.env`."""
        self.assertEqual(bot.DIRECTION, '')
        self.assertEqual(bot.ALLOWED_LANES, '')

    def test_journals_are_written_inside_the_isolation(self):
        """Журналы управления не должны появляться в рабочем каталоге."""
        from app.services import audit
        isolation = str(safe_mode.isolation_dir())
        self.assertTrue(str(audit.journal_path('control')).startswith(isolation))
        self.assertTrue(str(audit.journal_path('panel')).startswith(isolation))
        self.assertFalse((PROJECT_ROOT / 'service.control.log').exists())
        self.assertFalse((PROJECT_ROOT / 'panel.log').exists())


class DangerousActionTests(unittest.TestCase):
    """Опасные действия невозможны: они блокируются, а не выполняются."""

    def test_service_start_is_forbidden(self):
        with self.assertRaises(safe_mode.SafeModeError):
            service_control.start()

    def test_service_stop_is_forbidden(self):
        with self.assertRaises(safe_mode.SafeModeError):
            service_control.stop()

    def test_graceful_stop_is_forbidden(self):
        with self.assertRaises(safe_mode.SafeModeError):
            service_control.stop_gracefully(4242)

    def test_health_check_does_not_touch_the_live_service(self):
        """Проверка состояния не идёт в сеть — возвращается безопасный ответ."""
        answer = service_control.health()
        self.assertTrue(answer.get('safe_mode'))
        self.assertNotEqual(answer.get('status'), 'ok')

    def test_spawning_the_application_is_blocked(self):
        with self.assertRaises(safe_mode.SafeModeError):
            subprocess.Popen([sys.executable, str(PROJECT_ROOT / 'app.py')])

    def test_spawning_any_python_is_blocked(self):
        with self.assertRaises(safe_mode.SafeModeError):
            subprocess.Popen([sys.executable, '-c', 'print(1)'])

    def test_taskkill_is_blocked(self):
        with self.assertRaises(safe_mode.SafeModeError):
            subprocess.Popen(['taskkill', '/PID', '1', '/F'])

    def test_http_request_to_the_live_service_is_blocked(self):
        with self.assertRaises(safe_mode.SafeModeError):
            requests.get(f'http://127.0.0.1:{LIVE_PORT}/health', timeout=1)

    def test_http_request_to_the_camera_is_blocked(self):
        with self.assertRaises(safe_mode.SafeModeError):
            requests.get('http://192.168.100.249/cgi-bin/magicBox.cgi', timeout=1)

    def test_http_request_to_telegram_is_blocked(self):
        with self.assertRaises(safe_mode.SafeModeError):
            requests.get('https://api.telegram.org/bot000:AAA/getMe', timeout=1)

    def test_socket_connection_to_the_live_service_is_blocked(self):
        with self.assertRaises(safe_mode.SafeModeError):
            socket.create_connection(('127.0.0.1', LIVE_PORT), timeout=1)

    def test_rtsp_connection_to_the_camera_is_blocked(self):
        """RTSP идёт через тот же сокет — блокируется на сетевом уровне."""
        with self.assertRaises(safe_mode.SafeModeError):
            socket.create_connection(('192.168.100.249', 554), timeout=1)

    def test_camera_subscription_is_forbidden(self):
        with self.assertRaises(safe_mode.SafeModeError):
            bot.subscribe_loop({'days': {}, 'seen': []})

    def test_telegram_polling_does_not_reach_the_bot(self):
        """Опрос обновлений бота в безопасном режиме не выполняется."""
        safe_mode.reset_attempts()
        bot.poll_telegram_updates({'days': {}, 'seen': []}, None)
        kinds = {item['kind'] for item in safe_mode.blocked_attempts()}
        self.assertNotIn('http', kinds)

    def test_blocked_attempts_are_recorded(self):
        safe_mode.reset_attempts()
        with self.assertRaises(safe_mode.SafeModeError):
            requests.get(f'http://127.0.0.1:{LIVE_PORT}/health', timeout=1)
        attempts = safe_mode.blocked_attempts()
        self.assertTrue(attempts)
        self.assertEqual(attempts[-1]['kind'], 'http')
        self.assertIn(str(LIVE_PORT), attempts[-1]['detail'])

    def test_mock_live_service_is_never_contacted(self):
        """Проверка на настоящем сокете: сервер не получает ни одного запроса.

        Поднимается локальный сервер-заглушка на случайном порту, порт
        объявляется как порт рабочего сервиса — и остаётся нетронутым.
        """
        import http.server
        import threading as _threading

        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.path)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"service": "anpr-fayz"}')

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        port = server.server_address[1]
        thread = _threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        previous = safe_mode.service_ports()
        os.environ[safe_mode.ENV_SERVICE_PORT] = str(port)
        try:
            with self.assertRaises(safe_mode.SafeModeError):
                requests.get(f'http://127.0.0.1:{port}/health', timeout=1)
            with self.assertRaises(safe_mode.SafeModeError):
                socket.create_connection(('127.0.0.1', port), timeout=1)
            self.assertEqual(received, [], 'запрос дошёл до сервера')
        finally:
            os.environ[safe_mode.ENV_SERVICE_PORT] = str(next(iter(previous)))
            server.shutdown()
            server.server_close()
            thread.join(5)


class ThreadLifecycleTests(unittest.TestCase):
    """Фоновые потоки завершаются до снятия тестовых подмен."""

    def test_background_thread_is_tracked(self):
        finished = threading.Event()

        def worker():
            finished.wait(5)

        thread = threading.Thread(target=worker, name='safe-mode-probe', daemon=True)
        thread.start()
        try:
            self.assertIn(thread.name, safe_mode.alive_threads())
        finally:
            finished.set()
            thread.join(5)

    def test_threads_are_joined_before_patches_are_removed(self):
        """Пока поток жив, защитные подмены остаются включёнными."""
        release = threading.Event()
        thread = threading.Thread(target=release.wait, name='safe-mode-guard', daemon=True)
        thread.start()
        try:
            # Поток жив: подмены снять нельзя — иначе поток вырвется наружу.
            self.assertFalse(safe_mode.finalize(timeout=0.2))
            self.assertTrue(safe_mode._patches, 'защитные подмены были сняты при живом потоке')
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(safe_mode.join_threads(timeout=2.0), [])


class WorkingDataTests(unittest.TestCase):
    """Боевые файлы после тестов не изменились."""

    def test_working_files_keep_their_fingerprint(self):
        before = anpr_testenv.working_fingerprint()
        # Запись в изолированную базу — как это делает обычный тест.
        store = bot.STORE
        store.add_event('safe-mode-probe', {'iso': '2026-11-18T10:00:00+05:00',
                                             'plate': 'ZZZTEST', 'lane': '1'})
        after = anpr_testenv.working_fingerprint()
        self.assertEqual(before, after, 'тест изменил рабочие файлы')

    def test_test_record_does_not_reach_the_working_database(self):
        bot.STORE.add_event('safe-mode-probe-2', {'iso': '2026-11-18T10:00:00+05:00',
                                                   'plate': 'ZZZTEST', 'lane': '1'})
        self.assertEqual(anpr_testenv.working_db_rows('ZZZTEST'), [])


if __name__ == '__main__':
    unittest.main()
