"""Раскрывающаяся панель управления сервисом.

Панель живёт внутри основного окна (`CTkFrame`, а не новое окно): она
показывается поверх содержимого, не сдвигая Dashboard и не перекрывая
кнопки управления окном.

Содержимое — три независимых блока (сервис, камера, Telegram) и строка
управления сервисом. Каждый блок показывает только своё состояние: сломанная
камера не красит сервис в красный, а недоступный Telegram не мешает
управлять сервисом.

Виджеты создаются один раз; при обновлении меняются только тексты и цвета.
"""
from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from app.ui import icons
from app.ui import motion
from app.ui import theme
from app.ui.widgets import animated_hover, focusable

#: Ширина панели, px.
PANEL_WIDTH = 440
#: Правое поле панели относительно окна, px.
PANEL_MARGIN = 20
#: Отступ панели от шапки, px.
PANEL_GAP = 8

#: Подписи разделов панели.
SECTION_TITLES = {
    'service': 'ANPR-сервис',
    'camera': 'Камера',
    'telegram': 'Telegram',
}

#: Иконки разделов (Tabler Filled).
SECTION_ICONS = {
    'service': 'device-desktop',
    'camera': 'device-cctv',
    'telegram': 'brand-messenger',
}


class StateBadge:
    """Точка и подпись состояния раздела."""

    def __init__(self, parent, motion_manager):
        self.motion = motion_manager
        self.frame = ctk.CTkFrame(parent, fg_color='transparent')
        self.dot = ctk.CTkFrame(self.frame, width=9, height=9, corner_radius=5,
                                fg_color=theme.MUTED)
        self.dot.pack(side='left', padx=(0, 6))
        self.label = ctk.CTkLabel(self.frame, text='—', text_color=theme.MUTED,
                                  font=theme.FONT_CAPTION, anchor='w')
        self.label.pack(side='left')

    def set_state(self, state, short=False):
        """Обновить цвет и подпись состояния (цвет меняется плавно)."""
        if not state:
            return
        color = state.get('color') or theme.MUTED
        text = state.get('short') if short and state.get('short') else state.get('label', '—')
        self.label.configure(text=text, text_color=color)
        self.motion.animate_color(self.dot, 'fg_color', color)


class DetailRows:
    """Строки «подпись — значение».

    Строки расположены в две колонки: подпись слева, значение справа.
    Подписи и значения не должны обрезаться, поэтому ширина колонок
    задана явно, а длинный текст переносится по словам.
    """

    def __init__(self, parent, width):
        self.frame = ctk.CTkFrame(parent, fg_color='transparent')
        self.frame.grid_columnconfigure(1, weight=1)
        self.caption_width = 124
        self.value_width = max(120, width - self.caption_width - 16)
        self._rows = []

    def update(self, rows):
        """Обновить строки (список пар «подпись, значение»)."""
        for index, (caption, value) in enumerate(rows):
            pair = self._ensure(index)
            pair[0].configure(text=caption)
            pair[1].configure(text=value or '—')
            pair[0].grid(row=index, column=0, sticky='w', padx=(0, 10))
            pair[1].grid(row=index, column=1, sticky='ew')
        for index in range(len(rows), len(self._rows)):
            for widget in self._rows[index]:
                widget.grid_forget()

    def _ensure(self, index):
        while len(self._rows) <= index:
            caption = ctk.CTkLabel(self.frame, text='', text_color=theme.MUTED,
                                   font=theme.FONT_MICRO, anchor='w', width=self.caption_width)
            value = ctk.CTkLabel(self.frame, text='', text_color=theme.TEXT,
                                 font=theme.FONT_CAPTION, anchor='w', justify='left',
                                 width=self.value_width, wraplength=self.value_width)
            self._rows.append((caption, value))
        return self._rows[index]


class Section:
    """Один раздел панели: иконка, название, состояние и подробности."""

    def __init__(self, parent, key, motion_manager):
        self.key = key
        self.frame = ctk.CTkFrame(parent, fg_color='transparent')
        top = ctk.CTkFrame(self.frame, fg_color='transparent')
        top.pack(fill='x')
        self.icon = ctk.CTkLabel(top, text='', width=20, height=20)
        self.icon.pack(side='left', padx=(0, 8))
        self.title = ctk.CTkLabel(top, text=SECTION_TITLES.get(key, ''), text_color=theme.TEXT,
                                  font=theme.FONT_BODY_BOLD, anchor='w')
        self.title.pack(side='left')
        self.badge = StateBadge(top, motion_manager)
        self.badge.frame.pack(side='right')
        self.rows = DetailRows(self.frame, PANEL_WIDTH - 56)
        self.rows.frame.pack(fill='x', pady=(4, 0))

    def update(self, state, rows):
        """Обновить состояние и подробности раздела."""
        self.badge.set_state(state)
        self.rows.update(rows)


