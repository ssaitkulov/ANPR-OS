"""Общая оснастка тестов интерфейса Эврики.

Один набор заглушек и одно окно приложения на все UI-тесты: правка
заглушки (например, ответа `/health`) должна меняться в одном месте,
а не в каждом файле.

Живой сервис, камера и база не затрагиваются: `service.*`, `cv2` и
`load_dashboard` подменяются заглушками.

Ожидание — по состоянию, а не по часам: `pump_until()` и `settle()` ждут
конкретного условия с ограничением по времени. Фиксированные паузы
(`pump(0.4)`) не нужны там, где состояние меняется синхронно, и были
главным источником времени полного прогона.
"""
from __future__ import annotations

import gc
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import customtkinter as ctk

from app.ui import icons
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

    def open(self, *args, **kwargs):
        return False

    def read(self):
        return False

    def release(self):
        self.released = True


def patch_service():
    """Подменить сервис на время модуля тестов."""
    reset_calls()
    patcher = patch('app.ui.shell.service', FAKE_SERVICE)
    patcher.start()
    return patcher


def reset_calls():
    """Очистить историю вызовов заглушки, сохранив её ответы.

    Каждый тест должен видеть только свои вызовы: без этого счётчики
    заглушки накапливают вызовы соседних тестов (например, запуск
    сервиса при создании окна).

    Сбрасываются сами атрибуты, а не только заглушка: `patch` возвращает
    подменённый атрибут как обычное значение, и после такой подмены
    родительская заглушка его уже не сбрасывает.
    """
    for name, response in (('health', HEALTH), ('start', HEALTH), ('stop', None)):
        attribute = getattr(FAKE_SERVICE, name)
        attribute.reset_mock()
        attribute.side_effect = None
        attribute.return_value = response


class ShellTestCase(unittest.TestCase):
    """Окно приложения с заглушками вместо живого сервиса и видео."""

    #: Развернуть окно хотя бы раз. Нужно там, где тест шлёт нажатия Tk:
    #: у неотображённых виджетов нет окна, и событие не доставляется.
    #: Тестам без нажатий карта не нужна — она стоит ~0,3 с на тест.
    MAP_WINDOW = True

    def setUp(self):
        try:
            self.root = ctk.CTk()
        except Exception as error:  # нет графики — тест пропускается
            self.skipTest(f'Tk недоступен: {error}')
        if not self.MAP_WINDOW:
            self.root.withdraw()
        self.errors = []
        self.root.report_callback_exception = self._report_error
        self.patches = [
            patch.object(AppShell, '_start_tray'),
            # Периодический опрос `/health` выключен: состояние в тестах
            # задаётся явно (`update_status`), а фоновая доставка ответа
            # делала результат зависимым от того, когда попал таймер.
            patch.object(AppShell, 'refresh'),
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
        # Tk не доставляются (у неотображённых виджетов нет окна). После
        # этого окно скрыто — развёрнутое окно перерисовывается на каждый
        # опрос раздела, а это половина времени теста.
        if self.MAP_WINDOW:
            self.root.update()
            self.root.withdraw()
        FAKE_SERVICE.health.side_effect = None
        FAKE_SERVICE.health.return_value = HEALTH
        self.shell._settings_cache = (0.0, {})
        self.wait_idle()        # завершается стартовая операция запуска
        reset_calls()           # счётчики заглушки — только для этого теста

    def tearDown(self):
        # Отложенные вызовы Tk надо дать сработать до закрытия окна —
        # тогда ошибки в них попадут в `self.errors`.
        self.pump(0.05)
        # Снимки иконок освобождаем до уничтожения Tk: иначе их __del__
        # сработает после выхода из главного цикла и даст warning.
        icons._cache.clear()
        try:
            self.shell._quit()
        except Exception:
            pass
        for item in self.patches:
            item.stop()
        self.header = None
        self.panel = None
        self.shell = None
        gc.collect()
        self.assertEqual(self.errors, [])

    # ------------------------------------------------------------------
    def _report_error(self, exc, value, tb):
        self.errors.append((exc, value))

    def pump(self, seconds=0.05):
        """Прокрутить цикл событий Tk заданное время."""
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            try:
                self.root.update()
            except tk.TclError:
                return
            if time.monotonic() >= deadline:
                return
            time.sleep(0.005)

    def pump_until(self, predicate, timeout=2.0):
        """Прокручивать цикл событий, пока условие не станет истинным.

        Возвращает `True`, если условие выполнилось. Ожидание ограничено
        по времени: тест не должен висеть, даже если условие не наступит.
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.root.update()
            except tk.TclError:
                return False
            if predicate():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)

    def wait_idle(self, timeout=3.0):
        """Дождаться завершения фоновой операции управления сервисом."""
        return self.pump_until(lambda: not self.shell.busy, timeout)

    def settle(self, timeout=0.5):
        """Дождаться, пока окно успокоится: операций и анимаций нет."""
        def quiet():
            if self.shell.busy:
                return False
            managers = (getattr(self.header, 'motion', None),
                        getattr(self.panel, 'motion', None))
            return all(manager.pending == 0 for manager in managers if manager is not None)
        return self.pump_until(quiet, timeout)

    def click(self, widget):
        """Синтетическое нажатие на виджет.

        CustomTkinter переносит обработчики на внутренний холст виджета,
        поэтому событие отправляется именно ему. Окно к этому моменту
        должно быть на экране: в неотображённое окно Tk событие не
        доставляет.
        """
        target = getattr(widget, '_canvas', widget)
        target.event_generate('<Button-1>')
        self.root.update()

    def rows_of(self, key):
        """Подписи видимых строк режима `key` контекстной панели."""
        return self.panel.modes[key].rows.captions()

    def set_health(self, data):
        """Задать ответ сервиса и применить его к индикаторам."""
        FAKE_SERVICE.health.return_value = data
        self.shell.update_status(data)

    def service_offline(self):
        """Сервис не отвечает: индикаторы показывают «неизвестно»."""
        from requests import ConnectionError
        FAKE_SERVICE.health.side_effect = ConnectionError('offline')
        self.pump_until(lambda: self.shell.queue.empty(), timeout=1.0)
