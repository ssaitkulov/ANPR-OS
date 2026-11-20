"""Контекстная панель диагностики.

Одна физическая панель с тремя режимами: `service`, `camera`, `telegram`.
Панель раскрывается по нажатию на соответствующий индикатор шапки и
показывает данные только выбранного компонента: сломанная камера не
превращает панель сервиса в красную, а недоступный Telegram не мешает
управлять ANPR-сервисом.

Управление ANPR-сервисом (запуск, остановка, перезапуск, журнал) есть
только в режиме «сервис» — в режимах камеры и Telegram его нет.

Содержимое каждого режима создаётся один раз. При переключении режима
меняется только видимый блок: сама панель не пересоздаётся и не закрывается,
поэтому переключение не мерцает, не сдвигает Dashboard и не меняет
горизонтальное положение панели.
"""
from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from app.ui import icons
from app.ui import motion
from app.ui import theme
from app.ui.widgets import TintedIcon, animated_hover, focusable

#: Ширина панели, px (одинаковая для всех режимов).
PANEL_WIDTH = 420
#: Правое поле панели относительно окна, px.
PANEL_MARGIN = 20
#: Отступ панели от группы индикаторов, px.
PANEL_GAP = 8
#: Внутренние отступы панели, px.
PANEL_PADDING = 18

#: Режимы панели.
MODES = ('service', 'camera', 'telegram')
#: Режим по умолчанию (открывается, когда режим не задан).
DEFAULT_MODE = 'service'

#: Заголовки режимов.
MODE_TITLES = {
    'service': 'ANPR-сервис',
    'camera': 'Камера',
    'telegram': 'Telegram',
}

#: Иконки режимов (Tabler Filled).
MODE_ICONS = {
    'service': 'device-desktop',
    'camera': 'device-cctv',
    'telegram': 'brand-messenger',
}


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
            pair[0].grid(row=index, column=0, sticky='w', padx=(0, 10), pady=(6, 4))
            pair[1].grid(row=index, column=1, sticky='ew', pady=(6, 4))
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

    def captions(self):
        """Подписи видимых строк (для проверок и тестов)."""
        visible = []
        for caption, _value in self._rows:
            try:
                if not caption.grid_info():
                    continue
            except tk.TclError:
                continue
            visible.append(caption.cget('text'))
        return visible


class ModeBlock:
    """Содержимое одного режима панели.

    Блок создаётся один раз и переиспользуется: при переключении режима
    меняются только тексты и цвета, поэтому переключение не мерцает.

    Раскладка — через `grid`: упаковка переставляет элементы при повторном
    показе, а `grid` сохраняет порядок строк. Пустые контейнеры
    CustomTkinter не создаются: такой контейнер занимает 200×200 px и
    растягивает панель.
    """

    def __init__(self, parent, key, width):
        self.key = key
        self.frame = ctk.CTkFrame(parent, fg_color='transparent')
        self.frame.grid_columnconfigure(0, weight=0)
        self.frame.grid_columnconfigure(1, weight=1)
        self.rows = DetailRows(self.frame, width)
        self.rows.frame.grid(row=0, column=0, columnspan=2, sticky='ew', pady=(2, 0))
        # Управление сервисом есть только в режиме «сервис».
        self.actions = None
        if key == 'service':
            # Высота задана явно: у пустого контейнера CustomTkinter
            # остаётся собственная высота (200 px), и он растянул бы панель.
            self.actions = ctk.CTkFrame(self.frame, fg_color='transparent', height=34)
            self.actions.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(16, 0))

    def update(self, rows):
        """Обновить строки показателей."""
        self.rows.update(rows)


