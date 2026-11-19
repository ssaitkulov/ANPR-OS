"""Постоянная верхняя панель приложения «Эврика».

Header — единственное место, где видно состояние системы и где управляют
ANPR-сервисом.

Устройство шапки:

* слева — логотип и навигация («Мониторинг», «Настройки»);
* справа — три независимых индикатора (сервис, камера, Telegram) и кнопка
  «Управление», которая раскрывает панель управления сервисом.

Индикаторы не связаны между собой: неисправная камера не красит сервис в
красный, а недоступный Telegram не мешает управлять сервисом. Общего
индикатора «система неисправна» нет и не появится.

Управление сервисом (запуск, остановка, перезапуск, журнал) живёт в
раскрывающейся панели `app/ui/service_panel.py` — она показывается поверх
содержимого окна и не сдвигает разделы.
"""
import tkinter as tk

import customtkinter as ctk

from app.ui import icons
from app.ui import motion
from app.ui import status as ui_status
from app.ui import theme
from app.ui.widgets import animated_hover, focusable
from app.ui.service_panel import ServicePanel

#: Подписи индикаторов шапки.
INDICATOR_CAPTIONS = {
    'service': 'Сервис',
    'camera': 'Камера',
    'telegram': 'Telegram',
}

#: Иконки индикаторов (Tabler Filled).
INDICATOR_ICONS = {
    'service': 'device-desktop',
    'camera': 'device-cctv',
    'telegram': 'brand-messenger',
}


class StatusChip:
    """Компактный индикатор: иконка, название, точка состояния и подпись.

    Индикатор — небольшая горизонтальная группа без обводки: фон прозрачный,
    при наведении слегка подсвечивается. Нажатие открывает панель
    управления, клавиатура — тоже (Enter и пробел).
    """

    def __init__(self, parent, key, caption, icon_name, on_click, motion_manager):
        self.key = key
        self.state = None
        self.on_click = on_click
        self.motion = motion_manager
        self.icon_name = icon_name
        self.frame = ctk.CTkFrame(parent, fg_color=theme.BG, corner_radius=10,
                                  border_width=0)
        self.icon = ctk.CTkLabel(self.frame, text='',
                                 image=icons.icon(icon_name, theme.MUTED, 15, master=self.frame),
                                 width=17, height=17)
        self.icon.pack(side='left', padx=(6, 4), pady=(9, 9))
        self.caption = ctk.CTkLabel(self.frame, text=caption, text_color=theme.TEXT,
                                    font=theme.FONT_CAPTION_BOLD, anchor='w')
        self.caption.pack(side='left', padx=(0, 6), pady=(9, 9))
        self.dot = ctk.CTkFrame(self.frame, width=9, height=9, corner_radius=5,
                                fg_color=theme.MUTED)
        self.dot.pack(side='left', pady=1)
        self.value = ctk.CTkLabel(self.frame, text='—', text_color=theme.MUTED,
                                  font=theme.FONT_CAPTION, anchor='w')
        self.value.pack(side='left', padx=(5, 4), pady=(9, 9))
        self._bind(self.frame)
        self._bind(self.icon)
        self._bind(self.caption)
        self._bind(self.value)
        self._bind(self.dot)
        for widget in (self.icon, self.caption, self.value, self.dot):
            widget.bind('<Enter>', lambda _event: self._hover(True), add='+')
            widget.bind('<Leave>', lambda _event: self._hover(False), add='+')
        self.frame.bind('<Enter>', lambda _event: self._hover(True), add='+')
        self.frame.bind('<Leave>', lambda _event: self._hover(False), add='+')
        self.frame.bind('<FocusIn>', lambda _event: self._hover(True), add='+')
        self.frame.bind('<FocusOut>', lambda _event: self._hover(False), add='+')
        focusable(self.frame)

    def _bind(self, widget):
        widget.bind('<Button-1>', lambda _event: self.on_click(self.key), add='+')
        widget.bind('<Return>', lambda _event: self.on_click(self.key), add='+')
        widget.bind('<space>', lambda _event: self.on_click(self.key), add='+')

    def _hover(self, active: bool):
        """Мягкая подсветка при наведении — без рамок и теней."""
        self.motion.animate_color(self.frame, 'fg_color',
                                  theme.CARD_ALT if active else theme.BG)

    def set_state(self, state, compact=False):
        """Показать состояние индикатора (словарь из `app.ui.status`)."""
        if not state:
            return
        self.state = state
        color = state.get('color') or theme.MUTED
        self.value.configure(text=state.get('short') if compact else state.get('label', '—'),
                             text_color=color)
        self.motion.animate_color(self.dot, 'fg_color', color)
        try:
            self.icon.configure(image=icons.icon(self.icon_name, color, 15, master=self.icon))
        except tk.TclError:
            pass

    def set_compact(self, tight: bool):
        """В очень узком окне вторичный текст скрывается, название остаётся."""
        try:
            if tight and self.value.winfo_manager():
                self.value.pack_forget()
            elif not tight and not self.value.winfo_manager():
                self.value.pack(side='left', padx=(6, 10))
        except tk.TclError:
            pass


