"""Общая оснастка тестов интерфейса Эврики.

Один набор заглушек и одно окно приложения на все UI-тесты: правка
заглушки (например, ответа `/health`) должна меняться в одном месте,
а не в каждом файле.

Живой сервис, камера и база не затрагиваются: `service.*`, `cv2` и
`load_dashboard` подменяются заглушками.
"""
from __future__ import annotations

import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import customtkinter as ctk

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

#: Пустая сводка для раздела «Мониторинг».
EMPTY_DASHBOARD = {'total': 0, 'unique': 0, 'hours': [0] * 24,
                   'directions': {'Въезд': 0, 'Выезд': 0, 'Неизвестно': 0}, 'recent': []}

#: Подменённый сервис: отвечает как живой, но ничего не запускает.
FAKE_SERVICE = MagicMock()
FAKE_SERVICE.URL = 'http://127.0.0.1:18749'
FAKE_SERVICE.PORT = 18749
FAKE_SERVICE.BASE = Path(__file__).resolve().parent.parent

#: Настройки Telegram задаются явно: индикаторы не зависят от боевого `.env`.
FAKE_SETTINGS = {
    'TELEGRAM_BOT_TOKEN': 'example', 'ALLOWED_CHAT_IDS': '42',
    'CAMERA_NAME': 'Fayz kirish',
}


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


def patch_service():
    """Подменить сервис на время модуля тестов."""
    FAKE_SERVICE.reset_mock()
    FAKE_SERVICE.health.return_value = HEALTH
    FAKE_SERVICE.start.return_value = HEALTH
    FAKE_SERVICE.stop.return_value = None
    FAKE_SERVICE.health.side_effect = None
    patcher = patch('app.ui.shell.service', FAKE_SERVICE)
    patcher.start()
    return patcher


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
            # Настройки Telegram задаются явно: индикатор не зависит от боевого `.env`.
            patch('app.ui.settings.read_settings', return_value=dict(FAKE_SETTINGS)),
        ]
        for item in self.patches:
            item.start()
        self.shell = AppShell(self.root)
        self.header = self.shell.header
        self.panel = self.header.details
        # Виджеты должны один раз отрисоваться: иначе синтетические нажатия
        # Tk не доставляются (события уходят в неотображённые окна).
        self.pump(0.3)
        self.root.withdraw()
        FAKE_SERVICE.health.side_effect = None
        FAKE_SERVICE.health.return_value = HEALTH
        self.shell._settings_cache = (0.0, {})
        self.pump(0.5)          # завершается стартовая операция запуска

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
        """Синтетическое нажатие на виджет.

        CustomTkinter переносит обработчики на внутренний холст виджета,
        поэтому событие отправляется именно ему.
        """
        target = getattr(widget, '_canvas', widget)
        target.event_generate('<Button-1>')
        self.root.update()

    def rows_of(self, key):
        """Подписи видимых строк режима `key` контекстной панели."""
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

    def service_offline(self):
        """Сервис не отвечает: индикаторы показывают «неизвестно»."""
        from requests import ConnectionError
        FAKE_SERVICE.health.side_effect = ConnectionError('offline')
        self.pump(0.2)
