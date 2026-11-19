"""Этап 2.2, фаза 2: структурированный журнал управления сервисом.

Проверяется, что журнал `service.control.log` и журнал панели `panel.log`:

* пишутся в формате JSONL — одна запись на строку, читается без разбора;
* содержат полный набор полей (время с часовым поясом, действие, источник,
  PID инициатора, PID цели, результат, длительность, причина ошибки);
* не содержат секретов: токены, пароли и учётные данные маскируются;
* ротируются по размеру и не растут бесконечно;
* в журнале сервиса есть временные метки строк.
"""
from tests import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД

import json
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app.services import audit
from app.services import control as service_control
from app.services import logs as service_logs
from app.core import safe_mode

#: Метка времени ISO 8601 с часовым поясом.
ISO_LINE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?[+-]\d{2}:\d{2}$')


class AuditRecordTests(unittest.TestCase):
    """Запись содержит все требуемые поля."""

    def test_record_has_all_required_fields(self):
        payload = audit.control('service.start', 'ok', source='panel',
                                initiator_pid=4242, target_pid=4243,
                                duration_ms=12.5)
        for field in ('ts', 'action', 'source', 'initiator_pid', 'target_pid',
                      'result', 'duration_ms', 'error'):
            self.assertIn(field, payload)
        self.assertEqual(payload['action'], 'service.start')
        self.assertEqual(payload['source'], 'panel')
        self.assertEqual(payload['initiator_pid'], 4242)
        self.assertEqual(payload['target_pid'], 4243)
        self.assertEqual(payload['result'], 'ok')
        self.assertEqual(payload['duration_ms'], 12.5)
        self.assertIsNone(payload['error'])

    def test_timestamp_is_iso8601_with_timezone(self):
        payload = audit.control('service.ping', 'ok')
        stamp = datetime.fromisoformat(payload['ts'])
        self.assertIsNotNone(stamp.tzinfo, 'метка времени без часового пояса')
        self.assertTrue(ISO_LINE.match(payload['ts']), payload['ts'])

    def test_journal_is_jsonl(self):
        audit.control('service.ping', 'ok')
        path = audit.journal_path('control')
        lines = [line for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
        self.assertTrue(lines)
        for line in lines[-3:]:
            parsed = json.loads(line)          # каждая строка — отдельный JSON
            self.assertIn('action', parsed)

    def test_control_and_panel_use_separate_files(self):
        self.assertEqual(audit.journal_path('control').name, 'service.control.log')
        self.assertEqual(audit.journal_path('panel').name, 'panel.log')

    def test_operation_records_request_and_result(self):
        with audit.operation('service.test', source='panel') as operation:
            operation['target_pid'] = 777
        records = audit.read('control')
        actions = [(item['action'], item['result']) for item in records
                   if item['action'] == 'service.test']
        self.assertIn(('service.test', 'requested'), actions)
        self.assertIn(('service.test', 'ok'), actions)
        finished = [item for item in records
                    if item['action'] == 'service.test' and item['result'] == 'ok'][-1]
        self.assertEqual(finished['target_pid'], 777)
        self.assertIsNotNone(finished['duration_ms'])

    def test_operation_records_error_reason(self):
        with self.assertRaises(RuntimeError):
            with audit.operation('service.broken', source='panel'):
                raise RuntimeError('порт занят')
        records = [item for item in audit.read('control') if item['action'] == 'service.broken']
        self.assertTrue(records)
        self.assertEqual(records[-1]['result'], 'error')
        self.assertEqual(records[-1]['error'], 'порт занят')


class SecretProtectionTests(unittest.TestCase):
    """Секреты в журнал не попадают."""

    def test_error_text_with_bot_token_is_masked(self):
        payload = audit.control('service.telegram', 'error',
                                error='HTTP https://api.telegram.org/bot123456:AAbbccDDeeffgghh/getMe — отказ')
        self.assertNotIn('123456:AAbbccDDeeffgghh', payload['error'])
        self.assertIn('скрыто', payload['error'])

    def test_password_in_text_is_masked(self):
        payload = audit.control('service.auth', 'error', error='password=hunter2 отклонено')
        self.assertNotIn('hunter2', payload['error'])

    def test_credentials_in_url_are_masked(self):
        payload = audit.control('service.auth', 'error',
                                error='соединение с http://admin:secret@192.168.1.1/ не удалось')
        self.assertNotIn('secret', payload['error'])

    def test_secret_fields_are_masked(self):
        payload = audit.control('service.auth', 'error', token='секретный токен',
                                password='секретный пароль', harmless='обычное значение')
        details = payload['details']
        self.assertEqual(details['token'], '<скрыто>')
        self.assertEqual(details['password'], '<скрыто>')
        self.assertEqual(details['harmless'], 'обычное значение')

    def test_registered_secret_is_masked_anywhere(self):
        audit.register_secret('tok-very-secret-value')
        payload = audit.control('service.auth', 'error', error='токен tok-very-secret-value не принят')
        self.assertNotIn('tok-very-secret-value', payload['error'])


class ControlOperationTests(unittest.TestCase):
    """Операции управления пишутся в `service.control.log`."""

    def _records(self, action):
        return [item for item in audit.read('control') if item['action'] == action]

    def test_stop_is_recorded(self):
        service_control.pid_file().write_text('4242', encoding='ascii')
        try:
            with safe_mode.guards_suspended(), \
                 patch.object(service_control, 'health', return_value={'pid': 4242}), \
                 patch.object(service_control, 'stop_gracefully') as graceful:
                service_control.stop(source='panel')
        finally:
            service_control.pid_file().unlink(missing_ok=True)
        self.assertTrue(graceful.called)
        records = self._records('service.stop')
        self.assertTrue(records)
        self.assertEqual(records[-1]['result'], 'ok')
        self.assertEqual(records[-1]['target_pid'], 4242)
        self.assertEqual(records[-1]['source'], 'panel')

    def test_failed_operation_keeps_the_reason(self):
        with safe_mode.guards_suspended(), \
             patch.object(service_control, 'health', side_effect=RuntimeError('порт занят')):
            with self.assertRaises(RuntimeError):
                service_control.stop(source='cli')
        records = self._records('service.stop')
        self.assertEqual(records[-1]['result'], 'error')
        self.assertIn('порт занят', records[-1]['error'])

    def test_denied_operation_is_recorded(self):
        with self.assertRaises(safe_mode.SafeModeError):
            service_control.stop(source='panel')
        records = [item for item in audit.read('control')
                   if item['action'] == 'service.stop' and item['result'] == 'denied']
        self.assertTrue(records)


class PanelJournalTests(unittest.TestCase):
    """Журнал панели: подтверждения пользователя и завершение."""

    def test_confirmation_is_recorded_with_the_answer(self):
        from app.ui import shell as ui_shell

        class FakeShell:
            root = None
            _confirm = ui_shell.AppShell._confirm

        with patch('app.ui.shell.messagebox.askyesno', return_value=True):
            answer = FakeShell()._confirm('Остановить систему', 'Остановить?',
                                          action='service.stop')
        self.assertTrue(answer)
        records = [item for item in audit.read('panel')
                   if item['action'] == 'service.stop.confirm']
        self.assertTrue(records)
        self.assertEqual(records[-1]['result'], 'requested')
        self.assertTrue(records[-1]['details']['confirmed'])

    def test_refused_confirmation_is_recorded(self):
        from app.ui import shell as ui_shell

        class FakeShell:
            root = None
            _confirm = ui_shell.AppShell._confirm

        with patch('app.ui.shell.messagebox.askyesno', return_value=False):
            answer = FakeShell()._confirm('Остановить систему', 'Остановить?',
                                          action='service.stop')
        self.assertFalse(answer)
        records = [item for item in audit.read('panel')
                   if item['action'] == 'service.stop.confirm']
        self.assertFalse(records[-1]['details']['confirmed'])

    def test_panel_events_use_the_panel_journal(self):
        audit.panel('panel.exit', 'ok', source='panel')
        self.assertTrue([item for item in audit.read('panel')
                         if item['action'] == 'panel.exit'])
        self.assertFalse([item for item in audit.read('control')
                          if item['action'] == 'panel.exit'])


class RotationTests(unittest.TestCase):
    """Журналы и файлы журналов сервиса ротируются."""

    def test_audit_journal_rotates_by_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'service.control.log'
            writer = audit.JournalWriter(path, max_bytes=2048, backups=2)
            for index in range(200):
                writer.write({'ts': '2026-11-19T10:00:00+05:00', 'action': f'event-{index}',
                              'source': 'test', 'initiator_pid': 1, 'target_pid': None,
                              'result': 'ok', 'duration_ms': None, 'error': None})
            self.assertTrue(path.exists())
            self.assertTrue(path.with_name(path.name + '.1').exists())
            self.assertLessEqual(path.stat().st_size, 4096)

    def test_service_log_lines_have_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'service.stdout.log'
            writer = service_logs.RotatingFileWriter(path, max_bytes=4096, backups=1)
            try:
                writer.write('строка журнала\n')
                writer.write('ещё строка\n')
            finally:
                writer.close()
            lines = [line for line in path.read_text(encoding='utf-8').splitlines() if line]
            self.assertTrue(lines)
            for line in lines:
                self.assertTrue(ISO_LINE.match(line.split(' ')[0]), line)
                self.assertIn('строка', line)

    def test_incomplete_line_gets_timestamp_on_flush(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'service.stderr.log'
            writer = service_logs.RotatingFileWriter(path, max_bytes=4096, backups=1)
            try:
                writer.write('без перевода строки')
                self.assertEqual(path.read_text(encoding='utf-8'), '')
                writer.flush_pending()
            finally:
                writer.close()
            text = path.read_text(encoding='utf-8')
            self.assertTrue(ISO_LINE.match(text.split(' ')[0]), text)


if __name__ == '__main__':
    unittest.main()
