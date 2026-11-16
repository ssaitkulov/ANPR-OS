"""Local Windows settings window. Does not start the collector or send messages."""
from pathlib import Path
import ipaddress
import os
import queue
import re
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from requests.auth import HTTPDigestAuth

BASE = Path(__file__).resolve().parent
ENV = BASE / '.env'
FIELDS = [
    ('CAMERA_NAME', 'Название камеры', 'Главные ворота'),
    ('CAMERA_URL', 'Адрес камеры', 'http://192.168.100.249/'),
    ('DAHUA_USERNAME', 'Логин камеры', 'admin'),
    ('DAHUA_PASSWORD', 'Пароль камеры', ''),
    ('TELEGRAM_BOT_TOKEN', 'Токен бота (BotFather)', ''),
    ('TELEGRAM_CHAT_ID', 'Чат для уведомлений (ID)', ''),
    ('ALLOWED_CHAT_IDS', 'Разрешённые чаты (ID через запятую)', ''),
    ('TIMEZONE', 'Часовой пояс', 'Asia/Tashkent'),
]


def read_settings(path=ENV):
    result = {}
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                result[key.strip()] = value.strip().strip('\"').strip("'")
    return result


def camera_settings(values):
    raw = values.get('CAMERA_URL', '').strip()
    url = urlsplit(raw if '://' in raw else 'http://' + raw)
    try:
        host, port = url.hostname, url.port or 80
        ipaddress.IPv4Address(host)
    except (ValueError, TypeError):
        raise ValueError('Введите IPv4-адрес камеры, например http://192.168.100.249:80/')
    if url.scheme != 'http' or url.username or url.password or url.path not in ('', '/') or url.query or url.fragment:
        raise ValueError('Используйте http://IP:порт/ без логина, пароля и дополнительных путей.')
    if not 1 <= port <= 65535:
        raise ValueError('Порт должен быть от 1 до 65535.')
    if not values.get('DAHUA_USERNAME') or not values.get('DAHUA_PASSWORD'):
        raise ValueError('Заполните логин и пароль камеры.')
    return host, str(port)


def validate(values):
    if any('\n' in v or '\r' in v or '\x00' in v for v in values.values()):
        raise ValueError('Значения должны занимать одну строку.')
    if any(v.startswith(('"', "'")) or v.endswith(('"', "'")) for v in values.values()):
        raise ValueError('Значения не должны начинаться или заканчиваться кавычками.')
    host, port = camera_settings(values)
    if not values.get('CAMERA_NAME', '').strip():
        raise ValueError('Введите название камеры.')
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', values.get('TELEGRAM_BOT_TOKEN', '')):
        raise ValueError('Введите токен бота от BotFather.')
    chat = values.get('TELEGRAM_CHAT_ID', '')
    allowed = [s.strip() for s in values.get('ALLOWED_CHAT_IDS', '').split(',') if s.strip()]
    if not re.fullmatch(r'-?[1-9]\d*', chat) or not allowed or any(not re.fullmatch(r'-?[1-9]\d*', s) for s in allowed):
        raise ValueError('Введите числовой ID получателя и хотя бы один разрешённый ID.')
    if chat not in allowed:
        raise ValueError('Добавьте чат для уведомлений в список разрешённых чатов.')
    try:
        ZoneInfo(values.get('TIMEZONE', ''))
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError('Неизвестный часовой пояс. Пример: Asia/Tashkent. Установите зависимости.')
    result = dict(values)
    result.pop('CAMERA_URL', None)
    result.update(DAHUA_HOST=host, DAHUA_PORT=port, ALLOWED_CHAT_IDS=','.join(dict.fromkeys(allowed)))
    return result