class ServicePanel:
    """Раскрывающаяся панель управления под правой частью шапки."""

    def __init__(self, overlay, owner, on_toggle=None):
        self.overlay = overlay
        self.owner = owner
        self.on_toggle = on_toggle
        self.key = None
        self.open_state = False
        self._anchor = None
        self._progress_job = None
        self._token = object()

        self.motion = motion.Motion(overlay)
        self.frame = ctk.CTkFrame(overlay, fg_color=theme.CARD, corner_radius=12,
                                   border_width=0, width=PANEL_WIDTH)

        body = ctk.CTkFrame(self.frame, fg_color='transparent')
        body.pack(fill='both', expand=True, padx=18, pady=(14, 16))

        head = ctk.CTkFrame(body, fg_color='transparent')
        head.pack(fill='x')
        ctk.CTkLabel(head, text='Управление сервисом', text_color=theme.TEXT,
                     font=theme.FONT_TITLE, anchor='w').pack(side='left')
        self.close_button = ctk.CTkButton(head, text='', width=28, height=28, corner_radius=8,
                                           fg_color=theme.CARD, hover_color=theme.CARD,
                                           image=icons.icon('x', theme.MUTED, 14, master=head),
                                           command=self.close)
        focusable(self.close_button)
        animated_hover(self.close_button, self.motion, theme.CARD, theme.CARD_ALT)
        self.close_button.pack(side='right')

        self.sections = {}
        for key in ('service', 'camera', 'telegram'):
            section = Section(body, key, self.motion)
            section.frame.pack(fill='x', pady=(16, 0))
            self.sections[key] = section

        # --- строка выполнения операции ---
        self.progress_track = ctk.CTkFrame(body, fg_color=theme.CARD_ALT, height=4,
                                           corner_radius=2)
        self.progress_bar = ctk.CTkFrame(self.progress_track, fg_color=theme.ACCENT,
                                         width=48, height=4, corner_radius=2)
        self.progress_label = ctk.CTkLabel(body, text='', text_color=theme.ACCENT,
                                           font=theme.FONT_CAPTION, anchor='w')

        # --- кнопки управления ---
        actions = ctk.CTkFrame(body, fg_color='transparent')
        actions.pack(fill='x', pady=(18, 0))
        self.start_button = self._button(actions, 'Запустить', 'player-play', theme.CTA,
                                         '#101827', '#E0C85A', self.owner.start_service)
        self.restart_button = self._button(actions, 'Перезапустить', 'player-skip-back',
                                            theme.CARD_ALT, theme.TEXT, theme.ROW_HOVER,
                                            self.owner.restart_service)
        self.stop_button = self._button(actions, 'Остановить', 'player-stop', theme.DANGER,
                                        '#FFFFFF', '#D03934', self.owner.stop_service)
        self.log_button = self._button(actions, 'Журнал', 'file-text', theme.CARD,
                                       theme.MUTED, theme.CARD_ALT, self.owner.open_log)

        self.message_label = ctk.CTkLabel(body, text='', text_color=theme.MUTED,
                                          font=theme.FONT_CAPTION, anchor='w', justify='left',
                                          wraplength=PANEL_WIDTH - 60)
        self.message_icon = ctk.CTkLabel(body, text='', width=18, height=18)

    # ------------------------------------------------------------------
    # Сборка
    # ------------------------------------------------------------------
    def _button(self, parent, text, icon_name, color, text_color, hover, command):
        button = ctk.CTkButton(parent, text=text, width=0, height=34, corner_radius=10,
                               fg_color=color, hover_color=color, text_color=text_color,
                               font=theme.FONT_CAPTION_BOLD,
                               image=icons.icon(icon_name, text_color, 14, master=parent),
                               command=command)
        focusable(button)
        animated_hover(button, self.motion, color, hover)
        button.pack(side='left', padx=(0, 8))
        return button

    # ------------------------------------------------------------------
    # Состояние
    # ------------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        """Открыта ли панель."""
        return self.open_state

    @property
    def action_button(self):
        """Кнопка главного действия (по текущему состоянию сервиса)."""
        data = self.owner.panel_data()
        service = data.get('service') or {}
        if service.get('running'):
            return self.stop_button
        return self.start_button

    # ------------------------------------------------------------------
    # Открытие и закрытие
    # ------------------------------------------------------------------
    def open(self, anchor=None, section=None):
        """Показать панель (с лёгким сдвигом сверху)."""
        if anchor is not None:
            self._anchor = anchor
        self.key = section or self.key
        self.refresh()
        self.frame.lift()
        self.open_state = True
        self._token = object()
        token = self._token
        x, y = self._position()
        self.motion.slide(self.frame, x, y, on_complete=None,
                          guard=lambda: token is self._token and self.open_state)
        self._notify()
        try:
            self.frame.focus_set()
        except tk.TclError:
            pass

    def close(self):
        """Скрыть панель (короткая анимация ухода)."""
        if not self.open_state:
            return
        self.open_state = False
        self.key = None
        self._stop_progress()
        self._token = object()
        token = self._token
        x, y = self._position()

        def finish():
            # Панель могли открыть снова, пока шла анимация — тогда её не hide.
            if token is not self._token or self.open_state:
                return
            try:
                self.frame.place_forget()
            except tk.TclError:
                pass

        self.motion.slide_out(self.frame, x, y, 150, finish,
                              guard=lambda: token is self._token)
        self._notify()

    def _notify(self):
        """Сообщить владельцу о смене состояния панели."""
        if self.on_toggle is None:
            return
        try:
            self.on_toggle(self.open_state)
        except Exception:
            pass

    def toggle(self, anchor=None, section=None):
        """Открыть или закрыть панель."""
        if self.open_state and (section is None or section == self.key):
            self.close()
            return
        self.open(anchor=anchor, section=section)

    # ------------------------------------------------------------------
    # Геометрия
    # ------------------------------------------------------------------
    def _position(self):
        """Положение панели: под правой частью шапки, по правому краю."""
        try:
            overlay_width = self.overlay.winfo_width()
            overlay_height = self.overlay.winfo_height()
        except tk.TclError:
            return PANEL_MARGIN, 64
        if overlay_width <= 1:
            overlay_width = self.overlay.winfo_screenwidth()
        if overlay_height <= 1:
            overlay_height = self.overlay.winfo_screenheight()
        x = max(PANEL_MARGIN, overlay_width - PANEL_WIDTH - PANEL_MARGIN)
        y = PANEL_GAP + 4
        anchor = self._anchor
        if anchor is not None:
            try:
                bottom = (anchor.winfo_rooty() - self.overlay.winfo_rooty()
                          + anchor.winfo_height())
                y = bottom + PANEL_GAP
            except tk.TclError:
                pass
        y = max(PANEL_GAP, min(y, max(PANEL_GAP, overlay_height - 40)))
        return x, y

    def _fit(self):
        """Подогнать панель под окно: при нехватке места она сдвигается вверх."""
        if not self.open_state:
            return
        try:
            height = self.frame.winfo_reqheight()
            overlay_height = self.overlay.winfo_height()
        except tk.TclError:
            return
        if height <= 0:
            return
        x, y = self._position()
        if y + height > overlay_height - PANEL_GAP:
            y = max(PANEL_GAP, overlay_height - height - PANEL_GAP)
        try:
            self.frame.place_configure(x=x, y=y)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Содержимое
    # ------------------------------------------------------------------
    def refresh(self):
        """Обновить содержимое панели по данным владельца."""
        data = self.owner.panel_data()
        for key, section in self.sections.items():
            block = data.get(key) or {}
            section.update(block.get('state'), block.get('rows') or [])
        self._update_actions(data)
        self._update_message(data.get('message') or '', data.get('message_tone') or '')
        self._fit()

    def _update_actions(self, data):
        """Показать только те действия, что соответствуют текущему состоянию."""
        service = data.get('service') or {}
        code = (service.get('state') or {}).get('code')
        running = bool(service.get('running'))
        busy = bool(data.get('busy'))
        operation = data.get('operation')

        show_start = not running and code != 'error'
        show_restart = running
        show_stop = running
        if code == 'error':
            # Восстановление предлагаем только после проверки фактического
            # состояния процесса: жив процесс или нет.
            if service.get('process_alive'):
                show_restart = True
            else:
                show_start = True
        self._toggle(self.start_button, show_start)
        self._toggle(self.restart_button, show_restart)
        self._toggle(self.stop_button, show_stop)
        self._toggle(self.log_button, True)

        state = 'disabled' if busy else 'normal'
        for button in (self.start_button, self.restart_button, self.stop_button):
            try:
                button.configure(state=state)
            except tk.TclError:
                pass
        self._show_progress(busy, operation)

    @staticmethod
    def _toggle(widget, visible: bool):
        try:
            if visible and not widget.winfo_manager():
                widget.pack(side='left', padx=(0, 8))
            elif not visible and widget.winfo_manager():
                widget.pack_forget()
        except tk.TclError:
            pass

    def _update_message(self, text, tone=''):
        """Показать сообщение операции (с знаком ошибки или успеха)."""
        value = str(text or '').strip()
        tone = str(tone or '')
        try:
            self.message_label.configure(
                text=value,
                text_color=theme.DANGER if tone == 'error' else theme.MUTED)
            if not value:
                self._pack_message(False)
                return
            icon_name, color = '', theme.MUTED
            if tone == 'error':
                icon_name, color = 'alert-triangle', theme.DANGER
            elif tone == 'ok':
                icon_name, color = 'circle-check', theme.SUCCESS
            if icon_name:
                self.message_icon.configure(
                    image=icons.icon(icon_name, color, 15, master=self.message_icon))
            self._pack_message(True, icon=bool(icon_name))
        except tk.TclError:
            pass

    def _pack_message(self, visible: bool, icon=False):
        """Показать или убрать строку сообщения."""
        if visible:
            if icon:
                self.message_icon.pack(side='left', padx=(0, 6), pady=(12, 0))
                self.message_label.pack(side='left', fill='x', expand=True, pady=(12, 0))
            else:
                self.message_label.pack(fill='x', pady=(12, 0))
        else:
            if self.message_icon.winfo_manager():
                self.message_icon.pack_forget()
            if self.message_label.winfo_manager():
                self.message_label.pack_forget()

    # ------------------------------------------------------------------
    # Индикатор выполнения операции
    # ------------------------------------------------------------------
    def _show_progress(self, busy: bool, operation=None):
        """Ненавязчивый индикатор выполнения операции."""
        labels = {
            'start': 'Запускаем сервис…',
            'stop': 'Останавливаем сервис…',
            'restart': 'Перезапускаем сервис…',
        }
        if not busy:
            self._stop_progress()
            return
        try:
            if not self.progress_track.winfo_manager():
                self.progress_track.pack(fill='x', pady=(14, 0))
                self.progress_label.pack(fill='x', pady=(6, 0))
            self.progress_label.configure(text=labels.get(operation, 'Выполняется операция…'))
        except tk.TclError:
            return
        if self._progress_job is None:
            self._progress_job = self.motion.call(30, self._move_progress)

    def _stop_progress(self):
        """Остановить индикатор выполнения и убрать его из панели."""
        if self._progress_job is not None:
            self.motion.cancel(self._progress_job)
            self._progress_job = None
        try:
            if self.progress_track.winfo_manager():
                self.progress_track.pack_forget()
            if self.progress_label.winfo_manager():
                self.progress_label.pack_forget()
        except tk.TclError:
            pass

    def _move_progress(self):
        """Сдвинуть полоску индикатора (одна задача — один отложенный вызов)."""
        self._progress_job = None
        try:
            width = max(1, self.progress_track.winfo_width())
            bar_width = 48
            span = max(1, width - bar_width)
            self.progress_bar.place(x=0, y=0)
            self.motion.tween(600, lambda fraction: self.progress_bar.place_configure(
                x=int(span * fraction)))
        except tk.TclError:
            return
        self._progress_job = self.motion.call(700, self._move_progress)

    # ------------------------------------------------------------------
    # Очистка
    # ------------------------------------------------------------------
    def shutdown(self):
        """Отменить все отложенные вызовы панели (при закрытии окна)."""
        self._stop_progress()
        self.motion.cancel_all()
