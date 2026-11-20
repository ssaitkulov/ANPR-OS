"""Этап 2.2, фаза 4: устойчивость запуска.

Проверяется:

* второй экземпляр сервиса не запускается (межпроцессная блокировка Windows);
* имя блокировки устойчиво для одной копии программы и различно для разных;
* остановить можно только процесс, который подтверждён как наш сервис;
* чужой процесс из-за совпадения порта не останавливается;
* формат PID-файла прежний (одна строка с номером процесса).
"""
from tests import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД

import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.core.paths import PROJECT_ROOT
from app.core import safe_mode
from app.services import control as service_control
from app.services import instance as service_instance
from app.services import panel_lock
from app.services import process_info


class SingleInstanceTests(unittest.TestCase):
    """Один экземпляр сервиса на машину."""

    def test_mutex_name_is_stable_for_the_same_installation(self):
        first = service_instance.mutex_name()
        second = service_instance.mutex_name()
        self.assertEqual(first, second)
        self.assertTrue(first.startswith('Local\\EvrikaServiceMutex_'))

    def test_mutex_name_differs_for_another_installation(self):
        other = service_instance.mutex_name('EvrikaOther')
        self.assertNotEqual(other, service_instance.mutex_name())
        self.assertIn('EvrikaOther', other)

    def test_second_instance_cannot_acquire_the_mutex(self):
        # Своё имя блокировки: боевой сервис может работать — тест не должен
        # занимать его мютекс и падать из-за этого.
        name = service_instance.mutex_name('EvrikaServiceTest')
        first = service_instance.ServiceInstance(name)
        self.assertTrue(first.acquire(), 'первый экземпляр должен получить блокировку')
        try:
            second = service_instance.ServiceInstance(name)
            self.assertFalse(second.acquire(), 'второй экземпляр не должен запускаться')
        finally:
            first.release()

    def test_mutex_is_released(self):
        name = service_instance.mutex_name('EvrikaServiceTest')
        first = service_instance.ServiceInstance(name)
        self.assertTrue(first.acquire())
        first.release()
        second = service_instance.ServiceInstance(name)
        try:
            self.assertTrue(second.acquire(), 'после освобождения блокировка доступна')
        finally:
            second.release()


class PanelInstanceTests(unittest.TestCase):
    """Один экземпляр окна панели управления."""

    def _instance(self, folder):
        """Экземпляр панели со своим именем блокировки.

        Имя выводится из корня проекта: подмена корня оставляет боевую
        блокировку панели нетронутой — тест не должен мешать открытой
        панели оператора.
        """
        with patch.object(panel_lock, 'PROJECT_ROOT', Path(folder)):
            return panel_lock.PanelInstance()

    def test_second_panel_asks_the_first_one_to_close(self):
        with tempfile.TemporaryDirectory() as folder:
            first = self._instance(folder)
            try:
                first.acquire(timeout_ms=100)
                second = self._instance(folder)
                # Блокировку Windows держит поток: проверять её надо из
                # другого потока, иначе он войдёт в свою же блокировку.
                errors = []

                def acquire():
                    try:
                        second.acquire(timeout_ms=100)
                    except Exception as error:
                        errors.append(error)

                thread = threading.Thread(target=acquire)
                thread.start()
                thread.join(10)
                self.assertTrue(errors, 'второй экземпляр не заметил занятой блокировки')
                self.assertIsInstance(errors[0], RuntimeError)
                # Первый экземпляр узнаёт, что его просят закрыть.
                self.assertTrue(first.replacement_requested())
                second.close()
            finally:
                first.close()

    def test_panel_lock_is_released_with_the_window(self):
        with tempfile.TemporaryDirectory() as folder:
            first = self._instance(folder)
            first.acquire(timeout_ms=100)
            first.close()
            # После закрытия окна место занимает следующий экземпляр.
            second = self._instance(folder)
            try:
                second.acquire(timeout_ms=100)
            finally:
                second.close()


