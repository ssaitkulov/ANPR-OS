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
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core import preferences
from app.core import safe_mode
from app.core.paths import PROJECT_ROOT, env_file
from app.ui.tklife import cancel_pending_scripts
import requests
from requests.auth import HTTPDigestAuth

BASE = PROJECT_ROOT
ENV = env_file()
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


def read_settings(path=None):
    path = Path(path) if path is not None else env_file()
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


def save_settings(values, path=None):
    path = Path(path) if path is not None else env_file()
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
    if safe_mode.is_active():
        return 'Проверка камеры отключена в безопасном тестовом режиме.'
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
    if safe_mode.is_active():
        return 'Проверка Telegram отключена в безопасном тестовом режиме.'
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


class SettingsSection:
    """Раздел «Настройки»: вкладки камеры, бота, мониторинга и приложения.

    Может работать как отдельное окно (`setup_anpr.py`) и как раздел
    единого окна приложения (параметр `container`).

    Компоновка: вертикальная навигация слева, форма шириной не больше
    800 px справа, кнопка «Сохранить» — внизу в предсказуемом месте.
    Несохранённые значения не теряются при переключении вкладок: все поля
    связаны с одними и теми же переменными, а вкладки только скрываются.
    """

    BG = '#101722'
    CARD = '#1a2432'
    BORDER = '#303e51'
    TEXT = '#f2f5fa'
    MUTED = '#a5b1c2'
    BLUE = '#2869df'
    BLUE_HOVER = '#1d55b9'

    #: Вкладки в порядке навигации.
    TABS = ('Камера', 'Telegram', 'Мониторинг', 'Приложение')

    #: Ограничения компоновки.
    FORM_MAX_WIDTH = 800
    NAV_WIDTH = 168
    LABEL_WIDTH = 190
    FIELD_HEIGHT = 32

    def __init__(self, master, on_close=None, container=None, show_back=None):
        self.on_close = on_close
        self.container = container
        self.stopped = False
        self._closing = False
        self._after_ids = set()
        self._form_width = 0
        self._notes = []
        if container is not None or on_close is not None:
            parent = container if container is not None else master
            root = ctk.CTkFrame(parent, fg_color=self.BG, corner_radius=0)
            if container is None:
                root.pack(fill='both', expand=True)
        else:
            ctk.set_appearance_mode('dark')
            root = master
            root.title('Evrika · Настройки')
            root.configure(fg_color=self.BG)
            root.iconbitmap(str(ASSETS / 'evrika.ico'))
            root.geometry('1000x640')
            root.minsize(820, 520)
        self.root = root
        if show_back is None:
            show_back = on_close is not None and container is None
        self.show_back = show_back
        self.results = queue.Queue()

        current = read_settings()
        current['CAMERA_URL'] = f"http://{current.get('DAHUA_HOST', '192.168.100.249')}:{current.get('DAHUA_PORT', '80')}/"
        self.variables = {key: tk.StringVar(value=current.get(key, default)) for key, _, default in FIELDS}
        self.direction = tk.StringVar(value=next((label for label, value in DIRECTIONS.items()
                                                 if value == current.get('DIRECTION', 'Approach')), 'Оба направления'))
        self.check_buttons = []

        self._build_header()
        self._build_footer()
        self._build_body()
        self.select_tab(self.TABS[0])
        self.schedule(100, self.poll)

    # ------------------------------------------------------------------
    # Компоновка
    # ------------------------------------------------------------------
    def _build_header(self):
        header = ctk.CTkFrame(self.root, fg_color='transparent')
        header.pack(fill='x', padx=22, pady=(14, 4))
        if self.show_back:
            ctk.CTkButton(header, text='‹  Назад', width=85, height=32, fg_color=self.CARD,
                          hover_color='#34455a', command=self.close).pack(side='left', padx=(0, 14))
        ctk.CTkLabel(header, text='Настройки', text_color=self.TEXT,
                     font=('Segoe UI', 20, 'bold'), anchor='w').pack(side='left')

    def _build_footer(self):
        footer = ctk.CTkFrame(self.root, fg_color='#151f2d', corner_radius=0)
        footer.pack(side='bottom', fill='x')
        actions = ctk.CTkFrame(footer, fg_color='transparent')
        actions.pack(fill='x', padx=22, pady=(8, 2))
        self.save_button = ctk.CTkButton(actions, text='Сохранить', command=self.save, width=150,
                                         height=36, fg_color=self.BLUE, hover_color='#1d55b9',
                                         font=('Segoe UI', 12, 'bold'))
        self.save_button.pack(side='right')
        ctk.CTkLabel(actions, text='Настройки камеры применяются после перезапуска сервиса',
                     text_color=self.MUTED, font=('Segoe UI', 11), anchor='w').pack(side='left', fill='x', expand=True)
        self.status = tk.StringVar(value='')
        ctk.CTkLabel(footer, textvariable=self.status, text_color='#b8c9e1', font=('Segoe UI', 11),
                     anchor='w', justify='left').pack(fill='x', padx=22, pady=(0, 8))

    def _build_body(self):
        body = ctk.CTkFrame(self.root, fg_color='transparent')
        body.pack(fill='both', expand=True, padx=22, pady=(4, 8))

        nav = ctk.CTkFrame(body, fg_color='transparent', width=self.NAV_WIDTH)
        nav.pack(side='left', fill='y')
        nav.pack_propagate(False)
        self.nav_buttons = {}
        for title in self.TABS:
            button = ctk.CTkButton(nav, text=title, width=self.NAV_WIDTH - 20, height=36, anchor='w',
                                    fg_color='transparent', hover_color='#263b57', text_color=self.TEXT,
                                    font=('Segoe UI', 12), command=lambda title=title: self.select_tab(title))
            button.pack(anchor='w', pady=2, padx=4)
            self.nav_buttons[title] = button

        # Форма: ширина не больше 800 px, при нехватке места — прокрутка.
        self.scroll = ctk.CTkScrollableFrame(body, fg_color='transparent', corner_radius=0)
        self.scroll.pack(side='left', fill='both', expand=True, padx=(14, 0))
        self.form = ctk.CTkFrame(self.scroll, fg_color='transparent', width=self.FORM_MAX_WIDTH)
        self.form.pack(anchor='w', padx=(6, 6), pady=(2, 8))
        self.form.grid_columnconfigure(0, weight=1)
        self.scroll.bind('<Configure>', self._fit_form)

        self.tab_frames = {}
        for title in self.TABS:
            frame = ctk.CTkFrame(self.form, fg_color='transparent')
            frame.grid(row=0, column=0, sticky='nsew')
            frame.grid_columnconfigure(1, weight=1)
            self.tab_frames[title] = frame

        self._build_camera_tab(self.tab_frames['Камера'])
        self._build_telegram_tab(self.tab_frames['Telegram'])
        self._build_monitoring_tab(self.tab_frames['Мониторинг'])
        self._build_application_tab(self.tab_frames['Приложение'])

    def _build_camera_tab(self, tab):
        self._field(tab, 0, 'Название', 'CAMERA_NAME')
        self._field(tab, 1, 'Адрес камеры', 'CAMERA_URL')
        self._field(tab, 2, 'Логин', 'DAHUA_USERNAME')
        self._field(tab, 3, 'Пароль', 'DAHUA_PASSWORD', secret=True)
        self._check_button(tab, 4, 'Проверить камеру', check_camera)

    def _build_telegram_tab(self, tab):
        self._field(tab, 0, 'Токен бота', 'TELEGRAM_BOT_TOKEN', secret=True)
        self._field(tab, 1, 'Чат уведомлений (ID)', 'TELEGRAM_CHAT_ID')
        self._field(tab, 2, 'Разрешённые чаты (ID)', 'ALLOWED_CHAT_IDS')
        note = ('ID через запятую. Добавьте ID получателя в оба поля; для личного чата напишите боту /start.\n'
                'В группе команды доступны её участникам.')
        self._note(tab, 3, note)
        self._check_button(tab, 4, 'Проверить бота', check_bot)

    def _build_monitoring_tab(self, tab):
        self._field(tab, 0, 'Часовой пояс', 'TIMEZONE')
        ctk.CTkLabel(tab, text='Проезды', text_color=self.TEXT, anchor='w',
                     font=('Segoe UI', 12)).grid(row=1, column=0, sticky='w', padx=(4, 16), pady=6)
        ctk.CTkOptionMenu(tab, variable=self.direction, values=list(DIRECTIONS), height=self.FIELD_HEIGHT,
                          fg_color='#26364c', button_color='#334b6b', button_hover_color='#416087').grid(
                              row=1, column=1, sticky='ew', padx=(0, 4), pady=6)
        ctk.CTkLabel(tab, text='Папка фотографий', text_color=self.TEXT, anchor='w',
                     font=('Segoe UI', 12)).grid(row=2, column=0, sticky='w', padx=(4, 16), pady=(14, 6))
        folder_row = ctk.CTkFrame(tab, fg_color='transparent')
        folder_row.grid(row=2, column=1, sticky='ew', padx=(0, 4), pady=(14, 6))
        ctk.CTkButton(folder_row, text='Обзор…', width=90, height=self.FIELD_HEIGHT,
                      command=self.choose_photo_folder).pack(side='right', padx=(8, 0))
        ctk.CTkEntry(folder_row, textvariable=self.variables['IMAGE_DIR'], height=self.FIELD_HEIGHT,
                     fg_color='#111b29', border_color='#3a4a60').pack(side='left', fill='x', expand=True)
        self._note(tab, 3, 'Фото сохраняются файлами. Новая папка применяется после перезапуска.')
        self._note(tab, 4, 'Настройки хранятся в .env без шифрования.')

    def _build_application_tab(self, tab):
        try:
            autostart_enabled = preferences.get_autostart()
        except OSError:
            autostart_enabled = False
            self.status.set('Не удалось прочитать автозапуск Windows.')
        self.autostart = tk.BooleanVar(value=autostart_enabled)
        self.minimize_to_tray = tk.BooleanVar(value=preferences.get_minimize_to_tray())
        ctk.CTkSwitch(tab, text='Запускать с Windows', variable=self.autostart,
                      progress_color=self.BLUE).grid(row=0, column=0, columnspan=2, sticky='w', padx=4, pady=(18, 10))
        ctk.CTkSwitch(tab, text='Сворачивать в трей кнопкой «—»', variable=self.minimize_to_tray,
                      progress_color=self.BLUE).grid(row=1, column=0, columnspan=2, sticky='w', padx=4, pady=10)
        self._note(tab, 2, 'Кнопка × всегда сворачивает панель в трей.')

    # ------------------------------------------------------------------
    # Вкладки и форма
    # ------------------------------------------------------------------
    def select_tab(self, title):
        """Показать вкладку. Значения полей при этом не меняются."""
        frame = self.tab_frames.get(title)
        if frame is None:
            return
        for name, tab in self.tab_frames.items():
            if tab is frame:
                tab.grid(row=0, column=0, sticky='nsew')
            else:
                tab.grid_remove()
        self.active_tab = title
        for name, button in self.nav_buttons.items():
            active = name == title
            button.configure(fg_color=self.BLUE if active else 'transparent',
                             hover_color=self.BLUE_HOVER if active else '#263b57',
                             text_color='#ffffff' if active else self.TEXT)

    def _fit_form(self, event=None):
        """Ширина формы: не больше 800 px и не шире доступного места.

        Ширина задаётся через `minsize` колонки: иначе форма сжимается до
        ширины полей, а колонка с полями не растягивается на всю форму.
        """
        try:
            if not self.form.winfo_exists():
                return
            available = event.width if event is not None else self.scroll.winfo_width()
            width = max(320, min(self.FORM_MAX_WIDTH, int(available) - 12))
            if width == self._form_width:
                return
            self._form_width = width
            self.form.configure(width=width)
            self.form.grid_columnconfigure(0, weight=1, minsize=width)
            for note in self._notes:
                note.configure(wraplength=max(200, width - self.LABEL_WIDTH - 28))
        except tk.TclError:
            # Виджеты уже уничтожены (окно закрывается) — тихо выходим.
            return

    def _field(self, parent, row, label, key, secret=False):
        ctk.CTkLabel(parent, text=label, text_color=self.TEXT, anchor='w', width=self.LABEL_WIDTH,
                     justify='left', font=('Segoe UI', 12)).grid(row=row, column=0, sticky='w',
                                                                 padx=(4, 16), pady=6)
        ctk.CTkEntry(parent, textvariable=self.variables[key], show='•' if secret else '',
                     height=self.FIELD_HEIGHT, fg_color='#111b29', border_color='#3a4a60',
                     text_color=self.TEXT).grid(row=row, column=1, sticky='ew', padx=(0, 4), pady=6)

    def _note(self, parent, row, text):
        label = ctk.CTkLabel(parent, text=text, text_color=self.MUTED, justify='left', anchor='w',
                             wraplength=520, font=('Segoe UI', 11))
        label.grid(row=row, column=0, columnspan=2, sticky='ew', padx=4, pady=(8, 0))
        self._notes.append(label)
        return label

    def _check_button(self, parent, row, label, function):
        button = ctk.CTkButton(parent, text=label, command=lambda: self.check(function),
                               height=self.FIELD_HEIGHT, width=170, fg_color='#283a53', hover_color='#365172',
                               border_width=1, border_color='#405471')
        button.grid(row=row, column=1, sticky='e', padx=(0, 4), pady=(12, 8))
        self.check_buttons.append(button)

    def choose_photo_folder(self):
        selected = filedialog.askdirectory(parent=self.root.winfo_toplevel(), title='Папка фотографий',
                                           initialdir=self.variables['IMAGE_DIR'].get() or str(BASE))
        if selected:
            self.variables['IMAGE_DIR'].set(selected)

    # ------------------------------------------------------------------
    # Данные и сохранение
    # ------------------------------------------------------------------
    def values(self):
        return {**{key: var.get().strip() for key, var in self.variables.items()}, 'DIRECTION': DIRECTIONS[self.direction.get()]}

    def save(self):
        """Сохранить настройки.

        На вкладке «Приложение» сохраняются параметры приложения, на
        остальных — данные камеры и бота (.env). Формат .env не меняется.
        """
        if self.active_tab == 'Приложение':
            self.save_application()
            return
        try:
            save_settings(self.values())
            self.status.set('Настройки сохранены. Перезапустите сервис, чтобы применить изменения.')
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

    # ------------------------------------------------------------------
    # Жизненный цикл
    # ------------------------------------------------------------------
    def poll(self):
        if self.stopped or self._closing:
            return
        try:
            self.status.set(self.results.get_nowait())
            for button in self.check_buttons:
                button.configure(state='normal')
        except queue.Empty:
            pass
        self.schedule(100, self.poll)

    def schedule(self, delay, callback):
        """Запланировать отложенный вызов с учётом его идентификатора."""
        if self._closing:
            return None
        holder = {}

        def run():
            identifier = holder.get('id')
            if identifier is not None:
                self._after_ids.discard(identifier)
            if self._closing:
                return
            callback()
        try:
            identifier = self.root.after(delay, run)
        except tk.TclError:
            return None
        holder['id'] = identifier
        self._after_ids.add(identifier)
        return identifier

    def shutdown(self):
        """Отменить отложенные вызовы перед закрытием приложения."""
        self.stopped = True
        self._closing = True
        for identifier in list(self._after_ids):
            try:
                self.root.after_cancel(identifier)
            except Exception:
                pass
        self._after_ids.clear()
        cancel_pending_scripts(self.root)

    def close(self):
        self.shutdown()
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        if self.on_close:
            self.on_close()


#: Прежнее имя класса окна настроек — тот же раздел в новом окне.
SettingsWindow = SettingsSection


def main() -> None:
    """Запуск окна настроек. Вызывается корневым лаунчером setup_anpr.py."""
    root = ctk.CTk()
    SettingsSection(root)
    root.mainloop()


if __name__ == '__main__':
    main()
