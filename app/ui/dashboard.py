"""Native, read-only monitoring window for the local ANPR database and camera."""
import os
import math
import io
import queue
import sqlite3
import sys
import threading
import time
import calendar
import tkinter as tk
from datetime import date, datetime
from contextlib import closing
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import customtkinter as ctk
from PIL import Image, ImageOps
from app.ui.settings import read_settings
import requests

from app.core.paths import PROJECT_ROOT

BASE = PROJECT_ROOT
BG = '#101722'; CARD = '#1a2432'; BORDER = '#303e51'; TEXT = '#f2f5fa'; MUTED = '#a5b1c2'; BLUE = '#377df1'


def direction(value):
    raw = str(value or '').strip().lower()
    if raw in {'approach', 'approaching', 'приближается', 'приближение', 'obverse', 'обverse', 'in', 'incoming', 'въезд', 'входящий', 'entry', 'enter', 'head', '0'}:
        return 'Въезд'
    if raw in {'leave', 'leaving', 'удаляется', 'удаление', 'reverse', 'out', 'outgoing', 'выезд', 'исходящий', 'exit', 'tail', '1'}:
        return 'Выезд'
    return 'Неизвестно'


def normalize_plate(value):
    """Match a plate without being sensitive to case or separators."""
    return ''.join(char for char in str(value or '').upper() if char.isalnum())


def open_database(path):
    """Открыть базу только для чтения; при неудаче — обычным соединением.

    Режим mode=ro не может восстановить WAL, если служебные файлы недоступны,
    поэтому мониторинг не должен отказывать из-за служебной ошибки SQLite.
    """
    resolved = Path(path).resolve()
    uri = resolved.as_uri() + '?mode=ro'
    try:
        return sqlite3.connect(uri, uri=True, timeout=2)
    except sqlite3.Error:
        return sqlite3.connect(str(resolved), timeout=2)


def load_dashboard(path, day, end_day=None, plate_query=''):
    end_day = end_day or day
    if end_day < day:
        raise ValueError('Invalid date range')
    with closing(open_database(path)) as conn:
        rows = conn.execute('SELECT id, captured_at, plate, direction, backfill FROM events WHERE day BETWEEN ? AND ? ORDER BY captured_at DESC, id DESC', (day, end_day)).fetchall()
    hours = [0] * 24
    counts = {'Въезд': 0, 'Выезд': 0, 'Неизвестно': 0}
    unique = set()
    for _, captured, plate, direct, _ in rows:
        try: hours[datetime.fromisoformat(captured).hour] += 1
        except (ValueError, TypeError): pass
        counts[direction(direct)] += 1
        if plate and plate not in ('—', 'UNKNOWN', 'unknown'): unique.add(plate)
    query = normalize_plate(plate_query)
    recent = [row for row in rows if query in normalize_plate(row[2])] if query else rows
    return {'total': len(rows), 'unique': len(unique), 'hours': hours, 'directions': counts, 'recent': recent}


def aggregate_hours(hours, interval, current_hour):
    """Group hourly counts into fixed bins through the specified hour."""
    if interval not in (1, 3, 6) or len(hours) != 24 or not 0 <= current_hour <= 23:
        raise ValueError('Invalid chart inputs')
    return [(start, sum(hours[start:start + interval]))
            for start in range(0, current_hour + 1, interval)]


def chart_last_hour(selected_day, now, end_day=None):
    end_day = end_day or selected_day
    return now.hour if selected_day is None or selected_day == end_day == now.date() else 23


def archived_photo(path, event_id):
    """Read a locally archived photo without requiring the service to be running."""
    try:
        with closing(open_database(path)) as conn:
            row = conn.execute('''SELECT p.path FROM events e
                                  JOIN passage_photo_paths p ON p.captured_at=e.captured_at AND p.plate=e.plate
                                  WHERE e.id=?''', (event_id,)).fetchone()
        return Path(row[0]).read_bytes() if row and row[0] else None
    except (OSError, sqlite3.Error):
        return None


