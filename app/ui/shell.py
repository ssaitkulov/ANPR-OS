"""Единое окно приложения «Эврика».

Панель управления и Мониторинг объединены в один `CTk`-каркас с постоянными
Header и Content и одним `mainloop()`. Разделы создаются один раз и
переключаются через `grid`/`grid_remove` — видеопоток и фоновые задачи при
навигации не пересоздаются.

Header (`app/ui/header.py`) — постоянная верхняя панель: три компактных
индикатора состояния (сервис, камера, Telegram). Нажатие на индикатор
открывает контекстную панель `app/ui/service_panel.py` с диагностикой
только этого компонента. Управление ANPR-сервисом (запуск, остановка,
перезапуск, журнал) живёт в режиме «сервис» этой панели. Дублирующего
StatusBar и кнопок управления в разделах нет.

Класс `AppShell` — преемник прежней `Panel`: имя `Panel` сохранено в
`app/ui/panel.py` как алиас, поэтому `import control_panel`, подмены в
тестах и точки входа работают как раньше.

Жизненный цикл: все отложенные вызовы Tk планируются через `schedule()`
с учётом их идентификаторов и отменяются при закрытии окна. Это убирает
ошибку `invalid command name … poll` при завершении приложения.
"""
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk
import pystray
import requests
from PIL import Image

from app.services import control as service
from app.services import audit
from app.services import process_info
from app.core import preferences
from app.core import window_state
from app.core.paths import ASSETS_DIR, log_dir
from app.ui import status as ui_status
from app.ui import theme
from app.ui import perf
from app.ui.header import SystemHeader
from app.ui.tklife import cancel_pending_scripts

ctk.set_appearance_mode('dark')
ASSETS = ASSETS_DIR

#: Заголовки разделов: ключ → название.
SECTION_TITLES = {'monitor': 'Мониторинг', 'settings': 'Настройки'}

#: Как часто обновляются данные о состоянии системы, мс.
HEALTH_INTERVAL = 3000

#: Как часто читается файл настроек для проверки готовности Telegram, с.
SETTINGS_TTL = 10.0

#: Что сообщить пользователю после успешной операции управления сервисом.
OPERATION_RESULTS = {
    'start': 'ANPR-сервис запущен.',
    'stop': 'ANPR-сервис остановлен.',
    'restart': 'ANPR-сервис перезапущен.',
}


