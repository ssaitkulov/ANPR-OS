"""Централизованные анимации интерфейса.

Правила модуля:

* только `after()` — никаких дополнительных потоков и таймеров;
* каждая анимация живёт в одном экземпляре `Motion`, который ведёт список
  отложенных вызовов и умеет отменять их целиком (при закрытии окна это
  избавляет от `invalid command name …` в stderr);
* обращение к уничтоженному виджету не роняет приложение: `TclError`
  перехватывается, анимация просто заканчивается;
* режим `REDUCED_MOTION` отключает движение: состояние меняется сразу.

Анимации намеренно сдержанные: короткая смена цвета индикатора и мягкое
появление панели. Никаких «дорогих» эффектов: интерфейс не должен дёргаться
и не должен перерисовывать себя целиком.
"""
from __future__ import annotations

import json
import os
import tkinter as tk

from app.core.paths import ui_preferences_file
from app.ui import theme

#: Переменная окружения, отключающая движение.
REDUCED_MOTION_ENV = 'EVRIKA_REDUCED_MOTION'

#: Шаг анимации, мс.
FRAME = 16


def reduced_motion() -> bool:
    """Включён ли режим «без анимаций».

    Включается переменной окружения `EVRIKA_REDUCED_MOTION=1` или ключом
    `reduced_motion` в файле настроек панели.
    """
    value = str(os.environ.get(REDUCED_MOTION_ENV, '')).strip().lower()
    if value in ('1', 'true', 'yes', 'on'):
        return True
    if value in ('0', 'false', 'no', 'off'):
        return False
    try:
        data = json.loads(ui_preferences_file().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    return bool(data.get('reduced_motion'))


def ease_out(fraction: float) -> float:
    """Плавное затухание: быстро в начале, мягко в конце."""
    value = max(0.0, min(1.0, float(fraction)))
    return 1 - (1 - value) ** 3


class Motion:
    """Планировщик анимаций одного виджета.

    Экземпляр хранит идентификаторы отложенных вызовов: `cancel_all()`
    отменяет их все, поэтому закрытие окна не оставляет «висящих» callback.
    """

    def __init__(self, widget, reduced: bool | None = None):
        self.widget = widget
        self.reduced = reduced_motion() if reduced is None else bool(reduced)
        self._jobs: set = set()
        self._colors: dict = {}

    # ------------------------------------------------------------------
    # Планирование
    # ------------------------------------------------------------------
    def call(self, delay: int, callback) -> str | None:
        """Запланировать отложенный вызов (с учётом отмены)."""
        try:
            identifier = self.widget.after(int(delay), lambda: self._run(identifier, callback))
        except tk.TclError:
            return None
        self._jobs.add(identifier)
        return identifier

    def _run(self, identifier, callback):
        self._jobs.discard(identifier)
        if not self.alive:
            return
        try:
            callback()
        except tk.TclError:
            # Виджет уничтожен между планированием и вызовом — тихо заканчиваем.
            return

    def cancel(self, identifier) -> None:
        """Отменить один отложенный вызов."""
        if identifier is None:
            return
        self._jobs.discard(identifier)
        try:
            self.widget.after_cancel(identifier)
        except tk.TclError:
            pass

    def cancel_all(self) -> None:
        """Отменить все отложенные вызовы этого планировщика."""
        for identifier in list(self._jobs):
            try:
                self.widget.after_cancel(identifier)
            except tk.TclError:
                pass
        self._jobs.clear()
        self._colors.clear()

    # ------------------------------------------------------------------
    # Состояние
    # ------------------------------------------------------------------
    @property
    def alive(self) -> bool:
        """Существует ли ещё виджет-владелец."""
        try:
            return bool(self.widget.winfo_exists())
        except tk.TclError:
            return False

    @property
    def pending(self) -> int:
        """Сколько отложенных вызовов ещё запланировано."""
        return len(self._jobs)

    # ------------------------------------------------------------------
    # Анимации
    # ------------------------------------------------------------------
    def tween(self, duration: int, step, on_complete=None, guard=None) -> None:
        """Плавно провести значение от 0 до 1.

        `step(fraction)` вызывается с долей прохождения; `on_complete` — после
        завершения. `guard()` может остановить анимацию на ходу (например,
        если панель успели закрыть, пока она показывалась).

        При включённом `REDUCED_MOTION` значение сразу равно 1.
        """
        if self.reduced or duration <= 0:
            try:
                step(1.0)
            except tk.TclError:
                return
            if on_complete:
                try:
                    on_complete()
                except tk.TclError:
                    pass
            return
        frames = max(1, int(duration / FRAME))

        def advance(remaining: int):
            stopped = guard is not None and not guard()
            if remaining <= 0 or not self.alive or stopped:
                if remaining <= 0 and on_complete and self.alive and not stopped:
                    try:
                        on_complete()
                    except tk.TclError:
                        pass
                return
            fraction = ease_out(1 - remaining / frames)
            try:
                step(fraction)
            except tk.TclError:
                return
            self.call(FRAME, lambda: advance(remaining - 1))

        advance(frames)

    def slide_out(self, frame, x: int, y: int, duration: int = 150, on_complete=None,
                  guard=None, width=None, height=None) -> None:
        """Убрать `frame` вниз и спрятать по завершении."""
        extra = {}
        if width:
            extra['width'] = int(width)
        if height:
            extra['height'] = int(height)
        try:
            frame.place_configure(x=x, y=y, **extra)
        except tk.TclError:
            return

        def move(fraction: float):
            try:
                frame.place_configure(y=int(y + 8 * fraction))
            except tk.TclError:
                pass

        self.tween(duration, move, on_complete, guard)

    def slide(self, frame, x: int, y: int, duration: int = 200, on_complete=None,
              guard=None, width=None, height=None) -> None:
        """Показать `frame` в точке (x, y) с лёгким вертикальным сдвигом.

        Прозрачность в CustomTkinter недоступна, поэтому имитировать её
        дорогой перерисовкой нельзя: вместо этого кадр «выезжает» сверху
        на несколько пикселей.

        `width`/`height` задают размер кадра явно: контейнеры
        CustomTkinter иначе подстраиваются под своё содержимое, а панель
        должна быть одинаковой ширины во всех режимах.
        """
        offset = 8
        extra = {}
        if width:
            extra['width'] = int(width)
        if height:
            extra['height'] = int(height)
        try:
            frame.place(x=x, y=y + offset, **extra)
        except tk.TclError:
            return

        def move(fraction: float):
            try:
                frame.place_configure(y=int(y + offset * (1 - fraction)))
            except tk.TclError:
                pass

        self.tween(duration, move, on_complete, guard)

    def animate_color(self, widget, option: str, target: str, start: str | None = None,
                      duration: int = 220) -> None:
        """Плавно изменить цвет свойства виджета.

        Если анимация к тому же цвету уже идёт, она не перезапускается:
        иначе каждое обновление состояния начинало бы новую анимацию и
        индикатор «дрожал» бы вместо спокойной смены цвета.
        """
        key = (id(widget), option)
        record = self._colors.get(key)
        if record is not None:
            current_target, job = record
            if current_target == target:
                return
            if job is not None:
                self.cancel(job)
        try:
            current = start or widget.cget(option)
        except tk.TclError:
            current = target
        if isinstance(current, (list, tuple)):
            current = current[0] if current else target
        if current in ('', None) or not str(current).startswith('#'):
            current = target
        if str(current).lower() == str(target).lower():
            self._colors.pop(key, None)
            return
        if self.reduced:
            try:
                widget.configure(**{option: target})
            except tk.TclError:
                pass
            self._colors.pop(key, None)
            return

        frames = max(1, int(duration / FRAME))

        def advance(remaining: int, origin: str):
            if not self.alive:
                self._colors.pop(key, None)
                return
            if remaining <= 0:
                # Последний кадр — ровно целевой цвет: иначе анимация
                # останавливается на доли процента раньше и цвет не
                # совпадает с целевым.
                self._colors.pop(key, None)
                try:
                    widget.configure(**{option: target})
                except tk.TclError:
                    pass
                return
            fraction = ease_out(1 - remaining / frames)
            color = theme.blend(origin, target, fraction)
            try:
                widget.configure(**{option: color})
            except tk.TclError:
                self._colors.pop(key, None)
                return
            identifier = self.call(FRAME, lambda: advance(remaining - 1, origin))
            self._colors[key] = (target, identifier)

        advance(frames, current)

    def pulse(self, widget, option: str, color: str, duration: int = 900) -> None:
        """Мягкое мигание — индикатор выполнения операции.

        Мигание должно быть ненавязчивым: цвет слегка уходит в сторону
        `color` и возвращается. При `REDUCED_MOTION` мигания нет.
        """
        if self.reduced:
            return
        try:
            base = widget.cget(option)
        except tk.TclError:
            return
        if isinstance(base, (list, tuple)):
            base = base[0] if base else color
        if not str(base).startswith('#'):
            return

        def step_back(fraction: float):
            try:
                widget.configure(**{option: theme.blend(color, base, fraction)})
            except tk.TclError:
                pass

        def step_to(fraction: float):
            try:
                widget.configure(**{option: theme.blend(base, color, fraction)})
            except tk.TclError:
                pass

        def cycle(index: int):
            if index % 2:
                self.tween(duration, step_back)
            else:
                self.tween(duration, step_to)
            if index < 4:
                self.call(duration, lambda: cycle(index + 1))

        cycle(0)
