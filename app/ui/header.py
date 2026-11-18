"""Постоянная верхняя панель приложения «Эврика».

Header — единственное место управления ANPR-сервисом и единственное место,
где показываются состояния системы:

* сервис (работает / остановлен / запускается / останавливается / ошибка);
* камера (подключена / нет связи / получение данных / ошибка подключения);
* Telegram (работает / ошибка доставки / нет соединения / не настроен).

Кнопки «Запустить»/«Остановить» и «Перезапустить» живут здесь же.
Нажатие на индикатор открывает компактную панель подробностей с
диагностикой (текущее состояние, последняя успешная проверка, последняя
ошибка и доступные из API детали).
"""
import tkinter as tk

import customtkinter as ctk

from app.ui import status as ui_status

#: Ширина панели подробностей, px.
DETAILS_WIDTH = 364


class StatusChip:
    """Небольшой индикатор: подпись, точка и значение состояния."""

    def __init__(self, parent, key, caption, on_click):
        self.key = key
        self.frame = ctk.CTkFrame(parent, fg_color='transparent')
        caption_label = ctk.CTkLabel(self.frame, text=caption.upper(), text_color=ui_status.MUTED,
                                     font=('Segoe UI', 9), anchor='w')
        caption_label.pack(anchor='w')
        row = ctk.CTkFrame(self.frame, fg_color='transparent')
        row.pack(anchor='w')
        self.dot = ctk.CTkFrame(row, width=9, height=9, corner_radius=5, fg_color=ui_status.MUTED)
        self.dot.pack(side='left', padx=(0, 6), pady=(1, 0))
        self.value = ctk.CTkLabel(row, text='Проверяю…', text_color='#f2f5fa',
                                  font=('Segoe UI', 11, 'bold'), anchor='w', width=112)
        self.value.pack(side='left')
        for widget in (self.frame, caption_label, row, self.dot, self.value):
            widget.bind('<Button-1>', lambda _event: on_click(self.key))

    def set_state(self, state):
        """Показать состояние индикатора (словарь из `app.ui.status`)."""
        self.value.configure(text=state['label'])
        self.dot.configure(fg_color=state['color'])


class DetailsPanel:
    """Компактная панель подробностей поверх содержимого окна."""

    def __init__(self, overlay, owner):
        self.owner = owner
        self.key = None
        self.frame = ctk.CTkFrame(overlay, fg_color='#1a2432', corner_radius=10,
                                   border_color='#303e51', border_width=1, width=DETAILS_WIDTH)
        top = ctk.CTkFrame(self.frame, fg_color='transparent')
        top.pack(fill='x', padx=14, pady=(10, 2))
        self.title = ctk.CTkLabel(top, text='', text_color='#f2f5fa', anchor='w',
                                  font=('Segoe UI', 12, 'bold'))
        self.title.pack(side='left')
        ctk.CTkButton(top, text='✕', width=26, height=26, fg_color='transparent',
                      hover_color='#263b57', text_color=ui_status.MUTED,
                      font=('Segoe UI', 12), command=self.hide).pack(side='right')
        self.rows = ctk.CTkFrame(self.frame, fg_color='transparent')
        self.rows.pack(fill='both', expand=True, padx=14, pady=(2, 12))

    def show(self, key, chip):
        """Открыть панель под индикатором `chip`."""
        self.key = key
        self._render()
        overlay = self.frame.master
        try:
            x = chip.frame.winfo_rootx() - overlay.winfo_rootx() + chip.frame.winfo_width() - DETAILS_WIDTH
            y = chip.frame.winfo_rooty() - overlay.winfo_rooty() + chip.frame.winfo_height() + 6
            width = overlay.winfo_width()
            height = overlay.winfo_height()
            x = max(8, min(x, max(8, width - DETAILS_WIDTH - 8)))
            y = max(8, min(y, max(8, height - self.frame.winfo_reqheight() - 8)))
            self.frame.configure(width=DETAILS_WIDTH)
            self.frame.place(x=x, y=y)
        except tk.TclError:
            return
        self.frame.lift()

    def hide(self):
        """Скрыть панель."""
        self.key = None
        try:
            self.frame.place_forget()
        except tk.TclError:
            pass

    def _render(self):
        for child in self.rows.winfo_children():
            child.destroy()
        title, rows = self.owner.details_data(self.key)
        self.title.configure(text=title)
        for caption, value in rows:
            self._row(caption, value)

    def _row(self, caption, value):
        row = ctk.CTkFrame(self.rows, fg_color='transparent')
        row.pack(fill='x', pady=(2, 0))
        ctk.CTkLabel(row, text=caption, text_color=ui_status.MUTED, anchor='w',
                     font=('Segoe UI', 10), width=150, justify='left').pack(side='left', anchor='w')
        ctk.CTkLabel(row, text=value or '—', text_color='#f2f5fa', anchor='w', justify='left',
                     font=('Segoe UI', 11), wraplength=DETAILS_WIDTH - 190).pack(side='left', fill='x', expand=True)