class MonitoringSection:
    """Раздел «Мониторинг»: видео, показатели, статистика и лента проездов.

    Раздел живёт всё время работы приложения: переключение на другой
    раздел не пересоздаёт ни видеопоток, ни фоновые задачи.
    """

    def __init__(self, master, on_close=None, show_back=True, service_hook=None, embedded=False):
        self.root = ctk.CTkFrame(master, fg_color=BG, corner_radius=0)
        if not embedded:
            self.root.pack(fill='both', expand=True)
        self.on_close = on_close
        self.show_back = show_back
        self.service_hook = service_hook
        # Пауза показа видео (сворачивание в трей) — отдельно от того,
        # что пользователь отключил видео вручную.
        self.paused = threading.Event()
        self.threads = []
        self.stop = threading.Event(); self.frames = queue.Queue(maxsize=1); self.results = queue.Queue(maxsize=1)
        # None tracks the current day, including the rollover at midnight.
        self.period_selection = (0, None)
        self.search_selection = (0, '')
        self.search_after = None
        self.display_period = None
        self.data_changed = threading.Event()
        self.visible_count = 30
        self.reset_feed_scroll = False
        self.date_window = None
        self.video_size = (640, 360)
        self.image = None
        self.stream_selection = (0, 0, True)
        self.stream_changed = threading.Event()
        self.fullscreen_window = None
        self.fullscreen_label = None
        self.fullscreen_image = None
        self.photo_results = queue.Queue(maxsize=60)
        self.photo_bytes = {}
        self.photo_buttons = {}
        self.settings = read_settings()
        try: self.tz = ZoneInfo(self.settings.get('TIMEZONE', 'Asia/Tashkent'))
        except (ZoneInfoNotFoundError, ValueError): self.tz = None
        root = self.root
        header = ctk.CTkFrame(root, fg_color='transparent'); header.pack(fill='x', padx=22, pady=(15, 8))
        if self.show_back:
            ctk.CTkButton(header, text='‹  Назад', width=85, height=32, fg_color=CARD,
                          hover_color='#34455a', command=self.close).pack(side='left', padx=(0, 14))
        ctk.CTkLabel(header, text='Мониторинг', font=('Segoe UI', 22, 'bold'), text_color=TEXT).pack(side='left')
        # Rasterized from filled/calendar-month.svg; Pillow handles the PNG at runtime.
        self.calendar_icon = ctk.CTkImage(light_image=Image.open(BASE / 'assets' / 'calendar-month-filled.png'), size=(16, 16))
        self.date_button = ctk.CTkButton(header, text='Сегодня  ▾', image=self.calendar_icon,
                                         compound='left', width=140, height=32,
                                         fg_color=CARD, hover_color='#34455a', text_color=TEXT,
                                         font=('Segoe UI', 11, 'bold'), command=self.open_date_picker)
        self.date_button.pack(side='right')
        layout = ctk.CTkFrame(root, fg_color='transparent'); layout.pack(fill='both', expand=True, padx=22, pady=(0, 20))
        layout.grid_columnconfigure(0, weight=3, uniform='column'); layout.grid_columnconfigure(1, weight=2, uniform='column')
        layout.grid_rowconfigure(0, weight=3, uniform='monitor_row')
        layout.grid_rowconfigure(1, weight=2, uniform='monitor_row')
        video = self.card(layout, 0, 0, padx=(0, 7), pady=(0, 7))
        video.pack_propagate(False)
        video_header = ctk.CTkFrame(video, fg_color='transparent')
        video_header.pack(fill='x', padx=10, pady=(10, 8))
        # Rasterized from the project's filled/video.svg; no SVG runtime dependency.
        self.live_icon = ctk.CTkImage(light_image=Image.open(BASE / 'assets' / 'live-video-filled.png'), size=(18, 18))
        ctk.CTkLabel(video_header, text='', image=self.live_icon, width=32, height=32,
                     corner_radius=8, fg_color='#263b57').pack(side='left', padx=(0, 8))
        ctk.CTkLabel(video_header, text='Прямой эфир', text_color=TEXT,
                     font=('Segoe UI', 12, 'bold'), anchor='w').pack(side='left')
        self.video_status = ctk.CTkLabel(video_header, text='● Подключение', text_color=MUTED,
                                         font=('Segoe UI', 10))
        self.video_status.pack(side='left', padx=(10, 0))
        self.video_enabled = ctk.CTkSwitch(video_header, text='Видео', width=65, height=24,
                                           switch_width=30, switch_height=16,
                                           font=('Segoe UI', 10), progress_color=BLUE,
                                           command=self.toggle_video)
        self.video_enabled.select()
        self.video_enabled.pack(side='right', padx=(8, 0))
        self.stream_switch = ctk.CTkSegmentedButton(video_header, values=['Основной', 'Дополнительный'],
                                                     command=self.change_stream, height=26,
                                                     selected_color=BLUE, selected_hover_color='#2869df',
                                                     font=('Segoe UI', 10))
        self.stream_switch.set('Основной')
        self.stream_switch.pack(side='right')
        self.video_label = ctk.CTkLabel(video, text='Подключение к камере…', text_color=MUTED, fg_color='#090e16')
        self.video_label.pack(fill='both', expand=True, padx=10, pady=(0, 10))
        self.video_label.bind('<Double-Button-1>', self.open_video_fullscreen)
        # Запуск и остановка ANPR-мониторинга — внутри блока камеры.
        anpr = ctk.CTkFrame(video, fg_color='transparent')
        anpr.pack(fill='x', padx=10, pady=(0, 10))
        self.service_button = ctk.CTkButton(anpr, text='Запустить', width=118, height=30,
                                             fg_color='#2869df', hover_color='#1d55b9',
                                             text_color=TEXT, font=('Segoe UI', 11, 'bold'),
                                             command=self._toggle_service)
        self.service_button.pack(side='right')
        ctk.CTkLabel(anpr, text='ANPR-мониторинг', text_color=MUTED, font=('Segoe UI', 11), anchor='w').pack(side='left')
        right = ctk.CTkFrame(layout, fg_color='transparent')
        right.grid(row=0, column=1, rowspan=2, sticky='nsew', padx=(7, 0))
        right.grid_columnconfigure((0, 1), weight=1, uniform='metric')
        right.grid_rowconfigure(2, weight=1)
        # PNGs are rasterized from the matching local filled SVG icons.
        self.metric_icons = {}
        for name in ('entry', 'exit', 'passages', 'plates'):
            with Image.open(BASE / 'assets' / f'metric-{name}.png') as source:
                self.metric_icons[name] = ctk.CTkImage(light_image=source.copy(), size=(20, 20))
        self.entry_metric = self.metric(right, 'Въезд', 'entry', 0, 0)
        self.exit_metric = self.metric(right, 'Выезд', 'exit', 0, 1)
        self.total = self.metric(right, 'Всего проездов', 'passages', 1, 0)
        self.unique = self.metric(right, 'Уникальные номера', 'plates', 1, 1)
        feed = ctk.CTkFrame(right, fg_color=CARD, border_color=BORDER, border_width=1, corner_radius=10)
        feed.grid(row=2, column=0, columnspan=2, sticky='nsew', pady=(4, 0))
        feed_header = ctk.CTkFrame(feed, fg_color='transparent')
        feed_header.pack(fill='x', padx=12, pady=(12, 8))
        ctk.CTkLabel(feed_header, text='', image=self.metric_icons['passages'], width=36, height=36,
                     corner_radius=9, fg_color='#263b57').pack(side='left', padx=(0, 10))
        ctk.CTkLabel(feed_header, text='Последние проезды', font=('Segoe UI', 14, 'bold'),
                     text_color=TEXT, anchor='w').pack(side='left', fill='x', expand=True)
        search_row = ctk.CTkFrame(feed, fg_color='#111b29', border_width=1,
                                  border_color='#34455a', corner_radius=8)
        search_row.pack(fill='x', padx=12, pady=(0, 6))
        # Rasterized from the local filled/search.svg.
        with Image.open(BASE / 'assets' / 'search-filled.png') as source:
            self.search_icon = ctk.CTkImage(light_image=source.copy(), size=(16, 16))
        ctk.CTkLabel(search_row, text='', image=self.search_icon, width=18, height=32).pack(side='left', padx=(10, 5))
        self.search_entry = ctk.CTkEntry(search_row, placeholder_text='Поиск по номеру',
                                         fg_color='#111b29', border_width=0, height=32,
                                         text_color=TEXT, placeholder_text_color=MUTED, font=('Segoe UI', 11))
        ctk.CTkButton(search_row, text='✕', width=30, height=28, fg_color='transparent',
                      hover_color='#263b57', text_color=MUTED, font=('Segoe UI', 13),
                      command=self.clear_search).pack(side='right', padx=(0, 5))
        self.search_entry.pack(side='left', fill='x', expand=True)
        self.search_entry.bind('<KeyRelease>', self.schedule_search)
        self.search_entry.bind('<<Paste>>', lambda _event: self.root.after_idle(self.schedule_search))
        self.search_entry.bind('<<Cut>>', lambda _event: self.root.after_idle(self.schedule_search))
        self.search_entry.bind('<Escape>', lambda _event: self.clear_search())
        self.feed = ctk.CTkScrollableFrame(feed, fg_color='transparent')
        self.feed.pack(fill='both', expand=True, padx=4, pady=(0, 8))
        self.feed.grid_columnconfigure(0, weight=1)
        graph = self.card(layout, 1, 0, padx=(0, 7), pady=(7, 0))
        graph.pack_propagate(False)
        self.hours = [0] * 24
        self.chart_interval = tk.StringVar(value='1 ч')
        graph_header = ctk.CTkFrame(graph, fg_color='transparent')
        graph_header.pack(fill='x', padx=12, pady=(12, 4))
        # Rasterized from the local filled/chart-area-line.svg.
        with Image.open(BASE / 'assets' / 'chart-statistics.png') as source:
            self.chart_icon = ctk.CTkImage(light_image=source.copy(), size=(20, 20))
        ctk.CTkLabel(graph_header, text='', image=self.chart_icon, width=36, height=36,
                     corner_radius=9, fg_color='#263b57').pack(side='left', padx=(0, 10))
        ctk.CTkLabel(graph_header, text='Статистика транспортных средств', text_color=TEXT,
                     font=('Segoe UI', 14, 'bold'), anchor='w').pack(side='left', fill='x', expand=True)
        ctk.CTkOptionMenu(graph_header, variable=self.chart_interval, values=['1 ч', '3 ч', '6 ч'],
                          command=lambda _: self.draw_chart(), width=75, height=28,
                          fg_color='#26364c', button_color='#334b6b', button_hover_color='#416087').pack(side='right')
        ctk.CTkLabel(graph_header, text='Интервал', text_color=MUTED, font=('Segoe UI', 11)).pack(side='right', padx=(0, 8))
        self.chart_note = ctk.CTkLabel(graph, text='По часам за весь период', text_color=MUTED,
                                       font=('Segoe UI', 10), anchor='w', height=18)
        self.graph = tk.Canvas(graph, bg=CARD, highlightthickness=0)
        self.graph.pack(fill='both', expand=True, padx=12, pady=(4, 12))
        from tkinter import font as tkfont
        self.chart_axis_font = tkfont.Font(root=self.graph, family='Segoe UI', size=9)
        self.graph.bind('<Configure>', lambda _: self.draw_chart())
        self._feed_content = None
        self.threads = [threading.Thread(target=self.data_worker, daemon=True),
                        threading.Thread(target=self.video_worker, daemon=True)]
        for thread in self.threads:
            thread.start()
        root.after(100, self.poll)

    def card(self, parent, row, col, **kwargs):
        frame=ctk.CTkFrame(parent, fg_color=CARD, border_color=BORDER, border_width=1, corner_radius=10); frame.grid(row=row, column=col, sticky='nsew', **kwargs); return frame

    def today(self):
        return datetime.now(self.tz).date()

    def select_period(self, start, end):
        if start > end or end > self.today():
            return
        generation, _ = self.period_selection
        self.period_selection = (generation + 1, None if start == end == self.today() else (start, end))
        self.display_period = (start, end)
        self.visible_count = 30
        self.reset_feed_scroll = True
        self._feed_content = None
        self.photo_bytes = {}
        if start == end:
            label = 'Сегодня  ▾' if start == self.today() else start.strftime('%d.%m.%Y  ▾')
        else:
            label = f'{start:%d.%m.%Y} – {end:%d.%m.%Y}  ▾'
        self.date_button.configure(text=label, width=215 if start != end else 140)
        if start == end:
            self.chart_note.pack_forget()
        else:
            self.chart_note.pack(fill='x', padx=(58, 12), before=self.graph)
        for metric in (self.total, self.unique, self.entry_metric, self.exit_metric):
            metric.configure(text='—')
        self.hours = [0] * 24
        self.draw_chart()
        self.render_passages(None)
        self.data_changed.set()
        if self.date_window is not None:
            self.date_window.destroy()
            self.date_window = None

    def open_date_picker(self):
        if self.date_window is not None:
            self.date_window.lift()
            return
        self.calendar_start, self.calendar_end = self.period_selection[1] or (self.today(), self.today())
        self.calendar_selecting_end = False
        self.calendar_year, self.calendar_month = self.calendar_end.year, self.calendar_end.month
        window = ctk.CTkToplevel(self.root)
        self.date_window = window
        window.title('Выбор периода')
        if sys.platform == 'win32':
            # CTkToplevel installs its own icon after 200 ms.
            window.after(250, lambda: window.iconbitmap(str(BASE / 'assets' / 'calendar-month-filled.ico')))
        else:
            import tkinter as tk
            window.calendar_icon = tk.PhotoImage(file=str(BASE / 'assets' / 'calendar-month-filled.png'))
            window.iconphoto(False, window.calendar_icon)
        window.configure(fg_color=BG)
        window.resizable(False, False)
        window.transient(self.root.winfo_toplevel())
        x = max(0, self.date_button.winfo_rootx() + self.date_button.winfo_width() - 310)
        window.geometry(f'310x375+{x}+{self.date_button.winfo_rooty() + 36}')
        def close_picker(_event=None):
            self.date_window = None
            window.destroy()
        window.protocol('WM_DELETE_WINDOW', close_picker)
        window.bind('<Escape>', close_picker)
        self.calendar_body = ctk.CTkFrame(window, fg_color='transparent')
        self.calendar_body.pack(fill='both', expand=True, padx=12, pady=10)
        self.draw_calendar()
        window.after_idle(lambda: (window.lift(), window.focus_force()))

    def draw_calendar(self):
        for child in self.calendar_body.winfo_children():
            child.destroy()
        months = ('Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
                  'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь')
        top = ctk.CTkFrame(self.calendar_body, fg_color='transparent')
        top.pack(fill='x', pady=(0, 6))
        ctk.CTkButton(top, text='‹', width=34, height=30, fg_color=CARD,
                      command=lambda: self.move_calendar(-1)).pack(side='left')
        ctk.CTkButton(top, text='›', width=34, height=30, fg_color=CARD,
                      command=lambda: self.move_calendar(1)).pack(side='right')
        ctk.CTkLabel(top, text=f'{months[self.calendar_month - 1]} {self.calendar_year}',
                     font=('Segoe UI', 13, 'bold'), text_color=TEXT).pack(expand=True)
        grid = ctk.CTkFrame(self.calendar_body, fg_color='transparent')
        grid.pack(fill='both', expand=True)
        for column, weekday in enumerate(('Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс')):
            grid.grid_columnconfigure(column, weight=1, uniform='day')
            ctk.CTkLabel(grid, text=weekday, height=25, text_color=MUTED,
                         font=('Segoe UI', 10)).grid(row=0, column=column, sticky='ew')
        for row, week in enumerate(calendar.monthcalendar(self.calendar_year, self.calendar_month), start=1):
            for column, day in enumerate(week):
                if day == 0:
                    continue
                value = date(self.calendar_year, self.calendar_month, day)
                color = BLUE if value in (self.calendar_start, self.calendar_end) else (
                    '#263f64' if self.calendar_start < value < self.calendar_end else CARD)
                ctk.CTkButton(grid, text=str(day), width=34, height=27, corner_radius=6,
                              fg_color=color,
                              hover_color='#344e70', text_color=TEXT,
                              state='disabled' if value > self.today() else 'normal',
                              command=lambda value=value: self.pick_calendar_day(value)).grid(row=row, column=column, padx=2, pady=2)
        label = ('Выберите конец периода' if self.calendar_selecting_end else
                 f'{self.calendar_start:%d.%m.%Y} – {self.calendar_end:%d.%m.%Y} · Выберите даты')
        ctk.CTkLabel(self.calendar_body, text=label, height=23, text_color=MUTED,
                     font=('Segoe UI', 10)).pack(fill='x')
        actions = ctk.CTkFrame(self.calendar_body, fg_color='transparent')
        actions.pack(fill='x', pady=(4, 0))
        ctk.CTkButton(actions, text='Сегодня', width=110, height=30, fg_color='#263b57',
                      command=lambda: self.select_period(self.today(), self.today())).pack(side='left')
        ctk.CTkButton(actions, text='Применить', width=150, height=30, fg_color=BLUE,
                      command=lambda: self.select_period(self.calendar_start, self.calendar_end)).pack(side='right')

    def pick_calendar_day(self, value):
        if self.calendar_selecting_end:
            self.calendar_start, self.calendar_end = sorted((self.calendar_start, value))
            self.calendar_selecting_end = False
        else:
            self.calendar_start = self.calendar_end = value
            self.calendar_selecting_end = True
        self.draw_calendar()

    def move_calendar(self, offset):
        index = self.calendar_year * 12 + self.calendar_month - 1 + offset
        year, month = divmod(index, 12)
        if not 1 <= year <= 9999 or (year, month + 1) > (self.today().year, self.today().month):
            return
        self.calendar_year, self.calendar_month = year, month + 1
        self.draw_calendar()

    def metric(self, parent, title, icon, row, column):
        frame = ctk.CTkFrame(parent, fg_color=CARD, border_color=BORDER, border_width=1, corner_radius=10)
        frame.grid(row=row, column=column, sticky='nsew', padx=(0, 5) if column == 0 else (5, 0), pady=(0, 8))
        ctk.CTkLabel(frame, text='', image=self.metric_icons[icon], width=38, height=38,
                     corner_radius=9, fg_color='#263b57').pack(side='left', padx=(12, 10), pady=10)
        box = ctk.CTkFrame(frame, fg_color='transparent'); box.pack(side='left', fill='x', expand=True, padx=(0, 8), pady=8)
        caption = ctk.CTkLabel(box, text=title, text_color='#b5c2d4', font=('Segoe UI', 11),
                               anchor='w', justify='left', height=18)
        caption.pack(fill='x')
        def fit_caption(event):
            width = max(30, event.width - 2)
            if caption.cget('wraplength') != width:
                caption.configure(wraplength=width)
        box.bind('<Configure>', fit_caption)
        label = ctk.CTkLabel(box, text='—', text_color=TEXT, font=('Segoe UI', 23, 'bold'), anchor='w', height=28)
        label.pack(fill='x')
        return label

    def schedule_search(self, _event=None):
        if self.search_after is not None:
            self.root.after_cancel(self.search_after)
        self.search_after = self.root.after(250, self.apply_search)

    def clear_search(self):
        self.search_entry.delete(0, tk.END)
        self.schedule_search()

    def apply_search(self):
        self.search_after = None
        generation, previous = self.search_selection
        query = normalize_plate(self.search_entry.get())
        if query == previous:
            return
        self.search_selection = (generation + 1, query)
        self.visible_count = 30
        self.reset_feed_scroll = True
        self._feed_content = None
        self.photo_bytes = {}
        self.render_passages(None)
        self.data_changed.set()

    def render_passages(self, rows, error=False):
        for child in self.feed.winfo_children(): child.destroy()
        self.photo_buttons = {}
        if rows is None:
            ctk.CTkLabel(self.feed, text='Не удалось загрузить данные' if error else 'Загрузка…',
                         text_color=MUTED).grid(row=0, column=0, pady=12)
            return
        for index, (event_id, captured, plate, direct, archive) in enumerate(rows[:self.visible_count]):
            try: stamp = datetime.fromisoformat(captured).strftime('%d.%m.%Y  %H:%M:%S')
            except (ValueError, TypeError): continue
            card = ctk.CTkFrame(self.feed, fg_color='#202d40', corner_radius=8)
            card.grid(row=index, column=0, sticky='ew', padx=3, pady=4)
            button = ctk.CTkButton(card, text='Нет\nфото', width=86, height=72,
                                   fg_color='#34445a', hover_color='#41536c',
                                   text_color=MUTED, font=('Segoe UI', 10), state='disabled')
            button.pack(side='left', padx=8, pady=8)
            self.photo_buttons[event_id] = button
            details = ctk.CTkFrame(card, fg_color='transparent')
            details.pack(side='left', fill='x', expand=True, padx=(0, 8), pady=6)
            ctk.CTkLabel(details, text='№  ' + (plate or '—'), text_color=TEXT, anchor='w',
                         font=('Segoe UI', 13, 'bold')).pack(fill='x')
            ctk.CTkLabel(details, text='◷  ' + stamp, text_color=MUTED, anchor='w',
                         font=('Segoe UI', 11)).pack(fill='x')
            state = direction(direct) + ('  ·  Архив' if archive else '')
            ctk.CTkLabel(details, text=state, text_color='#8ebaff', anchor='w',
                         font=('Segoe UI', 11)).pack(fill='x')
            if event_id in self.photo_bytes: self.set_photo(event_id, self.photo_bytes[event_id])
        if not rows:
            message = 'По номеру ничего не найдено' if self.search_selection[1] else 'Проездов за выбранный период нет'
            ctk.CTkLabel(self.feed, text=message, text_color=MUTED).grid(row=0, column=0, pady=12)
        elif len(rows) > self.visible_count:
            ctk.CTkButton(self.feed, text='Показать ещё', height=30, fg_color='#263b57',
                          command=self.show_more_passages).grid(row=self.visible_count, column=0, sticky='ew', padx=4, pady=8)

    def show_more_passages(self):
        self.visible_count += 30
        self.render_passages(self._feed_content or ())
        self.data_changed.set()

    def set_photo(self, event_id, data):
        button = self.photo_buttons.get(event_id)
        if not button: return
        try:
            with Image.open(io.BytesIO(data)) as source:
                preview = ImageOps.contain(source.convert('RGB'), (86, 72), Image.Resampling.LANCZOS)
            image = ctk.CTkImage(light_image=preview, dark_image=preview, size=preview.size)
            button.configure(text='', image=image, state='normal', command=lambda: self.open_photo(data))
            button.photo = image
        except (OSError, ValueError):
            return

    def open_photo(self, data):
        try:
            with Image.open(io.BytesIO(data)) as source: image = source.convert('RGB')
        except (OSError, ValueError): return
        window = ctk.CTkToplevel(self.root)
        window.title('Фото проезда'); window.configure(fg_color='#090e16')
        window.transient(self.root.winfo_toplevel())
        window.attributes('-fullscreen', True)
        window.lift()
        window.after_idle(lambda: (window.lift(), window.focus_force(), window.grab_set()))
        window.bind('<Escape>', lambda _: window.destroy())
        ctk.CTkButton(window, text='Закрыть  ✕', width=110, command=window.destroy).pack(anchor='ne', padx=16, pady=12)
        label = ctk.CTkLabel(window, text=''); label.pack(fill='both', expand=True, padx=12, pady=(0, 12))
        def resize(_=None):
            w, h = label.winfo_width(), label.winfo_height()
            if w < 2 or h < 2: return
            fitted = ImageOps.contain(image, (w, h), Image.Resampling.LANCZOS)
            label.photo = ctk.CTkImage(light_image=fitted, dark_image=fitted, size=fitted.size)
            label.configure(image=label.photo)
        label.bind('<Configure>', resize)

    def data_worker(self):
        checked_at = {}
        checked_generation = None
        with requests.Session() as session:
            session.trust_env = False
            while not self.stop.is_set():
                self.data_changed.clear()
                generation, selected = self.period_selection
                search_generation, query = self.search_selection
                today = datetime.now(self.tz).date()
                start, end = selected or (today, today)
                if checked_generation != (generation, search_generation):
                    checked_at = {}
                    checked_generation = (generation, search_generation)
                try:
                    data = load_dashboard(BASE / 'anpr.sqlite3', start.isoformat(), end.isoformat(), query)
                    try: self.results.get_nowait()
                    except queue.Empty: pass
                    self.results.put_nowait((generation, search_generation, (start, end), 'data', data))
                    now = time.monotonic()
                    current_ids = [row[0] for row in data['recent'][:self.visible_count]]
                    current_id_set = set(current_ids)
                    checked_at = {key: value for key, value in checked_at.items() if key in current_id_set}
                    for event_id in current_ids:
                        if (self.stop.is_set() or generation != self.period_selection[0]
                                or search_generation != self.search_selection[0]): break
                        if event_id in self.photo_bytes or now - checked_at.get(event_id, -100) < 5: continue
                        checked_at[event_id] = now
                        try:
                            photo = archived_photo(BASE / 'anpr.sqlite3', event_id)
                            if photo is None:
                                response = session.get(f'http://127.0.0.1:18749/passages/{event_id}/photo', timeout=1)
                                if response.ok and response.headers.get('Content-Type', '').startswith('image/'):
                                    photo = response.content
                            if photo:
                                self.photo_results.put_nowait((generation, search_generation, event_id, photo))
                        except (requests.RequestException, queue.Full): pass
                except (OSError, sqlite3.Error):
                    try:
                        try: self.results.get_nowait()
                        except queue.Empty: pass
                        self.results.put_nowait((generation, search_generation, (start, end), 'error', None))
                    except queue.Full: pass
                self.data_changed.wait(2)

    def change_stream(self, choice):
        subtype = {'Основной': 0, 'Дополнительный': 1}[choice]
        generation, current, enabled = self.stream_selection
        if current == subtype:
            return
        self.stream_selection = (generation + 1, subtype, enabled)
        self.stream_changed.set()
        self.set_video_status('connecting')
        self.show_video_message('Переключение потока…')

    def toggle_video(self):
        generation, subtype, _ = self.stream_selection
        enabled = bool(self.video_enabled.get())
        self.stream_selection = (generation + 1, subtype, enabled)
        self.stream_changed.set()
        self.stream_switch.configure(state='normal' if enabled else 'disabled')
        self.set_video_status('connecting' if enabled else 'disabled')
        self.show_video_message('Подключение к камере…' if enabled else 'Видео выключено')

    def _toggle_service(self):
        """Кнопка ANPR-мониторинга в блоке камеры."""
        if self.service_hook is not None:
            self.service_hook()

    def pause_video(self):
        """Приостановить показ видео (окно свёрнуто в трей).

        Соединение закрывается, ANPR-мониторинг при этом продолжает
        работать: события камеры обрабатывает сервис, а не это окно.
        """
        self.paused.set()
        self.stream_changed.set()
        switch = getattr(self, 'video_enabled', None)
        if switch is not None and switch.get():
            self.set_video_status('paused')

    def resume_video(self):
        """Снять автоматическую паузу (окно вернулось из трея).

        Ручное отключение видео пользователем — отдельное состояние: поток
        продолжит молчать, пока переключатель «Видео» выключен.
        """
        self.paused.clear()
        self.stream_changed.set()

    def shutdown(self):
        """Остановить фоновые потоки раздела перед закрытием приложения."""
        self.stop.set()
        self.stream_changed.set()
        self.data_changed.set()
        for thread in self.threads:
            thread.join(2)

    def set_video_status(self, state):
        text, color = {
            'connecting': ('● Подключение', MUTED),
            'online': ('● Онлайн', '#54d49a'),
            'offline': ('● Офлайн', '#ec8181'),
            'disabled': ('● Выключен', MUTED),
            'paused': ('● Пауза', MUTED),
        }[state]
        self.video_status.configure(text=text, text_color=color)

    def show_video_message(self, message):
        # Clear Tk's image before dropping the CTkImage reference.
        self.video_label._label.configure(image='')
        self.video_label.configure(image=None, text=message)
        self.image = None
        if self.fullscreen_label is not None:
            self.fullscreen_label._label.configure(image='')
            self.fullscreen_label.configure(image=None, text=message)
            self.fullscreen_image = None

    def open_video_fullscreen(self, _event=None):
        if not self.stream_selection[2] or self.fullscreen_window is not None:
            return
        window = ctk.CTkToplevel(self.root)
        self.fullscreen_window = window
        window.title('Прямой эфир')
        window.configure(fg_color='#090e16')
        window.transient(self.root.winfo_toplevel())
        window.attributes('-fullscreen', True)
        window.protocol('WM_DELETE_WINDOW', self.close_video_fullscreen)
        window.bind('<Escape>', self.close_video_fullscreen)
        ctk.CTkButton(window, text='Закрыть  ✕', width=110,
                      command=self.close_video_fullscreen).pack(anchor='ne', padx=16, pady=(12, 0))
        self.fullscreen_label = ctk.CTkLabel(window, text='Подключение к камере…',
                                             text_color=MUTED, fg_color='#090e16')
        self.fullscreen_label.pack(fill='both', expand=True, padx=12, pady=12)
        self.fullscreen_label.bind('<Double-Button-1>', self.close_video_fullscreen)
        window.after_idle(lambda: (window.lift(), window.focus_force()))

    def close_video_fullscreen(self, _event=None):
        window = self.fullscreen_window
        self.fullscreen_window = None
        self.fullscreen_label = None
        if window is not None:
            window.destroy()
        self.fullscreen_image = None

    def video_worker(self):
        os.environ.setdefault('OPENCV_LOG_LEVEL', 'SILENT')
        os.environ.setdefault('OPENCV_FFMPEG_LOGLEVEL', '-8')
        try: import cv2
        except ImportError:
            self.frames.put((self.stream_selection[0], 'error', 'Видеомодуль недоступен')); return
        host=self.settings.get('DAHUA_HOST', '')
        user=quote(self.settings.get('DAHUA_USERNAME', ''), safe=''); password=quote(self.settings.get('DAHUA_PASSWORD', ''), safe='')
        channel=self.settings.get('DAHUA_CHANNEL', '1')
        # Пауза показа видео не смешана с ручным отключением: поток просто
        # ждёт возобновления и не открывает второе соединение.
        paused = getattr(self, 'paused', None)
        while not self.stop.is_set():
            self.stream_changed.clear()
            generation, subtype, enabled = self.stream_selection
            if not enabled or (paused is not None and paused.is_set()):
                self.stream_changed.wait()
                continue
            url=f'rtsp://{user}:{password}@{host}:554/cam/realmonitor?channel={channel}&subtype={subtype}'
            capture = cv2.VideoCapture()
            try:
                capture.open(url, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000])
                if not capture.isOpened(): raise OSError('video unavailable')
                last_published = 0.0
                while not self.stop.is_set() and self.stream_selection[0] == generation:
                    if paused is not None and paused.is_set():
                        break
                    ok, frame=capture.read()
                    if not ok: break
                    now = time.monotonic()
                    if now - last_published < 1 / 20:
                        continue
                    last_published = now
                    target_w, target_h = self.video_size
                    height, width = frame.shape[:2]
                    scale = min(target_w / width, target_h / height, 1.0)
                    if scale < 1:
                        frame = cv2.resize(frame, (max(1, int(width * scale)), max(1, int(height * scale))), interpolation=cv2.INTER_AREA)
                    image=Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                    try:
                        if self.frames.full(): self.frames.get_nowait()
                        self.frames.put_nowait((generation, 'frame', image))
                    except (queue.Empty, queue.Full): pass
            except Exception: pass
            finally: capture.release()
            if not self.stop.is_set() and self.stream_selection[0] == generation:
                if paused is not None and paused.is_set():
                    # Пауза — это не обрыв сигнала: ждём возобновления.
                    self.stream_changed.wait()
                    continue
                try:
                    if self.frames.full(): self.frames.get_nowait()
                    self.frames.put_nowait((generation, 'error', 'Нет видеосигнала · повторное подключение…'))
                except (queue.Empty, queue.Full): pass
                self.stream_changed.wait(3)

    def draw_chart(self):
        canvas = self.graph
        canvas.delete('all')
        w, h = canvas.winfo_width(), canvas.winfo_height()
        if w < 80 or h < 65:
            return
        interval = {'1 ч': 1, '3 ч': 3, '6 ч': 6}[self.chart_interval.get()]
        now = datetime.now(self.tz)
        start, end = self.display_period or (None, None)
        current_hour = chart_last_hour(start, now, end)
        bins = aggregate_hours(self.hours, interval, current_hour)
        peak = max((count for _, count in bins), default=0)
        step = max(1, math.ceil(peak / 4))
        ceiling = step * 4
        left = max(30, self.chart_axis_font.measure(str(ceiling)) + 12)
        right, top, bottom = w - 16, 12, h - 26
        chart_w, chart_h = right - left, bottom - top
        for level in range(5):
            value = level * step
            y = bottom - chart_h * value / ceiling
            canvas.create_line(left, y, right, y, fill=BORDER)
            canvas.create_text(left - 7, y, text=str(value), fill=MUTED,
                               font=self.chart_axis_font, anchor='e')
        # Today's line ends at the current interval; historical days span 24 hours.
        for hour in range(0, 25, 3):
            x = left + chart_w * hour / 24
            canvas.create_text(x, bottom + 12, text=f'{hour:02}', fill=MUTED,
                               font=self.chart_axis_font)
        points = [(left + chart_w * hour / 24, bottom - chart_h * value / ceiling)
                  for hour, value in bins]
        if len(points) > 1:
            curve = []
            for (x1, y1), (x2, y2) in zip(points, points[1:]):
                for i in range(8):
                    t = i / 8
                    eased = (1 - math.cos(math.pi * t)) / 2
                    curve.extend((x1 + (x2 - x1) * t, y1 + (y2 - y1) * eased))
            curve.extend(points[-1])
            canvas.create_polygon(*curve, points[-1][0], bottom, points[0][0], bottom,
                                  fill='#203d68', outline='')
            canvas.create_line(*curve, fill='#4598ff', width=3)
        for x, y in points:
            canvas.create_oval(x - 4, y - 4, x + 4, y + 4,
                               fill='#4598ff', outline='#e4f0ff', width=1)

    def poll(self):
        self.video_size = (max(self.video_label.winfo_width(), 2), max(self.video_label.winfo_height(), 2))
        if self.fullscreen_label is not None:
            self.video_size = (max(self.video_size[0], self.fullscreen_label.winfo_width()),
                               max(self.video_size[1], self.fullscreen_label.winfo_height()))
        try:
            generation, kind, value=self.frames.get_nowait()
            if generation != self.stream_selection[0]:
                pass
            elif kind == 'frame':
                self.set_video_status('online')
                w=max(self.video_label.winfo_width(), 2); h=max(self.video_label.winfo_height(), 2)
                fitted=ImageOps.contain(value, (w, h), Image.Resampling.LANCZOS)
                image=ctk.CTkImage(light_image=fitted, dark_image=fitted, size=fitted.size)
                self.video_label.configure(image=image, text='')
                self.image=image
                if self.fullscreen_label is not None:
                    w=max(self.fullscreen_label.winfo_width(), 2); h=max(self.fullscreen_label.winfo_height(), 2)
                    fitted=ImageOps.contain(value, (w, h), Image.Resampling.LANCZOS)
                    image=ctk.CTkImage(light_image=fitted, dark_image=fitted, size=fitted.size)
                    self.fullscreen_label.configure(image=image, text='')
                    self.fullscreen_image=image
            else:
                self.set_video_status('offline')
                self.show_video_message(value)
        except queue.Empty: pass
        try:
            generation, search_generation, period, kind, data=self.results.get_nowait()
            today = self.today()
            if (generation != self.period_selection[0] or search_generation != self.search_selection[0]
                    or period != (self.period_selection[1] or (today, today))):
                pass
            elif kind == 'error':
                self.total.configure(text='—'); self.unique.configure(text='—')
                self.entry_metric.configure(text='—'); self.exit_metric.configure(text='—')
                self.render_passages(None, error=True)
                self._feed_content = None
            else:
                self.display_period = period
                self.total.configure(text=str(data['total'])); self.unique.configure(text=str(data['unique']))
                self.entry_metric.configure(text=str(data['directions']['Въезд']))
                self.exit_metric.configure(text=str(data['directions']['Выезд']))
                self.hours=data['hours']; self.draw_chart()
                content = tuple(data['recent'])
                if content != self._feed_content:
                    current_ids = {row[0] for row in content}
                    self.photo_bytes = {key: value for key, value in self.photo_bytes.items() if key in current_ids}
                    self.render_passages(content)
                    self._feed_content = content
                    if self.reset_feed_scroll:
                        self.feed.after_idle(lambda: self.feed._parent_canvas.yview_moveto(0))
                        self.reset_feed_scroll = False
        except queue.Empty: pass
        try:
            while True:
                generation, search_generation, event_id, photo = self.photo_results.get_nowait()
                if (generation == self.period_selection[0] and search_generation == self.search_selection[0]
                        and event_id in self.photo_buttons):
                    self.photo_bytes[event_id] = photo
                    self.set_photo(event_id, photo)
        except queue.Empty: pass
        self.root.after(40, self.poll)

    def close(self):
        self.stop.set()
        self.stream_changed.set()
        self.data_changed.set()
        if self.date_window is not None:
            self.date_window.destroy()
            self.date_window = None
        self.close_video_fullscreen()
        self.root.destroy()
        if self.on_close:
            self.on_close()


#: Прежнее имя класса окна мониторинга — тот же раздел в новом окне.
MonitorWindow = MonitoringSection
