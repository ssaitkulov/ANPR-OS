"""Тесты этапа 2: единое управление сервисом, дублирование и жизненный цикл Tk.

Проверяются:
* управление сервисом существует только в Header;
* остановка и перезапуск требуют подтверждения;
* повторные команды блокируются, параллельные операции невозможны;
* индикаторы показывают фактические данные `/health`;
* разделы не пересоздаются при переключении;
* несохранённые значения формы не теряются;
* остановка ANPR и отключение RTSP — разные операции;
* отложенные вызовы Tk отменяются при закрытии окна.

Живой сервис, камера и база не затрагиваются: `service.*`, `cv2` и
`load_dashboard` подменяются заглушками.
"""
from tests import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД
import queue
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import customtkinter as ctk

from app.ui import status as ui_status
from app.ui.dashboard import MonitoringSection
from app.ui.shell import AppShell

#: Ответ сервиса, как его отдаёт `/health`.
HEALTH = {
    'service': 'anpr-fayz', 'pid': 4242, 'status': 'ok', 'camera': '192.168.100.249',
    'started_at': 1700000000, 'camera_message': 'подключена, получаю данные.',
    'telegram_message': 'последнее уведомление доставлено хотя бы в один разрешённый чат.',
    'last_camera_data': '18.11.2026 13:00:00',
    'telegram_delivery': {'ok': True, 'delivered': 2, 'failed': 0, 'at': '2026-11-18T13:04:05+05:00'},
    'telegram_connection': {'ok': True, 'at': '2026-11-18T13:04:06+05:00',
                            'failed_at': '', 'error': ''},
}

EMPTY_DASHBOARD = {'total': 0, 'unique': 0, 'hours': [0] * 24,
                   'directions': {'Въезд': 0, 'Выезд': 0, 'Неизвестно': 0}, 'recent': []}

#: Подменённый сервис. Держится на всё время модуля: фоновые потоки
#: не успевают завершиться до конца теста, и обращение к настоящему сервису
#: (остановка живого ANPR) недопустимо.
FAKE_SERVICE = MagicMock()
FAKE_SERVICE.URL = 'http://127.0.0.1:18749'
FAKE_SERVICE.BASE = Path(__file__).resolve().parent.parent


def setUpModule():
    global _service_patch
    FAKE_SERVICE.reset_mock()
    FAKE_SERVICE.health.return_value = HEALTH
    FAKE_SERVICE.start.return_value = HEALTH
    FAKE_SERVICE.stop.return_value = None
    _service_patch = patch('app.ui.shell.service', FAKE_SERVICE)
    _service_patch.start()


def tearDownModule():
    _service_patch.stop()


