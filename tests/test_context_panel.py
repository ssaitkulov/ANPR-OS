"""Тесты контекстной панели диагностики.

Модель «три компактных индикатора — одна контекстная панель»:

* нажатие на индикатор открывает панель в режиме этого компонента;
* нажатие на другой индикатор переключает содержимое, не закрывая панель;
* повторное нажатие на активный индикатор закрывает панель;
* в каждом режиме — только своя диагностика, без кнопок чужого компонента;
* панель одна: ширина одинакова во всех режимах, Dashboard не сдвигается,
  при 125 % DPI панель остаётся в окне.

Общие проверки (Escape, клик вне панели, смена раздела, независимость
статусов, отмена отложенных вызовов) — в `tests/test_service_panel.py`,
оснастка — в `tests/ui_harness.py`.
"""
from tests import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД
import unittest
from unittest.mock import patch

import customtkinter as ctk

from tests.ui_harness import HEALTH, FAKE_SERVICE, ShellTestCase


def setUpModule():
    global _service_patch
    from tests import ui_harness
    _service_patch = ui_harness.patch_service()


def tearDownModule():
    _service_patch.stop()


class ModeSwitchTests(ShellTestCase):
    """Переключение режимов контекстной панели."""

    def test_click_on_an_indicator_opens_only_its_mode(self):
        self.shell.update_status(HEALTH)
        for key in ('service', 'camera', 'telegram'):
            self.click(self.header.chips[key].frame)
            self.assertTrue(self.panel.is_open)
            self.assertEqual(self.panel.key, key)
            self.header.close_details()

    def test_click_on_another_indicator_switches_the_content(self):
        self.shell.update_status(HEALTH)
        self.click(self.header.service_chip.frame)
        self.assertEqual(self.panel.key, 'service')
        self.click(self.header.camera_chip.frame)
        # Панель не закрылась — содержимое сменилось.
        self.assertTrue(self.panel.is_open)
        self.assertEqual(self.panel.key, 'camera')
        self.assertIn('IP-адрес', self.rows_of('camera'))
        self.assertNotIn('IP-адрес', self.rows_of('service'))

    def test_second_click_on_the_active_indicator_closes_the_panel(self):
        self.shell.update_status(HEALTH)
        self.click(self.header.camera_chip.frame)
        self.assertTrue(self.panel.is_open)
        self.click(self.header.camera_chip.frame)
        self.assertFalse(self.panel.is_open)
        # Панель убирается после анимации ухода.
        self.pump_until(lambda: self.panel.frame.winfo_manager() == '', timeout=1.0)
        self.assertEqual(self.panel.frame.winfo_manager(), '')

    def test_rapid_switching_between_modes_is_stable(self):
        """Быстрое переключение не оставляет панель в промежуточном состоянии."""
        self.shell.update_status(HEALTH)
        for _ in range(3):
            for key in ('service', 'camera', 'telegram'):
                self.click(self.header.chips[key].frame)
                self.root.update()
        self.settle()
        self.assertTrue(self.panel.is_open)
        self.assertEqual(self.panel.key, 'telegram')
        self.assertEqual(self.errors, [])
        # Панель осталась на месте: ширина и положение те же.
        self.assertEqual(self.panel.frame.winfo_width(), 420)

    def test_switching_does_not_change_the_panel_position(self):
        self.shell.update_status(HEALTH)
        self.click(self.header.service_chip.frame)
        self.settle()
        x = self.panel.frame.winfo_x()
        self.click(self.header.camera_chip.frame)
        self.settle()
        self.assertEqual(self.panel.frame.winfo_x(), x)

    def test_panel_width_is_the_same_in_every_mode(self):
        self.shell.update_status(HEALTH)
        widths = []
        for key in ('service', 'camera', 'telegram'):
            self.click(self.header.chips[key].frame)
            self.pump_until(lambda: self.panel.frame.winfo_width() > 0, timeout=1.0)
            widths.append(self.panel.frame.winfo_width())
        self.assertEqual(len(set(widths)), 1)
        self.assertTrue(400 <= widths[0] <= 440)

    def test_only_one_panel_exists(self):
        """Панель одна: три режима — три блока одного окна."""
        self.shell.update_status(HEALTH)
        self.click(self.header.service_chip.frame)
        panels = [widget for widget in self.root.winfo_children()
                  if isinstance(widget, ctk.CTkFrame)
                  and widget.winfo_manager() == 'place']
        self.assertEqual(len(panels), 1)

    def test_active_indicator_is_highlighted(self):
        self.shell.update_status(HEALTH)
        self.click(self.header.telegram_chip.frame)
        self.settle()
        self.assertEqual(str(self.header.telegram_chip.frame.cget('fg_color')).lower(),
                         '#26364c')
        self.header.close_details()
        self.settle()
        self.assertNotEqual(str(self.header.telegram_chip.frame.cget('fg_color')).lower(),
                            '#26364c')


