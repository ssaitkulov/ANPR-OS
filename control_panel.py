"""Minimal native Windows control panel for the ANPR collector."""
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk
from pathlib import Path
import service_control as service


class Panel:
    BG = '#0b0d0f'; TEXT = '#f2f4f7'; MUTED = '#9aa4b2'; GREEN = '#36d17c'; RED = '#ff6b72'; AMBER = '#e5ad52'; CARD = '#171a1e'; BORDER = '#2a3037'

    def __init__(self, root):
        self.root, self.queue, self.busy = root, queue.Queue(), False
        self.running = False
        self.animation_phase = 0
        root.title('Система Управления'); root.geometry('650x600'); root.minsize(600, 580); root.configure(bg=self.BG)
        self._styles()
        try:
            self.app_icon = tk.PhotoImage(file=str(Path(__file__).resolve().parent / 'icon.svg'))
            root.iconphoto(True, self.app_icon)
        except tk.TclError:
            self.app_icon = None
        self.icons = {}
        icon_dir = Path(__file__).resolve().parent / 'filled'
        for key, name in {'camera': 'camera.svg', 'telegram': 'send.svg', 'settings': 'settings.svg'}.items():
            try: self.icons[key] = tk.PhotoImage(file=str(icon_dir / name)).subsample(2)
            except tk.TclError: pass
        try:
            self.logo = tk.PhotoImage(file=r'D:\PROJECT\2x2 Shield System\Branding\Logo\SVG\SVG\uz logo.svg').subsample(2)
        except tk.TclError:
            self.logo = None

        page = ttk.Frame(root, style='Page.TFrame', padding=(24, 24, 24, 24)); page.pack(fill='both', expand=True, padx=20, pady=20)
        if self.logo:
            ttk.Label(page, image=self.logo, style='Logo.TLabel').pack(anchor='w')
        else:
            ttk.Label(page, text='Камера и Telegram', style='Title.TLabel').pack(anchor='w')
        ttk.Label(page, text='Система управления', style='Subtitle.TLabel').pack(anchor='w', pady=(6, 20))

        state = ttk.Frame(page, style='State.TFrame', padding=(20, 14)); state.pack(fill='x')
        self.status = tk.StringVar(value='Проверяю…'); self.hint = tk.StringVar(value='')
        ttk.Label(state, textvariable=self.status, style='State.TLabel').pack(anchor='w')
        ttk.Label(state, textvariable=self.hint, style='Hint.TLabel').pack(anchor='w', pady=(6, 0))

        connections = ttk.Frame(page, style='Page.TFrame'); connections.pack(fill='x', pady=(16, 0))
        self.camera = self._connection(connections, 'camera', 'Камера', 'Проверяю…')
        self.telegram = self._connection(connections, 'telegram', 'Telegram', 'Проверяю…')
        connections.columnconfigure(0, weight=1); connections.columnconfigure(1, weight=1)

        self.last = tk.StringVar(value='')
        ttk.Label(page, textvariable=self.last, style='Last.TLabel').pack(anchor='w', pady=(18, 0))

        actions = ttk.Frame(page, style='Page.TFrame'); actions.pack(anchor='w', pady=(20, 0))
        self.main_button = self._button(actions, 'Включить систему', self.toggle, self.RED); self.main_button.pack(side='left')
        settings_button = self._button(actions, '  Настройки', self.settings, self.CARD)
        if self.icons.get('settings'):
            settings_button.configure(image=self.icons['settings'], compound='left')
        settings_button.pack(side='left', padx=(10, 0))
        more = tk.Menubutton(actions, text='•••', bg=self.CARD, fg=self.TEXT, activebackground='#252a30', activeforeground=self.TEXT, relief='flat', bd=0, padx=20, pady=10, font=('Segoe UI', 10)); menu = tk.Menu(more, tearoff=False, bg=self.CARD, fg=self.TEXT, activebackground='#2d343d', activeforeground=self.TEXT); menu.add_command(label='Перезапустить систему', command=self.restart); menu.add_command(label='Открыть журнал', command=self.open_log); more['menu'] = menu; more.pack(side='left', padx=(10, 0))

        self.message = tk.StringVar(value=''); ttk.Label(page, textvariable=self.message, style='Message.TLabel', wraplength=530).pack(anchor='w', pady=(10, 0))
        ttk.Label(page, text='Окно можно закрыть — система продолжит работать. После перезагрузки откройте run.bat.', style='Help.TLabel', wraplength=530).pack(anchor='w', pady=(14, 0))
        root.after(100, self.process); self.work(service.start); root.after(1000, self.refresh)

    def _styles(self):
        s = ttk.Style(self.root)
        try: s.theme_use('vista')
        except tk.TclError: pass
        self.BG = '#13161a'
        s.configure('Page.TFrame', background=self.BG); s.configure('State.TFrame', background='#163126'); s.configure('Logo.TLabel', background=self.BG)
        s.configure('Title.TLabel', background=self.BG, foreground=self.TEXT, font=('Segoe UI', 23, 'bold')); s.configure('Subtitle.TLabel', background=self.BG, foreground=self.MUTED, font=('Segoe UI', 11))
        s.configure('State.TLabel', background='#163126', foreground=self.TEXT, font=('Segoe UI', 17, 'bold')); s.configure('Hint.TLabel', background='#163126', foreground='#b9c8c2', font=('Segoe UI', 10))
        s.configure('Connection.TFrame', background=self.CARD); s.configure('Connection.TLabel', background=self.CARD, foreground=self.TEXT, font=('Segoe UI', 11)); s.configure('ConnectionHint.TLabel', background=self.CARD, foreground=self.MUTED, font=('Segoe UI', 9))
        s.configure('Last.TLabel', background=self.BG, foreground=self.MUTED, font=('Segoe UI', 9)); s.configure('Primary.TButton', font=('Segoe UI', 10, 'bold'), padding=(17, 10), foreground=self.GREEN); s.configure('Stop.TButton', font=('Segoe UI', 10, 'bold'), padding=(17, 10), foreground=self.RED); s.configure('Action.TButton', padding=(13, 10)); s.configure('Message.TLabel', background=self.BG, foreground=self.GREEN, font=('Segoe UI', 10, 'bold')); s.configure('Help.TLabel', background=self.BG, foreground=self.MUTED, font=('Segoe UI', 9))

    def _connection(self, parent, key, title, value):
        box = tk.Frame(parent, bg=self.CARD, highlightbackground=self.BORDER, highlightthickness=1, padx=14, pady=11); box.grid(row=0, column=len(getattr(self, '_connection_boxes', [])), sticky='nsew', padx=(0 if not getattr(self, '_connection_boxes', []) else 12, 0)); self._connection_boxes = getattr(self, '_connection_boxes', []) + [box]
        if not hasattr(self, '_status_widgets'):
            self._status_widgets = {}
        var, hint = tk.StringVar(value=value), tk.StringVar(value='')
        header = tk.Frame(box, bg=self.CARD)
        header.pack(anchor='w', fill='x')
        if self.icons.get(key):
            tk.Label(header, image=self.icons[key], bg=self.CARD, bd=0, padx=0, pady=0).pack(side='left', padx=(0, 8))
        tk.Label(header, text=title, bg=self.CARD, fg=self.MUTED, font=('Segoe UI', 9), padx=0, pady=0).pack(side='left')
        indicator = tk.Label(box, textvariable=var, bg=self.CARD, fg=self.TEXT, font=('Segoe UI', 10, 'bold'), anchor='w', padx=0, pady=0)
        indicator.pack(anchor='w', pady=(7, 0))
        self._status_widgets[key] = (box, indicator)
        tk.Label(box, textvariable=hint, bg=self.CARD, fg=self.MUTED, font=('Segoe UI', 9), anchor='w', padx=0, pady=0).pack(anchor='w', pady=(4, 0))
        return var, hint

    def _button(self, parent, text, command, color):
        return tk.Button(parent, text=text, command=command, bg='#252a30', fg=self.TEXT, activebackground='#343a42', activeforeground=self.TEXT, relief='flat', bd=0, padx=16, pady=8, font=('Segoe UI', 10, 'bold'), cursor='hand2', highlightthickness=1, highlightbackground=self.BORDER)

    def settings(self): subprocess.Popen([sys.executable, str(service.BASE / 'setup_anpr.py')], cwd=service.BASE)
    def open_log(self): subprocess.Popen(['notepad.exe', str(service.BASE / 'service.stderr.log')])
    def restart(self): self.work(self._restart)
    def _restart(self): service.stop(); return service.start()
    def toggle(self): self.work(service.stop if self.running else service.start)

    def work(self, action):
        if self.busy: return
        self.busy = True; self.message.set(''); self.main_button.configure(state='disabled')
        self.status.set('Подождите')
        self.hint.set('Меняем состояние системы…')
        self.animate_status()
        def worker():
            try: action(); self.queue.put(('message', ''))
            except Exception as error: self.queue.put(('message', str(error) if isinstance(error, (RuntimeError, ValueError)) else 'Не удалось выполнить действие.'))
        threading.Thread(target=worker, daemon=True).start()

    def refresh(self):
        def worker():
            try: self.queue.put(('health', service.health()))
            except Exception: self.queue.put(('offline', None))
        threading.Thread(target=worker, daemon=True).start(); self.root.after(3000, self.refresh)

    def process(self):
        try:
            while True:
                kind, data = self.queue.get_nowait()
                if kind == 'message': self.message.set(data); self.busy = False; self.main_button.configure(state='normal')
                elif kind == 'offline':
                    if self.busy: continue
                    self.running = False; self.status.set('Система выключена'); self.hint.set('Нажмите «Включить систему», чтобы начать.'); self.main_button.configure(text='Включить систему', bg='#174334', fg=self.GREEN); self.camera[0].set('Нет данных'); self.camera[1].set('Программа не подключена'); self.telegram[0].set('Нет данных'); self.telegram[1].set('Программа не подключена'); self.last.set(''); self._state_color(False); self._connection_color('camera', False); self._connection_color('telegram', False)
                else:
                    if self.busy: continue
                    self.running = data.get('status') == 'ok'; self.status.set('Система работает' if self.running else 'Система остановлена'); self.hint.set('Программа запущена. Состояние подключений — ниже.' if self.running else 'Проверьте состояние программы и перезапустите её.'); self.main_button.configure(text='Выключить систему' if self.running else 'Включить систему', bg='#382124' if self.running else '#174334', fg=self.RED if self.running else self.GREEN); camera_ok = 'подключена' in data.get('camera_message', '').lower(); self.camera[0].set('Подключена' if camera_ok else 'Нет подтверждения'); self.camera[1].set('Получаем данные' if camera_ok else 'Проверяем связь'); tg = data.get('telegram_message', '').lower(); self.telegram[0].set('Отправлено' if 'доставлено' in tg else ('Ошибка отправки' if 'не удалось' in tg else 'Не проверено')); self.telegram[1].set('Последнее уведомление принято' if 'доставлено' in tg else ('Проверьте интернет и настройки' if 'не удалось' in tg else 'Ждём первую отправку')); self.last.set('Последний сигнал: ' + (data.get('last_camera_data') or 'пока нет'))
                    self._state_color(self.running)
                    self._connection_color('camera', self.running and camera_ok)
                    self._connection_color('telegram', self.running and 'доставлено' in tg)
        except queue.Empty: pass
        self.root.after(100, self.process)

    def _state_color(self, running):
        color = '#163126' if running else '#272423'
        style = ttk.Style(self.root)
        style.configure('State.TFrame', background=color)
        style.configure('State.TLabel', background=color)
        style.configure('Hint.TLabel', background=color)

    def _connection_color(self, key, confirmed):
        box, label = self._status_widgets[key]
        box.configure(highlightbackground='#287a50' if confirmed else self.BORDER)
        label.configure(fg=self.GREEN if confirmed else self.TEXT)

    def animate_status(self):
        if not self.busy:
            return
        self.animation_phase = (self.animation_phase + 1) % 4
        self.status.set('Подождите' + '.' * self.animation_phase)
        self.root.after(400, self.animate_status)


if __name__ == '__main__':
    root = tk.Tk(); Panel(root); root.mainloop()
