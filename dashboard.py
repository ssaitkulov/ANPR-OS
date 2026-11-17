"""Native, read-only monitoring window for the local ANPR database and camera."""
import os
import math
import io
import queue
import sqlite3
import threading
import time
from datetime import datetime
from contextlib import closing
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import customtkinter as ctk
from PIL import Image, ImageOps
import setup_anpr
import requests

BASE = Path(__file__).resolve().parent
BG = '#101722'; CARD = '#1a2432'; BORDER = '#303e51'; TEXT = '#f2f5fa'; MUTED = '#a5b1c2'; BLUE = '#377df1'


def direction(value):
    raw = str(value or '').strip().lower()
    if raw in {'approach', 'approaching', 'приближается', 'приближение', 'obverse', 'обverse', 'in', 'incoming', 'въезд', 'входящий', 'entry', 'enter', 'head', '0'}:
        return 'Въезд'
    if raw in {'leave', 'leaving', 'удаляется', 'удаление', 'reverse', 'out', 'outgoing', 'выезд', 'исходящий', 'exit', 'tail', '1'}:
        return 'Выезд'
    return 'Неизвестно'


def load_dashboard(path, day):
    uri = Path(path).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as conn:
        rows = conn.execute('SELECT id, captured_at, plate, direction, backfill FROM events WHERE day=? ORDER BY captured_at DESC, id DESC', (day,)).fetchall()
    hours = [0] * 24
    counts = {'Въезд': 0, 'Выезд': 0, 'Неизвестно': 0}
    unique = set()
    for _, captured, plate, direct, _ in rows:
        try: hours[datetime.fromisoformat(captured).hour] += 1
        except (ValueError, TypeError): pass
        counts[direction(direct)] += 1
        if plate and plate not in ('—', 'UNKNOWN', 'unknown'): unique.add(plate)
    return {'total': len(rows), 'unique': len(unique), 'hours': hours, 'directions': counts, 'recent': rows[:30]}


def aggregate_hours(hours, interval, current_hour):
    """Group today's hourly counts into fixed bins through the current hour."""
    if interval not in (1, 3, 6) or len(hours) != 24 or not 0 <= current_hour <= 23:
        raise ValueError('Invalid chart inputs')
    return [(start, sum(hours[start:start + interval]))
            for start in range(0, current_hour + 1, interval)]


