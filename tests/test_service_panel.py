"""Тесты блока «Сервис»: компактная шапка и раскрывающаяся панель управления.

Проверяется новая модель интерфейса:

* в шапке три независимых индикатора (сервис, камера, Telegram) — сломанная
  камера не красит сервис в красный, недоступный Telegram не мешает
  управлять сервисом;
* управление сервисом живёт в раскрывающейся панели: панель открывается
  кнопкой «Управление» и нажатием на индикатор, закрывается повторным
  нажатием, Escape, кликом вне панели и сменой раздела;
* панель не сдвигает Dashboard и не перекрывает кнопки окна;
* набор кнопок зависит от состояния сервиса, во время операции действия
  заблокированы;
* подтверждения остановки и перезапуска сохраняются и не отправляют запрос
  до ответа пользователя;
* анимации не оставляют «висящих» отложенных вызовов и не ломают
  приложение при закрытии окна.

Живой сервис, камера и база не затрагиваются: `service.*`, `cv2` и
`load_dashboard` подменяются заглушками (общая оснастка —
`tests/ui_harness.py`).
"""
from tests import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД
import tkinter as tk
import unittest
from unittest.mock import patch

from app.ui import status as ui_status
from tests.ui_harness import HEALTH, FAKE_SERVICE, ShellTestCase, reset_calls


def setUpModule():
    global _service_patch
    from tests import ui_harness
    _service_patch = ui_harness.patch_service()


def tearDownModule():
    _service_patch.stop()


class HeaderIndicatorTests(ShellTestCase):
    """Три независимых индикатора в шапке."""

    def test_three_indicators_are_present(self):
        self.assertEqual(sorted(self.header.chips), ['camera', 'service', 'telegram'])
        for chip in self.header.chips.values():
            self.assertTrue(chip.frame.winfo_exists())
            self.assertTrue(chip.icon.label.winfo_exists())
            self.assertTrue(chip.value.winfo_exists())

    def test_indicators_have_no_borders(self):
        for chip in self.header.chips.values():
            self.assertEqual(int(chip.frame.cget('border_width')), 0)

    def test_header_has_no_manage_button_and_no_dots(self):
        """Кнопки «Управление» и отдельных цветных точек больше нет."""
        self.assertFalse(hasattr(self.header, 'manage_button'))
        for chip in self.header.chips.values():
            self.assertFalse(hasattr(chip, 'dot'))

    def test_indicator_shows_icon_and_status_only(self):
        """Индикатор — это иконка и краткий статус, без названий."""
        self.shell.update_status(HEALTH)
        chip = self.header.camera_chip
        self.assertEqual(chip.value.cget('text'), 'Подключена')
        self.assertEqual(chip.icon.label.cget('text'), '')

    def test_tooltip_follows_the_state(self):
        self.shell.update_status(HEALTH)
        self.assertEqual(self.header.service_chip.tooltip.text, 'ANPR-сервис: Работает')
        self.assertEqual(self.header.camera_chip.tooltip.text, 'Камера: Подключена')
        self.assertEqual(self.header.telegram_chip.tooltip.text, 'Telegram: Работает')
        broken = dict(HEALTH, camera_message='связь прервалась. Программа попробует снова.')
        with patch('app.ui.settings.read_settings', return_value={
                'TELEGRAM_BOT_TOKEN': 'example', 'ALLOWED_CHAT_IDS': '42'}):
            self.shell.update_status(broken)
        self.assertEqual(self.header.camera_chip.tooltip.text, 'Камера: Нет связи')

    def test_camera_failure_does_not_change_the_service_indicator(self):
        """Сломанная камера не должна красить сервис в другой цвет."""
        self.shell._settings_cache = (0.0, {})
        settings = {'TELEGRAM_BOT_TOKEN': 'example', 'ALLOWED_CHAT_IDS': '42'}
        broken = dict(HEALTH, camera_message='связь прервалась. Программа попробует снова.',
                      last_camera_data='')
        with patch('app.ui.settings.read_settings', return_value=settings):
            self.shell.update_status(broken)
            self.assertEqual(self.header.service_chip.value.cget('text'), 'Работает')
            self.assertEqual(self.header.camera_chip.value.cget('text'), 'Нет связи')
            self.assertEqual(self.header.telegram_chip.value.cget('text'), 'Работает')
            # Цвет сервиса не меняется из-за камеры.
            self.assertEqual(self.shell._states['service']['color'], ui_status.SUCCESS)
            self.assertEqual(self.shell._states['camera']['color'], ui_status.DANGER)

    def test_telegram_failure_does_not_change_the_service_indicator(self):
        self.shell._settings_cache = (0.0, {})
        settings = {'TELEGRAM_BOT_TOKEN': 'example', 'ALLOWED_CHAT_IDS': '42'}
        lost = dict(HEALTH, telegram_connection={'ok': False, 'at': '',
                                                 'failed_at': '2026-11-18T13:09:07+05:00',
                                                 'error': 'Connection refused'})
        with patch('app.ui.settings.read_settings', return_value=settings):
            self.shell.update_status(lost)
            self.assertEqual(self.header.service_chip.value.cget('text'), 'Работает')
            self.assertEqual(self.header.telegram_chip.value.cget('text'), 'Недоступен')

    def test_no_general_system_warning(self):
        """Общего индикатора «система неисправна» в шапке нет."""
        self.shell._settings_cache = (0.0, {})
        settings = {'TELEGRAM_BOT_TOKEN': 'example', 'ALLOWED_CHAT_IDS': '42'}
        broken = dict(HEALTH, camera_message='ошибка подключения к камере',
                      telegram_connection={'ok': False, 'at': '', 'failed_at': '', 'error': 'x'})
        with patch('app.ui.settings.read_settings', return_value=settings):
            self.shell.update_status(broken)
            texts = [chip.value.cget('text') for chip in self.header.chips.values()]
        self.assertNotIn('Система неисправна', texts)
        for text in texts:
            self.assertNotIn('система', text.lower())


