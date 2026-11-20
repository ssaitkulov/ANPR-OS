"""Постоянная верхняя панель приложения «Эврика».

Header — единственное место, где видно состояние системы.

Устройство шапки:

* слева — логотип и навигация («Мониторинг», «Настройки»);
* справа — три компактных индикатора: ANPR-сервис, камера, Telegram.

Индикатор — это цветная Filled-иконка и краткая подпись состояния
(«Работает», «Подключена», «Проверка»). Цвет иконки и есть состояние
компонента: отдельных цветных точек, названий и кнопки «Управление» нет,
поэтому шапка не перегружена и не растёт в высоту.

Нажатие на индикатор открывает контекстную панель
(`app/ui/service_panel.py`) с диагностикой только этого компонента.
Индикаторы не связаны между собой: неисправная камера не красит сервис
в красный, а недоступный Telegram не мешает управлять сервисом. Общего
индикатора «система неисправна» нет и не появится.
"""
import tkinter as tk

import customtkinter as ctk

from app.ui import motion
from app.ui import status as ui_status
from app.ui import theme
from app.ui.widgets import TintedIcon, Tooltip, bind_widget, focusable
from app.ui.service_panel import ServicePanel

#: Подписи индикаторов шапки (в подсказках и заголовках панели).
INDICATOR_CAPTIONS = {
    'service': 'ANPR-сервис',
    'camera': 'Камера',
    'telegram': 'Telegram',
}

#: Иконки индикаторов (Tabler Filled).
INDICATOR_ICONS = {
    'service': 'device-desktop',
    'camera': 'device-cctv',
    'telegram': 'brand-messenger',
}

#: Размер иконки индикатора, px.
ICON_SIZE = 19
#: Высота интерактивной области индикатора, px.
CHIP_HEIGHT = 37


class StatusChip:
    """Компактный индикатор компонента: цветная иконка и краткий статус.

    Индикатор — небольшая горизонтальная группа без обводки: фон
    прозрачный, при наведении слегка подсвечивается, у активного
    (раскрытая панель этого компонента) фон становится `CARD_ALT`.
    Нажатие открывает панель, клавиатура — тоже (Enter и пробел).
    """

    def __init__(self, parent, key, caption, icon_name, on_click, motion_manager,
                 tooltip_parent=None):
        self.key = key
        self.caption = caption
        self.icon_name = icon_name
        self.on_click = on_click
        self.motion = motion_manager
        self.state = None
        self._hovered = False
        self._active = False

        self.frame = ctk.CTkFrame(parent, fg_color=theme.BG, corner_radius=9,
                                  border_width=0, height=CHIP_HEIGHT)
        self.icon = TintedIcon(self.frame, icon_name, size=ICON_SIZE,
                               color=theme.MUTED, motion_manager=motion_manager)
        self.icon.label.pack(side='left', padx=(10, 6), pady=(9, 9))
        self.value = ctk.CTkLabel(self.frame, text='—', text_color=theme.TEXT,
                                  font=theme.FONT_CAPTION, anchor='w')
        self.value.pack(side='left', padx=(0, 10), pady=(9, 9))
        self.tooltip = Tooltip(self.frame, parent=tooltip_parent)

        for widget in (self.frame, self.icon.label, self.value):
            self._bind(widget)
            widget.bind('<Enter>', lambda _event: self._hover(True), add='+')
            widget.bind('<Leave>', lambda _event: self._hover(False), add='+')
        # Фокус приходит на окно самого виджета: у CustomTkinter `bind`
        # уходит на внутренний холст, где события фокуса не возникают.
        bind_widget(self.frame, '<FocusIn>', lambda _event: self._hover(True))
        bind_widget(self.frame, '<FocusOut>', lambda _event: self._hover(False))
        focusable(self.frame)

    # ------------------------------------------------------------------
    def _bind(self, widget):
        """Нажатие мышью и клавиатурой открывают панель.

        Мышь: событие приходит на внутренний холст CustomTkinter, туда и
        вешается обработчик. Клавиатура: событие приходит на окно самого
        виджета (оно получает фокус), поэтому нужен прямой `bind`.
        """
        widget.bind('<Button-1>', lambda _event: self.on_click(self.key), add='+')
        bind_widget(widget, '<Return>', lambda _event: self.on_click(self.key))
        bind_widget(widget, '<space>', lambda _event: self.on_click(self.key))

    def _background(self) -> str:
        """Фон индикатора: активный, наведение, обычный."""
        if self._active:
            return theme.CARD_ALT
        if self._hovered:
            return theme.CARD
        return theme.BG

    def _refresh_background(self):
        """Мягко перекрасить фон индикатора (без рамок)."""
        self.motion.animate_color(self.frame, 'fg_color', self._background(), duration=140)

    def _hover(self, active: bool):
        """Подсветка при наведении и при фокусе с клавиатуры."""
        if self._hovered == active:
            return
        self._hovered = active
        self._refresh_background()
        if active:
            self.tooltip.schedule()
        else:
            self.tooltip.hide()

    # ------------------------------------------------------------------
    def set_state(self, state, compact=False):
        """Показать состояние индикатора (словарь из `app.ui.status`).

        Цвет иконки — это состояние компонента; подпись рядом нужна для
        доступности (цвет всегда дополнен текстом). Подсказка обновляется
        вместе с состоянием.
        """
        if not state:
            return
        self.state = state
        color = state.get('color') or theme.MUTED
        text = state.get('short') or state.get('label', '—')
        self.value.configure(text=text or '—')
        self.icon.set_color(color)
        self.tooltip.set_text(f'{self.caption}: {state.get("label", "—")}')

    def set_active(self, active: bool):
        """Отметить индикатор активным (раскрыта его панель)."""
        if self._active == active:
            return
        self._active = active
        self._refresh_background()

    def set_compact(self, tight: bool):
        """В очень узком окне подпись скрывается, иконка остаётся видна."""
        try:
            if tight and self.value.winfo_manager():
                self.value.pack_forget()
            elif not tight and not self.value.winfo_manager():
                self.value.pack(side='left', padx=(0, 10), pady=(9, 9))
        except tk.TclError:
            pass

    def shutdown(self):
        """Остановить анимации индикатора."""
        self.tooltip.hide()
        try:
            self.icon.shutdown()
        except Exception:
            pass