class AppShell:
    """Главное окно приложения."""

    BG = theme.BG; TEXT = theme.TEXT; MUTED = theme.MUTED
    GREEN = theme.SUCCESS; RED = theme.DANGER
    CARD = theme.CARD; BORDER = theme.CARD_ALT; BLUE = theme.ACCENT; BLUE_HOVER = theme.ACCENT

    def __init__(self, root):
        self.root, self.queue, self.busy = root, queue.Queue(), False
        self.running = False
        self.operation = None      # текущая операция управления: start / stop / restart
        self.phase = None          # фаза перезапуска: stop / start
        self.closing = False       # началось завершение приложения
        self.closed = False        # окно уже уничтожено
        self.previous = None
        # Журнал проездов больше не отображается в оболочке: список живёт
        # в разделе «Мониторинг». Модель данных сохранена для совместимости.
        self.events = deque(maxlen=100)
        self.sections = {}
        self.section_objects = {}
        self.current_section = None
        self._normal_geometry = None
        self._geometry_job = None
        self._after_ids = set()
        self._health = None
        self._health_inflight = threading.Event()
        self._health_error = ''
        self._last_check = None
        self._last_error = ''
        self._states = {}
        self._settings_cache = (0.0, {})

        self.message = tk.StringVar(value='')
        self.message_tone = ''
        self._silent_operation = False

        root.title('Эврика')
        root.configure(fg_color=self.BG)
        root.iconbitmap(str(ASSETS / 'evrika.ico'))
        root.protocol('WM_DELETE_WINDOW', self.hide)
        root.bind('<Unmap>', self._on_unmap)
        root.bind('<Configure>', self._remember_geometry)
        perf.heartbeat(root)
        root.minsize(*window_state.MIN_SIZE)

        self.header = SystemHeader(root, root, self)
        self.nav_buttons = self.header.nav_buttons
        self._build_content()
        self._apply_window_state()
        self._start_tray()

        # Мониторинг открывается по умолчанию; раздел создаётся один раз.
        self.show_section('monitor')

        self.schedule(100, self.process)
        self.work(service.start, 'start', silent=True)
        self.schedule(1000, self.refresh)

    # ------------------------------------------------------------------
    # Каркас окна
    # ------------------------------------------------------------------
    def _build_content(self):
        self.content = ctk.CTkFrame(self.root, fg_color='transparent')
        self.content.pack(fill='both', expand=True, padx=24, pady=(0, 16))
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)

    def logo_image(self):
        """Логотип ЭВРИКА для верхней панели."""
        return Image.open(ASSETS / 'evrika-logo-ru.png')

    @property
    def section_titles(self):
        return SECTION_TITLES

    # ------------------------------------------------------------------
    # Разделы
    # ------------------------------------------------------------------
    def show_section(self, name):
        """Показать раздел. Раздел создаётся один раз и переиспользуется."""
        section = self._ensure_section(name)
        for widget in self.sections.values():
            if widget is not section:
                widget.grid_remove()
        section.grid(row=0, column=0, sticky='nsew')
        self.current_section = name
        header = getattr(self, 'header', None)
        if header is not None:
            header.close_details()
        for key, button in self.nav_buttons.items():
            active = key == name
            # Активную вкладку красит подложка сегментированного
            # переключателя, поэтому сюда передаётся только признак
            # активности.
            button.configure(fg_color=theme.ACCENT if active else 'transparent')
        self.root.title('Эврика · ' + SECTION_TITLES.get(name, 'Эврика'))

    def _ensure_section(self, name):
        """Создать раздел при первом обращении и закешировать его."""
        if name in self.sections:
            return self.sections[name]
        section = self._create_section(name)
        self.sections[name] = section.root if hasattr(section, 'root') else section
        self.section_objects[name] = section
        return self.sections[name]

    def _create_section(self, name):
        """Создать новый раздел. Точка подмены в тестах."""
        if name == 'monitor':
            from app.ui.dashboard import MonitoringSection
            return MonitoringSection(self.content, embedded=True, show_back=False)
        from app.ui.settings import SettingsSection
        return SettingsSection(self.content, container=self.content)

    def _monitor_section(self):
        """Объект раздела «Мониторинг» (может быть ещё не создан)."""
        return getattr(self, 'section_objects', {}).get('monitor')

    # ------------------------------------------------------------------
    # Состояние системы
    # ------------------------------------------------------------------
    def _start_health_check(self):
        """Запустить опрос, только если предыдущий ещё не закончился."""
        if self.closing or self._health_inflight.is_set():
            return False
        self._health_inflight.set()
        threading.Thread(target=self._health_worker, daemon=True).start()
        return True

    def refresh(self):
        """Периодическая проверка состояния сервиса (в фоновом потоке)."""
        if self.closing:
            return
        self._start_health_check()
        self.schedule(HEALTH_INTERVAL, self.refresh)

    def refresh_now(self):
        """Проверить состояние немедленно, не дожидаясь очередного цикла."""
        self._start_health_check()

    def _health_worker(self):
        try:
            data = service.health()
        except requests.ConnectionError:
            self.queue.put(('offline', None))
        except Exception as error:
            detail = str(error) if isinstance(error, (RuntimeError, ValueError)) else 'Не удалось проверить состояние сервиса.'
            self.queue.put(('health_error', detail))
        else:
            self.queue.put(('health', data))
        finally:
            self._health_inflight.clear()

    def update_status(self, data=None, error=''):
        """Обновить индикаторы состояния по данным `/health`."""
        self._health = data
        if data is not None:
            self._last_check = datetime.now()
            self._health_error = ''
        elif error:
            self._health_error = error
        running = bool(data and data.get('status') == 'ok')
        self.running = running
        self.previous = (running, data.get('pid') if data else None)
        service_state = ui_status.service_view(data, self.busy, self.operation, self.phase,
                                                self._health_error)
        camera_state = ui_status.camera_view(data, running)
        telegram_state = ui_status.telegram_view(data, running, self._telegram_configured())
        self._states = {'service': service_state, 'camera': camera_state, 'telegram': telegram_state}
        header = getattr(self, 'header', None)
        if header is not None:
            header.set_state('service', service_state)
            header.set_state('camera', camera_state)
            header.set_state('telegram', telegram_state)
            if header.details.is_open:
                header.refresh_panel()

    def _settings(self):
        """Настройки из `.env` с коротким кэшем (чтение файла не на каждый кадр)."""
        stamp = time.monotonic()
        cached_stamp, cached = self._settings_cache
        if stamp - cached_stamp > SETTINGS_TTL:
            try:
                from app.ui.settings import read_settings
                cached = read_settings()
            except OSError:
                cached = {}
            self._settings_cache = (stamp, cached)
        return cached

    def _telegram_configured(self):
        """Настроен ли Telegram (токен бота и разрешённые чаты в .env)."""
        return ui_status.telegram_configured(self._settings())

    # ------------------------------------------------------------------
    # Данные панели управления
    # ------------------------------------------------------------------
    def panel_data(self):
        """Данные панели управления: три независимых раздела.

        Каждый раздел собирается только из своих источников: состояние
        сервиса не зависит от камеры, состояние камеры — от Telegram.
        Показатели, которых нет, в панель не попадают.
        """
        data = self._health or {}
        running = self.running
        states = self._states or {}
        settings = self._settings()

        service_state = states.get('service') or ui_status.service_view()
        camera_state = states.get('camera') or ui_status.camera_view(running=running)
        telegram_state = states.get('telegram') or ui_status.telegram_view(running=running)

        # --- ANPR-сервис ---
        pid = data.get('pid')
        started = data.get('started_at')
        service_rows = []
        if pid:
            service_rows.append(('PID', str(pid)))
        if isinstance(started, (int, float)) and started:
            uptime = ui_status.format_duration(time.time() - started)
            if uptime:
                service_rows.append(('Время работы', uptime))
        service_rows.append(('Порт сервиса', str(service.PORT)))
        service_rows.append(('Последняя проверка',
                              self._last_check.strftime('%H:%M:%S') if self._last_check else 'Нет данных'))
        service_rows.append(('Последняя ошибка', self._last_error or 'Нет ошибок'))
        panel = getattr(getattr(self, 'header', None), 'details', None)
        panel_open = bool(panel and panel.is_open)
        process_alive = self._service_process_alive() if not running and panel_open else True

        # --- камера ---
        camera_rows = []
        name = str(settings.get('CAMERA_NAME') or '').strip()
        if name:
            camera_rows.append(('Название', name))
        host = str(data.get('camera') or settings.get('DAHUA_HOST') or '').strip()
        if host:
            camera_rows.append(('IP-адрес', host))
        # Приём ANPR-событий — отдельный диагноз от работоспособности
        # RTSP: камера может быть подключена, а события не приходят.
        events = ui_status.camera_events_view(data, running)
        camera_rows.append(('События ANPR', events['label']))
        last_data = str(data.get('last_camera_data') or '').strip()
        if last_data and camera_state.get('code') != ui_status.CAMERA_CONNECTED:
            # Камера недоступна — её последние данные верны только
            # исторически, и это обязательно отмечается.
            last_data = ui_status.mark_stale(last_data)
        camera_rows.append(('Последнее событие', last_data or 'Нет данных'))

        # --- Telegram ---
        configured = self._telegram_configured()
        connection = ui_status.telegram_connection_view(data, running, configured)
        delivery = ui_status.telegram_delivery_view(data)
        link = data.get('telegram_connection') or {}
        telegram_rows = [
            ('Соединение', connection['label']),
            ('Последняя связь', ui_status.format_time(link.get('at')) or 'Нет данных'),
            ('Последняя доставка', delivery['label']),
        ]
        if link.get('error'):
            telegram_rows.append(('Ошибка связи', str(link['error'])))

        return {
            'service': {'state': service_state, 'rows': service_rows,
                        'running': running, 'process_alive': process_alive},
            'camera': {'state': camera_state, 'rows': camera_rows},
            'telegram': {'state': telegram_state, 'rows': telegram_rows},
            'busy': self.busy,
            'operation': self.operation,
            'phase': self.phase,
            'message': self.message.get(),
            'message_tone': self.message_tone,
        }

    def details_data(self, key):
        """Строки панели подробностей для выбранного индикатора (совместимость)."""
        data = self.panel_data()
        block = data.get(key) or {}
        state = block.get('state') or {}
        rows = [('Состояние', state.get('label', '—'))]
        rows.extend(block.get('rows') or [])
        if state.get('detail'):
            rows.append(('Пояснение', state['detail']))
        title = {'service': 'СЕРВИС', 'camera': 'КАМЕРА', 'telegram': 'TELEGRAM'}.get(key, '')
        return title, rows

    def _known_service_pid(self):
        """PID сервиса: из ответа `/health` или из PID-файла."""
        pid = (self._health or {}).get('pid')
        if pid:
            try:
                return int(pid)
            except (TypeError, ValueError):
                pass
        try:
            path = service.pid_file()
            if path.exists():
                return int(path.read_text(encoding='ascii').strip())
        except (OSError, ValueError):
            return None
        return None

    def _service_process_alive(self):
        """Жив ли процесс сервиса на самом деле.

        Проверка нужна в состоянии «Ошибка» и «Остановлен»: сервис может
        не отвечать, но процесс продолжать работать. Предлагать «Запустить»
        вслепую нельзя — порт может быть занят.
        """
        pid = self._known_service_pid()
        if not pid:
            return False
        try:
            if not process_info.pid_alive(pid):
                return False
            return process_info.looks_like_service(pid)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Журнал проездов в оболочке
    # ------------------------------------------------------------------
    def merge_events(self, data):
        """Список проездов (данные). Отображение — в разделе «Мониторинг»."""
        lines = []
        for passage in data.get('passages', []):
            stamp = self.event_time(passage.get('captured_at'))
            if stamp is None:
                continue
            plate = str(passage.get('plate') or 'Номер не распознан')
            if plate == '—':
                plate = 'Номер не распознан'
            details = [f'{stamp:%d.%m %H:%M:%S}', plate]
            direction = passage.get('direction')
            if direction and direction != 'не определено': details.append(direction)
            lane = str(passage.get('lane') or '').strip()
            if lane: details.append(f'Полоса {lane}')
            speed = str(passage.get('speed') or '').strip()
            if speed and speed.lower() not in ('unknown', 'none'): details.append(f'{speed} км/ч')
            if passage.get('backfill'): details.append('Архив')
            lines.append(' · '.join(details))
        if list(self.events) != lines:
            self.events = deque(lines, maxlen=100)
            self.render_events()

    def render_events(self):
        """Журнал проездов в оболочке не отображается — список в «Мониторинге»."""

    @staticmethod
    def event_time(value):
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Очередь фоновых событий
    # ------------------------------------------------------------------
    def process(self):
        try:
            while True:
                kind, data = self.queue.get_nowait()
                if kind == 'show': self.show()
                elif kind == 'confirm_exit': self.confirm_stop_and_exit()
                elif kind == 'exit_complete': self._quit(); return
                elif kind == 'exit_error':
                    self.busy = False
                    self.operation = None
                    self._set_busy(False)
                    self.show_message(data)
                    messagebox.showerror('Не удалось остановить систему', data, parent=self.root)
                elif kind == 'operation_done': self._finish_operation(data)
                elif kind == 'phase': self.phase = data
                elif kind == 'message': self.show_message(data)
                elif kind == 'offline':
                    if not self.busy: self.update_status(None)
                elif kind == 'health':
                    if not self.busy: self.update_status(data)
                elif kind == 'health_error':
                    if not self.busy: self.update_status(None, error=data)
        except queue.Empty:
            pass
        self.schedule(100, self.process)

    def show_message(self, text, tone=''):
        """Результат операции управления сервисом.

        Сообщение показывается в панели управления: в шапке нет места для
        текста, а ошибку операции нужно увидеть сразу — панель открывается.

        `tone` — оформление сообщения: `error` (ошибка операции), `ok`
        (операция выполнена) или пустая строка (обычное уведомление).

        Панель управления открывается сама только при ошибке: результат
        успешной операции пользователь и так видит в открытой панели.
        """
        value = (text or '').strip()
        if len(value) > 140:
            value = value[:137] + '…'
        self.message.set(value)
        self.message_tone = tone or ''
        header = getattr(self, 'header', None)
        if header is not None:
            header.sync_message()
            if value and self.message_tone == 'error' \
                    and header.details is not None and not header.details.is_open:
                header.details.open(section='service')

    # ------------------------------------------------------------------
    # Управление сервисом
    # ------------------------------------------------------------------
    def toggle_service(self):
        """Запустить или остановить ANPR-сервис (переключатель)."""
        if self.busy or self.closing:
            return
        if self.running:
            self.stop_service()
        else:
            self.start_service()

    def start_service(self):
        """Запустить ANPR-сервис (без подтверждения: сервис ничего не теряет)."""
        if self.busy or self.closing:
            return
        audit.panel('service.start.requested', 'requested', source='panel',
                    confirmation=False)
        self.work(lambda: service.start(source='panel'), 'start')

    def stop_service(self):
        """Остановить ANPR-сервис (с подтверждением пользователя)."""
        if self.busy or self.closing:
            return
        if not self._confirm('Остановить ANPR-сервис?',
                            'Распознавание номеров и обработка новых событий '
                            'будут приостановлены до повторного запуска.',
                            action='service.stop'):
            return
        self.work(lambda: service.stop(source='panel'), 'stop')

    def restart_service(self):
        """Перезапустить ANPR-сервис (с подтверждением пользователя)."""
        if self.busy or self.closing:
            return
        if not self._confirm('Перезапустить ANPR-сервис?',
                            'Во время перезапуска обработка событий может быть '
                            'временно недоступна.',
                            action='service.restart'):
            return
        self.work(self._restart_sequence, 'restart')

    def _confirm(self, title, text, action: str = ''):
        """Запрос подтверждения пользователя.

        Ответ пользователя — в журнал панели: по нему видно, был ли запрос
        подтверждения и чем закончился.
        """
        try:
            answer = bool(messagebox.askyesno(title, text, parent=self.root, default=messagebox.NO))
        except tk.TclError:
            answer = False
        if action:
            audit.panel(f'{action}.confirm', 'requested', source='panel',
                        title=title, confirmed=answer)
        return answer

    def _restart_sequence(self):
        """Остановить и снова запустить сервис (в фоновом потоке)."""
        audit.panel('service.restart.requested', 'requested', source='panel')
        self.queue.put(('phase', 'stop'))
        service.stop(source='panel')
        self.queue.put(('phase', 'start'))
        return service.start(source='panel')

    def work(self, action, operation='start', silent=False):
        """Выполнить операцию управления сервисом в фоновом потоке.

        `silent` — не показывать результат операции (автоматический запуск
        при старте приложения: сообщение пользователю ничего не даёт).
        """
        if self.busy or self.closing:
            return
        self.busy = True
        self.operation = operation
        self.phase = None
        self._last_error = ''
        self._silent_operation = silent
        self.show_message('')
        self._set_busy(True)

        def worker():
            try:
                action()
            except Exception as error:
                detail = str(error) if isinstance(error, (RuntimeError, ValueError)) else 'Не удалось выполнить действие.'
                audit.panel(f'{operation}.finished', 'error', source='panel', error=detail)
                self.queue.put(('operation_done', {'ok': False, 'error': detail}))
            else:
                audit.panel(f'{operation}.finished', 'ok', source='panel')
                self.queue.put(('operation_done', {'ok': True, 'error': ''}))
        threading.Thread(target=worker, daemon=True).start()

    def _finish_operation(self, result):
        """Операция завершена: снимаем блокировку и обновляем индикаторы."""
        operation_name = self.operation
        self.busy = False
        self.operation = None
        self.phase = None
        self._set_busy(False)
        error = (result or {}).get('error') or ''
        if error:
            self._last_error = error
            self.show_message(error, tone='error')
        elif getattr(self, '_silent_operation', False):
            self.show_message('')
        else:
            self.show_message(OPERATION_RESULTS.get(operation_name, ''), tone='ok')
        self.refresh_now()

    def _set_busy(self, busy):
        """Занятость панели управления.

        Пока идёт операция, состояние сервиса показывается сразу («Запускается»,
        «Останавливается», «Перезапускается») — ждать следующего опроса
        не нужно.
        """
        header = getattr(self, 'header', None)
        if busy:
            state = ui_status.service_view(self._health, True, self.operation, self.phase)
            self._states['service'] = state
            if header is not None:
                header.set_state('service', state)
        if header is not None:
            header.set_busy(busy)

    # ------------------------------------------------------------------
    # Журнал сервиса
    # ------------------------------------------------------------------
    def open_log(self):
        """Открыть системный журнал сервиса."""
        subprocess.Popen(['notepad.exe', str(log_dir() / 'service.stderr.log')])

    # ------------------------------------------------------------------
    # Окно, трей, выход
    # ------------------------------------------------------------------
    def _start_tray(self):
        image = Image.open(ASSETS / 'evrika-tray.png').convert('RGBA')
        self.tray = pystray.Icon('Evrika', image, 'Эврика — ANPR мониторинг', pystray.Menu(
            pystray.MenuItem('Открыть', lambda icon, item: self.queue.put(('show', None)), default=True),
            pystray.MenuItem('Остановить и выйти', lambda icon, item: self.queue.put(('confirm_exit', None)))))
        self.tray.run_detached()

    def _on_unmap(self, event):
        if event.widget is self.root and self.root.state() == 'iconic' and preferences.get_minimize_to_tray():
            self.schedule_idle(self.root.withdraw)

    def hide(self):
        """Свернуть окно в трей. Видео приостанавливается, сервис работает."""
        self.save_window_state()
        header = getattr(self, 'header', None)
        if header is not None:
            header.close_details()
        self.root.withdraw()
        self._set_video_paused(True)

    def show(self):
        """Вернуть окно из трея. Видео возобновляется, если его не отключил пользователь."""
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self._set_video_paused(False)

    def _set_video_paused(self, paused):
        section = self._monitor_section()
        if section is None:
            return
        try:
            section.pause_video() if paused else section.resume_video()
        except Exception:
            pass

    def confirm_stop_and_exit(self):
        """Остановить сервис и закрыть приложение (пункт трея)."""
        self.root.deiconify()
        self.root.lift()
        if self.busy:
            messagebox.showinfo('Эврика', 'Дождитесь завершения текущей операции.', parent=self.root)
            return
        if not messagebox.askyesno('Выход из Эврики',
                                   'Остановить мониторинг и выйти из программы?',
                                   parent=self.root, default=messagebox.NO):
            return
        self.busy = True
        self.operation = 'stop'
        self._set_busy(True)
        self.show_message('')

        def worker():
            try:
                service.stop(source='panel')
            except Exception as error:
                detail = str(error) if isinstance(error, (RuntimeError, ValueError)) else 'Не удалось остановить систему.'
                audit.panel('panel.exit', 'error', source='panel', error=detail)
                self.queue.put(('exit_error', detail))
            else:
                self.queue.put(('exit_complete', None))
        threading.Thread(target=worker, daemon=True).start()

    def _quit(self):
        """Закрыть приложение. Повторный вызов безопасен."""
        if self.closed:
            return
        self.closed = True
        self.closing = True
        audit.panel('panel.exit', 'ok', source='panel')
        self.save_window_state()
        self._cancel_all()
        try:
            self.tray.stop()
        except Exception:
            pass
        self._stop_sections()
        header = getattr(self, 'header', None)
        if header is not None:
            try:
                header.shutdown()
            except Exception:
                pass
        # Отложенные вызовы Tk (в том числе внутренние CustomTkinter)
        # отменяются до уничтожения виджетов — иначе в stderr появляется
        # «invalid command name …».
        cancel_pending_scripts(self.root)
        try:
            self.root.destroy()
        except Exception:
            pass
        # Уничтожение виджетов вызывает события Configure: на них интерфейс
        # мог запланировать новые вызовы. Их тоже отменяем — интерпретатор
        # Tk при этом ещё жив.
        cancel_pending_scripts(self.root)

    def _stop_sections(self):
        for section in getattr(self, 'section_objects', {}).values():
            shutdown = getattr(section, 'shutdown', None)
            if shutdown is None:
                continue
            try:
                shutdown()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Отложенные вызовы Tk
    # ------------------------------------------------------------------
    def schedule(self, delay, callback):
        """Запланировать отложенный вызов Tk с учётом его идентификатора.

        Идентификатор запоминается, чтобы при закрытии окна все отложенные
        вызовы были отменены до уничтожения виджетов. Повторное планирование
        после начала закрытия запрещено.
        """
        if self.closing:
            return None
        callback = perf.timed(f'shell.schedule:{getattr(callback, "__qualname__", "?")}', callback)
        holder = {}

        def run():
            identifier = holder.get('id')
            if identifier is not None:
                self._after_ids.discard(identifier)
            if self.closing:
                return
            callback()
        try:
            identifier = self.root.after(delay, run)
        except tk.TclError:
            return None
        holder['id'] = identifier
        self._after_ids.add(identifier)
        return identifier

    def schedule_idle(self, callback):
        """То же, что `schedule`, но вызов сразу после обработки событий."""
        if self.closing:
            return None
        callback = perf.timed(f'shell.idle:{getattr(callback, "__qualname__", "?")}', callback)
        holder = {}

        def run():
            identifier = holder.get('id')
            if identifier is not None:
                self._after_ids.discard(identifier)
            if self.closing:
                return
            callback()
        try:
            identifier = self.root.after_idle(run)
        except tk.TclError:
            return None
        holder['id'] = identifier
        self._after_ids.add(identifier)
        return identifier

    def cancel_after(self, identifier):
        """Отменить один отложенный вызов."""
        if identifier is None:
            return
        self._after_ids.discard(identifier)
        try:
            self.root.after_cancel(identifier)
        except Exception:
            pass

    def _cancel_all(self):
        """Отменить все отложенные вызовы окна."""
        for identifier in list(self._after_ids):
            try:
                self.root.after_cancel(identifier)
            except Exception:
                pass
        self._after_ids.clear()

    # ------------------------------------------------------------------
    # Геометрия окна
    # ------------------------------------------------------------------
    def _virtual_bounds(self):
        return (self.root.winfo_vrootx(), self.root.winfo_vrooty(),
                self.root.winfo_vrootwidth(), self.root.winfo_vrootheight())

    def _remember_geometry(self, event=None):
        """Запомнить геометрию один раз после пакета Configure."""
        if event is not None and event.widget is not self.root:
            return
        self.cancel_after(self._geometry_job)
        self._geometry_job = self.schedule_idle(self._save_normal_geometry)

    def _save_normal_geometry(self):
        try:
            if self.root.state() == 'normal':
                self._normal_geometry = self.root.geometry()
        except tk.TclError:
            pass

    def _apply_window_state(self):
        """Восстановить размер, положение и состояние максимизации из прошлого запуска."""
        state = window_state.load_state()
        min_width, min_height = window_state.MIN_SIZE
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width, height = window_state.DEFAULT_SIZE
        if state:
            try:
                width = int(state.get('width') or width)
                height = int(state.get('height') or height)
            except (TypeError, ValueError):
                width, height = window_state.DEFAULT_SIZE
        # Окно должно помещаться на экране целиком: рамка окна съедает
        # несколько пикселей, поэтому оставляем небольшой запас.
        margin, frame = 40, 16
        width = max(min_width, min(width, screen_width - margin))
        height = max(min_height, min(height, screen_height - margin))
        x = max(0, (screen_width - width - frame) // 2)
        y = max(0, (screen_height - height - frame) // 2)
        if state and window_state.within_bounds(state, self._virtual_bounds()):
            # Сохранённое положение тоже удерживаем в пределах экрана.
            origin_x, origin_y, virtual_width, virtual_height = self._virtual_bounds()
            x = max(origin_x, min(int(state['x']), origin_x + virtual_width - width - frame))
            y = max(origin_y, min(int(state['y']), origin_y + virtual_height - height - frame))
        try:
            self.root.geometry(window_state.format_geometry(width, height, x, y))
            if state and state.get('maximized'):
                self.root.state('zoomed')
            self._normal_geometry = window_state.format_geometry(width, height)
        except tk.TclError:
            pass

    def save_window_state(self):
        """Запомнить размер, положение и состояние окна для следующего запуска."""
        try:
            maximized = self.root.state() == 'zoomed'
            # У максимизированного окна обычная геометрия хранится отдельно.
            text = self._normal_geometry if maximized else self.root.geometry()
            if not text:
                text = self.root.geometry()
            state = window_state.state_from_geometry(text, maximized)
            if state:
                window_state.save_state(state)
        except Exception:
            pass


def main() -> None:
    """Запуск единого окна приложения."""
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Evrika.ANPR.ControlPanel')
    from app.services.panel_lock import PanelInstance
    instance = PanelInstance()
    try:
        try:
            instance.acquire()
        except RuntimeError as error:
            messagebox.showerror('Эврика', str(error))
        else:
            root = ctk.CTk()
            shell = AppShell(root)

            def check_replacement():
                if instance.replacement_requested():
                    shell._quit()
                else:
                    shell.schedule(200, check_replacement)
            shell.schedule(200, check_replacement)
            root.mainloop()
    finally:
        instance.close()


if __name__ == '__main__':
    main()
