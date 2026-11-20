"""Тесты контекстной панели диагностики.

Проверяется модель «три компактных индикатора — одна контекстная панель»:

* нажатие на индикатор открывает панель в режиме этого компонента;
* нажатие на другой индикатор переключает содержимое, не закрывая панель;
* повторное нажатие на активный индикатор закрывает панель;
* Escape, клик вне панели и смена раздела закрывают панель;
* в режиме камеры нет управления ANPR-сервисом, и наоборот;
* панель одна: ширина одинакова во всех режимах, Dashboard не сдвигается;
* состояния индикаторов независимы: сломанная камера не меняет сервис.

Живой сервис, камера и база не затрагиваются: `service.*`, `cv2` и
`load_dashboard` подменяются заглушками.
"""
from tests import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import customtkinter as ctk
import requests

from app.ui import status as ui_status
from app.ui.shell import AppShell

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

FAKE_SERVICE = MagicMock()
FAKE_SERVICE.URL = 'http://127.0.0.1:18749'
FAKE_SERVICE.PORT = 18749
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


class FakeCapture:
    """Пустая RTSP-захватка: видео в тестах не нужно."""

    instances = []

    def __init__(self, *args, **kwargs):
        FakeCapture.instances.append(self)
        self.released = False

    def isOpened(self):
        return False

    def read(self):
        return False

    def release(self):
        self.released = True