class ServicePanelTests(ShellTestCase):
    """Раскрывающаяся панель управления."""

    def test_panel_opens_with_the_manage_toggle(self):
        self.shell.update_status(HEALTH)
        self.header.toggle_panel()
        self.root.update()
        self.assertTrue(self.panel.is_open)
        self.assertEqual(self.panel.frame.winfo_manager(), 'place')

    def test_escape_closes_the_panel(self):
        self.header.toggle_panel()
        self.root.update()
        # Escape приходит окну, в фокусе которого находится панель, а
        # привязка живёт на корневом окне приложения. Свёрнутое окно фокус
        # не держит — на время проверки окно возвращается на экран, иначе
        # Tk выбросит событие вместо доставки.
        self.root.deiconify()
        self.root.update()
        try:
            self.panel.frame.focus_force()
        except tk.TclError:
            pass
        self.root.event_generate('<Escape>')
        self.assertFalse(self.panel.is_open)

    def test_click_outside_closes_the_panel(self):
        self.header.toggle_panel()
        self.root.update()
        self.click(self.shell.root)      # клик по пустому месту окна
        self.assertFalse(self.panel.is_open)

    def test_click_inside_the_panel_keeps_it_open(self):
        self.header.toggle_panel()
        self.root.update()
        self.click(self.panel.title)
        self.assertTrue(self.panel.is_open)

    def test_switching_section_closes_the_panel(self):
        self.header.toggle_panel()
        self.root.update()
        self.shell.show_section('settings')
        self.assertFalse(self.panel.is_open)

    def test_refresh_skips_the_layout_while_the_panel_is_closed(self):
        """Скрытая панель обновляет кнопки, но не делает дорогую перекладку Tk."""
        self.assertFalse(self.panel.is_open)
        with patch.object(self.panel, '_layout_content',
                          side_effect=AssertionError('скрытая панель не должна перекладываться')):
            self.header.refresh_panel()
        self.shell.busy = True
        self.shell._set_busy(True)
        self.assertEqual(str(self.header.action_button.cget('state')), 'disabled')
        self.shell.busy = False
        self.shell._set_busy(False)
        self.header.toggle_panel()
        self.assertTrue(self.panel.is_open)

    def test_panel_does_not_move_the_dashboard(self):
        self.shell.update_status(HEALTH)
        self.settle()
        before = self.shell.content.winfo_rooty()
        self.header.toggle_panel()
        self.settle()
        self.assertEqual(self.shell.content.winfo_rooty(), before)

    def test_panel_stays_inside_the_window(self):
        # Геометрия проверяется на отображённом окне: у скрытого окна
        # размеры ещё не применены.
        self.root.deiconify()
        self.root.geometry('1280x800+10+10')
        self.pump_until(lambda: self.root.winfo_width() == 1280, timeout=1.0)
        self.header.toggle_panel()
        self.settle()
        frame = self.panel.frame
        self.assertGreaterEqual(frame.winfo_x(), 0)
        self.assertLessEqual(frame.winfo_x() + frame.winfo_reqwidth(),
                             self.root.winfo_width())
        self.root.withdraw()

    def test_panel_shows_service_details(self):
        self.shell.update_status(HEALTH)
        self.header.toggle_panel()
        rows = dict(self.shell.panel_data()['service']['rows'])
        self.assertEqual(rows.get('PID'), '4242')
        self.assertEqual(rows.get('Порт сервиса'), '18749')
        self.assertIn('Время работы', rows)
        self.assertIn('Последняя проверка', rows)

    def test_panel_shows_camera_and_telegram_details(self):
        self.shell.update_status(HEALTH)
        self.header.toggle_panel()
        data = self.shell.panel_data()
        camera = dict(data['camera']['rows'])
        telegram = dict(data['telegram']['rows'])
        self.assertEqual(camera.get('IP-адрес'), '192.168.100.249')
        self.assertEqual(camera.get('События ANPR'), 'принимаются')
        self.assertEqual(telegram.get('Соединение'), 'Есть')
        self.assertEqual(telegram.get('Последняя доставка'), 'Доставлено')

    def test_panel_shows_no_fake_numbers(self):
        """Показателей, которых нет в данных, в панели не должно быть."""
        self.service_offline()
        self.shell.update_status(None)
        data = self.shell.panel_data()
        service = dict(data['service']['rows'])
        camera = dict(data['camera']['rows'])
        self.assertNotIn('PID', service)
        self.assertNotIn('Время работы', service)
        self.assertEqual(camera.get('События ANPR'), 'нет данных')