def wait_for(predicate, timeout=3.0):
    """Подождать выполнения условия (результат фонового потока)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# ----------------------------------------------------------------------
# Модель состояний (без Tk)
# ----------------------------------------------------------------------
class StatusModelTests(unittest.TestCase):
    def test_service_states(self):
        self.assertEqual(ui_status.service_view(HEALTH)['code'], ui_status.SERVICE_RUNNING)
        self.assertEqual(ui_status.service_view(None)['code'], ui_status.SERVICE_STOPPED)
        self.assertEqual(ui_status.service_view(None, busy=True, operation='start')['code'],
                         ui_status.SERVICE_STARTING)
        self.assertEqual(ui_status.service_view(None, busy=True, operation='stop')['code'],
                         ui_status.SERVICE_STOPPING)
        self.assertEqual(ui_status.service_view(None, busy=True, operation='restart', phase='start')['code'],
                         ui_status.SERVICE_STARTING)
        self.assertEqual(ui_status.service_view(None, busy=True, operation='restart', phase='stop')['code'],
                         ui_status.SERVICE_STOPPING)
        self.assertEqual(ui_status.service_view(None, error='порт занят')['code'], ui_status.SERVICE_ERROR)

    def test_camera_states(self):
        self.assertEqual(ui_status.camera_view(HEALTH, running=True)['code'], ui_status.CAMERA_CONNECTED)
        self.assertEqual(ui_status.camera_view(HEALTH, running=False)['code'], ui_status.CAMERA_NO_LINK)
        connected = dict(HEALTH, camera_message='подключена. Ожидаю проезд автомобиля.',
                         last_camera_data='')
        self.assertEqual(ui_status.camera_view(connected, running=True)['code'], ui_status.CAMERA_CONNECTED)
        broken = dict(HEALTH, camera_message='связь прервалась. Программа попробует снова.')
        self.assertEqual(ui_status.camera_view(broken, running=True)['code'], ui_status.CAMERA_NO_LINK)
        connecting = dict(HEALTH, camera_message='подключаюсь…')
        self.assertEqual(ui_status.camera_view(connecting, running=True)['code'], ui_status.CAMERA_RECEIVING)
        failed = dict(HEALTH, camera_message='ошибка подключения к камере')
        self.assertEqual(ui_status.camera_view(failed, running=True)['code'], ui_status.CAMERA_ERROR)

    def test_telegram_states(self):
        self.assertEqual(ui_status.telegram_view(HEALTH, running=True)['code'], ui_status.TELEGRAM_OK)
        failed = dict(HEALTH, telegram_delivery={'ok': False, 'delivered': 0, 'failed': 1, 'at': 'x'})
        self.assertEqual(ui_status.telegram_view(failed, running=True)['code'],
                         ui_status.TELEGRAM_DELIVERY_ERROR)
        waiting = dict(HEALTH, telegram_delivery={'ok': False, 'delivered': 0, 'failed': 0, 'at': ''})
        # Связь жива — значит «Нет соединения» показывать нельзя, даже если
        # отправок ещё не было: это отдельные диагнозы.
        self.assertEqual(ui_status.telegram_view(waiting, running=True)['code'], ui_status.TELEGRAM_OK)
        self.assertEqual(ui_status.telegram_view(HEALTH, running=False)['code'],
                         ui_status.TELEGRAM_NO_CONNECTION)
        self.assertEqual(ui_status.telegram_view(HEALTH, running=True, configured=False)['code'],
                         ui_status.TELEGRAM_NOT_CONFIGURED)

    def test_telegram_connection_and_delivery_are_separate_diagnostics(self):
        """Связь и доставка — два статуса: успешная отправка не значит, что
        Telegram доступен сейчас, и наоборот."""
        alive = dict(HEALTH, telegram_connection={'ok': True, 'at': '2026-11-18T13:04:06+05:00',
                                                   'failed_at': '', 'error': ''})
        self.assertEqual(ui_status.telegram_connection_view(alive, running=True)['code'],
                         ui_status.TELEGRAM_CONNECTED)
        self.assertEqual(ui_status.telegram_delivery_view(alive)['code'],
                         ui_status.TELEGRAM_DELIVERY_OK)

        # Связь пропала, но последняя доставка была успешной.
        lost = dict(HEALTH, telegram_connection={'ok': False, 'at': '2026-11-18T13:04:06+05:00',
                                                 'failed_at': '2026-11-18T13:09:07+05:00',
                                                 'error': 'Connection refused'})
        self.assertEqual(ui_status.telegram_connection_view(lost, running=True)['code'],
                         ui_status.TELEGRAM_CONNECTION_LOST)
        self.assertEqual(ui_status.telegram_delivery_view(lost)['code'],
                         ui_status.TELEGRAM_DELIVERY_OK)
        self.assertEqual(ui_status.telegram_view(lost, running=True)['code'],
                         ui_status.TELEGRAM_NO_CONNECTION)

        # Связь жива, но доставка сорвалась.
        broken = dict(HEALTH, telegram_delivery={'ok': False, 'delivered': 0, 'failed': 1, 'at': 'x'})
        self.assertEqual(ui_status.telegram_connection_view(broken, running=True)['code'],
                         ui_status.TELEGRAM_CONNECTED)
        self.assertEqual(ui_status.telegram_delivery_view(broken)['code'],
                         ui_status.TELEGRAM_DELIVERY_FAILED)

        # Обращений к Telegram ещё не было — состояние неизвестно, а не «нет связи».
        unknown = dict(HEALTH, telegram_connection={'ok': False, 'at': '', 'failed_at': '', 'error': ''})
        self.assertEqual(ui_status.telegram_connection_view(unknown, running=True)['code'],
                         ui_status.TELEGRAM_CONNECTION_UNKNOWN)
        self.assertEqual(ui_status.telegram_view(unknown, running=True)['code'],
                         ui_status.TELEGRAM_CHECKING)

    def test_telegram_requires_token_and_allowed_chats(self):
        self.assertFalse(ui_status.telegram_configured({}))
        self.assertFalse(ui_status.telegram_configured({'TELEGRAM_BOT_TOKEN': '1:AA'}))
        self.assertTrue(ui_status.telegram_configured({'TELEGRAM_BOT_TOKEN': '1:AA', 'ALLOWED_CHAT_IDS': '42'}))

    def test_duration_and_time_formatting(self):
        self.assertEqual(ui_status.format_duration(3725), '1 ч 02 мин')
        self.assertEqual(ui_status.format_duration(65), '1 мин 05 с')
        self.assertEqual(ui_status.format_duration(None), '')
        self.assertEqual(ui_status.format_time(''), '')
        self.assertTrue(ui_status.format_time('2026-11-18T13:04:05+05:00').count(':') == 2)


# ----------------------------------------------------------------------
# Оболочка с настоящим окном Tk
# ----------------------------------------------------------------------
class FakeCapture:
    """Заглушка cv2.VideoCapture: подключение «висит», кадров нет."""

    instances = []

    def __init__(self):
        self.opened = False
        self.released = False
        FakeCapture.instances.append(self)

    def open(self, *args, **kwargs):
        self.opened = True
        return True

    def isOpened(self):
        return self.opened

    def read(self):
        time.sleep(0.05)
        return False, None

    def release(self):
        self.released = True


class ShellTestCase(unittest.TestCase):
    """Оболочка с подменённым сервисом, камерой и базой."""

    def setUp(self):
        try:
            self.root = ctk.CTk()
        except Exception as error:  # нет графической подсистемы
            self.skipTest(f'Tk недоступен: {error}')
        self.root.withdraw()
        self.errors = []
        self.root.report_callback_excepter = self._report_error
        self.patches = [
            patch.object(AppShell, '_start_tray'),                       # без иконки в трее
            patch('app.ui.shell.window_state.save_state', return_value=True),
            patch('app.ui.dashboard.load_dashboard', return_value=EMPTY_DASHBOARD),
            patch.dict('sys.modules', {'cv2': type('FakeCv2', (), {
                'VideoCapture': FakeCapture, 'CAP_FFMPEG': 1,
                'CAP_PROP_OPEN_TIMEOUT_MSEC': 2, 'CAP_PROP_READ_TIMEOUT_MSEC': 3})}),
        ]
        for item in self.patches:
            item.start()
        self.shell = AppShell(self.root)
        # Дать завершиться стартовой операции (сервис подменён заглушкой).
        self.pump(0.4)
        self.wait(lambda: not self.shell.busy)

    def tearDown(self):
        try:
            self.shell._quit()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        for item in self.patches:
            item.stop()

    def _report_error(self, exc, value, tb):
        self.errors.append((exc, value))

    def wait(self, predicate, timeout=3.0):
        """Подождать условия, прокачивая цикл событий Tk."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.root.update()
            except Exception:
                break
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    def pump(self, seconds=0.4):
        """Прокрутить цикл событий Tk заданное время."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.02)


class ServiceControlTests(ShellTestCase):
    def test_service_control_exists_only_in_the_header(self):
        header = self.shell.header
        self.assertTrue(header.action_button.winfo_exists())
        self.assertTrue(header.restart_button.winfo_exists())
        # В разделах управления сервисом нет.
        monitor = self.shell.section_objects['monitor']
        self.assertFalse(hasattr(monitor, 'service_button'))
        self.assertFalse(hasattr(self.shell, 'service_button'))
        # Нижняя панель статуса и меню «Ещё» удалены.
        self.assertFalse(hasattr(self.shell, 'status_bar'))
        self.assertFalse(hasattr(self.shell, 'more'))
        self.assertFalse(hasattr(self.shell, '_connection_labels'))

    def test_stop_requires_confirmation(self):
        with patch('app.ui.shell.messagebox.askyesno', return_value=False):
            self.shell.running = True
            self.shell.toggle_service()
        self.assertFalse(self.shell.busy)
        FAKE_SERVICE.reset_mock()
        with patch('app.ui.shell.messagebox.askyesno', return_value=True):
            self.shell.running = True
            self.shell.toggle_service()
            self.assertTrue(wait_for(lambda: FAKE_SERVICE.stop.called))
        FAKE_SERVICE.stop.assert_called_once()

    def test_start_does_not_ask_confirmation(self):
        FAKE_SERVICE.reset_mock()
        with patch('app.ui.shell.messagebox.askyesno', return_value=False) as confirm:
            self.shell.running = False
            self.shell.toggle_service()
            confirm.assert_not_called()
            self.assertTrue(wait_for(lambda: FAKE_SERVICE.start.called))
        FAKE_SERVICE.start.assert_called_once()

    def test_restart_requires_confirmation(self):
        FAKE_SERVICE.reset_mock()
        with patch('app.ui.shell.messagebox.askyesno', return_value=False):
            self.shell.restart_service()
            self.pump(0.3)
        FAKE_SERVICE.stop.assert_not_called()
        FAKE_SERVICE.start.assert_not_called()
        with patch('app.ui.shell.messagebox.askyesno', return_value=True):
            self.shell.restart_service()
            self.assertTrue(self.wait(lambda: FAKE_SERVICE.stop.called and FAKE_SERVICE.start.called))
        FAKE_SERVICE.start.assert_called_once()

    def test_controls_are_blocked_during_operation(self):
        self.shell.busy = True
        self.shell._set_busy(True)
        self.assertEqual(str(self.shell.header.action_button.cget('state')), 'disabled')
        self.assertEqual(str(self.shell.header.restart_button.cget('state')), 'disabled')
        # Повторная команда при занятом сервисе игнорируется.
        FAKE_SERVICE.reset_mock()
        self.shell.toggle_service()
        self.shell.restart_service()
        FAKE_SERVICE.stop.assert_not_called()
        FAKE_SERVICE.start.assert_not_called()
        self.assertTrue(self.shell.busy)

    def test_parallel_operations_are_impossible(self):
        started = []
        release = queue.Queue()

        def slow_action():
            started.append(1)
            release.get(timeout=3)

        self.shell.work(slow_action, 'start')
        self.shell.work(slow_action, 'start')
        self.assertTrue(wait_for(lambda: len(started) == 1))
        self.assertEqual(len(started), 1)
        release.put(1)
        self.assertTrue(self.wait(lambda: self.shell.busy is False))

    def test_indicators_show_factual_data(self):
        # Настройки Telegram задаются явно: индикатор не зависит от боевого `.env`.
        configured = {'TELEGRAM_BOT_TOKEN': 'example', 'ALLOWED_CHAT_IDS': '42'}
        self.shell._settings_cache = (0.0, {})   # кэш настроек сброшен
        with patch('app.ui.settings.read_settings', return_value=configured):
            self.shell.update_status(HEALTH)
            self.assertEqual(self.shell.header.service_chip.value.cget('text'), 'Работает')
            self.assertEqual(self.shell.header.camera_chip.value.cget('text'), 'Подключена')
            self.assertEqual(self.shell.header.telegram_chip.value.cget('text'), 'Работает')
            self.shell.update_status(None)
            self.assertEqual(self.shell.header.service_chip.value.cget('text'), 'Остановлен')
            self.assertEqual(self.shell.header.camera_chip.value.cget('text'), 'Нет связи')

    def test_details_panel_opens_and_closes(self):
        self.shell.update_status(HEALTH)
        self.shell.header.toggle_details('service')
        self.assertEqual(self.shell.header.details.frame.winfo_manager(), 'place')
        title, rows = self.shell.details_data('service')
        captions = [caption for caption, _ in rows]
        self.assertIn('Последняя проверка', captions)
        self.assertIn('Последняя ошибка', captions)
        self.shell.header.toggle_details('service')
        self.assertEqual(self.shell.header.details.frame.winfo_manager(), '')

    def test_narrow_window_switches_indicators_to_short_labels(self):
        self.shell.update_status(HEALTH)
        self.root.geometry('1024x650+20+20')
        self.pump(0.5)
        self.shell.header._fit_width()
        self.pump(0.2)
        self.assertTrue(self.shell.header.compact)
        # Полная подпись остаётся в данных состояния.
        self.assertEqual(self.shell._states['service']['label'], 'Работает')
        # Блок управления не выходит за пределы окна.
        control = self.shell.header.control
        self.assertLessEqual(control.winfo_rootx() + control.winfo_width(),
                             self.root.winfo_rootx() + self.root.winfo_width())
        self.root.geometry('1500x940+20+20')
        self.pump(0.4)


class SectionLifecycleTests(ShellTestCase):
    def test_sections_are_created_once(self):
        monitor = self.shell.section_objects['monitor']
        for _ in range(5):
            self.shell.show_section('settings')
            self.shell.show_section('monitor')
        self.assertIs(self.shell.section_objects['monitor'], monitor)
        self.assertEqual(len(self.shell.sections), 2)

    def test_switching_does_not_duplicate_polling(self):
        monitor = self.shell.section_objects['monitor']
        self.pump(0.3)
        scheduled = len(monitor._after_ids)
        self.assertEqual(scheduled, 1)  # только цикл опроса раздела
        for _ in range(5):
            self.shell.show_section('settings')
            self.shell.show_section('monitor')
        self.pump(0.3)
        self.assertEqual(len(monitor._after_ids), 1)

    def test_video_and_anpr_are_independent(self):
        monitor = self.shell.section_objects['monitor']
        FAKE_SERVICE.reset_mock()
        monitor.video_enabled.toggle()      # пользователь выключил видео
        FAKE_SERVICE.start.assert_not_called()
        FAKE_SERVICE.stop.assert_not_called()
        self.assertFalse(monitor.stream_selection[2])
        # Пауза в трее не включает видео обратно.
        monitor.pause_video()
        monitor.resume_video()
        self.assertFalse(monitor.stream_selection[2])

    def test_unsaved_form_values_survive_navigation(self):
        from app.ui.settings import SettingsSection
        settings = self.shell.section_objects['settings'] if 'settings' in self.shell.section_objects else None
        if settings is None:
            self.shell.show_section('settings')
            settings = self.shell.section_objects['settings']
        self.assertIsInstance(settings, SettingsSection)
        original = settings.variables['CAMERA_NAME'].get()
        settings.variables['CAMERA_NAME'].set('Проверка значения')
        settings.select_tab('Telegram')
        settings.select_tab('Камера')
        self.assertEqual(settings.variables['CAMERA_NAME'].get(), 'Проверка значения')
        self.shell.show_section('monitor')
        self.shell.show_section('settings')
        self.assertEqual(settings.variables['CAMERA_NAME'].get(), 'Проверка значения')
        settings.variables['CAMERA_NAME'].set(original)

    def test_settings_tabs_are_vertical_navigation(self):
        self.shell.show_section('settings')
        settings = self.shell.section_objects['settings']
        self.assertEqual(list(settings.nav_buttons), ['Камера', 'Telegram', 'Мониторинг', 'Приложение'])
        self.assertLessEqual(settings.form.winfo_width(), settings.FORM_MAX_WIDTH + 1)


class LifecycleTests(ShellTestCase):
    def test_scheduled_callbacks_are_cancelled_on_close(self):
        calls = []
        self.root.after_cancel = lambda identifier: calls.append(identifier)
        before = len(self.shell._after_ids)
        for _ in range(3):
            self.shell.schedule(10000, lambda: None)
        self.assertEqual(len(self.shell._after_ids), before + 3)
        self.shell._quit()
        self.assertEqual(self.shell._after_ids, set())
        self.assertGreaterEqual(len(calls), 3)

    def test_no_new_callbacks_after_closing(self):
        before = len(self.shell._after_ids)
        self.shell.closing = True
        self.assertIsNone(self.shell.schedule(10, lambda: None))
        self.assertEqual(len(self.shell._after_ids), before)

    def test_repeated_close_is_safe(self):
        self.shell._quit()
        self.shell._quit()
        self.assertTrue(self.shell.closed)
        self.assertFalse(self.errors)

    def test_closing_during_polling_is_clean(self):
        self.pump(0.5)                      # раздел «Мониторинг» опрашивает видео и базу
        self.shell._quit()
        self.pump(0.3)                      # события Tk после уничтожения окна
        self.assertEqual(self.errors, [])

    def test_closing_during_rtsp_connection_releases_capture(self):
        self.pump(0.5)
        self.shell._quit()
        self.assertTrue(wait_for(lambda: any(item.released for item in FakeCapture.instances)))
        self.assertEqual(self.errors, [])

    def test_sections_are_shutdown(self):
        monitor = self.shell.section_objects['monitor']
        self.shell._quit()
        self.assertTrue(monitor.stop.is_set())
        self.assertEqual(monitor._after_ids, set())


class VideoPauseTests(ShellTestCase):
    def test_tray_pause_and_resume(self):
        monitor = self.shell.section_objects['monitor']
        self.shell.hide()
        self.assertTrue(monitor.paused.is_set())
        self.shell.show()
        self.assertFalse(monitor.paused.is_set())

    def test_manual_disable_survives_tray_pause(self):
        monitor = self.shell.section_objects['monitor']
        monitor.video_enabled.toggle()
        self.assertFalse(monitor.stream_selection[2])
        self.shell.hide()
        self.shell.show()
        self.assertFalse(monitor.stream_selection[2])


if __name__ == '__main__':
    unittest.main()
