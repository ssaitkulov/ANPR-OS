"""Windows control panel for the ANPR collector."""
import queue
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime
import tkinter as tk
from tkinter import messagebox
from pathlib import Path
import customtkinter as ctk
import pystray
import requests
from PIL import Image
import service_control as service
import app_preferences as preferences

ctk.set_appearance_mode('dark')
ASSETS = Path(__file__).resolve().parent / 'assets'

class Panel:
    BG = '#101722'; TEXT = '#f2f5fa'; MUTED = '#a5b1c2'; GREEN = '#56d8a0'; RED = '#ff8792'
    CARD = '#1a2432'; BORDER = '#303e51'; BLUE = '#2869df'; BLUE_HOVER = '#1d55b9'

    def __init__(self, root):
        self.root, self.queue, self.busy = root, queue.Queue(), False
        self.running = False
        self.animation_phase = 0
        self.events = deque(maxlen=100)
        self.previous = None
        self.events_expanded = False
        self.event_seen = set()
        self.event_fetching = False
        root.title('Evrika · Панель управления'); root.geometry('680x640'); root.minsize(570, 580)
        root.configure(fg_color=self.BG)
        root.iconbitmap(str(ASSETS / 'evrika.ico'))
        root.protocol('WM_DELETE_WINDOW', self.hide)
        root.bind('<Unmap>', self._on_unmap)
        page = ctk.CTkFrame(root, fg_color='transparent')
        self.page = page
        page.pack(fill='both', expand=True, padx=24, pady=18)

        header = ctk.CTkFrame(page, fg_color='transparent')
        header.pack(fill='x', pady=(0, 16))
        ctk.CTkButton(header, text='Настройки', command=self.settings, fg_color=self.CARD,
                      hover_color='#34455a', border_color=self.BORDER, border_width=1,
                      height=34, width=110).pack(side='right')
        ctk.CTkButton(header, text='Мониторинг', command=self.open_monitor, fg_color=self.BLUE,
                      hover_color=self.BLUE_HOVER, height=34, width=120).pack(side='right', padx=(0, 8))
        brand = ctk.CTkFrame(header, fg_color='transparent'); brand.pack(side='left', fill='x', expand=True)
        self.logo = ctk.CTkImage(light_image=Image.open(ASSETS / 'evrika-logo-ru.png'), size=(145, 30))
        ctk.CTkLabel(brand, text='', image=self.logo, anchor='w').pack(fill='x')
        ctk.CTkLabel(brand, text='Панель управления', text_color=self.MUTED, font=('Segoe UI', 12), anchor='w').pack(fill='x')

        self.state_card = ctk.CTkFrame(page, fg_color='#202735', border_color=self.BORDER, border_width=1, corner_radius=12)
        self.state_card.pack(fill='x')
        state_inner = ctk.CTkFrame(self.state_card, fg_color='transparent')
        state_inner.pack(fill='x', padx=18, pady=15)
        self.main_button = ctk.CTkButton(state_inner, text='Запустить', command=self.toggle,
                                         fg_color=self.BLUE, hover_color=self.BLUE_HOVER,
                                         height=40, width=135, font=('Segoe UI', 12, 'bold'))
        self.main_button.pack(side='right', padx=(12, 0))
        state_text = ctk.CTkFrame(state_inner, fg_color='transparent')
        state_text.pack(side='left', fill='both', expand=True)
        self.status = tk.StringVar(value='Проверяю…'); self.hint = tk.StringVar(value='')
        ctk.CTkLabel(state_text, text='МОНИТОРИНГ', text_color=self.MUTED, font=('Segoe UI', 10, 'bold'), anchor='w').pack(fill='x')
        ctk.CTkLabel(state_text, textvariable=self.status, text_color=self.TEXT, font=('Segoe UI', 18, 'bold'), anchor='w').pack(fill='x')
        ctk.CTkLabel(state_text, textvariable=self.hint, text_color=self.MUTED, font=('Segoe UI', 11), anchor='w', justify='left', wraplength=330).pack(fill='x', pady=(2, 0))

        ctk.CTkLabel(page, text='Подключения', text_color=self.TEXT, font=('Segoe UI', 13, 'bold'), anchor='w').pack(fill='x', pady=(16, 6))
        connections = ctk.CTkFrame(page, fg_color='transparent'); connections.pack(fill='x')
        self._status_widgets = {}
        self.camera = self._connection(connections, 'camera', 'Камера', 0)
        self.telegram = self._connection(connections, 'telegram', 'Telegram', 1)
        connections.columnconfigure((0, 1), weight=1, uniform='connections')

        journal = ctk.CTkFrame(page, fg_color=self.CARD, border_color=self.BORDER, border_width=1, corner_radius=12)
        journal.pack(fill='both', expand=True, pady=(14, 0))
        journal_header = ctk.CTkFrame(journal, fg_color='transparent')
        journal_header.pack(fill='x', padx=14, pady=(10, 0))
        ctk.CTkLabel(journal_header, text='Последние проезды', text_color=self.TEXT, font=('Segoe UI', 13, 'bold'), anchor='w').pack(side='left', fill='x', expand=True)
        self.events_button = ctk.CTkButton(journal_header, text='Показать все', command=self.toggle_events,
                                           fg_color='transparent', hover_color='#34455a', text_color='#86b4ff',
                                           width=110, height=28, font=('Segoe UI', 11))
        self.events_button.pack(side='right')
        self.events_label = ctk.CTkTextbox(journal, text_color=self.MUTED, fg_color='transparent',
                                          font=('Segoe UI', 12), height=65, wrap='word', activate_scrollbars=True)
        self.events_label.pack(fill='both', expand=True, padx=9, pady=(0, 8))
        self.render_events()

        footer = ctk.CTkFrame(page, fg_color='transparent'); footer.pack(fill='x', pady=(10, 0))
        self.more = ctk.CTkOptionMenu(footer, values=['Ещё', 'Перезапустить систему', 'Открыть журнал'], command=self._more,
                                      fg_color=self.CARD, button_color=self.CARD, button_hover_color='#34455a', width=100, height=32)
        self.more.pack(side='right')
        ctk.CTkLabel(footer, text='Работает в фоне при закрытии окна', text_color=self.MUTED,
                     anchor='w', font=('Segoe UI', 11)).pack(side='left', fill='x', expand=True)
        self.message = tk.StringVar(value='')
        ctk.CTkLabel(page, textvariable=self.message, text_color='#ff9ca5', anchor='w', wraplength=500, font=('Segoe UI', 11)).pack(fill='x')
        self._start_tray()
        root.after(100, self.process); self.work(service.start); root.after(1000, self.refresh); root.after(1200, self.refresh_events)

    def _connection(self, parent, key, title, column):
        box = ctk.CTkFrame(parent, fg_color=self.CARD, border_color=self.BORDER, border_width=1, corner_radius=10)
        box.grid(row=0, column=column, sticky='nsew', padx=(0, 6) if column == 0 else (6, 0))
        var, hint = tk.StringVar(value='Проверяю…'), tk.StringVar(value='')
        ctk.CTkLabel(box, text=title, text_color=self.MUTED, font=('Segoe UI', 12, 'bold'), anchor='w').pack(fill='x', padx=14, pady=(12, 4))
        indicator = ctk.CTkLabel(box, textvariable=var, text_color=self.TEXT, font=('Segoe UI', 13, 'bold'), anchor='w')
        indicator.pack(fill='x', padx=14)
        ctk.CTkLabel(box, textvariable=hint, text_color=self.MUTED, font=('Segoe UI', 11), anchor='w').pack(fill='x', padx=14, pady=(0, 10))
        self._status_widgets[key] = (box, indicator)
        return var, hint

    def _more(self, choice):
        self.more.set('Ещё')
        if choice == 'Перезапустить систему': self.restart()
        elif choice == 'Открыть журнал': self.open_log()

    def _start_tray(self):
        image = Image.open(ASSETS / 'evrika-tray.png').convert('RGBA')
        self.tray = pystray.Icon('Evrika', image, 'Evrika', pystray.Menu(
            pystray.MenuItem('Открыть', lambda icon, item: self.queue.put(('show', None)), default=True),
            pystray.MenuItem('Остановить и выйти', lambda icon, item: self.queue.put(('confirm_exit', None)))))
        self.tray.run_detached()

    def _on_unmap(self, event):
        if event.widget is self.root and self.root.state() == 'iconic' and preferences.get_minimize_to_tray():
            self.root.after_idle(self.root.withdraw)

    def hide(self):
        self.root.withdraw()

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
        self.main_button.configure(state='disabled')
        self.message.set('')
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
        self.tray.stop()
        self.root.destroy()

    def open_monitor(self):
        if getattr(self, 'monitor', None) and self.monitor.root.winfo_exists():
            return
        from dashboard import MonitorWindow
        self.root.update_idletasks()
        self.panel_geometry = self.root.geometry()
        self.page.pack_forget()
        self.root.minsize(900, 620)
        self.root.geometry('1180x760')
        self.root.title('Эврика · Мониторинг')
        try:
            self.monitor = MonitorWindow(self.root, on_close=self.close_monitor)
        except Exception:
            self.close_monitor()
            raise

    def close_monitor(self):
        self.monitor = None
        self.root.minsize(570, 580)
        self.root.geometry(self.panel_geometry)
        self.root.title('Evrika · Панель управления')
        self.page.pack(fill='both', expand=True, padx=24, pady=18)

    def settings(self):
        from setup_anpr import SettingsWindow
        self.root.update_idletasks()
        self.panel_geometry = self.root.geometry()
        self.page.pack_forget()
        self.root.minsize(590, 480)
        self.root.geometry('670x530')
        self.root.title('Эврика · Настройки')
        try:
            self.settings_view = SettingsWindow(self.root, on_close=self.close_settings)
        except Exception:
            self.close_settings()
            raise

    def close_settings(self):
        self.settings_view = None
        self.root.minsize(570, 580)
        self.root.geometry(self.panel_geometry)
        self.root.title('Evrika · Панель управления')
        self.page.pack(fill='both', expand=True, padx=24, pady=18)
    def open_log(self): subprocess.Popen(['notepad.exe', str(service.BASE / 'service.stderr.log')])
    def restart(self): self.work(self._restart)
    def _restart(self):
        service.stop()
        self.queue.put(('stopped', None))
        return service.start()
    def toggle(self): self.work(service.stop if self.running else service.start)

    def work(self, action):
        if self.busy: return
        self.busy = True; self.message.set(''); self.main_button.configure(state='disabled')
        self.status.set('Подождите')
        self.hint.set('Меняем состояние системы…')
        self.animate_status()
        stopping = action == service.stop
        def worker():
            try:
                action()
                if stopping: self.queue.put(('stopped', None))
                self.queue.put(('message', ''))
            except Exception as error: self.queue.put(('message', str(error) if isinstance(error, (RuntimeError, ValueError)) else 'Не удалось выполнить действие.'))
        threading.Thread(target=worker, daemon=True).start()

    def refresh(self):
        def worker():
            try: self.queue.put(('health', service.health()))
            except Exception: self.queue.put(('offline', None))
        threading.Thread(target=worker, daemon=True).start(); self.root.after(3000, self.refresh)

    def refresh_events(self):
        if not self.event_fetching:
            self.event_fetching = True
            def worker():
                try:
                    with requests.Session() as session:
                        session.trust_env = False
                        response = session.get(service.URL + '/passages', timeout=2)
                        response.raise_for_status()
                        data = response.json()
                    if data.get('service') != 'anpr-fayz':
                        raise ValueError('Неизвестный сервис')
                    self.queue.put(('events', data))
                except (requests.RequestException, ValueError):
                    self.queue.put(('events_unavailable', None))
            threading.Thread(target=worker, daemon=True).start()
        self.root.after(1500, self.refresh_events)

    def merge_events(self, data):
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

    def toggle_events(self):
        self.events_expanded = not self.events_expanded
        self.events_button.configure(text='Свернуть' if self.events_expanded else 'Показать все')
        self.render_events()

    def render_events(self):
        events = list(self.events) if self.events_expanded else list(self.events)[:3]
        self.events_label.configure(state='normal')
        self.events_label.delete('1.0', 'end')
        self.events_label.insert('1.0', '\n'.join(events) if events else 'Проездов пока нет')
        self.events_label.configure(state='disabled')

    def record_event(self, description, at=None):
        stamp = at or datetime.now().astimezone()
        self.events.appendleft(f'{stamp:%H:%M:%S} · {description}')
        self.render_events()

    @staticmethod
    def event_time(value):
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None

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
            self.hint.set('Мониторинг не выполняется\nНажмите «Запустить» для начала работы')
        self.main_button.configure(text='Остановить' if running else 'Запустить',
                                   fg_color='#45303a' if running else self.BLUE,
                                   hover_color='#603844' if running else self.BLUE_HOVER, text_color=self.TEXT)
        self.camera[0].set(camera_state)
        self.camera[1].set('Данные поступают' if running and camera_message == 'подключена, получаю данные.' and data.get('last_camera_data') else '')
        self.telegram[0].set(telegram_state)
        delivery_time = self.event_time(delivery_at)
        self.telegram[1].set(f'Последняя отправка: {delivery_time:%H:%M}' if telegram_state == 'Отправлено' and delivery_time else '')
        self._state_color(running)
        self._connection_color('camera', camera_state == 'Подключена')
        self._connection_color('telegram', telegram_state == 'Отправлено')

    def process(self):
        try:
            while True:
                kind, data = self.queue.get_nowait()
                if kind == 'show': self.root.deiconify(); self.root.lift(); self.root.focus_force()
                elif kind == 'confirm_exit': self.confirm_stop_and_exit()
                elif kind == 'exit_complete': self._quit(); return
                elif kind == 'exit_error':
                    self.busy = False
                    self.main_button.configure(state='normal')
                    self.message.set(data)
                    messagebox.showerror('Не удалось остановить систему', data, parent=self.root)
                elif kind == 'stopped': pass
                elif kind == 'events': self.event_fetching = False; self.merge_events(data)
                elif kind == 'events_unavailable': self.event_fetching = False
                elif kind == 'message': self.message.set(data); self.busy = False; self.main_button.configure(state='normal')
                elif kind == 'offline':
                    if not self.busy: self.update_status(None)
                elif kind == 'health':
                    if not self.busy: self.update_status(data)
        except queue.Empty: pass
        self.root.after(100, self.process)

    def _state_color(self, running):
        self.state_card.configure(fg_color='#142e2a' if running else '#202735', border_color='#246253' if running else self.BORDER)

    def _connection_color(self, key, confirmed):
        box, label = self._status_widgets[key]
        box.configure(border_color='#246253' if confirmed else self.BORDER)
        label.configure(text_color=self.GREEN if confirmed else self.TEXT)

    def animate_status(self):
        if not self.busy:
            return
        self.animation_phase = (self.animation_phase + 1) % 4
        self.status.set('Подождите' + '.' * self.animation_phase)
        self.root.after(400, self.animate_status)


if __name__ == '__main__':
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Evrika.ANPR.ControlPanel')
    from panel_instance import PanelInstance
    instance = PanelInstance()
    try:
        try:
            instance.acquire()
        except RuntimeError as error:
            messagebox.showerror('Эврика', str(error))
        else:
            root = ctk.CTk()
            panel = Panel(root)
            def check_replacement():
                if instance.replacement_requested():
                    panel._quit()
                else:
                    root.after(200, check_replacement)
            root.after(200, check_replacement)
            root.mainloop()
    finally:
        instance.close()
