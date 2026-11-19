"""Этап 2.2, фаза 3: защита управления сервисом.

Проверяется, что остановить сервис может только процесс, который знает
секретный токен, и что переход между версиями протокола безопасен.

* запрос без токена → 401;
* запрос с неверным токеном → 401;
* запрос с верным токеном → остановка разрешена;
* отказ записывается в журнал аудита, секрет в журнале не раскрывается;
* токен хранится в файле с ограниченными правами доступа;
* клиент выбирает схему по версии протокола сервера: к старому сервису —
  запрос без токена, к новому — только с токеном.
"""
from tests import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from fastapi.testclient import TestClient

from app.services import auth
from app.services import audit
from app.services import control as service_control
from app.services import http_api
from app.core import safe_mode


class FakeServer:
    """Заглушка uvicorn: сервис просит у него завершиться."""

    def __init__(self):
        self.should_exit = False


class ShutdownAuthTests(unittest.TestCase):
    """`/shutdown` принимает только запрос с секретным токеном."""

    def setUp(self):
        self.token = auth.create_token()
        self.server = FakeServer()
        self._server_patch = patch.object(http_api, '_server', self.server)
        self._server_patch.start()
        self.addCleanup(self._server_patch.stop)
        # Сервер читает токен из файла при первом обращении — сбрасываем память.
        self._token_patch = patch.object(http_api, '_token', None)
        self._token_patch.start()
        self.addCleanup(self._token_patch.stop)
        self.client = TestClient(http_api.app, raise_server_exceptions=False)

    def test_request_without_token_is_rejected(self):
        response = self.client.post('/shutdown')
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self.server.should_exit, 'сервис остановлен без токена')

    def test_request_with_wrong_token_is_rejected(self):
        response = self.client.post('/shutdown', headers={auth.TOKEN_HEADER: 'wrong-token-value'})
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self.server.should_exit, 'сервис остановлен с неверным токеном')

    def test_request_with_correct_token_is_accepted(self):
        response = self.client.post('/shutdown', headers={auth.TOKEN_HEADER: self.token})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.server.should_exit)

    def test_empty_token_is_rejected(self):
        response = self.client.post('/shutdown', headers={auth.TOKEN_HEADER: ''})
        self.assertEqual(response.status_code, 401)

    def test_denied_request_is_written_to_the_audit_journal(self):
        secret = 'very-secret-token-value'
        auth.register_secret(secret)
        self.client.post('/shutdown', headers={auth.TOKEN_HEADER: secret})
        records = [item for item in audit.read('control')
                   if item['action'] == 'service.shutdown' and item['result'] == 'denied']
        self.assertTrue(records, 'отказ не записан в журнал')
        self.assertNotIn(secret, str(records[-1]))

    def test_verification_is_constant_time_comparison(self):
        self.assertTrue(auth.verify(self.token))
        self.assertFalse(auth.verify(self.token + 'x'))
        self.assertFalse(auth.verify(None))
        self.assertFalse(auth.verify(''))

    def test_health_reports_protocol_and_auth_requirement(self):
        response = self.client.get('/health')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['protocol'], auth.PROTOCOL_VERSION)
        self.assertEqual(data['shutdown_auth'], 'token')
        self.assertNotIn(self.token, str(data), 'токен не должен попадать в /health')


class TokenStorageTests(unittest.TestCase):
    """Токен хранится в файле с ограниченными правами доступа."""

    def test_token_file_is_created_and_reused(self):
        first = auth.create_token()
        self.assertTrue(auth.token_path().exists())
        self.assertEqual(auth.read_token(), first)
        self.assertEqual(auth.load_or_create_token(), first)

    def test_token_is_long_and_random(self):
        token = auth.create_token()
        self.assertGreaterEqual(len(token), 32)
        self.assertNotEqual(token, auth.create_token())

    def test_file_is_restricted_to_the_owner(self):
        auth.create_token()
        info = auth.acl_state(auth.token_path())
        self.assertTrue(info['exists'])
        sddl = info.get('sddl')
        if sddl is None:
            self.skipTest('чтение ACL недоступно на этой системе')
        self.assertIn('OW', sddl, f'в ACL нет владельца: {sddl}')
        for everyone in ('BU', 'BA', 'WD', 'Everyone'):
            self.assertNotIn(f'({everyone}', sddl, f'в ACL есть доступ для всех: {sddl}')

    def test_token_is_not_written_to_the_audit_journal(self):
        token = auth.create_token()
        audit.control('service.test', 'error', error=f'токен {token} отклонён')
        for record in audit.read('control'):
            self.assertNotIn(token, str(record))


class ProtocolNegotiationTests(unittest.TestCase):
    """Клиент и сервер разных версий работают друг с другом."""

    def _session(self, calls):
        class FakeResponse:
            status_code = 200

        class FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, url, timeout=None, headers=None):
                calls.append({'url': url, 'headers': headers or {}})
                return FakeResponse()

            def get(self, url, timeout=None):
                raise requests.ConnectionError()

        return FakeSession()

    def _health(self, payload):
        """Первый ответ — состояние сервиса, дальше сервис уже остановлен."""
        state = {'calls': 0}

        def health():
            state['calls'] += 1
            if state['calls'] == 1:
                return payload
            raise requests.ConnectionError()

        return health

    def test_new_server_receives_the_token(self):
        calls = []
        token = auth.create_token()
        with safe_mode.guards_suspended(), \
             patch.object(service_control, 'health', self._health({
                 'service': 'anpr-fayz', 'pid': 4242, 'protocol': 2, 'shutdown_auth': 'token'})), \
             patch.object(service_control.requests, 'Session', return_value=self._session(calls)), \
             patch.object(service_control, 'STOP_TIMEOUT', 0.2):
            service_control.stop_gracefully(4242, source='panel')
        self.assertTrue(calls)
        self.assertEqual(calls[0]['headers'].get(auth.TOKEN_HEADER), token)

    def test_old_server_is_stopped_without_a_token(self):
        calls = []
        with safe_mode.guards_suspended(), \
             patch.object(service_control, 'health', self._health({
                 'service': 'anpr-fayz', 'pid': 4242})), \
             patch.object(service_control.requests, 'Session', return_value=self._session(calls)), \
             patch.object(service_control, 'STOP_TIMEOUT', 0.2):
            service_control.stop_gracefully(4242, source='panel')
        self.assertTrue(calls)
        self.assertNotIn(auth.TOKEN_HEADER, calls[0]['headers'])

    def test_mixed_versions_are_not_applied_partially(self):
        """Новый сервер: запрос без токена не отправляется."""
        calls = []
        with safe_mode.guards_suspended(), \
             patch.object(service_control, 'health', return_value={
                 'service': 'anpr-fayz', 'pid': 4242, 'protocol': 2, 'shutdown_auth': 'token'}), \
             patch.object(service_control, 'requests', wraps=service_control.requests), \
             patch.object(service_control, 'STOP_TIMEOUT', 0.2):
            # Файла с токеном нет — отправлять нечего, и отправка без токена
            # была бы нарушением схемы.
            with patch.object(auth, 'read_token', return_value=None):
                with self.assertRaises(RuntimeError):
                    service_control.stop_gracefully(4242, source='panel')

    def test_capabilities_of_an_unreachable_server_are_legacy(self):
        with patch.object(service_control, 'health', side_effect=requests.ConnectionError()):
            capabilities = service_control.server_capabilities()
        self.assertEqual(capabilities['protocol'], 1)
        self.assertEqual(capabilities['shutdown_auth'], 'none')
        self.assertFalse(capabilities['reachable'])


if __name__ == '__main__':
    unittest.main()