def save_settings(values, path=ENV):
    updates = validate(values)
    lines = path.read_text(encoding='utf-8-sig').splitlines() if path.exists() else []
    output = []
    for line in lines:
        key = line.split('=', 1)[0].strip() if '=' in line and not line.lstrip().startswith('#') else ''
        if key not in updates:
            output.append(line)
    output.extend(f'{key}={value}' for key, value in updates.items())
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text('\n'.join(output) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def check_camera(values):
    host, port = camera_settings(values)
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(f'http://{host}:{port}/cgi-bin/magicBox.cgi',
                                   params={'action': 'getDeviceType'},
                                   auth=HTTPDigestAuth(values['DAHUA_USERNAME'], values['DAHUA_PASSWORD']),
                                   timeout=(5, 10), allow_redirects=False)
        if response.status_code in (401, 403):
            return 'Камера: неверный логин/пароль или недостаточно прав.'
        if response.status_code != 200 or not response.text.strip().startswith('type='):
            return f'Камера: неожиданный ответ (HTTP {response.status_code}).'
        return 'Камера доступна: ' + response.text.strip().removeprefix('type=')[:100]
    except requests.RequestException:
        return 'Камера недоступна. Проверьте питание, сеть, адрес и порт.'


def check_bot(values):
    token = values.get('TELEGRAM_BOT_TOKEN', '')
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', token):
        return 'Введите токен бота от BotFather.'
    try:
        response = requests.get(f'https://api.telegram.org/bot{token}/getMe', timeout=(5, 10))
        data = response.json()
        if not data.get('ok'):
            return 'Telegram: токен не принят.'
        return 'Бот доступен: @' + data['result']['username'] + '. Отправка в чат ещё не проверена.'
    except (requests.RequestException, ValueError, KeyError):
        return 'Не удалось проверить Telegram. Проверьте интернет и токен.'


class SettingsWindow:
    def __init__(self, root):
        self.root = root
        self.results = queue.Queue()
        root.title('ANPR · Настройки')
        root.geometry('760x660')
        root.minsize(720, 640)
        panel = ttk.Frame(root, padding=24)
        panel.pack(fill='both', expand=True)
        panel.columnconfigure(1, weight=1)
        ttk.Label(panel, text='Камера → Telegram', font=('Segoe UI', 20, 'bold')).grid(row=0, column=0, columnspan=2, sticky='w')
        ttk.Label(panel, text='Настройте одну камеру и получателей уведомлений.').grid(row=1, column=0, columnspan=2, sticky='w', pady=(6, 20))
        current = read_settings()
        current['CAMERA_URL'] = f"http://{current.get('DAHUA_HOST', '192.168.100.249')}:{current.get('DAHUA_PORT', '80')}/"
        self.variables = {}
        for row, (key, label, default) in enumerate(FIELDS, 2):
            ttk.Label(panel, text=label).grid(row=row, column=0, sticky='w', padx=(0, 16), pady=7)
            var = tk.StringVar(value=current.get(key, default))
            self.variables[key] = var
            secret = key in ('DAHUA_PASSWORD', 'TELEGRAM_BOT_TOKEN')
            ttk.Entry(panel, textvariable=var, show='•' if secret else '').grid(row=row, column=1, sticky='ew', pady=7)
        ttk.Label(panel, text='Для личного чата: укажите свой ID в обоих полях и напишите боту /start.\nДля группы: добавьте её ID в оба поля. Команды будут доступны участникам этой группы.', wraplength=690).grid(row=10, column=0, columnspan=2, sticky='w', pady=(12, 8))
        buttons = ttk.Frame(panel)
        buttons.grid(row=11, column=0, columnspan=2, sticky='w', pady=10)
        self.check_buttons = []
        for label, function in [('Проверить камеру', check_camera), ('Проверить бота', check_bot)]:
            button = ttk.Button(buttons, text=label, command=lambda f=function: self.check(f))
            button.pack(side='left', padx=(0, 10))
            self.check_buttons.append(button)
        ttk.Button(buttons, text='Сохранить', command=self.save).pack(side='left')
        self.status = tk.StringVar(value='Проверки не отправляют сообщения и не запускают сбор событий.')
        ttk.Label(panel, textvariable=self.status, wraplength=690).grid(row=12, column=0, columnspan=2, sticky='w', pady=10)
        ttk.Label(panel, text='Настройки сохраняются локально в .env (без шифрования).\nПосле изменения настроек перезапустите скрипт.', wraplength=690).grid(row=13, column=0, columnspan=2, sticky='w', pady=8)
        root.after(100, self.poll)

    def values(self):
        return {key: var.get().strip() for key, var in self.variables.items()}

    def save(self):
        try:
            save_settings(self.values())
            self.status.set('Настройки сохранены. Перезапустите скрипт, чтобы применить изменения.')
        except ValueError as error:
            messagebox.showerror('Проверьте настройки', str(error))
        except OSError:
            messagebox.showerror('Не удалось сохранить', 'Проверьте доступ к папке проекта.')

    def check(self, function):
        values = self.values()
        self.status.set('Проверяю подключение…')
        for button in self.check_buttons:
            button.state(['disabled'])
        def worker():
            try:
                result = function(values)
            except ValueError as error:
                result = str(error)
            except Exception:
                result = 'Не удалось выполнить проверку. Проверьте настройки.'
            self.results.put(result)
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            self.status.set(self.results.get_nowait())
            for button in self.check_buttons:
                button.state(['!disabled'])
        except queue.Empty:
            pass
        self.root.after(100, self.poll)


if __name__ == '__main__':
    root = tk.Tk()
    SettingsWindow(root)
    root.mainloop()