class MonitorWindow:
    def __init__(self, master, on_close=None):
        self.root = ctk.CTkFrame(master, fg_color=BG, corner_radius=0)
        self.root.pack(fill='both', expand=True)
        self.on_close = on_close
        self.stop = threading.Event(); self.frames = queue.Queue(maxsize=1); self.results = queue.Queue(maxsize=1)
        self.video_size = (640, 360)
        self.photo_results = queue.Queue(maxsize=60)
        self.photo_bytes = {}
        self.photo_buttons = {}
        self.settings = setup_anpr.read_settings()
        try: self.tz = ZoneInfo(self.settings.get('TIMEZONE', 'Asia/Tashkent'))
        except (ZoneInfoNotFoundError, ValueError): self.tz = None
        root = self.root
        header = ctk.CTkFrame(root, fg_color='transparent'); header.pack(fill='x', padx=22, pady=(15, 8))
        ctk.CTkButton(header, text='‹  Назад', width=85, height=32, fg_color=CARD,
                      hover_color='#34455a', command=self.close).pack(side='left', padx=(0, 14))
        ctk.CTkLabel(header, text='Мониторинг', font=('Segoe UI', 22, 'bold'), text_color=TEXT).pack(side='left')
        ctk.CTkLabel(header, text='СЕГОДНЯ', font=('Segoe UI', 11, 'bold'), text_color=MUTED).pack(side='right')
        layout = ctk.CTkFrame(root, fg_color='transparent'); layout.pack(fill='both', expand=True, padx=22, pady=(0, 20))
        layout.grid_columnconfigure(0, weight=3, uniform='column'); layout.grid_columnconfigure(1, weight=2, uniform='column')
        layout.grid_rowconfigure(0, weight=3); layout.grid_rowconfigure(1, weight=2)
        video = self.card(layout, 0, 0, padx=(0, 7), pady=(0, 7))
        ctk.CTkLabel(video, text='КАМЕРА · ПРЯМОЙ ЭФИР', text_color=MUTED, font=('Segoe UI', 11, 'bold'), anchor='w').pack(fill='x', padx=14, pady=(8, 2))
        self.video_label = ctk.CTkLabel(video, text='Подключение к камере…', text_color=MUTED, fg_color='#090e16')
        self.video_label.pack(fill='both', expand=True, padx=10, pady=(0, 10))
        right = ctk.CTkFrame(layout, fg_color='transparent')
        right.grid(row=0, column=1, rowspan=2, sticky='nsew', padx=(7, 0))
        right.grid_columnconfigure((0, 1), weight=1, uniform='metric')
        right.grid_rowconfigure(2, weight=1)
        self.entry_metric = self.metric(right, 'ВЪЕЗД', '↘', 0, 0)
        self.exit_metric = self.metric(right, 'ВЫЕЗД', '↗', 0, 1)
        self.total = self.metric(right, 'ВСЕ ПРОЕЗДЫ', '●', 1, 0)
        self.unique = self.metric(right, 'УНИКАЛЬНЫЕ НОМЕРА', '№', 1, 1)
        feed = ctk.CTkFrame(right, fg_color=CARD, border_color=BORDER, border_width=1, corner_radius=10)
        feed.grid(row=2, column=0, columnspan=2, sticky='nsew', pady=(4, 0))
        ctk.CTkLabel(feed, text='Последние проезды', font=('Segoe UI', 14, 'bold'), text_color=TEXT, anchor='w').pack(fill='x', padx=14, pady=(10, 4))
        self.feed = ctk.CTkScrollableFrame(feed, fg_color='transparent')
        self.feed.pack(fill='both', expand=True, padx=4, pady=(0, 8))
        self.feed.grid_columnconfigure(0, weight=1)
        graph = self.card(layout, 1, 0, padx=(0, 7), pady=(7, 0))
        import tkinter as tk
        self.hours = [0] * 24
        self.chart_interval = tk.StringVar(value='1 ч')
        graph_header = ctk.CTkFrame(graph, fg_color='transparent')
        graph_header.pack(fill='x', padx=14, pady=(10, 0))
        icon = tk.Canvas(graph_header, width=27, height=27, bg=BLUE, highlightthickness=0)
        icon.create_rectangle(6, 15, 9, 21, fill='white', outline='')
        icon.create_rectangle(12, 11, 15, 21, fill='white', outline='')
        icon.create_rectangle(18, 6, 21, 21, fill='white', outline='')
        icon.pack(side='left', padx=(0, 8))
        ctk.CTkLabel(graph_header, text='Статистика транспортных средств', text_color=TEXT,
                     font=('Segoe UI', 14, 'bold'), anchor='w').pack(side='left', fill='x', expand=True)
        ctk.CTkOptionMenu(graph_header, variable=self.chart_interval, values=['1 ч', '3 ч', '6 ч'],
                          command=lambda _: self.draw_chart(), width=75, height=28,
                          fg_color='#26364c', button_color='#334b6b', button_hover_color='#416087').pack(side='right')
        ctk.CTkLabel(graph_header, text='Интервал', text_color=MUTED, font=('Segoe UI', 11)).pack(side='right', padx=(0, 8))
        self.graph = tk.Canvas(graph, bg=CARD, highlightthickness=0)
        self.graph.pack(fill='both', expand=True, padx=14, pady=10)
        self.graph.bind('<Configure>', lambda _: self.draw_chart())
        self._feed_content = None
        threading.Thread(target=self.data_worker, daemon=True).start()
        threading.Thread(target=self.video_worker, daemon=True).start()
        root.after(100, self.poll)

    def card(self, parent, row, col, **kwargs):
        frame=ctk.CTkFrame(parent, fg_color=CARD, border_color=BORDER, border_width=1, corner_radius=10); frame.grid(row=row, column=col, sticky='nsew', **kwargs); return frame

    def metric(self, parent, title, symbol, row, column):
        frame = ctk.CTkFrame(parent, fg_color=CARD, border_color=BORDER, border_width=1, corner_radius=10)
        frame.grid(row=row, column=column, sticky='ew', padx=(0, 5) if column == 0 else (5, 0), pady=(0, 8))
        ctk.CTkLabel(frame, text=symbol, width=32, height=32, corner_radius=7, fg_color='#263f64',
                     text_color='#78acff', font=('Segoe UI', 17, 'bold')).pack(side='left', padx=(10, 8), pady=10)
        box = ctk.CTkFrame(frame, fg_color='transparent'); box.pack(side='left', fill='x', expand=True, pady=6)
        ctk.CTkLabel(box, text=title, text_color=MUTED, font=('Segoe UI', 9, 'bold'), anchor='w').pack(fill='x')
        label = ctk.CTkLabel(box, text='—', text_color=TEXT, font=('Segoe UI', 22, 'bold'), anchor='w')
        label.pack(fill='x')
        return label

    def render_passages(self, rows):
        for child in self.feed.winfo_children(): child.destroy()
        self.photo_buttons = {}
        for index, (event_id, captured, plate, direct, archive) in enumerate(rows):
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
            ctk.CTkLabel(self.feed, text='Проездов сегодня пока нет', text_color=MUTED).grid(row=0, column=0, pady=12)

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
        with requests.Session() as session:
            session.trust_env = False
            while not self.stop.is_set():
                try:
                    data = load_dashboard(BASE / 'anpr.sqlite3', datetime.now(self.tz).strftime('%Y-%m-%d'))
                    if self.results.empty(): self.results.put_nowait(('data', data))
                    now = time.monotonic()
                    current_ids = {row[0] for row in data['recent'][:30]}
                    checked_at = {key: value for key, value in checked_at.items() if key in current_ids}
                    for event_id in current_ids:
                        if self.stop.is_set(): break
                        if event_id in self.photo_bytes or now - checked_at.get(event_id, -100) < 5: continue
                        checked_at[event_id] = now
                        try:
                            response = session.get(f'http://127.0.0.1:18749/passages/{event_id}/photo', timeout=1)
                            if response.ok and response.headers.get('Content-Type', '').startswith('image/'):
                                self.photo_results.put_nowait((event_id, response.content))
                        except (requests.RequestException, queue.Full): pass
                except (OSError, sqlite3.Error):
                    if self.results.empty(): self.results.put_nowait(('error', None))
                self.stop.wait(2)

    def video_worker(self):
        os.environ.setdefault('OPENCV_LOG_LEVEL', 'SILENT')
        os.environ.setdefault('OPENCV_FFMPEG_LOGLEVEL', '-8')
        try: import cv2
        except ImportError:
            self.frames.put(('error', 'Видеомодуль недоступен')); return
        host=self.settings.get('DAHUA_HOST', '')
        user=quote(self.settings.get('DAHUA_USERNAME', ''), safe=''); password=quote(self.settings.get('DAHUA_PASSWORD', ''), safe='')
        channel=self.settings.get('DAHUA_CHANNEL', '1')
        url=f'rtsp://{user}:{password}@{host}:554/cam/realmonitor?channel={channel}&subtype=0'
        while not self.stop.is_set():
            capture = cv2.VideoCapture()
            try:
                capture.open(url, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000])
                if not capture.isOpened(): raise OSError('video unavailable')
                last_published = 0.0
                while not self.stop.is_set():
                    ok, frame=capture.read()
                    if not ok: break
                    now = time.monotonic()
                    # Drain the stream continuously; only publish the newest frame.
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
                        self.frames.put_nowait(('frame', image))
                    except queue.Empty: pass
            except Exception: pass
            finally: capture.release()
            if not self.stop.is_set():
                try:
                    if self.frames.full(): self.frames.get_nowait()
                    self.frames.put_nowait(('error', 'Нет видеосигнала · повторное подключение…'))
                except queue.Empty: pass
                self.stop.wait(3)

    def draw_chart(self):
        canvas = self.graph
        canvas.delete('all')
        w, h = canvas.winfo_width(), canvas.winfo_height()
        if w < 80 or h < 65:
            return
        interval = {'1 ч': 1, '3 ч': 3, '6 ч': 6}[self.chart_interval.get()]
        current_hour = datetime.now(self.tz).hour
        bins = aggregate_hours(self.hours, interval, current_hour)
        peak = max((count for _, count in bins), default=0)
        step = max(1, math.ceil(peak / 4))
        ceiling = step * 4
        left, right, top, bottom = 30, w - 14, 12, h - 26
        chart_w, chart_h = right - left, bottom - top
        for level in range(5):
            value = level * step
            y = bottom - chart_h * value / ceiling
            canvas.create_line(left, y, right, y, fill=BORDER)
            canvas.create_text(left - 7, y, text=str(value), fill=MUTED,
                               font=('Segoe UI', 9), anchor='e')
        # Grid spans the day, but the line ends at the current interval.
        for hour in range(0, 25, 3):
            x = left + chart_w * hour / 24
            canvas.create_text(x, bottom + 12, text=f'{hour:02}', fill=MUTED,
                               font=('Segoe UI', 9))
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
        try:
            kind, value=self.frames.get_nowait()
            if kind == 'frame':
                w=max(self.video_label.winfo_width(), 2); h=max(self.video_label.winfo_height(), 2)
                fitted=ImageOps.contain(value, (w, h), Image.Resampling.LANCZOS)
                self.image=ctk.CTkImage(light_image=fitted, dark_image=fitted, size=fitted.size)
                self.video_label.configure(image=self.image, text='')
            else: self.video_label.configure(image=None, text=value)
        except queue.Empty: pass
        try:
            kind, data=self.results.get_nowait()
            if kind == 'error':
                self.total.configure(text='—'); self.unique.configure(text='—')
                self.entry_metric.configure(text='—'); self.exit_metric.configure(text='—')
            else:
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
        except queue.Empty: pass
        try:
            while True:
                event_id, photo = self.photo_results.get_nowait()
                self.photo_bytes[event_id] = photo
                self.set_photo(event_id, photo)
        except queue.Empty: pass
        self.root.after(40, self.poll)

    def close(self):
        self.stop.set()
        self.root.destroy()
        if self.on_close:
            self.on_close()