class ModeContentTests(ShellTestCase):
    """В каждом режиме — только своя диагностика."""

    def test_service_mode_shows_service_rows_only(self):
        self.shell.update_status(HEALTH)
        self.click(self.header.service_chip.frame)
        rows = self.rows_of('service')
        self.assertEqual(rows, ['PID', 'Время работы', 'Порт сервиса',
                                'Последняя проверка', 'Последняя ошибка'])
        self.assertNotIn('IP-адрес', rows)
        self.assertNotIn('Соединение', rows)

    def test_camera_mode_shows_camera_rows_only(self):
        self.shell.update_status(HEALTH)
        self.click(self.header.camera_chip.frame)
        rows = self.rows_of('camera')
        self.assertEqual(rows, ['Название', 'IP-адрес', 'События ANPR',
                                'Последнее событие'])
        self.assertNotIn('PID', rows)
        self.assertNotIn('Соединение', rows)

    def test_telegram_mode_shows_telegram_rows_only(self):
        self.shell.update_status(HEALTH)
        self.click(self.header.telegram_chip.frame)
        rows = self.rows_of('telegram')
        self.assertEqual(rows, ['Соединение', 'Последняя связь', 'Последняя доставка'])
        self.assertNotIn('PID', rows)
        self.assertNotIn('IP-адрес', rows)

    def test_camera_mode_has_no_service_controls(self):
        self.shell.update_status(HEALTH)
        self.click(self.header.camera_chip.frame)
        for button in (self.panel.start_button, self.panel.restart_button,
                       self.panel.stop_button, self.panel.log_button):
            self.assertFalse(button.winfo_manager())

    def test_telegram_mode_has_no_service_controls(self):
        self.shell.update_status(HEALTH)
        self.click(self.header.telegram_chip.frame)
        for button in (self.panel.start_button, self.panel.restart_button,
                       self.panel.stop_button, self.panel.log_button):
            self.assertFalse(button.winfo_manager())

    def test_service_mode_keeps_the_controls(self):
        self.shell.update_status(HEALTH)
        self.click(self.header.service_chip.frame)
        self.assertTrue(self.panel.restart_button.winfo_manager())
        self.assertTrue(self.panel.stop_button.winfo_manager())
        self.assertTrue(self.panel.log_button.winfo_manager())
        self.assertFalse(self.panel.start_button.winfo_manager())

    def test_stopped_service_offers_start(self):
        self.service_offline()
        self.shell.update_status(None)
        self.click(self.header.service_chip.frame)
        self.assertTrue(self.panel.start_button.winfo_manager())
        self.assertFalse(self.panel.stop_button.winfo_manager())
        self.assertTrue(self.panel.log_button.winfo_manager())

    def test_unreachable_camera_marks_the_last_data_as_stale(self):
        broken = dict(HEALTH, camera_message='связь прервалась. Программа попробует снова.')
        self.set_health(broken)
        self.click(self.header.camera_chip.frame)
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
        data = self.shell.panel_data()
        telegram = dict(data['telegram']['rows'])
        self.assertEqual(telegram.get('Соединение'), 'Нет')
        self.assertEqual(telegram.get('Ошибка связи'), 'Connection refused')


class StatusWhileOpenTests(ShellTestCase):
    """Состояние меняется при открытой панели."""

    def test_state_change_while_the_panel_is_open(self):
        """Индикатор и заголовок панели показывают новое состояние."""
        self.shell.update_status(HEALTH)
        self.click(self.header.camera_chip.frame)
        broken = dict(HEALTH, camera_message='связь прервалась. Программа попробует снова.')
        self.set_health(broken)
        self.assertEqual(self.header.camera_chip.value.cget('text'), 'Нет связи')
        self.assertEqual(self.panel.status_label.cget('text'), 'Нет связи')
        self.assertIn('устарело', dict(self.shell.panel_data()['camera']['rows'])
                      .get('Последнее событие'))

    def test_service_stopped_while_the_panel_is_open(self):
        self.shell.update_status(HEALTH)
        self.click(self.header.service_chip.frame)
        self.service_offline()
        self.shell.update_status(None)
        self.assertEqual(self.header.service_chip.value.cget('text'), 'Остановлен')
        self.assertTrue(self.panel.start_button.winfo_manager())
        self.assertFalse(self.panel.stop_button.winfo_manager())




class PanelLifecycleTests(ShellTestCase):
    """Панель живёт по правилам окна: трей, DPI, закрытие."""

    def test_minimizing_to_tray_closes_the_panel(self):
        """Сворачивание в трей закрывает панель."""
        self.shell.update_status(HEALTH)
        self.click(self.header.service_chip.frame)
        self.assertTrue(self.panel.is_open)
        self.shell.hide()
        self.assertFalse(self.panel.is_open)

    def test_panel_stays_inside_the_window_at_125_percent(self):
        ctk.set_widget_scaling(1.25)
        try:
            self.root.deiconify()
            self.root.geometry('1280x800+10+10')
            self.pump_until(lambda: self.root.winfo_width() == 1280, timeout=2.0)
            self.shell.update_status(HEALTH)
            self.click(self.header.camera_chip.frame)
            self.settle()
            frame = self.panel.frame
            self.assertGreater(frame.winfo_width(), 400)
            self.assertGreaterEqual(frame.winfo_x(), 0)
            self.assertLessEqual(frame.winfo_x() + frame.winfo_width(),
                                 self.root.winfo_width())
        finally:
            ctk.set_widget_scaling(1.0)

    def test_mode_switch_without_animation_under_reduced_motion(self):
        from app.ui import motion as motion_module
        with patch.object(motion_module, 'reduced_motion', return_value=True):
            manager = motion_module.Motion(self.root)
            self.assertTrue(manager.reduced)
        self.shell.update_status(HEALTH)
        self.click(self.header.service_chip.frame)
        self.root.update()
        self.assertTrue(self.panel.is_open)
        self.click(self.header.telegram_chip.frame)
        self.root.update()
        self.assertEqual(self.panel.key, 'telegram')
        self.assertEqual(self.errors, [])

    def test_rapid_switching_does_not_accumulate_callbacks(self):
        self.shell.update_status(HEALTH)
        for _ in range(5):
            for key in ('service', 'camera', 'telegram'):
                self.click(self.header.chips[key].frame)
                self.root.update()
        self.settle()
        self.assertLessEqual(self.panel.motion.pending, 4)
        self.assertLessEqual(self.header.motion.pending, 4)


if __name__ == '__main__':
    unittest.main()