class ServicePanel:
    """Контекстная панель: одна панель, три режима."""

    def __init__(self, overlay, owner, on_toggle=None):
        self.overlay = overlay
        self.owner = owner
        self.on_toggle = on_toggle
        self.key = None
        self.open_state = False
        self.closed = False
        self._anchor = None
        self._progress_job = None
        self._token = object()

        self.motion = motion.Motion(overlay)
        self.frame = ctk.CTkFrame(overlay, fg_color=theme.CARD, corner_radius=12,
                                  border_width=0, width=PANEL_WIDTH)

        body = ctk.CTkFrame(self.frame, fg_color='transparent')
        body.pack(fill='both', expand=True, padx=PANEL_PADDING, pady=(14, 16))
        # Размеры панели задаются явно: иначе ширина определяется
        # содержимым (и режимы разной длины давали бы разную ширину),
        # а высота — пустыми контейнерами CustomTkinter.
        body.pack_propagate(False)
        self.body = body

        # --- заголовок: название режима, состояние и закрытие ---
        head = ctk.CTkFrame(body, fg_color='transparent')
        head.pack(fill='x', pady=(0, 6))
        self.head = head
        self.title = ctk.CTkLabel(head, text='', text_color=theme.TEXT,
                                  font=theme.FONT_TITLE, anchor='w')
        self.title.pack(side='left')
        self.close_button = ctk.CTkButton(head, text='', width=28, height=28, corner_radius=8,
                                          fg_color=theme.CARD, hover_color=theme.CARD,
                                          image=icons.icon('x', theme.MUTED, 14, master=head),
                                          command=self.close)
        focusable(self.close_button)
        animated_hover(self.close_button, self.motion, theme.CARD, theme.CARD_ALT)
        self.close_button.pack(side='right', padx=(10, 0))
        self.status_label = ctk.CTkLabel(head, text='—', text_color=theme.MUTED,
                                         font=theme.FONT_CAPTION, anchor='e')
        self.status_label.pack(side='right')
        self.status_icon = TintedIcon(head, MODE_ICONS[DEFAULT_MODE], size=18,
                                      color=theme.MUTED, motion_manager=self.motion)
        self.status_icon.label.pack(side='right', padx=(0, 6))

        # --- содержимое режимов ---
        self.content = ctk.CTkFrame(body, fg_color='transparent', height=0)
        self.content.pack(fill='x')
        # Высота содержимого задаётся вручную: блоки режимов размещаются
        # через `place`, а высота панели должна точно соответствовать
        # выбранному режиму.
        self.content.pack_propagate(False)
        self.modes = {}
        for key in MODES:
            block = ModeBlock(self.content, key, PANEL_WIDTH - 2 * PANEL_PADDING)
            self.modes[key] = block
        #: Совместимое имя: разделы панели = блоки режимов.
        self.sections = self.modes

        self._build_service_actions(self.modes['service'])

    # ------------------------------------------------------------------
    # Сборка (управление сервисом — только в режиме «сервис»)
    # ------------------------------------------------------------------
    def _build_service_actions(self, mode):
        """Кнопки управления сервисом, индикатор операции и сообщение."""
        actions = mode.actions
        self.start_button = self._button(actions, 'Запустить', 'player-play', theme.CTA,
                                         '#101827', '#E0C85A', self.owner.start_service)
        self.restart_button = self._button(actions, 'Перезапустить', 'player-skip-back',
                                            theme.CARD_ALT, theme.TEXT, theme.ROW_HOVER,
                                            self.owner.restart_service)
        self.stop_button = self._button(actions, 'Остановить', 'player-stop', theme.DANGER,
                                        '#FFFFFF', '#D03934', self.owner.stop_service)
        self.log_button = self._button(actions, 'Журнал', 'file-text', theme.CARD,
                                       theme.MUTED, theme.CARD_ALT, self.owner.open_log)

        self.progress_track = ctk.CTkFrame(mode.frame, fg_color=theme.CARD_ALT, height=4,
                                           corner_radius=2)
        self.progress_bar = ctk.CTkFrame(self.progress_track, fg_color=theme.ACCENT,
                                         width=48, height=4, corner_radius=2)
        self.progress_label = ctk.CTkLabel(mode.frame, text='', text_color=theme.ACCENT,
                                           font=theme.FONT_CAPTION, anchor='w')
        self.message_label = ctk.CTkLabel(mode.frame, text='', text_color=theme.MUTED,
                                          font=theme.FONT_CAPTION, anchor='w', justify='left',
                                          wraplength=PANEL_WIDTH - 2 * PANEL_PADDING - 26)
        self.message_icon = ctk.CTkLabel(mode.frame, text='', width=18, height=18)
        self._layout_footer(mode)

    def _layout_footer(self, mode):
        """Разместить индикатор операции и сообщение под управлением."""
        self.progress_track.grid(row=2, column=0, columnspan=2, sticky='ew', pady=(14, 0))
        self.progress_label.grid(row=3, column=0, columnspan=2, sticky='ew', pady=(6, 0))
        self.message_icon.grid(row=4, column=0, sticky='w', pady=(12, 0))
        self.message_label.grid(row=4, column=1, sticky='ew', pady=(12, 0))

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
        """Показать панель в нужном режиме.

        Если панель уже открыта, содержимое переключается: сама панель
        остаётся на месте, поэтому переключение не мерцает и не сдвигает
        Dashboard.
        """
        if anchor is not None:
            self._anchor = anchor
        key = section or self.key or DEFAULT_MODE
        switching = self.open_state and key != self.key
        self.key = key
        self.open_state = True
        self.refresh()
        self.frame.lift()
        self._token = object()
        token = self._token
        x, y = self._position()
        if switching:
            # Панель уже на экране: меняется только содержимое.
            self.frame.place_configure(x=x, y=y)
            self._animate_content()
        else:
            self.motion.slide(self.frame, x, y, duration=200,
                              guard=lambda: token is self._token and self.open_state)
        self._notify()

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

    def toggle(self, anchor=None, section=None):
        """Открыть, переключить или закрыть панель.

        Повторное нажатие на активный индикатор закрывает панель, нажатие
        на другой — переключает содержимое, не закрывая её.
        """
        if self.open_state and (section is None or section == self.key):
            self.close()
            return
        self.open(anchor=anchor, section=section)

    def _notify(self):
        """Сообщить владельцу о смене состояния панели."""
        if self.on_toggle is None:
            return
        try:
            self.on_toggle(self.open_state, self.key)
        except TypeError:
            try:
                self.on_toggle(self.open_state)
            except Exception:
                pass
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Геометрия
    # ------------------------------------------------------------------
    def _unscaled(self, value) -> float:
        """Перевести экранные пиксели в систему координат CustomTkinter.

        `place` у CustomTkinter масштабирует координаты под DPI, поэтому
        положение панели нужно считать в той же системе, что и её
        размеры: иначе при 125 % панель вышла бы за правый край окна.
        """
        try:
            return float(self.frame._reverse_widget_scaling(value))
        except Exception:
            return float(value)

    def _position(self):
        """Положение панели: под группой индикаторов, по правому краю."""
        try:
            overlay_width = self.overlay.winfo_width()
            overlay_height = self.overlay.winfo_height()
        except tk.TclError:
            return PANEL_MARGIN, 64
        if overlay_width <= 1:
            overlay_width = self.overlay.winfo_screenwidth()
        if overlay_height <= 1:
            overlay_height = self.overlay.winfo_screenheight()
        width = self._unscaled(overlay_width)
        height = self._unscaled(overlay_height)
        x = max(PANEL_MARGIN, width - PANEL_WIDTH - PANEL_MARGIN)
        y = PANEL_GAP + 4
        anchor = self._anchor
        if anchor is not None:
            try:
                bottom = (self._unscaled(anchor.winfo_rooty())
                          - self._unscaled(self.overlay.winfo_rooty())
                          + self._unscaled(anchor.winfo_height()))
                y = bottom + PANEL_GAP
            except tk.TclError:
                pass
        y = max(PANEL_GAP, min(y, max(PANEL_GAP, height - 40)))
        return int(x), int(y)

    def _fit(self):
        """Подогнать панель под окно: при нехватке места она сдвигается вверх."""
        if not self.open_state:
            return
        try:
            height = self._unscaled(self.frame.winfo_reqheight())
            overlay_height = self._unscaled(self.overlay.winfo_height())
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
        if self.closed:
            return
        data = self.owner.panel_data()
        key = self.key or DEFAULT_MODE
        block = data.get(key) or {}
        state = block.get('state') or {}
        rows = block.get('rows') or []

        self._update_head(key, state)
        mode = self.modes[key]
        mode.update(rows)
        if key == 'service':
            self._update_actions(data)
            self._update_message(data.get('message') or '', data.get('message_tone') or '')
        else:
            # Управление ANPR-сервисом в режимах камеры и Telegram не
            # показывается: чужой компонент не должен выглядеть сломанным.
            self._hide_actions()
            self._stop_progress()
            self._hide_message()
        if not self.open_state:
            # Скрытая панель не делает дорогую перекладку Tk; состояние
            # кнопок выше обновляется в любом случае.
            return
        self._layout_content(key)
        self._fit()

    def _update_head(self, key, state):
        """Заголовок: название режима, цветная иконка состояния и статус."""
        self.title.configure(text=MODE_TITLES.get(key, ''))
        color = (state or {}).get('color') or theme.MUTED
        self.status_icon.set_icon(MODE_ICONS.get(key, MODE_ICONS[DEFAULT_MODE]))
        self.status_icon.set_color(color)
        label = (state or {}).get('short') or (state or {}).get('label') or '—'
        self.status_label.configure(text=label)

    def _layout_content(self, key):
        """Показать блок выбранного режима и задать размеры панели."""
        for other_key, other in self.modes.items():
            if other_key != key and other.frame.winfo_manager():
                other.frame.place_forget()
        mode = self.modes[key]
        try:
            self.frame.update_idletasks()
        except tk.TclError:
            pass
        content_height = mode.frame.winfo_reqheight()
        head_height = self.head.winfo_reqheight()
        try:
            self.content.configure(height=content_height)
            self.body.configure(width=PANEL_WIDTH - 2 * PANEL_PADDING,
                                height=head_height + content_height + 6)
        except tk.TclError:
            pass
        if not mode.frame.winfo_manager():
            mode.frame.place(x=0, y=0, relwidth=1)

    def _animate_content(self):
        """Короткое появление содержимого при переключении режима.

        Панель при этом не пересоздаётся и не меняет положение: меняется
        только содержимое и высота.
        """
        key = self.key or DEFAULT_MODE
        mode = self.modes.get(key)
        if mode is None or self.motion.reduced:
            return
        offset = 10

        def step(fraction: float):
            try:
                mode.frame.place_configure(y=int(offset * (1 - fraction)))
            except tk.TclError:
                pass

        self.motion.tween(140, step,
                          guard=lambda: self.open_state and self.key == key)

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

    def _hide_actions(self):
        """Убрать управление сервисом (режимы камеры и Telegram)."""
        for button in (self.start_button, self.restart_button,
                       self.stop_button, self.log_button):
            self._toggle(button, False)

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

    def _hide_message(self):
        """Убрать строку сообщения (в режимах без управления сервисом)."""
        try:
            self.message_label.configure(text='')
        except tk.TclError:
            pass
        self._pack_message(False)

    def _pack_message(self, visible: bool, icon=False):
        """Показать или убрать строку сообщения."""
        try:
            if visible:
                if icon:
                    self.message_icon.grid(row=4, column=0, sticky='w', pady=(12, 0))
                else:
                    self.message_icon.grid_remove()
                self.message_label.grid(row=4, column=1, sticky='ew', pady=(12, 0))
            else:
                self.message_icon.grid_remove()
                self.message_label.grid_remove()
        except tk.TclError:
            pass

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
            self.progress_track.grid(row=2, column=0, columnspan=2, sticky='ew', pady=(14, 0))
            self.progress_label.grid(row=3, column=0, columnspan=2, sticky='ew', pady=(6, 0))
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
            self.progress_track.grid_remove()
            self.progress_label.grid_remove()
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
        self.closed = True
        self._stop_progress()
        try:
            self.status_icon.shutdown()
        except Exception:
            pass
        self.motion.cancel_all()