class SystemHeader:
    """Верхняя панель: навигация слева, три индикатора справа."""

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

        # --- правая часть: три компактных индикатора ---
        self.control = ctk.CTkFrame(self.frame, fg_color='transparent', corner_radius=10)
        self.control.pack(side='right')
        for key in ('service', 'camera', 'telegram'):
            chip = StatusChip(self.control, key, INDICATOR_CAPTIONS[key],
                              INDICATOR_ICONS[key], self.toggle_details, self.motion,
                              tooltip_parent=master)
            chip.frame.pack(side='left', padx=(0, theme.GROUP_GAP - 2))
            self.chips[key] = chip
        self.service_chip = self.chips['service']
        self.camera_chip = self.chips['camera']
        self.telegram_chip = self.chips['telegram']

        # Контекстная панель диагностики (одна на все три компонента).
        self.details = ServicePanel(overlay, owner, on_toggle=self._on_panel_toggle)
        self.panel = self.details

        overlay.bind('<Button-1>', self._on_overlay_click, add='+')
        overlay.bind('<Escape>', self._on_escape, add='+')
        overlay.bind('<Configure>', self._fit_width, add='+')

    # ------------------------------------------------------------------
    # Панель диагностики
    # ------------------------------------------------------------------
    @property
    def action_button(self):
        """Кнопка главного действия (по текущему состоянию сервиса)."""
        return self.details.action_button

    @property
    def restart_button(self):
        """Кнопка перезапуска сервиса."""
        return self.details.restart_button

    def _on_panel_toggle(self, opened, key=None):
        """Активный индикатор отмечается подсветкой, остальные — нет."""
        for chip_key, chip in self.chips.items():
            chip.set_active(bool(opened) and chip_key == key)

    def toggle_details(self, key):
        """Нажатие на индикатор: открыть, переключить или закрыть панель."""
        chip = self.chips.get(key)
        if chip is not None:
            chip.tooltip.hide()
        self.details.toggle(anchor=self.control, section=key)

    def toggle_panel(self):
        """Совместимое переключение панели (без кнопки «Управление»)."""
        self.details.toggle(anchor=self.control)

    def close_details(self):
        """Закрыть панель диагностики."""
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
        """Обновить содержимое панели диагностики (один раз за цикл опроса)."""
        self.details.refresh()

    def set_running(self, running):
        """Учесть состояние сервиса в панели диагностики."""
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
        """Передать результат операции в панель диагностики."""
        self.details.refresh()

    # ------------------------------------------------------------------
    # Ширина
    # ------------------------------------------------------------------
    def _fit_width(self, _event=None):
        """Подогнать шапку под ширину окна.

        Шапка компактна по построению: у индикаторов нет названий и
        цветных точек, отдельной кнопки «Управление» тоже нет. Если
        окно всё же слишком узкое, первой скрывается краткая подпись
        состояния — иконка (носитель состояния) остаётся видна.

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
        self._full_width = max(self._full_width, needed) if not self.compact \
            else self._full_width
        full = self._full_width or needed
        # Подпись состояния скрывается первой: иконка — носитель
        # состояния, и она должна остаться видимой всегда.
        if full > available and not self.compact:
            self.compact = True
            self.tight = True
            self._refresh_labels()
            try:
                self.control.update_idletasks()
                self._short_width = self.control.winfo_reqwidth()
            except tk.TclError:
                self._short_width = 0
        elif self.compact and full <= available - 24:
            self.compact = False
            self.tight = False
            self._refresh_labels()
            try:
                self.control.update_idletasks()
                self._short_width = 0
            except tk.TclError:
                pass

    def _refresh_labels(self):
        for key, chip in self.chips.items():
            chip.set_compact(self.compact)
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
        for chip in self.chips.values():
            chip.shutdown()
        self.details.shutdown()
        self.motion.cancel_all()
