"""Единое окно приложения «Эврика».

Панель управления и Мониторинг объединены в один `CTk`-каркас с постоянными
Header, Content и StatusBar и одним `mainloop()`. Разделы создаются один раз
и переключаются через `grid`/`grid_remove` — видеопоток и фоновые задачи при
навигации не пересоздаются.

Класс `AppShell` — преемник прежней `Panel`: имя `Panel` сохранено в
`app/ui/panel.py` как алиас, поэтому `import control_panel`, подмены в
тестах и точки входа работают как раньше.
"""
import queue
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk
import pystray
from PIL import Image

from app.services import control as service
from app.core import preferences
from app.core import window_state
from app.core.paths import ASSETS_DIR

ctk.set_appearance_mode('dark')
ASSETS = ASSETS_DIR

#: Заголовки разделов: ключ → название.
SECTION_TITLES = {'monitor': 'Мониторинг', 'settings': 'Настройки'}


class AppShell:
    """Главное окно приложения."""

    BG = '#101722'; TEXT = '#f2f5fa'; MUTED = '#a5b1c2'; GREEN = '#56d8a0'; RED = '#ff8792'
    CARD = '#1a2432'; BORDER = '#303e51'; BLUE = '#2869df'; BLUE_HOVER = '#1d55b9'

    def __init__(self, root):
        self.root, self.queue, self.busy = root, queue.Queue(), False
        self.running = False
        self.previous = None
        # Журнал проездов больше не отображается в оболочке: список живёт
        # в разделе «Мониторинг». Модель данных сохранена для совместимости.
        self.events = deque(maxlen=100)
        self.sections = {}
        self.section_objects = {}
        self.current_section = None
        self._normal_geometry = None
        self._message_after = None

        self.status = tk.StringVar(value='Проверяю…')
        self.hint = tk.StringVar(value='')
        self.message = tk.StringVar(value='')
        self.camera = (tk.StringVar(value='Проверяю…'), tk.StringVar(value=''))
        self.telegram = (tk.StringVar(value='Проверяю…'), tk.StringVar(value=''))
        self.service_state = tk.StringVar(value='Проверяю…')

        root.title('Эврика')
        root.configure(fg_color=self.BG)
        root.iconbitmap(str(ASSETS / 'evrika.ico'))
        root.protocol('WM_DELETE_WINDOW', self.hide)
        root.bind('<Unmap>', self._on_unmap)
        root.bind('<Configure>', self._remember_geometry)
        root.minsize(*window_state.MIN_SIZE)

        self._build_header()
        self._build_content()
        self._build_statusbar()
        self._apply_window_state()
        self._start_tray()

        # Мониторинг открывается по умолчанию; раздел создаётся один раз.
        self.show_section('monitor')

        root.after(100, self.process)
        self.work(service.start)
        root.after(1000, self.refresh)

    # ------------------------------------------------------------------
    # Каркас окна
    # ------------------------------------------------------------------
    def _build_header(self):
        header = ctk.CTkFrame(self.root, fg_color='transparent')
        header.pack(fill='x', padx=24, pady=(18, 8))
        self.logo = ctk.CTkImage(light_image=Image.open(ASSETS / 'evrika-logo-ru.png'), size=(145, 30))
        ctk.CTkLabel(header, text='', image=self.logo, anchor='w').pack(side='left')
        self.nav_buttons = {}
        for key in ('monitor', 'settings'):
            button = ctk.CTkButton(header, text=SECTION_TITLES[key], width=120, height=34,
                                    fg_color=self.CARD, hover_color='#34455a', border_color=self.BORDER,
                                    border_width=1, font=('Segoe UI', 12),
                                    command=lambda key=key: self.show_section(key))
            button.pack(side='left', padx=(10, 0))
            self.nav_buttons[key] = button
        self.more = ctk.CTkOptionMenu(header, values=['Ещё', 'Перезапустить систему',
                                                      'Запустить или остановить мониторинг', 'Открыть журнал'],
                                      command=self._more, fg_color=self.CARD, button_color=self.CARD,
                                      button_hover_color='#34455a', width=120, height=32)
        self.more.pack(side='right')
        self.state_frame = ctk.CTkFrame(header, fg_color='transparent')
        self.state_frame.pack(side='right', padx=(16, 0))
        self.state_label = ctk.CTkLabel(self.state_frame, textvariable=self.status,
                                         text_color=self.TEXT, font=('Segoe UI', 12, 'bold'))
        self.state_label.pack(side='left')
        self.state_dot = ctk.CTkFrame(self.state_frame, width=10, height=10, corner_radius=5, fg_color=self.MUTED)
        self.state_dot.pack(side='left', padx=(0, 8), pady=(0, 2))
        self.hint_label = ctk.CTkLabel(self.state_frame, textvariable=self.hint,
                                       text_color=self.MUTED, font=('Segoe UI', 11))
        self.hint_label.pack(side='left', padx=(12, 0))
        self.message_label = ctk.CTkLabel(header, textvariable=self.message, text_color='#ff9ca5',
                                          font=('Segoe UI', 11), width=240, anchor='w')
        self.message_label.pack(side='left', padx=(18, 0))

    def _build_content(self):
        self.content = ctk.CTkFrame(self.root, fg_color='transparent')
        self.content.pack(fill='both', expand=True, padx=24, pady=(0, 8))
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)

    def _build_statusbar(self):
        bar = ctk.CTkFrame(self.root, fg_color=self.CARD, corner_radius=10)
        bar.pack(side='bottom', fill='x', padx=24, pady=(0, 16))
        inner = ctk.CTkFrame(bar, fg_color='transparent')
        inner.pack(fill='x', padx=18, pady=10)
        self._connection_labels = {}
        self._connection_labels['camera'] = self._status_group(inner, 'Камера', self.camera)
        self._connection_labels['telegram'] = self._status_group(inner, 'Telegram', self.telegram)
        self._status_group(inner, 'Сервис', (self.service_state, tk.StringVar(value='')))

    def _status_group(self, parent, caption, variables):
        box = ctk.CTkFrame(parent, fg_color='transparent')
        box.pack(side='left', padx=(0, 30))
        ctk.CTkLabel(box, text=caption.upper(), text_color=self.MUTED, font=('Segoe UI', 10)).pack(anchor='w')
        state_label = ctk.CTkLabel(box, textvariable=variables[0], text_color=self.TEXT,
                                   font=('Segoe UI', 12, 'bold'))
        state_label.pack(anchor='w')
        hint_label = ctk.CTkLabel(box, textvariable=variables[1], text_color=self.MUTED, font=('Segoe UI', 11))
        hint_label.pack(anchor='w')
        return state_label, hint_label

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
        for key, button in self.nav_buttons.items():
            active = key == name
            button.configure(fg_color=self.BLUE if active else self.CARD,
                             hover_color=self.BLUE_HOVER if active else '#34455a',
                             text_color='#ffffff' if active else self.TEXT)
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
            section = MonitoringSection(self.content, embedded=True, show_back=False,
                                        service_hook=self.toggle_service)
            self.main_button = section.service_button
            return section
        from app.ui.settings import SettingsSection
        return SettingsSection(self.content, container=self.content)

    def _monitor_section(self):
        """Объект раздела «Мониторинг» (может быть ещё не создан)."""
        return getattr(self, 'section_objects', {}).get('monitor')

    # ------------------------------------------------------------------
    # Состояние системы
    # ------------------------------------------------------------------
    def refresh(self):
        def worker():
            try:
                self.queue.put(('health', service.health()))
            except Exception:
                self.queue.put(('offline', None))
        threading.Thread(target=worker, daemon=True).start()
        self.root.after(3000, self.refresh)

    def update_status(self, data):
        running = bool(data and data.get('status') == 'ok')
        camera_message = data.get('camera_message', '').lower() if data else ''
        telegram_message = data.get('telegram_message', '').lower() if data else ''
        if not running:
            camera_state, telegram_state = 'Неактивна', 'Неактивен'
        else:
            camera_state = ('Подключена' if camera_message.startswith('подключена') else
                            'Нет соединения' if 'прервалась' in camera_message or 'закрыто' in camera_message else 'Подключение…')
            telegram_state = ('Отправлено' if 'доставлено' in telegram_message else
                              'Ошибка отправки' if 'не удалось' in telegram_message else 'Ожидание отправки')
        delivery = data.get('telegram_delivery') if data else None
        if running and delivery is not None:
            # Счётчики пустые — отправки ещё не было: это не ошибка.
            attempted = bool(delivery.get('at')) or bool(delivery.get('delivered')) or bool(delivery.get('failed'))
            if attempted:
                telegram_state = 'Отправлено' if delivery.get('ok') else 'Ошибка отправки'
        delivery_at = delivery.get('at') if delivery else None
        started_at = data.get('started_at') if data else None
        current = (running, camera_state, telegram_state, delivery_at, data.get('pid') if data else None)
        self.previous = current
        self.running = running
        self.status.set('Система активна' if running else 'Система остановлена')
        if running:
            started = datetime.fromtimestamp(started_at).astimezone().strftime('%H:%M') if isinstance(started_at, (int, float)) else None
            self.hint.set('Мониторинг запущен' + (f'  ·  Работает с {started}' if started else ''))
        else:
            self.hint.set('Мониторинг не выполняется')
        main_button = getattr(self, 'main_button', None)
        if main_button is not None:
            main_button.configure(text='Остановить' if running else 'Запустить',
                                  fg_color='#45303a' if running else self.BLUE,
                                  hover_color='#603844' if running else self.BLUE_HOVER, text_color=self.TEXT)
        self.camera[0].set(camera_state)
        self.camera[1].set('Данные поступают' if running and camera_message == 'подключена, получаю данные.' and data.get('last_camera_data') else '')
        delivery_time = self.event_time(delivery_at)
        self.telegram[0].set(telegram_state)
        self.telegram[1].set(f'Последняя отправка: {delivery_time:%H:%M}' if telegram_state == 'Отправлено' and delivery_time else '')
        service_state = getattr(self, 'service_state', None)
        if service_state is not None:
            service_state.set('Работает' if running else 'Остановлен')
        self._state_color(running)
        self._connection_color('camera', camera_state == 'Подключена')
        self._connection_color('telegram', telegram_state == 'Отправлено')

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

    def process(self):
        try:
            while True:
                kind, data = self.queue.get_nowait()
                if kind == 'show': self.show()
                elif kind == 'confirm_exit': self.confirm_stop_and_exit()
                elif kind == 'exit_complete': self._quit(); return
                elif kind == 'exit_error':
                    self.busy = False
                    self._enable_main_button()
                    self.show_message(data)
                    messagebox.showerror('Не удалось остановить систему', data, parent=self.root)
                elif kind == 'stopped': pass
                elif kind == 'message': self.show_message(data); self.busy = False; self._enable_main_button()
                elif kind == 'offline':
                    if not self.busy: self.update_status(None)
                elif kind == 'health':
                    if not self.busy: self.update_status(data)
        except queue.Empty:
            pass
        self.root.after(100, self.process)

    def _enable_main_button(self):
        main_button = getattr(self, 'main_button', None)
        if main_button is not None:
            main_button.configure(state='normal')

    def _state_color(self, running):
        if self.state_dot is not None:
            self.state_dot.configure(fg_color=self.GREEN if running else self.MUTED)

    def _connection_color(self, key, confirmed):
        labels = getattr(self, '_connection_labels', {}).get(key)
        if labels is None:
            return
        labels[0].configure(text_color=self.GREEN if confirmed else self.TEXT)

    def animate_status(self):
        if not self.busy:
            return
        self.animation_phase = (getattr(self, 'animation_phase', 0) + 1) % 4
        self.status.set('Подождите' + '.' * self.animation_phase)
        self.root.after(400, self.animate_status)

    # ------------------------------------------------------------------
    # Управление сервисом
    # ------------------------------------------------------------------
    def toggle_service(self):
        self.work(service.stop if self.running else service.start)

    def work(self, action):
        if self.busy:
            return
        self.busy = True
        self.show_message('')
        main_button = getattr(self, 'main_button', None)
        if main_button is not None:
            main_button.configure(state='disabled')
        self.status.set('Подождите')
        self.hint.set('Меняем состояние системы…')
        self.animate_status()
        stopping = action == service.stop

        def worker():
            try:
                action()
                if stopping: self.queue.put(('stopped', None))
                self.queue.put(('message', ''))
            except Exception as error:
                detail = str(error) if isinstance(error, (RuntimeError, ValueError)) else 'Не удалось выполнить действие.'
                self.queue.put(('message', detail))
        threading.Thread(target=worker, daemon=True).start()

    def restart(self):
        self.work(self._restart)

    def _restart(self):
        service.stop()
        self.queue.put(('stopped', None))
        return service.start()

    def _more(self, choice):
        self.more.set('Ещё')
        if choice == 'Перезапустить систему': self.restart()
        elif choice == 'Запустить или остановить мониторинг': self.toggle_service()
        elif choice == 'Открыть журнал': self.open_log()

    def open_log(self):
        subprocess.Popen(['notepad.exe', str(service.BASE / 'service.stderr.log')])

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
            self.root.after_idle(self.root.withdraw)

    def hide(self):
        """Закрыть окно — свернуть в трей. Видео приостанавливается."""
        self.save_window_state()
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
        main_button = getattr(self, 'main_button', None)
        if main_button is not None:
            main_button.configure(state='disabled')
        self.show_message('')
        self.status.set('Останавливаем систему…')
        self.hint.set('Пожалуйста, подождите.')

        def worker():
            try:
                service.stop()
            except Exception as error:
                detail = str(error) if isinstance(error, (RuntimeError, ValueError)) else 'Не удалось остановить систему.'
                self.queue.put(('exit_error', detail))
            else:
                self.queue.put(('exit_complete', None))
        threading.Thread(target=worker, daemon=True).start()

    def _quit(self):
        self.save_window_state()
        self.tray.stop()
        self._stop_sections()
        self.root.destroy()

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
    # Сообщения
    # ------------------------------------------------------------------
    def show_message(self, text):
        """Короткое сообщение в шапке: длинный текст здесь неуместен."""
        if self._message_after is not None:
            try:
                self.root.after_cancel(self._message_after)
            except Exception:
                pass
            self._message_after = None
        value = (text or '').strip()
        if len(value) > 70:
            value = value[:67] + '…'
        self.message.set(value)
        if value:
            self._message_after = self.root.after(8000, self._clear_message)

    def _clear_message(self):
        self._message_after = None
        self.message.set('')

    # ------------------------------------------------------------------
    # Геометрия окна
    # ------------------------------------------------------------------
    def _virtual_bounds(self):
        return (self.root.winfo_vrootx(), self.root.winfo_vrooty(),
                self.root.winfo_vrootwidth(), self.root.winfo_vrootheight())

    def _remember_geometry(self, _event=None):
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
        width = max(min_width, min(width, screen_width))
        height = max(min_height, min(height, screen_height))
        geometry = window_state.format_geometry(width, height)
        if state and window_state.within_bounds(state, self._virtual_bounds()):
            geometry = window_state.format_geometry(width, height, state.get('x'), state.get('y'))
        try:
            self.root.geometry(geometry)
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
                    root.after(200, check_replacement)
            root.after(200, check_replacement)
            root.mainloop()
    finally:
        instance.close()


if __name__ == '__main__':
    main()