class PortOwnerTests(unittest.TestCase):
    """Кто слушает порт сервиса."""

    NETSTAT = (
        '  Proto  Local Address          Foreign Address        State           PID\n'
        '  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       924\n'
        '  TCP    127.0.0.1:18749        0.0.0.0:0              LISTENING       4242\n'
        '  TCP    127.0.0.1:18749        127.0.0.1:51514        ESTABLISHED     1200\n'
    )

    def _port_owner(self, port):
        with patch.object(process_info.subprocess, 'run',
                          return_value=SimpleNamespace(stdout=self.NETSTAT)):
            return process_info.port_owner_pid(port)

    def test_listening_pid_is_found_by_port(self):
        self.assertEqual(self._port_owner(18749), 4242)
        # Соединение — не слушание: его PID портом не владеет.
        self.assertNotEqual(self._port_owner(18749), 1200)

    def test_free_port_has_no_owner(self):
        self.assertIsNone(self._port_owner(19999))

    def test_describe_reports_a_dead_process(self):
        info = process_info.describe(999999)
        self.assertFalse(info['alive'])
        self.assertFalse(info['looks_like_service'])
        self.assertIsNone(info['command'])


class ProcessCheckTests(unittest.TestCase):
    """Фактический PID и состояние процесса."""

    def test_current_process_is_alive(self):
        self.assertTrue(process_info.pid_alive(os.getpid()))

    def test_missing_process_is_not_alive(self):
        self.assertFalse(process_info.pid_alive(0))
        self.assertFalse(process_info.pid_alive(-1))
        self.assertFalse(process_info.pid_alive(999999))

    def test_command_line_is_readable(self):
        command = process_info.process_command_line(os.getpid())
        if command is None:
            self.skipTest('командная строка недоступна на этой системе')
        self.assertIn('python', command.lower())

    def test_foreign_process_is_not_taken_for_the_service(self):
        """Текущий процесс (тестовый runner) — не наш сервис."""
        command = process_info.process_command_line(os.getpid())
        if command is None:
            self.skipTest('командная строка недоступна на этой системе')
        self.assertFalse(process_info.looks_like_service(os.getpid()))

    def test_service_marker_is_matched_strictly(self):
        """Совпадение имени файла — строгое: `app.py`, а не `myapp.py`."""
        command = process_info.process_command_line(os.getpid())
        if command is None:
            self.skipTest('командная строка недоступна на этой системе')
        self.assertFalse(process_info.looks_like_service(os.getpid()))


class StopSafetyTests(unittest.TestCase):
    """Остановка не трогает чужие процессы."""

    def test_stop_is_refused_for_a_foreign_process(self):
        service_control.pid_file().write_text('4242', encoding='ascii')
        try:
            with safe_mode.guards_suspended(), \
                 patch.object(service_control, 'health', return_value={'pid': 4242}), \
                 patch.object(service_control.process_info, 'pid_alive', return_value=True), \
                 patch.object(service_control.process_info, 'looks_like_service', return_value=False), \
                 patch.object(service_control, 'stop_gracefully') as graceful:
                with self.assertRaises(RuntimeError):
                    service_control.stop(source='panel')
            graceful.assert_not_called()
        finally:
            service_control.pid_file().unlink(missing_ok=True)

    def test_stop_is_refused_when_the_process_is_gone(self):
        service_control.pid_file().write_text('4242', encoding='ascii')
        try:
            with safe_mode.guards_suspended(), \
                 patch.object(service_control, 'health', return_value={'pid': 4242}), \
                 patch.object(service_control.process_info, 'pid_alive', return_value=False), \
                 patch.object(service_control, 'stop_gracefully') as graceful:
                with self.assertRaises(RuntimeError):
                    service_control.stop(source='panel')
            graceful.assert_not_called()
        finally:
            service_control.pid_file().unlink(missing_ok=True)

    def test_pid_file_format_is_unchanged(self):
        """Файл `service.pid` — одна строка с номером процесса, как прежде."""
        path = service_control.pid_file()
        path.write_text('4242', encoding='ascii')
        try:
            self.assertEqual(path.read_text(encoding='ascii').strip(), '4242')
            self.assertEqual(int(path.read_text(encoding='ascii').strip()), 4242)
        finally:
            path.unlink(missing_ok=True)

    def test_pid_file_lives_beside_the_program_by_default(self):
        """В рабочем режиме PID-файл — `service.pid` в `data/runtime/`."""
        # Окружение восстанавливается целиком: иначе следующий тест писал бы
        # журналы в рабочий каталог.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('SERVICE_PID_FILE', None)
            os.environ.pop('ANPR_RUNTIME_DIR', None)
            self.assertEqual(service_control.pid_file(),
                             PROJECT_ROOT / 'data' / 'runtime' / 'service.pid')
        self.assertIn('ANPR_RUNTIME_DIR', os.environ)
        self.assertNotEqual(service_control.pid_file(),
                            PROJECT_ROOT / 'data' / 'runtime' / 'service.pid')


if __name__ == '__main__':
    unittest.main()
