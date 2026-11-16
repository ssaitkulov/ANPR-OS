"""A local control window: closing it leaves the collector running."""
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk
import requests
import service_control as service


class Panel:
    def __init__(self, root):
        self.root = root
        self.queue = queue.Queue()
        self.busy = False
        root.title('Камера и Telegram — управление')
        root.geometry('760x570')
        frame = ttk.Frame(root, padding=28)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Камера и Telegram', font=('Segoe UI', 22, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='Здесь видно, работает ли сбор фотографий.', font=('Segoe UI', 11)).pack(anchor='w', pady=(8, 22))
        self.lines = {}
        for key in ('program', 'camera', 'telegram', 'last'):
            var = tk.StringVar(value='Проверяю…')
            self.lines[key] = var
            ttk.Label(frame, textvariable=var, wraplength=680, font=('Segoe UI', 12)).pack(anchor='w', pady=10)
        row = ttk.Frame(frame)
        row.pack(anchor='w', pady=20)
        self.buttons = []
        for title, action in [('Запустить', service.start), ('Остановить сбор', service.stop), ('Перезапустить', self.restart)]:
            button = ttk.Button(row, text=title, command=lambda f=action: self.work(f))
            button.pack(side='left', padx=(0, 8))
            self.buttons.append(button)
        ttk.Button(row, text='Настройки', command=self.settings).pack(side='left')
        self.message = tk.StringVar(value='Запускаю сбор…')
        ttk.Label(frame, textvariable=self.message, wraplength=680).pack(anchor='w', pady=10)
        ttk.Label(frame, text='Окно можно закрыть — сбор продолжится.\nЧтобы прекратить сбор, нажмите «Остановить сбор».\nПосле перезагрузки компьютера откройте run.bat.\nПосле изменения настроек нажмите «Перезапустить».', wraplength=680).pack(anchor='w', pady=8)
        root.after(100, self.process)
        self.work(service.start)
        root.after(1000, self.refresh)

    def settings(self):
        subprocess.Popen([sys.executable, str(service.BASE / 'setup_anpr.py')], cwd=service.BASE)

    def restart(self):
        service.stop()
        return service.start()

    def work(self, action):
        if self.busy:
            return
        self.busy = True
        self.message.set('Подождите несколько секунд…')
        for button in self.buttons:
            button.state(['disabled'])
        def worker():
            try:
                action()
                self.queue.put(('message', 'Готово. Состояние обновится автоматически.'))
            except Exception as error:
                text = str(error) if isinstance(error, (RuntimeError, ValueError)) else 'Не удалось выполнить действие. Проверьте настройки и журнал service.stderr.log.'
                self.queue.put(('message', text))
        threading.Thread(target=worker, daemon=True).start()

    def refresh(self):
        def worker():
            try:
                self.queue.put(('health', service.health()))
            except Exception:
                self.queue.put(('offline', None))
        threading.Thread(target=worker, daemon=True).start()
        self.root.after(3000, self.refresh)

    def process(self):
        try:
            while True:
                kind, data = self.queue.get_nowait()
                if kind == 'message':
                    self.message.set(data)
                    self.busy = False
                    for button in self.buttons:
                        button.state(['!disabled'])
                elif kind == 'offline':
                    self.lines['program'].set('Программа не отвечает. Нажмите «Запустить».')
                    self.lines['camera'].set('Камера: состояние неизвестно.')
                    self.lines['telegram'].set('Telegram: состояние неизвестно.')
                    self.lines['last'].set('Новые события сейчас не подтверждены.')
                else:
                    self.lines['program'].set('Программа работает.' if data['status'] == 'ok' else 'Сбор остановился. Нажмите «Перезапустить».')
                    self.lines['camera'].set('Камера: ' + data['camera_message'])
                    self.lines['telegram'].set('Telegram: ' + data['telegram_message'])
                    self.lines['last'].set('Последнее получение данных: ' + (data['last_camera_data'] or 'пока не было'))
        except queue.Empty:
            pass
        self.root.after(100, self.process)


if __name__ == '__main__':
    root = tk.Tk()
    Panel(root)
    root.mainloop()