class PanelActionsTests(ShellTestCase):
    """Кнопки управления в панели."""

    def test_running_service_offers_restart_stop_and_log(self):
        self.shell.update_status(HEALTH)
        self.header.toggle_panel()
        self.assertTrue(self.panel.restart_button.winfo_manager())
        self.assertTrue(self.panel.stop_button.winfo_manager())
        self.assertFalse(self.panel.start_button.winfo_manager())
        self.assertTrue(self.panel.log_button.winfo_manager())

    def test_stopped_service_offers_start_and_log(self):
        self.service_offline()
        self.shell.update_status(None)
        self.header.toggle_panel()
        self.assertFalse(self.panel.restart_button.winfo_manager())
        self.assertFalse(self.panel.stop_button.winfo_manager())
        self.assertTrue(self.panel.start_button.winfo_manager())
        self.assertTrue(self.panel.log_button.winfo_manager())

    def test_busy_state_blocks_actions_and_shows_progress(self):
        self.shell.update_status(HEALTH)
        self.shell.busy = True
        self.shell.operation = 'restart'
        self.header.refresh_panel()
        self.header.toggle_panel()
        for button in (self.panel.start_button, self.panel.restart_button, self.panel.stop_button):
            self.assertEqual(str(button.cget('state')), 'disabled')
        self.assertTrue(self.panel.progress_track.winfo_manager())
        self.shell.busy = False
        self.shell.operation = None

    def test_repeated_restart_is_impossible(self):
        """Повторное нажатие не запускает вторую операцию."""
        self.shell.update_status(HEALTH)
        with patch('app.ui.shell.messagebox.askyesno', return_value=True):
            self.shell.restart_service()
            self.shell.restart_service()
        self.assertTrue(self.shell.busy)
        self.shell.busy = False
        self.shell.operation = None

    def test_error_state_offers_recovery_according_to_the_process(self):
        """В состоянии ошибки действие выбирается по факту процесса."""
        self.service_offline()
        self.shell.update_status(None, error='сервис не отвечает')
        with patch('app.ui.shell.process_info.pid_alive', return_value=False):
            self.header.toggle_panel()
            self.assertTrue(self.panel.start_button.winfo_manager())
            self.assertFalse(self.panel.restart_button.winfo_manager())
            self.header.close_details()
        with patch('app.ui.shell.process_info.pid_alive', return_value=True), \
                patch('app.ui.shell.process_info.looks_like_service', return_value=True):
            self.shell._health = {'pid': 4242, 'status': 'worker_stopped'}
            self.shell.update_status(self.shell._health)
            self.header.toggle_panel()
            self.assertTrue(self.panel.restart_button.winfo_manager())
            self.assertFalse(self.panel.start_button.winfo_manager())


class ConfirmationTests(ShellTestCase):
    """Подтверждения остановки и перезапуска."""

    def test_stop_requires_confirmation(self):
        reset_calls()
        with patch('app.ui.shell.messagebox.askyesno', return_value=False) as confirm:
            self.shell.stop_service()
            confirm.assert_called_once()
            title, text = confirm.call_args[0][0], confirm.call_args[0][1]
        self.assertEqual(title, 'Остановить ANPR-сервис?')
        self.assertIn('Распознавание номеров', text)
        FAKE_SERVICE.stop.assert_not_called()

    def test_restart_requires_confirmation(self):
        reset_calls()
        with patch('app.ui.shell.messagebox.askyesno', return_value=False) as confirm:
            self.shell.restart_service()
            confirm.assert_called_once()
            title, text = confirm.call_args[0][0], confirm.call_args[0][1]
        self.assertEqual(title, 'Перезапустить ANPR-сервис?')
        self.assertIn('обработка событий', text)
        FAKE_SERVICE.stop.assert_not_called()
        FAKE_SERVICE.start.assert_not_called()

    def test_start_does_not_ask_confirmation(self):
        reset_calls()
        with patch('app.ui.shell.messagebox.askyesno') as confirm:
            self.shell.start_service()
            confirm.assert_not_called()
        self.assertTrue(self.shell.busy)
        self.shell.busy = False
        self.shell.operation = None