class SystemHeader:
    """Верхняя панель: навигация слева, единый блок управления справа."""

    def __init__(self, master, overlay, owner):
        self.owner = owner
        self.frame = ctk.CTkFrame(master, fg_color='transparent')
        self.frame.pack(fill='x', padx=24, pady=(16, 8))
        self.chips = {}

        # --- левая часть: логотип и навигация ---
        self.logo = ctk.CTkImage(light_image=owner.logo_image(), size=(128, 27))
        ctk.CTkLabel(self.frame, text='', image=self.logo, anchor='w').pack(side='left')
        nav = ctk.CTkFrame(self.frame, fg_color='transparent')
        nav.pack(side='left', padx=(14, 0))
        self.nav_buttons = {}
        for key in ('monitor', 'settings'):
            button = ctk.CTkButton(nav, text=owner.section_titles[key], width=112, height=34,
                                    fg_color='#1a2432', hover_color='#34455a', border_color='#303e51',
                                    border_width=1, font=('Segoe UI', 12),
                                    command=lambda key=key: owner.show_section(key))
            button.pack(side='left', padx=(8, 0))
            self.nav_buttons[key] = button

        # --- правая часть: единый блок управления системой ---
        control = ctk.CTkFrame(self.frame, fg_color='#1a2432', corner_radius=10,
                               border_color='#303e51', border_width=1)
        control.pack(side='right')
        inner = ctk.CTkFrame(control, fg_color='transparent')
        inner.pack(padx=12, pady=8)
        row = ctk.CTkFrame(inner, fg_color='transparent')
        row.pack(fill='x')

        self.service_chip = StatusChip(row, 'service', 'Сервис', self.toggle_details)
        self.service_chip.frame.pack(side='left', padx=(0, 12))
        self.chips['service'] = self.service_chip

        self.action_button = ctk.CTkButton(row, text='Запустить', width=104, height=34,
                                           fg_color='#2869df', hover_color='#1d55b9',
                                           font=('Segoe UI', 11, 'bold'),
                                           command=self.owner.toggle_service)
        self.action_button.pack(side='left')

        self.restart_button = ctk.CTkButton(row, text='Перезапустить', width=104, height=34,
                                            fg_color='#263b57', hover_color='#365172',
                                            font=('Segoe UI', 11),
                                            command=self.owner.restart_service)
        self.restart_button.pack(side='left', padx=(6, 0))

        ctk.CTkFrame(row, width=1, height=34, fg_color='#303e51').pack(side='left', fill='y', padx=12)

        self.camera_chip = StatusChip(row, 'camera', 'Камера', self.toggle_details)
        self.camera_chip.frame.pack(side='left', padx=(0, 12))
        self.chips['camera'] = self.camera_chip

        self.telegram_chip = StatusChip(row, 'telegram', 'Telegram', self.toggle_details)
        self.telegram_chip.frame.pack(side='left', padx=(0, 12))
        self.chips['telegram'] = self.telegram_chip

        self.log_button = ctk.CTkButton(row, text='Журнал', width=76, height=34,
                                        fg_color='transparent', hover_color='#263b57',
                                        border_color='#303e51', border_width=1,
                                        font=('Segoe UI', 11), command=self.owner.open_log)
        self.log_button.pack(side='left')

        # Короткие сообщения (результат операции) — под блоком индикаторов.
        self.message_label = ctk.CTkLabel(inner, textvariable=owner.message, text_color='#ff9ca5',
                                           font=('Segoe UI', 11), anchor='w', justify='left')
        self.details = DetailsPanel(overlay, owner)
        overlay.bind('<Button-1>', self._on_overlay_click, add='+')

    # ------------------------------------------------------------------
    def sync_message(self):
        """Показать или скрыть строку сообщения под индикаторами."""
        value = (self.owner.message.get() or '').strip()
        if value:
            if not self.message_label.winfo_manager():
                self.message_label.pack(fill='x', pady=(6, 0))
        elif self.message_label.winfo_manager():
            self.message_label.pack_forget()

    # ------------------------------------------------------------------
    def toggle_details(self, key):
        """Открыть/закрыть панель подробностей для индикатора."""
        if self.details.key == key:
            self.details.hide()
            return
        chip = self.chips.get(key)
        if chip is None:
            return
        self.details.show(key, chip)

    def close_details(self):
        self.details.hide()

    def _on_overlay_click(self, event):
        """Клик вне панели подробностей закрывает её."""
        if self.details.key is None:
            return
        widget = getattr(event, 'widget', None)
        if widget is None:
            return
        if self._within(widget, self.details.frame):
            return
        for chip in self.chips.values():
            if self._within(widget, chip.frame):
                return
        self.details.hide()

    @staticmethod
    def _within(widget, ancestor):
        current = widget
        while current is not None:
            if current is ancestor:
                return True
            current = getattr(current, 'master', None)
        return False

    # ------------------------------------------------------------------
    def set_state(self, key, state):
        """Обновить индикатор состояния."""
        chip = self.chips.get(key)
        if chip is not None:
            chip.set_state(state)

    def set_running(self, running):
        """Кнопка запуска/остановки отражает текущее состояние сервиса."""
        self.action_button.configure(text='Остановить' if running else 'Запустить',
                                     fg_color='#45303a' if running else '#2869df',
                                     hover_color='#603844' if running else '#1d55b9')

    def set_busy(self, busy):
        """На время операции кнопки управления блокируются."""
        state = 'disabled' if busy else 'normal'
        self.action_button.configure(state=state)
        self.restart_button.configure(state=state)