class SystemHeader:
    """Верхняя панель: навигация слева, индикаторы и управление справа."""

    def __init__(self, master, overlay, owner):
        self.owner = owner
        self.overlay = overlay
        self.compact = False
        self.frame = ctk.CTkFrame(master, fg_color='transparent')
        self.frame.pack(fill='x', padx=theme.GROUP_GAP * 2, pady=(14, 6))
        self.chips = {}
        self._states = {}
        self._full_width = 0
        self._short_width = 0
        self.tight = False
        self._fitting = False
        self._refit = False
        self._refit_job = None
        self.closed = False
        self.motion = motion.Motion(master)

        # --- левая часть: логотип и навигация ---
        self.left = ctk.CTkFrame(self.frame, fg_color='transparent')
        self.left.pack(side='left')
        self.logo = ctk.CTkImage(light_image=owner.logo_image(), size=(116, 25))
        ctk.CTkLabel(self.left, text='', image=self.logo, anchor='w').pack(side='left')
        nav = ctk.CTkFrame(self.left, fg_color='transparent')
        nav.pack(side='left', padx=(14, 0))
        self.nav_buttons = {}
        for key in ('monitor', 'settings'):
            button = ctk.CTkButton(nav, text=owner.section_titles[key], width=100, height=34,
                                    corner_radius=10, fg_color='transparent',
                                    hover_color=theme.CARD_ALT, text_color=theme.TEXT,
                                    font=theme.FONT_CAPTION, command=lambda key=key: owner.show_section(key))
            button.pack(side='left', padx=(6, 0))
            self.nav_buttons[key] = button

        # --- правая часть: индикаторы и кнопка управления ---
        self.control = ctk.CTkFrame(self.frame, fg_color='transparent', corner_radius=10)
        self.control.pack(side='right')
        for key in ('service', 'camera', 'telegram'):
            chip = StatusChip(self.control, key, INDICATOR_CAPTIONS[key], INDICATOR_ICONS[key],
                              self.toggle_details, self.motion)
            chip.frame.pack(side='left', padx=(0, theme.GROUP_GAP))
            self.chips[key] = chip
        self.service_chip = self.chips['service']
        self.camera_chip = self.chips['camera']
        self.telegram_chip = self.chips['telegram']

        self.manage_button = ctk.CTkButton(self.control, text='Управление', width=118, height=34,
                                           corner_radius=10, fg_color=theme.CARD,
                                           hover_color=theme.CARD, text_color=theme.TEXT,
                                           font=theme.FONT_CAPTION_BOLD,
                                           image=icons.icon('chevron-down', theme.MUTED, 14, master=self.control),
                                           command=self.toggle_panel)
        focusable(self.manage_button)
        animated_hover(self.manage_button, self.motion, theme.CARD, theme.CARD_ALT)
        self.manage_button.pack(side='left', padx=(2, 0))

        # Панель управления сервисом (раскрывающаяся).
        self.details = ServicePanel(overlay, owner, on_toggle=self._update_manage_button)
        self.panel = self.details

        overlay.bind('<Button-1>', self._on_overlay_click, add='+')
        overlay.bind('<Escape>', self._on_escape, add='+')
        overlay.bind('<Configure>', self._fit_width, add='+')

    # ------------------------------------------------------------------
    # Панель управления
    # ------------------------------------------------------------------
    @property
    def action_button(self):
        """Кнопка главного действия (по текущему состоянию сервиса)."""
        return self.details.action_button

    @property
    def restart_button(self):
        """Кнопка перезапуска сервиса."""
        return self.details.restart_button

    def _update_manage_button(self, opened: bool):
        """Стрелка кнопки «Управление» показывает направление панели."""
        try:
            self.manage_button.configure(
                image=icons.icon('chevron-down', theme.MUTED, 14, master=self.control, flip=opened))
        except tk.TclError:
            pass

    def toggle_details(self, key):
        """Открыть панель управления (нажатие на индикатор)."""
        self.details.toggle(anchor=self.manage_button, section=key)

    def toggle_panel(self):
        """Кнопка «Управление»: открыть или закрыть панель."""
        self.details.toggle(anchor=self.manage_button)

    def close_details(self):
        """Закрыть панель управления."""
        self.details.close()

    def _on_escape(self, _event=None):
        """Escape закрывает панель."""
        if self.details.is_open:
            self.details.close()
            return 'break'

    def _on_overlay_click(self, event):
        """Клик вне панели закрывает её."""
        if not self.details.is_open:
            return
        widget = getattr(event, 'widget', None)
        if widget is None:
            return
        if self._within(widget, self.details.frame):
            return
        if self._within(widget, self.manage_button):
            return
        for chip in self.chips.values():
            if self._within(widget, chip.frame):
                return
        self.details.close()

    @staticmethod
    def _within(widget, ancestor):
        current = widget
        while current is not None:
            if current is ancestor:
                return True
            current = getattr(current, 'master', None)
        return False

    # ------------------------------------------------------------------
    # Состояния
    # ------------------------------------------------------------------
    def set_state(self, key, state):
        """Обновить индикатор состояния."""
        chip = self.chips.get(key)
        if chip is None or not state or self.closed:
            return
        self._states[key] = state
        chip.set_state(state, self.compact)
        try:
            self.control.update_idletasks()
        except tk.TclError:
            pass
        if not self.compact:
            self._full_width = self.control.winfo_reqwidth()

    def refresh_panel(self):
        """Обновить содержимое панели управления (один раз за цикл опроса)."""
        self.details.refresh()

    def set_running(self, running):
        """Учесть состояние сервиса в панели управления."""
        self.details.refresh()

    def set_busy(self, busy):
        """На время операции кнопки управления блокируются.

        Индикатор сервиса сразу показывает выполняемую операцию
        («Запускается», «Останавливается», «Перезапускается») — ждать
        следующего опроса пользователю не нужно.
        """
        if busy:
            owner = self.owner
            state = ui_status.service_view(None, True, getattr(owner, 'operation', None),
                                           getattr(owner, 'phase', None))
            self.set_state('service', state)
        self.details.refresh()

    def sync_message(self):
        """Передать результат операции в панель управления."""
        self.details.refresh()

    # ------------------------------------------------------------------
    # Ширина
    # ------------------------------------------------------------------
    def _fit_width(self, _event=None):
        """Подогнать шапку под ширину окна.

        Названия компонентов («Сервис», «Камера», «Telegram») не исчезают
        никогда: сначала сокращается вторичный текст (короткая подпись
        состояния), и только в самом узком окне он прячется, а цвет точки —
        критический статус — остаётся виден.

        Пересчёт защищён от повторного входа: `update_idletasks()` внутри
        обрабатывает и события Configure, поэтому без защиты шапка
        пересчитывала бы саму себя до переполнения стека.
        """
        if self.closed:
            return
        if self._fitting:
            self._refit = True
            return
        self._fitting = True
        try:
            self._fit_width_now()
        finally:
            self._fitting = False
            if self._refit:
                self._refit = False
                self._schedule_refit()

    def _schedule_refit(self):
        """Отложенный пересчёт ширины (с защитой от рекурсии)."""
        if self._refit_job is not None:
            try:
                self.overlay.after_cancel(self._refit_job)
            except tk.TclError:
                pass
            self._refit_job = None
        try:
            self._refit_job = self.overlay.after_idle(self._fit_width)
        except tk.TclError:
            self._refit_job = None

    def _fit_width_now(self):
        """Собственно пересчёт ширины (вызывается под защитой)."""
        try:
            if not self.control.winfo_exists():
                return
            available = self.overlay.winfo_width() - self.left.winfo_reqwidth() - 40
            needed = self.control.winfo_reqwidth()
        except tk.TclError:
            return
        if not self.compact:
            self._full_width = max(self._full_width, needed)
        full = self._full_width or needed
        if full > available and not self.compact:
            self.compact = True
            self._refresh_labels()
            try:
                self.control.update_idletasks()
                self._short_width = self.control.winfo_reqwidth()
            except tk.TclError:
                self._short_width = 0
        elif self.compact and full <= available - 40:
            self.compact = False
            self._refresh_labels()
            try:
                self.control.update_idletasks()
            except tk.TclError:
                pass
        short = self._short_width or needed
        tight = self.compact and short > available
        if tight != self.tight:
            self.tight = tight
            self._refresh_labels()

    def _refresh_labels(self):
        for key, chip in self.chips.items():
            chip.set_compact(self.tight)
            state = self._states.get(key)
            if state is not None:
                chip.set_state(state, self.compact)
        try:
            self.control.update_idletasks()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Закрытие
    # ------------------------------------------------------------------
    def shutdown(self):
        """Отменить отложенные вызовы шапки и панели.

        Шапка перестаёт реагировать на события до отмены вызовов: при
        закрытии окна виджеты ещё Destroy-ятся, и на это приходят
        Configure-события. Без остановки шапка успела бы запланировать
        новые вызовы — и они сработали бы уже на закрытом окне.
        """
        self.closed = True
        if self._refit_job is not None:
            try:
                self.overlay.after_cancel(self._refit_job)
            except tk.TclError:
                pass
            self._refit_job = None
        self.details.shutdown()
        self.motion.cancel_all()