class OperationMessageTests(ShellTestCase):
    """Сообщение о результате операции."""

    def test_successful_operation_reports_the_result(self):
        with patch('app.ui.shell.messagebox.askyesno', return_value=True):
            self.shell.stop_service()
            self.wait_idle()
        data = self.shell.panel_data()
        self.assertEqual(data['message'], 'ANPR-сервис остановлен.')
        self.assertEqual(data['message_tone'], 'ok')

    def test_failed_operation_reports_the_reason_and_opens_the_panel(self):
        self.header.close_details()
        with patch('app.ui.shell.service.start', side_effect=RuntimeError('порт занят')):
            self.shell.start_service()
            self.wait_idle()
        data = self.shell.panel_data()
        self.assertIn('порт занят', data['message'])
        self.assertEqual(data['message_tone'], 'error')
        # Ошибку нужно увидеть сразу — панель открывается сама.
        self.assertTrue(self.panel.is_open)

    def test_error_is_shown_without_fake_details(self):
        self.shell.show_message('сервис не отвечает', tone='error')
        self.assertEqual(str(self.panel.message_label.cget('text')), 'сервис не отвечает')
        self.assertTrue(self.panel.message_icon.winfo_manager())


class AnimationTests(ShellTestCase):
    """Анимации не оставляют «висящих» вызовов."""

    def test_repeated_open_and_close_does_not_accumulate_callbacks(self):
        self.shell.update_status(HEALTH)
        for _ in range(6):
            self.header.toggle_panel()
            self.settle()
            self.header.toggle_panel()
            self.settle()
        self.assertLessEqual(self.panel.motion.pending, 4)
        self.assertLessEqual(self.header.motion.pending, 4)

    def test_closing_the_window_cancels_panel_callbacks(self):
        self.header.toggle_panel()
        self.settle()
        self.shell._quit()
        self.assertEqual(self.panel.motion.pending, 0)
        self.assertEqual(self.errors, [])

    def test_header_does_not_schedule_callbacks_after_shutdown(self):
        """Закрытая шапка не планирует новых вызовов.

        При закрытии окна виджеты ещё Destroy-ятся, и на это приходят
        Configure-события. Если шапка на них реагирует, она успевает
        запланировать вызовы — они сработают уже на закрытом окне
        («invalid command name …»).
        """
        self.shell.update_status(HEALTH)
        self.header.toggle_panel()
        self.settle()
        self.header.shutdown()
        pending = self.header.motion.pending
        self.header.overlay.event_generate('<Configure>')
        self.settle()
        self.assertEqual(self.header.motion.pending, pending)

    def test_closing_the_window_leaves_no_pending_callbacks(self):
        """После закрытия окна не остаётся отложенных вызовов Tk.

        Именно такие вызовы печатают «invalid command name …», когда
        срабатывают уже на уничтоженном окне.
        """
        self.root.deiconify()
        self.shell.update_status(HEALTH)
        self.header.toggle_panel()
        self.settle()
        self.shell._quit()
        self.assertEqual(tuple(self.root.tk.call('after', 'info')), ())

    def test_closing_the_window_prints_no_invalid_command(self):
        """Закрытие окна не печатает «invalid command name …»."""
        import os
        import tempfile
        self.shell.update_status(HEALTH)
        self.header.toggle_panel()
        self.settle()
        with tempfile.TemporaryFile(mode='w+b') as buffer:
            saved = os.dup(2)
            try:
                os.dup2(buffer.fileno(), 2)
                self.shell._quit()
                self.pump(0.05)
            finally:
                os.dup2(saved, 2)
                os.close(saved)
            buffer.seek(0)
            output = buffer.read().decode('utf-8', 'replace')
        self.assertNotIn('invalid command name', output)

    def test_reduced_motion_opens_the_panel_without_animation(self):
        from app.ui import motion as motion_module
        self.shell.update_status(HEALTH)
        with patch.object(motion_module, 'reduced_motion', return_value=True):
            manager = motion_module.Motion(self.root)
            self.assertTrue(manager.reduced)
        self.header.toggle_panel()
        self.root.update()
        self.assertTrue(self.panel.is_open)
        self.assertEqual(self.panel.frame.winfo_manager(), 'place')


if __name__ == '__main__':
    unittest.main()