class ShellTestCase(unittest.TestCase):
    """Окно приложения с заглушками вместо живого сервиса и видео."""

    def setUp(self):
        try:
            self.root = ctk.CTk()
        except Exception as error:  # нет графики — тест пропускается
            self.skipTest(f'Tk недоступен: {error}')
        self.errors = []
        self.root.report_callback_exception = self._report_error
        self.patches = [
            patch.object(AppShell, '_start_tray'),
            patch('app.ui.shell.window_state.save_state', return_value=True),
            patch('app.ui.dashboard.load_dashboard', return_value=EMPTY_DASHBOARD),
            patch.dict('sys.modules', {'cv2': type('FakeCv2', (), {
                'VideoCapture': FakeCapture, 'CAP_FFMPEG': 1,
                'CAP_PROP_OPEN_TIMEOUT_MSEC': 2, 'CAP_PROP_READ_TIMEOUT_MSEC': 3})}),
            patch('app.ui.settings.read_settings', return_value={
                'TELEGRAM_BOT_TOKEN': 'example', 'ALLOWED_CHAT_IDS': '42',
                'CAMERA_NAME': 'Fayz kirish'}),
        ]
        for item in self.patches:
            item.start()
        self.shell = AppShell(self.root)
        self.header = self.shell.header
        self.panel = self.header.details
        self.pump(0.3)
        self.root.withdraw()
        FAKE_SERVICE.health.side_effect = None
        FAKE_SERVICE.health.return_value = HEALTH
        self.shell._settings_cache = (0.0, {})
        self.pump(0.5)

    def tearDown(self):
        try:
            self.shell._quit()
        except Exception:
            pass
        for item in self.patches:
            item.stop()
        self.pump(0.2)
        self.assertEqual(self.errors, [])

    # ------------------------------------------------------------------
    def _report_error(self, exc, value, tb):
        self.errors.append((exc, value))

    def pump(self, seconds=0.4):
        """Прокрутить цикл событий Tk заданное время."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                self.root.update()
            except tk.TclError:
                return
            time.sleep(0.02)

    def click(self, widget):
        """Синтетическое нажатие на виджет."""
        target = getattr(widget, '_canvas', widget)
        target.event_generate('<Button-1>')
        self.root.update()

    def rows_of(self, key):
        """Подписи видимых строк режима `key`."""
        return self.panel.modes[key].rows.captions()

    def set_health(self, data):
        """Задать ответ сервиса и дождаться текущего цикла опроса.

        Фоновый опрос `/health` идёт каждые несколько секунд: без
        ожидания уже начатого цикла он вернул бы прежние данные и
        проверка состояния была бы нестабильной.
        """
        FAKE_SERVICE.health.return_value = data
        self.pump(0.4)
        self.shell.update_status(data)
        self.pump(0.1)


class ModeSwitchTests(ShellTestCase):
    """Переключение режимов контекстной панели."""

    def test_click_on_an_indicator_opens_only_its_mode(self):
        self.shell.update_status(HEALTH)
        for key in ('service', 'camera', 'telegram'):
            self.click(self.header.chips[key].frame)
            self.pump(0.2)
            self.assertTrue(self.panel.is_open)
            self.assertEqual(self.panel.key, key)
            self.header.close_details()
            self.pump(0.3)

    def test_click_on_another_indicator_switches_the_content(self):
        self.shell.update_status(HEALTH)
        self.click(self.header.service_chip.frame)
        self.pump(0.2)
        self.assertEqual(self.panel.key, 'service')
        self.click(self.header.camera_chip.frame)
        self.pump(0.2)
        # Панель не закрылась — содержимое сменилось.
        self.assertTrue(self.panel.is_open)
        self.assertEqual(self.panel.key, 'camera')
        self.assertIn('IP-адрес', self.rows_of('camera'))
        self.assertNotIn('IP-адрес', self.rows_of('service'))

    def test_second_click_on_the_active_indicator_closes_the_panel(self):
        self.shell.update_status(HEALTH)
        self.click(self.header.camera_chip.frame)
        self.pump(0.2)
        self.assertTrue(self.panel.is_open)
        self.click(self.header.camera_chip.frame)
        self.pump(0.4)
        self.assertFalse(self.panel.is_open)
        self.assertEqual(self.panel.frame.winfo_manager(), '')

    def test_rapid_switching_between_modes_is_stable(self):
        """Быстрое переключение не оставляет панель в промежуточном состоянии."""
        self.shell.update_status(HEALTH)
        for _ in range(3):
            for key in ('service', 'camera', 'telegram'):
                self.click(self.header.chips[key].frame)
                self.root.update()
        self.pump(0.6)
        self.assertTrue(self.panel.is_open)
        self.assertEqual(self.panel.key, 'telegram')
        self.assertEqual(self.errors, [])
        # Панель осталась на месте: ширина и положение те же.
        frame = self.panel.frame
        self.assertEqual(frame.winfo_width(), 420)

    def test_switching_does_not_change_the_panel_position(self):
        self.shell.update_status(HEALTH)
        self.click(self.header.service_chip.frame)
        self.pump(0.4)
        x = self.panel.frame.winfo_x()
        self.click(self.header.camera_chip.frame)
        self.pump(0.4)
        self.assertEqual(self.panel.frame.winfo_x(), x)

    def test_panel_width_is_the_same_in_every_mode(self):
        self.shell.update_status(HEALTH)
        widths = []
        for key in ('service', 'camera', 'telegram'):
            self.click(self.header.chips[key].frame)
            self.pump(0.3)
            widths.append(self.panel.frame.winfo_width())
        self.assertEqual(len(set(widths)), 1)
        self.assertTrue(400 <= widths[0] <= 440)

    def test_only_one_panel_exists(self):
        """Панель одна: три режима — три блока одного окна."""
        self.shell.update_status(HEALTH)
        self.click(self.header.service_chip.frame)
        self.pump(0.2)
        panels = [widget for widget in self.root.winfo_children()
                  if isinstance(widget, ctk.CTkFrame)
                  and widget.winfo_manager() == 'place']
        self.assertEqual(len(panels), 1)

    def test_active_indicator_is_highlighted(self):
        self.shell.update_status(HEALTH)
        self.click(self.header.telegram_chip.frame)
        self.pump(0.6)
        self.assertEqual(str(self.header.telegram_chip.frame.cget('fg_color')).lower(),
                         '#26364c')
        self.header.close_details()
        self.pump(0.6)
        self.assertNotEqual(str(self.header.telegram_chip.frame.cget('fg_color')).lower(),
                            '#26364c')


class ModeContentTests(ShellTestCase):
    """В каждом режиме — только своя диагностика."""

    def test_service_mode_shows_service_rows_only(self):
        self.shell.update_status(HEALTH)
        self.click(self.header.service_chip.frame)
        self.pump(0.2)
        rows = self.rows_of('service')
        self.assertEqual(rows, ['PID', 'Время работы', 'Порт сервиса',
                                'Последняя проверка', 'Последняя ошибка'])
        self.assertNotIn('IP-адрес', rows)
        self.assertNotIn('Соединение', rows)

    def test_camera_mode_shows_camera_rows_only(self):
        self.shell.update_status(HEALTH)
        self.click(self.header.camera_chip.frame)
        self.pump(0.2)
        rows = self.rows_of('camera')
        self.assertEqual(rows, ['Название', 'IP-адрес', 'События ANPR',
                                'Последнее событие'])
        self.assertNotIn('PID', rows)
        self.assertNotIn('Соединение', rows)

    def test_telegram_mode_shows_telegram_rows_only(self):
        self.shell.update_status(HEALTH)
        self.click(self.header.telegram_chip.frame)
        self.pump(0.2)
        rows = self.rows_of('telegram')
        self.assertEqual(rows, ['Соединение', 'Последняя связь', 'Последняя доставка'])
        self.assertNotIn('PID', rows)
        self.assertNotIn('IP-адрес', rows)

    def test_camera_mode_has_no_service_controls(self):
        self.shell.update_status(HEALTH)
        self.click(self.header.camera_chip.frame)
        self.pump(0.2)
        self.assertFalse(self.panel.start_button.winfo_manager())
        self.assertFalse(self.panel.restart_button.winfo_manager())
        self.assertFalse(self.panel.stop_button.winfo_manager())
        self.assertFalse(self.panel.log_button.winfo_manager())

    def test_telegram_mode_has_no_service_controls(self):
        self.shell.update_status(HEALTH)
        self.click(self.header.telegram_chip.frame)
        self.pump(0.2)
        self.assertFalse(self.panel.start_button.winfo_manager())
        self.assertFalse(self.panel.stop_button.winfo_manager())

    def test_service_mode_keeps_the_controls(self):
        self.shell.update_status(HEALTH)
        self.click(self.header.service_chip.frame)
        self.pump(0.2)
        self.assertTrue(self.panel.restart_button.winfo_manager())
        self.assertTrue(self.panel.stop_button.winfo_manager())
        self.assertTrue(self.panel.log_button.winfo_manager())
        self.assertFalse(self.panel.start_button.winfo_manager())

    def test_stopped_service_offers_start(self):
        FAKE_SERVICE.health.side_effect = requests.ConnectionError('offline')
        self.pump(0.2)
        self.shell.update_status(None)
        self.click(self.header.service_chip.frame)
        self.pump(0.2)
        self.assertTrue(self.panel.start_button.winfo_manager())
        self.assertFalse(self.panel.stop_button.winfo_manager())
        self.assertTrue(self.panel.log_button.winfo_manager())

    def test_unreachable_camera_marks_the_last_data_as_stale(self):
        broken = dict(HEALTH, camera_message='связь прервалась. Программа попробует снова.')
        self.set_health(broken)
        self.click(self.header.camera_chip.frame)
        self.pump(0.2)
        data = self.shell.panel_data()
        camera = dict(data['camera']['rows'])
        self.assertEqual(camera.get('События ANPR'), 'ошибка')
        self.assertIn('устарело', camera.get('Последнее событие'))
        self.assertEqual(self.panel.status_label.cget('text'), 'Нет связи')

    def test_telegram_error_is_reported_as_a_row(self):
        lost = dict(HEALTH, telegram_connection={'ok': False, 'at': '',
                                                 'failed_at': '2026-11-18T13:09:07+05:00',
                                                 'error': 'Connection refused'})
        self.set_health(lost)
        self.click(self.header.telegram_chip.frame)
        self.pump(0.2)
        data = self.shell.panel_data()
        telegram = dict(data['telegram']['rows'])
        self.assertEqual(telegram.get('Соединение'), 'Нет')
        self.assertEqual(telegram.get('Ошибка связи'), 'Connection refused')


class StatusIndependenceTests(ShellTestCase):
    """Статусы компонентов независимы друг от друга."""

    def test_camera_failure_does_not_change_the_service_indicator(self):
        broken = dict(HEALTH, camera_message='связь прервалась. Программа попробует снова.')
        self.set_health(broken)
        self.assertEqual(self.header.service_chip.value.cget('text'), 'Работает')
        self.assertEqual(self.header.camera_chip.value.cget('text'), 'Нет связи')
        self.assertEqual(self.shell._states['service']['color'], ui_status.SUCCESS)
        self.assertEqual(self.shell._states['camera']['color'], ui_status.DANGER)

    def test_telegram_failure_does_not_change_the_service_indicator(self):
        lost = dict(HEALTH, telegram_connection={'ok': False, 'at': '',
                                                 'failed_at': '2026-11-18T13:09:07+05:00',
                                                 'error': 'Connection refused'})
        self.set_health(lost)
        self.assertEqual(self.header.service_chip.value.cget('text'), 'Работает')
        self.assertEqual(self.header.telegram_chip.value.cget('text'), 'Недоступен')

    def test_state_change_while_the_panel_is_open(self):
        """Состояние меняется при открытой панели — индикатор и панель."""
        self.shell.update_status(HEALTH)
        self.click(self.header.camera_chip.frame)
        self.pump(0.2)
        broken = dict(HEALTH, camera_message='связь прервалась. Программа попробует снова.')
        self.set_health(broken)
        self.assertEqual(self.header.camera_chip.value.cget('text'), 'Нет связи')
        self.assertEqual(self.panel.status_label.cget('text'), 'Нет связи')
        self.assertIn('устарело', dict(self.shell.panel_data()['camera']['rows'])
                      .get('Последнее событие'))

    def test_service_stopped_while_the_panel_is_open(self):
        self.shell.update_status(HEALTH)
        self.click(self.header.service_chip.frame)
        self.pump(0.2)
        FAKE_SERVICE.health.side_effect = requests.ConnectionError('offline')
        self.pump(0.2)
        self.shell.update_status(None)
        self.pump(0.3)
        self.assertEqual(self.header.service_chip.value.cget('text'), 'Остановлен')
        self.assertTrue(self.panel.start_button.winfo_manager())
        self.assertFalse(self.panel.stop_button.winfo_manager())


class PanelClosingTests(ShellTestCase):
    """Панель закрывается всеми ожидаемыми способами."""

    def test_escape_closes_the_panel(self):
        self.shell.update_status(HEALTH)
        self.click(self.header.camera_chip.frame)
        self.pump(0.2)
        self.root.deiconify()
        self.root.update()
        try:
            self.panel.frame.focus_force()
        except tk.TclError:
            pass
        self.root.event_generate('<Escape>')
        self.pump(0.4)
        self.assertFalse(self.panel.is_open)

    def test_click_outside_closes_the_panel(self):
        self.shell.update_status(HEALTH)
        self.click(self.header.service_chip.frame)
        self.pump(0.2)
        self.click(self.shell.root)
        self.pump(0.4)
        self.assertFalse(self.panel.is_open)

    def test_switching_section_closes_the_panel(self):
        self.shell.update_status(HEALTH)
        self.click(self.header.service_chip.frame)
        self.pump(0.2)
        self.shell.show_section('settings')
        self.pump(0.3)
        self.assertFalse(self.panel.is_open)

    def test_panel_does_not_move_the_dashboard(self):
        self.shell.update_status(HEALTH)
        self.pump(0.2)
        before = self.shell.content.winfo_rooty()
        self.click(self.header.camera_chip.frame)
        self.pump(0.4)
        self.assertEqual(self.shell.content.winfo_rooty(), before)

    def test_panel_stays_inside_the_window(self):
        self.root.deiconify()
        self.root.geometry('1280x800+10+10')
        self.pump(0.4)
        self.shell.update_status(HEALTH)
        self.click(self.header.telegram_chip.frame)
        self.pump(0.4)
        frame = self.panel.frame
        self.assertGreaterEqual(frame.winfo_x(), 0)
        self.assertLessEqual(frame.winfo_x() + frame.winfo_reqwidth(),
                             self.root.winfo_width())
        self.assertLessEqual(frame.winfo_y() + frame.winfo_reqheight(),
                             self.root.winfo_height())
        self.root.withdraw()


class ScalingTests(ShellTestCase):
    """Масштабирование интерфейса (125 %): панель остаётся в окне."""

    def test_panel_stays_inside_the_window_at_125_percent(self):
        import customtkinter as ctk
        self.root.deiconify()
        self.root.geometry('1280x800+10+10')
        self.pump(0.5)
        self.shell.update_status(HEALTH)
        try:
            ctk.set_widget_scaling(1.25)
            self.pump(0.4)
            self.click(self.header.camera_chip.frame)
            self.pump(0.5)
            frame = self.panel.frame
            self.assertGreater(frame.winfo_width(), 400)
            self.assertGreaterEqual(frame.winfo_x(), 0)
            self.assertLessEqual(frame.winfo_x() + frame.winfo_width(),
                                 self.root.winfo_width())
        finally:
            ctk.set_widget_scaling(1.0)
            self.pump(0.2)


class PanelAnimationTests(ShellTestCase):
    """Анимации не оставляют «висящих» вызовов."""

    def test_repeated_switching_does_not_accumulate_callbacks(self):
        self.shell.update_status(HEALTH)
        for _ in range(5):
            for key in ('service', 'camera', 'telegram'):
                self.click(self.header.chips[key].frame)
                self.pump(0.05)
        self.pump(0.5)
        self.assertLessEqual(self.panel.motion.pending, 4)
        self.assertLessEqual(self.header.motion.pending, 4)

    def test_closing_the_window_cancels_panel_callbacks(self):
        self.shell.update_status(HEALTH)
        self.click(self.header.service_chip.frame)
        self.pump(0.2)
        self.shell._quit()
        self.pump(0.3)
        self.assertEqual(self.panel.motion.pending, 0)
        self.assertEqual(self.errors, [])

    def test_closing_the_window_prints_no_invalid_command(self):
        import os
        import tempfile
        self.shell.update_status(HEALTH)
        self.click(self.header.camera_chip.frame)
        self.pump(0.3)
        with tempfile.TemporaryFile(mode='w+b') as buffer:
            saved = os.dup(2)
            try:
                os.dup2(buffer.fileno(), 2)
                self.shell._quit()
                self.pump(0.3)
            finally:
                os.dup2(saved, 2)
                os.close(saved)
            buffer.seek(0)
            output = buffer.read().decode('utf-8', 'replace')
        self.assertNotIn('invalid command name', output)

    def test_reduced_motion_switches_without_animation(self):
        from app.ui import motion as motion_module
        self.shell.update_status(HEALTH)
        with patch.object(motion_module, 'reduced_motion', return_value=True):
            manager = motion_module.Motion(self.root)
            self.assertTrue(manager.reduced)
        self.click(self.header.service_chip.frame)
        self.root.update()
        self.assertTrue(self.panel.is_open)
        self.click(self.header.telegram_chip.frame)
        self.root.update()
        self.assertEqual(self.panel.key, 'telegram')
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main()
