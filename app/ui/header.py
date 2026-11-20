"""Постоянная верхняя панель приложения «Эврика».

Header — единственное место, где видно состояние системы.

Устройство шапки:

* слева — логотип и сегментированный переключатель разделов
  («Мониторинг», «Настройки»);
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

#: Иконки вкладок навигации (Tabler Filled).
TAB_ICONS = {
    'monitor': 'dashboard',
    'settings': 'settings',
}

#: Высота сегментированного переключателя, px (общий токен шапки).
TABS_HEIGHT = theme.ROW_HEIGHT
#: Внутренний отступ контейнера вкладок, px.
TABS_PADDING = 3
#: Радиус активной подложки, px.
TABS_RADIUS = 8
#: Размер иконки вкладки, px.
TAB_ICON_SIZE = 17
#: Горизонтальный отступ содержимого вкладки, px.
TAB_PAD_X = 10
#: Зазор между иконкой и подписью вкладки, px.
TAB_GAP = 6
#: Длительность перехода подложки, мс.
TABS_TRANSITION = 180
#: Насколько подсвечивается неактивная вкладка при наведении (0..1).
TAB_HOVER_MIX = 0.35


class TabLabel:
    """Содержимое вкладки: иконка Tabler Filled и подпись.

    Копия содержимого создаётся дважды: одна стоит на месте (вкладка
    неактивна), вторая едет вместе с подложкой (вкладка активна).

    Почему две копии: заливку активной вкладки даёт подложка, а
    CustomTkinter не умеет настоящей прозрачности — вложенный виджет
    переносит на себя фон родителя. Поэтому подпись и иконка активной
    вкладки лежат поверх подложки и едут вместе с ней, а залитая
    подложка остаётся видимой.
    """

    def __init__(self, parent, icon_name, title, motion_manager):
        self.motion = motion_manager
        self.frame = ctk.CTkFrame(parent, fg_color='transparent',
                                  corner_radius=TABS_RADIUS, border_width=0)
        self.icon = TintedIcon(self.frame, icon_name, size=TAB_ICON_SIZE,
                               color=theme.MUTED, motion_manager=motion_manager)
        self.icon.label.pack(side='left', padx=(TAB_PAD_X, TAB_GAP))
        self.label = ctk.CTkLabel(self.frame, text=title, text_color=theme.MUTED,
                                  font=theme.FONT_CAPTION, anchor='w',
                                  height=TABS_HEIGHT - 2 * TABS_PADDING, corner_radius=0)
        self.label.pack(side='left', padx=(0, TAB_PAD_X))

    def set_color(self, color):
        """Цвет иконки и подписи: цвет — единственный носитель состояния."""
        self.icon.set_color(color)
        self.motion.animate_color(self.label, 'text_color', color, duration=140)


class SectionTab:
    """Вкладка сегментированного переключателя разделов.

    Объект сохраняет прежний интерфейс кнопки навигации: оболочка
    переключает вкладки через `configure(fg_color=…)`. В
    сегментированном переключателе акцентную заливку даёт подвижная
    подложка, поэтому вкладка только запоминает состояние и передаёт
    его переключателю.
    """

    def __init__(self, controller, key, title, icon_name, motion_manager):
        self.controller = controller
        self.key = key
        self.title = title
        self.config = {}

        self.static = TabLabel(controller.track, icon_name, title, motion_manager)
        self.static.frame.pack(side='left')
        self.slider = TabLabel(controller.pill, icon_name, title, motion_manager)

        for widget in (self.static.frame, self.static.icon.label,
                       self.static.label):
            widget.bind('<Button-1>', self._click, add='+')
            widget.bind('<Enter>', self._enter, add='+')
            widget.bind('<Leave>', self._leave, add='+')
        focusable(self.static.frame)
        bind_widget(self.static.frame, '<Return>', self._activate)
        bind_widget(self.static.frame, '<space>', self._activate)
        bind_widget(self.static.frame, '<FocusIn>',
                    lambda _event: controller.focus_in(self.key))
        bind_widget(self.static.frame, '<FocusOut>',
                    lambda _event: controller.focus_out(self.key))

    # ------------------------------------------------------------------
    def _click(self, _event=None):
        """Клик мышью по вкладке: переключить раздел."""
        try:
            self.static.frame.focus_set()
        except tk.TclError:
            pass
        self.controller.select(self.key)

    def _activate(self, _event=None):
        """Enter или пробел: то же, что клик мышью."""
        self.controller.select(self.key)

    def _enter(self, _event=None):
        """Наведение: слегка подсветить неактивную вкладку."""
        self.controller.hover(self.key, True)

    def _leave(self, _event=None):
        """Уход курсора: вернуть обычный цвет подписи."""
        self.controller.hover(self.key, False)

    def configure(self, **kwargs):
        """Совместимость с прежней кнопкой навигации.

        Оболочка сообщает активную вкладку заливкой (`fg_color`).
        Красит вкладку подложка переключателя, поэтому здесь достаточно
        запомнить состояние и передать его переключателю.
        """
        self.config.update(kwargs)
        color = str(kwargs.get('fg_color', '')).lower()
        if color in ('', 'transparent'):
            return
        self.controller.activate(self.key)


class SegmentedTabs:
    """Единый сегментированный переключатель разделов.

    Один контейнер с общим фоном и двумя вкладками. Активной вкладке
    соответствует подвижная акцентная подложка: она плавно переезжает
    к активной вкладке (180 мс, ease-in-out), сами вкладки при этом не
    пересоздаются. Повторное переключение отменяет начатую анимацию.

    Подложка — единственный элемент с собственной заливкой: у неактивной
    вкладки заливки нет, обводок нет.
    """

    def __init__(self, parent, motion_manager, on_select):
        self.motion = motion_manager
        self.on_select = on_select
        self.tabs = {}
        self.nav_buttons = {}      # совместимое имя: key → вкладка
        self.active = None
        self._geometry = None      # положение подложки (пиксели окна)
        self._animating = False
        self._token = object()
        self._hover = {}

        self.frame = ctk.CTkFrame(parent, fg_color=theme.CARD, corner_radius=10,
                                  border_width=0, height=TABS_HEIGHT)
        self.track = ctk.CTkFrame(self.frame, fg_color='transparent', corner_radius=0,
                                  border_width=0)
        self.track.pack(fill='both', expand=True, padx=TABS_PADDING, pady=TABS_PADDING)
        self.pill = ctk.CTkFrame(self.track, fg_color=theme.ACCENT,
                                 corner_radius=TABS_RADIUS, border_width=0)

    # ------------------------------------------------------------------
    def add_tab(self, key, title, icon_name):
        """Создать вкладку (один раз за жизнь приложения)."""
        tab = SectionTab(self, key, title, icon_name, self.motion)
        self.tabs[key] = tab
        self.nav_buttons[key] = tab
        self.pill.lift()       # подложка рисуется поверх подписей
        return tab

    # ------------------------------------------------------------------
    # Переключение
    # ------------------------------------------------------------------
    def select(self, key):
        """Клик или клавиатура: открыть раздел.

        Повторный выбор активной вкладки ничего не делает: раздел не
        перезапускается.
        """
        if key not in self.tabs or key == self.active:
            return
        self.on_select(key)

    def activate(self, key):
        """Сделать вкладку активной (её вызывает оболочка)."""
        if key not in self.tabs or key == self.active:
            return
        self.active = key
        for other_key, other in self.tabs.items():
            other.static.set_color(self._resting_color(other_key))
        self._show_in_pill(self.tabs[key])
        target = self._slot(key)
        if target is None:
            # Окно ещё не показано — положение вкладок неизвестно,
            # подложка встанет на место при первой перерисовке.
            self._geometry = None
            return
        self._animate(target)

    def hover(self, key, entering):
        """Наведение курсора на неактивную вкладку."""
        if key == self.active:
            return
        self._hover[key] = bool(entering)
        tab = self.tabs.get(key)
        if tab is not None:
            tab.static.set_color(self._resting_color(key))

    def focus_in(self, key):
        """Фокус с клавиатуры: у активной вкладки — на подложке.

        Подложка закрывает вкладку целиком, поэтому рамку фокуса рисуем
        на ней — иначе фокус был бы не виден.
        """
        if key == self.active:
            self._set_border(self.pill, 1, theme.TEXT)

    def focus_out(self, key):
        """Фокус ушёл: убрать рамку с подложки."""
        if key == self.active:
            self._set_border(self.pill, 0)

    # ------------------------------------------------------------------
    # Геометрия подложки
    # ------------------------------------------------------------------
    def _slot(self, key):
        """Положение и размер вкладки внутри трека (пиксели окна)."""
        tab = self.tabs.get(key)
        if tab is None:
            return None
        try:
            x = tab.static.frame.winfo_x()
            y = tab.static.frame.winfo_y()
            width = tab.static.frame.winfo_width()
            height = tab.static.frame.winfo_height()
        except tk.TclError:
            return None
        if width <= 1 or height <= 1:
            return None      # геометрия ещё не вычислена
        return x, y, width, height

    def _place(self, geometry):
        """Поставить подложку в указанное место (пиксели окна)."""
        x, y, width, height = geometry
        try:
            # `place` CustomTkinter масштабирует x и y, а нужны реальные
            # пиксели окна: геометрию вкладок уже посчитал Tk.
            self.pill.place_configure(x=x, y=y, width=width, height=height)
        except tk.TclError:
            return
        self._geometry = geometry

    def _animate(self, target):
        """Плавно переместить подложку к вкладке."""
        start = self._geometry
        self._token = object()
        token = self._token
        if start is None:
            self._place(target)
            return
        start_x, start_y, start_width, start_height = start

        def step(fraction):
            self._place((start_x + (target[0] - start_x) * fraction,
                         start_y + (target[1] - start_y) * fraction,
                         start_width + (target[2] - start_width) * fraction,
                         start_height + (target[3] - start_height) * fraction))

        self._animating = True
        self.motion.tween(TABS_TRANSITION, step, on_complete=lambda: self._animation_done(target),
                          guard=lambda: token is self._token,
                          easing=motion.ease_in_out)

    def _animation_done(self, target):
        """Анимация закончилась: подложка встаёт точно на место."""
        self._animating = False
        self._place(target)

    def sync(self):
        """Подогнать подложку под активную вкладку (после перерисовки окна)."""
        if self._animating:
            return
        target = self._slot(self.active)
        if target is None:
            return
        if self._geometry != target:
            self._place(target)

    # ------------------------------------------------------------------
    # Вспомогательное
    # ------------------------------------------------------------------
    def _resting_color(self, key):
        """Цвет подписи, стоящей на месте.

        У активной вкладки подпись не нужна — её закрывает подложка,
        поэтому она совпадает с фоном контейнера.
        """
        if key == self.active:
            return theme.CARD
        if self._hover.get(key):
            return theme.blend(theme.MUTED, theme.TEXT, TAB_HOVER_MIX)
        return theme.MUTED

    def _show_in_pill(self, tab):
        """Показать белую копию подписи активной вкладки в подложке."""
        for other in self.tabs.values():
            if other is not tab:
                other.slider.frame.pack_forget()
        tab.slider.set_color(theme.TEXT)
        tab.slider.frame.pack(side='left', padx=(TAB_PAD_X, 0))

    @staticmethod
    def _set_border(widget, width, color=None):
        """Рамка фокуса (постоянных обводок в интерфейсе нет)."""
        try:
            widget.configure(border_width=width)
            if color is not None:
                widget.configure(border_color=color)
        except (tk.TclError, ValueError):
            pass


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
        # Строка вкладок выше прежних кнопок навигации, поэтому верхний
        # отступ на пиксель меньше: высота шапки не изменилась.
        self.frame.pack(fill='x', padx=theme.GROUP_GAP * 2, pady=(13, 6))
        self.chips = {}
        self._states = {}
        self._full_width = 0
        self._short_width = 0
        self.tight = False
        self._fitting = False
        self._refit = False
        self._refit_job = None
        self._tabs_sync_job = None
        self.closed = False
        self.motion = motion.Motion(master)

        # --- левая часть: логотип и навигация ---
        self.left = ctk.CTkFrame(self.frame, fg_color='transparent')
        self.left.pack(side='left')
        self.logo = ctk.CTkImage(light_image=owner.logo_image(), size=(116, 25))
        ctk.CTkLabel(self.left, text='', image=self.logo, anchor='w').pack(side='left')
        nav = ctk.CTkFrame(self.left, fg_color='transparent')
        nav.pack(side='left', padx=(14, 0))
        self.tabs = SegmentedTabs(nav, self.motion, self._on_tab_selected)
        self.tabs.frame.pack(side='left')
        for key in ('monitor', 'settings'):
            self.tabs.add_tab(key, owner.section_titles[key], TAB_ICONS[key])
        self.nav_buttons = self.tabs.nav_buttons

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
    # Навигация
    # ------------------------------------------------------------------
    def _on_tab_selected(self, key):
        """Клик по вкладке: открыть соответствующий раздел."""
        self.owner.show_section(key)

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
        # Ширину и принудительную обработку idle-событий делает _fit_width_now
        # по <Configure>: здесь они только добавляли паузу на каждом опросе
        # и затирали накопленный максимум ширины.

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
        # Подложка вкладок живёт по реальным координатам окна: после
        # перерисовки её нужно поставить на место.
        self._schedule_tabs_sync()

    def _schedule_tabs_sync(self):
        """Одна подгонка подложки на пакет Configure."""
        if self._tabs_sync_job is not None:
            try:
                self.overlay.after_cancel(self._tabs_sync_job)
            except tk.TclError:
                pass
            self._tabs_sync_job = None
        try:
            self._tabs_sync_job = self.overlay.after_idle(self.tabs.sync)
        except tk.TclError:
            self._tabs_sync_job = None

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
        if self._tabs_sync_job is not None:
            try:
                self.overlay.after_cancel(self._tabs_sync_job)
            except tk.TclError:
                pass
            self._tabs_sync_job = None
        for chip in self.chips.values():
            chip.shutdown()
        self.details.shutdown()
        self.motion.cancel_all()
