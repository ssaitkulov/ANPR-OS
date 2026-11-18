"""Local Windows settings window. Does not start the collector or send messages."""
from pathlib import Path
import ipaddress
import os
import queue
import re
import threading
import tkinter as tk
from tkinter import messagebox, filedialog
import customtkinter as ctk
from PIL import Image
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core import preferences
import requests
from requests.auth import HTTPDigestAuth

BASE = Path(__file__).resolve().parent
ENV = BASE / '.env'
ASSETS = BASE / 'assets'
DIRECTIONS = {'Оба направления': '', 'Только въезд': 'Approach', 'Только выезд': 'Leave'}
FIELDS = [
    ('CAMERA_NAME', 'Название камеры', 'Главные ворота'),
    ('CAMERA_URL', 'Адрес камеры', 'http://192.168.100.249/'),
    ('DAHUA_USERNAME', 'Логин камеры', 'admin'),
    ('DAHUA_PASSWORD', 'Пароль камеры', ''),
    ('TELEGRAM_BOT_TOKEN', 'Токен бота (BotFather)', ''),
    ('TELEGRAM_CHAT_ID', 'Чат для уведомлений (ID)', ''),
    ('ALLOWED_CHAT_IDS', 'Разрешённые чаты (ID через запятую)', ''),
    ('TIMEZONE', 'Часовой пояс', 'Asia/Tashkent'),
    ('IMAGE_DIR', 'Папка фотографий', str(BASE / 'images')),
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
    if 'DIRECTION' in result and result['DIRECTION'] not in DIRECTIONS.values():
        raise ValueError('Выберите направление из списка.')
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
    BG = '#101722'
    CARD = '#1a2432'
    BORDER = '#303e51'
    TEXT = '#f2f5fa'
    MUTED = '#a5b1c2'
    BLUE = '#2869df'

    def __init__(self, root, on_close=None):
        self.on_close = on_close
        if on_close is not None:
            root = ctk.CTkFrame(root, fg_color=self.BG, corner_radius=0)
            root.pack(fill='both', expand=True)
        else:
            ctk.set_appearance_mode('dark')
            root.title('Evrika · Настройки')
            root.configure(fg_color=self.BG)
            root.iconbitmap(str(ASSETS / 'evrika.ico'))
            root.geometry('670x530')
            root.minsize(590, 480)
        self.root = root
        self.results = queue.Queue()

        header = ctk.CTkFrame(root, fg_color='transparent')
        header.pack(fill='x', padx=22, pady=(14, 4))
        if self.on_close is not None:
            ctk.CTkButton(header, text='‹  Назад', width=85, height=32, fg_color=self.CARD,
                          hover_color='#34455a', command=self.close).pack(side='left', padx=(0, 14))
        ctk.CTkLabel(header, text='Настройки', text_color=self.TEXT,
                     font=('Segoe UI', 20, 'bold'), anchor='w').pack(side='left')
        self.logo = ctk.CTkImage(light_image=Image.open(ASSETS / 'evrika-logo-ru.png'), size=(116, 24))
        ctk.CTkLabel(header, text='', image=self.logo).pack(side='right')

        footer = ctk.CTkFrame(root, fg_color='#151f2d', corner_radius=0)
        footer.pack(side='bottom', fill='x')
        actions = ctk.CTkFrame(footer, fg_color='transparent')
        actions.pack(fill='x', padx=22, pady=(8, 2))
        ctk.CTkButton(actions, text='Сохранить', command=self.save, width=140,
                      height=34, fg_color=self.BLUE, hover_color='#1d55b9',
                      font=('Segoe UI', 12, 'bold')).pack(side='right')
        ctk.CTkLabel(actions, text='Настройки камеры применяются после перезапуска',
                     text_color=self.MUTED, font=('Segoe UI', 11), anchor='w').pack(side='left', fill='x', expand=True)
        self.status = tk.StringVar(value='')
        ctk.CTkLabel(footer, textvariable=self.status, text_color='#b8c9e1', font=('Segoe UI', 11),
                     anchor='w', justify='left', wraplength=500).pack(fill='x', padx=22, pady=(0, 6))

        tabs = ctk.CTkTabview(root, fg_color=self.CARD, segmented_button_fg_color='#111b29',
                              segmented_button_selected_color=self.BLUE,
                              segmented_button_selected_hover_color='#1d55b9')
        tabs.pack(fill='both', expand=True, padx=20, pady=(0, 8))
        self.tabs = tabs
        current = read_settings()
        current['CAMERA_URL'] = f"http://{current.get('DAHUA_HOST', '192.168.100.249')}:{current.get('DAHUA_PORT', '80')}/"
        self.variables = {key: tk.StringVar(value=current.get(key, default)) for key, _, default in FIELDS}
        self.direction = tk.StringVar(value=next((label for label, value in DIRECTIONS.items()
                                                 if value == current.get('DIRECTION', 'Approach')), 'Оба направления'))
        self.check_buttons = []
        camera = tabs.add('Камера')
        telegram = tabs.add('Telegram')
        general = tabs.add('Мониторинг')
        application = tabs.add('Приложение')
        for tab in (camera, telegram, general, application):
            tab.grid_columnconfigure(1, weight=1)

        self._field(camera, 0, 'Название', 'CAMERA_NAME')
        self._field(camera, 1, 'Адрес камеры', 'CAMERA_URL')
        self._field(camera, 2, 'Логин', 'DAHUA_USERNAME')
        self._field(camera, 3, 'Пароль', 'DAHUA_PASSWORD', secret=True)
        self._check_button(camera, 4, 'Проверить камеру', check_camera)

        self._field(telegram, 0, 'Токен бота', 'TELEGRAM_BOT_TOKEN', secret=True)
        self._field(telegram, 1, 'Чат уведомлений (ID)', 'TELEGRAM_CHAT_ID')
        self._field(telegram, 2, 'Разрешённые чаты (ID)', 'ALLOWED_CHAT_IDS')
        note = ('ID через запятую. Добавьте ID получателя в оба поля; для личного чата напишите боту /start.\n'
                'В группе команды доступны её участникам.')
        ctk.CTkLabel(telegram, text=note, text_color=self.MUTED, justify='left', anchor='w',
                     wraplength=500, font=('Segoe UI', 11)).grid(row=3, column=0, columnspan=2, sticky='ew', padx=12, pady=(8, 0))
        self._check_button(telegram, 4, 'Проверить бота', check_bot)

        self._field(general, 0, 'Часовой пояс', 'TIMEZONE')
        ctk.CTkLabel(general, text='Проезды', text_color=self.TEXT,
                     anchor='w', font=('Segoe UI', 12)).grid(row=1, column=0, sticky='w', padx=(12, 16), pady=5)
        ctk.CTkOptionMenu(general, variable=self.direction, values=list(DIRECTIONS), height=32,
                          fg_color='#26364c', button_color='#334b6b', button_hover_color='#416087').grid(
                              row=1, column=1, sticky='ew', padx=(0, 12), pady=5)
        ctk.CTkLabel(general, text='Папка фотографий', text_color=self.TEXT, anchor='w',
                     font=('Segoe UI', 12)).grid(row=2, column=0, sticky='w', padx=(12, 16), pady=(14, 5))
        folder_row = ctk.CTkFrame(general, fg_color='transparent')
        folder_row.grid(row=2, column=1, sticky='ew', padx=(0, 12), pady=(14, 5))
        ctk.CTkButton(folder_row, text='Обзор…', width=82, height=32, command=self.choose_photo_folder).pack(side='right', padx=(8, 0))
        ctk.CTkEntry(folder_row, textvariable=self.variables['IMAGE_DIR'], height=32, fg_color='#111b29',
                     border_color='#3a4a60').pack(side='left', fill='x', expand=True)
        ctk.CTkLabel(general, text='Фото сохраняются файлами. Новая папка применяется после перезапуска.',
                     text_color=self.MUTED, anchor='w', justify='left', wraplength=500,
                     font=('Segoe UI', 11)).grid(row=3, column=0, columnspan=2, sticky='ew', padx=12, pady=(8, 0))
        ctk.CTkLabel(general, text='Настройки хранятся в .env без шифрования.',
                     text_color=self.MUTED, anchor='w', font=('Segoe UI', 11)).grid(
                         row=4, column=0, columnspan=2, sticky='ew', padx=12, pady=(12, 0))
        try:
            autostart_enabled = preferences.get_autostart()
        except OSError:
            autostart_enabled = False
            self.status.set('Не удалось прочитать автозапуск Windows.')
        self.autostart = tk.BooleanVar(value=autostart_enabled)
        self.minimize_to_tray = tk.BooleanVar(value=preferences.get_minimize_to_tray())
        ctk.CTkSwitch(application, text='Запускать с Windows', variable=self.autostart,
                      progress_color=self.BLUE).grid(row=0, column=0, columnspan=2, sticky='w', padx=12, pady=(18, 10))
        ctk.CTkSwitch(application, text='Сворачивать в трей кнопкой «—»', variable=self.minimize_to_tray,
                      progress_color=self.BLUE).grid(row=1, column=0, columnspan=2, sticky='w', padx=12, pady=10)
        ctk.CTkLabel(application, text='Кнопка × всегда сворачивает панель в трей.',
                     text_color=self.MUTED, anchor='w', justify='left', wraplength=490,
                     font=('Segoe UI', 11)).grid(row=2, column=0, columnspan=2, sticky='ew', padx=12, pady=(4, 12))
        root.after(100, self.poll)

    def choose_photo_folder(self):
        selected = filedialog.askdirectory(parent=self.root.winfo_toplevel(), title='Папка фотографий',
                                           initialdir=self.variables['IMAGE_DIR'].get() or str(BASE))
        if selected:
            self.variables['IMAGE_DIR'].set(selected)

    def _field(self, parent, row, label, key, secret=False):
        ctk.CTkLabel(parent, text=label, text_color=self.TEXT, anchor='w',
                     font=('Segoe UI', 12)).grid(row=row, column=0, sticky='w', padx=(12, 16), pady=5)
        ctk.CTkEntry(parent, textvariable=self.variables[key], show='•' if secret else '',
                     height=32, fg_color='#111b29', border_color='#3a4a60',
                     text_color=self.TEXT).grid(row=row, column=1, sticky='ew', padx=(0, 12), pady=5)

    def _check_button(self, parent, row, label, function):
        button = ctk.CTkButton(parent, text=label, command=lambda: self.check(function),
                               height=32, width=155, fg_color='#283a53', hover_color='#365172',
                               border_width=1, border_color='#405471')
        button.grid(row=row, column=1, sticky='e', padx=(0, 12), pady=(12, 8))
        self.check_buttons.append(button)

    def close(self):
        self.root.destroy()
        if self.on_close:
            self.on_close()

    def values(self):
        return {**{key: var.get().strip() for key, var in self.variables.items()}, 'DIRECTION': DIRECTIONS[self.direction.get()]}

    def save(self):
        if self.tabs.get() == 'Приложение':
            self.save_application()
            return
        try:
            save_settings(self.values())
            self.status.set('Настройки сохранены. Перезапустите скрипт, чтобы применить изменения.')
        except ValueError as error:
            messagebox.showerror('Проверьте настройки', str(error), parent=self.root.winfo_toplevel())
        except OSError:
            messagebox.showerror('Не удалось сохранить', 'Проверьте доступ к папке проекта.', parent=self.root.winfo_toplevel())

    def save_application(self):
        try:
            preferences.set_autostart(self.autostart.get())
            preferences.set_minimize_to_tray(self.minimize_to_tray.get())
            self.status.set('Параметры приложения сохранены.')
        except OSError as error:
            self.status.set('Не удалось сохранить параметры приложения.')
            messagebox.showerror('Не удалось сохранить', str(error), parent=self.root.winfo_toplevel())

    def check(self, function):
        values = self.values()
        self.status.set('Проверяю подключение…')
        for button in self.check_buttons:
            button.configure(state='disabled')
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
                button.configure(state='normal')
        except queue.Empty:
            pass
        self.root.after(100, self.poll)


if __name__ == '__main__':
    root = ctk.CTk()
    SettingsWindow(root)
    root.mainloop()
