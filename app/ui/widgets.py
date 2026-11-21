"""Мелкие помощники для виджетов CustomTkinter.

Один модуль — чтобы поведение кнопок и индикаторов (доступность с
клавиатуры, видимый фокус, подсказки, плавная смена цвета иконки) не
расходилось между шапкой и панелью.
"""
from __future__ import annotations

import tkinter
import tkinter as tk

import customtkinter as ctk

from app.ui import icons
from app.ui import motion as motion_module
from app.ui import theme


def bind_widget(widget, sequence: str, handler):
    """Привязать обработчик к самому виджету.

    CustomTkinter переносит `bind` на внутренний холст: события мыши
    приходят именно туда, а клавиатурные события и фокус — на сам виджет.
    Для клавиатуры и фокуса нужен прямой `bind` базового класса Tk.
    """
    try:
        tkinter.Misc.bind(widget, sequence, handler, add='+')
        return True
    except (tk.TclError, AttributeError, TypeError):
        return False


def activatable(widget):
    """Enter и пробел нажимают кнопку так же, как мышь."""
    try:
        widget.configure(takefocus=1)
    except (tk.TclError, ValueError):
        pass

    def activate(_event=None):
        try:
            widget.invoke()
        except (tk.TclError, AttributeError):
            pass
    for sequence in ('<Return>', '<space>'):
        try:
            widget.bind(sequence, activate, add='+')
        except (tk.TclError, NotImplementedError, TypeError, AttributeError):
            pass
        bind_widget(widget, sequence, activate)
    return widget


def animated_hover(widget, motion, normal: str, hover: str, duration: int = 140):
    """Плавная подсветка кнопки при наведении.

    CustomTkinter переключает цвет мгновенно, а интерфейс должен
    «дышать». Поэтому штатная подсветка отключается (цвет наведения
    совпадает с обычным), а переход делает планировщик анимаций.
    """
    target = getattr(widget, '_canvas', widget)
    try:
        widget.configure(hover_color=normal)
    except (tk.TclError, ValueError):
        pass

    def enter(_event=None):
        motion.animate_color(widget, 'fg_color', hover, duration=duration)

    def leave(_event=None):
        motion.animate_color(widget, 'fg_color', normal, duration=duration)

    for widget_part in (target, getattr(widget, '_text_label', None),
                        getattr(widget, '_image_label', None)):
        if widget_part is None:
            continue
        try:
            widget_part.bind('<Enter>', enter, add='+')
            widget_part.bind('<Leave>', leave, add='+')
        except tk.TclError:
            continue
    return widget


def focusable(widget, accent: str | None = None):
    """Сделать элемент доступным с клавиатуры и показать фокус.

    CustomTkinter не рисует рамку фокуса: без неё пользователь с клавиатуры
    не понимает, где он находится. Поэтому фокус показывается явно:
    у кнопки появляется тонкая рамка, у индикатора — подсветка фона.

    Рамка появляется только на время фокуса: постоянных обводок в
    интерфейсе нет.
    """
    color = accent or theme.ACCENT
    try:
        widget.configure(takefocus=1)
    except (tk.TclError, ValueError):
        pass
    target = getattr(widget, '_canvas', widget)

    def focus_in(_event=None):
        try:
            widget.configure(border_width=1, border_color=color)
        except (tk.TclError, ValueError):
            pass

    def focus_out(_event=None):
        try:
            widget.configure(border_width=0)
        except (tk.TclError, ValueError):
            pass

    # События фокуса получают и сам виджет, и его внутренний холст:
    # CustomTkinter рисует рамку на виджете, а фокус приходит на окно Tk.
    for part in (widget, target):
        if part is None:
            continue
        bind_widget(part, '<FocusIn>', focus_in)
        bind_widget(part, '<FocusOut>', focus_out)
    return widget


class Tooltip:
    """Подсказка при наведении — без отдельного окна.

    Подсказка рисуется обычной меткой в родительском окне: второе окно
    (даже прозрачное) создаёт лишние события Tk и проблемы с DPI, а
    метка в том же окне ведёт себя предсказуемо.
    """

    def __init__(self, widget, text: str = '', parent=None, delay: int = 400):
        self.widget = widget
        self.parent = parent if parent is not None else widget.winfo_toplevel()
        self.text = text or ''
        self.delay = delay
        self._job = None
        self._label = None
        try:
            widget.bind('<Destroy>', self._on_destroy, add='+')
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    def set_text(self, text: str) -> None:
        """Обновить текст подсказки (вместе с состоянием компонента)."""
        self.text = text or ''
        if self._label is not None:
            try:
                self._label.configure(text=self.text)
            except tk.TclError:
                pass

    def schedule(self) -> None:
        """Показать подсказку с небольшой задержкой (если ещё не показана)."""
        self.cancel()
        if not self.text:
            return
        try:
            self._job = self.widget.after(self.delay, self.show)
        except tk.TclError:
            self._job = None

    def cancel(self) -> None:
        """Отменить запланированное показывание."""
        if self._job is None:
            return
        try:
            self.widget.after_cancel(self._job)
        except tk.TclError:
            pass
        self._job = None

    def show(self) -> None:
        """Показать подсказку."""
        self._job = None
        if self._label is not None or not self.text:
            return
        try:
            label = ctk.CTkLabel(self.parent, text=self.text, text_color=theme.TEXT,
                                 font=theme.FONT_MICRO, fg_color=theme.CARD_ALT,
                                 corner_radius=8, justify='left')
            label.update_idletasks()
            # winfo_root* и winfo_height — физические пиксели, а place в CTk
            # масштабирует x/y. Переводим дельту обратно в его систему.
            x = max(0, self.widget.winfo_rootx() - self.parent.winfo_rootx())
            y = max(0, self.widget.winfo_rooty() - self.parent.winfo_rooty()
                    + self.widget.winfo_height() + 6)
            try:
                x = int(self.parent._reverse_widget_scaling(x))
                y = int(self.parent._reverse_widget_scaling(y))
            except (AttributeError, TypeError, tk.TclError):
                pass
            label.place(x=x, y=y)
            label.lift()
            self._label = label
        except tk.TclError:
            self._label = None

    def hide(self) -> None:
        """Скрыть подсказку."""
        self.cancel()
        label, self._label = self._label, None
        if label is None:
            return
        try:
            label.destroy()
        except tk.TclError:
            pass

    def _on_destroy(self, _event=None) -> None:
        self.cancel()


class TintedIcon:
    """Иконка, цвет которой меняется плавно.

    Цвет иконки — единственный носитель состояния компактного индикатора,
    поэтому он должен меняться без мигания. Картинки перекрашиваются
    кадрами (`app.ui.icons.blend_frames`) и показываются по очереди;
    при `REDUCED_MOTION` цвет меняется сразу.

    Предыдущая анимация отменяется: быстрая смена состояний не должна
    оставлять два «перекрашивающихся» потока.
    """

    def __init__(self, parent, name: str, size: int = 19, color: str | None = None,
                 motion_manager=None):
        self.name = name
        self.size = size
        self.motion = motion_manager or motion_module.Motion(parent)
        self.color = str(color or theme.MUTED)
        self._target = self.color
        self._job = None
        self.label = ctk.CTkLabel(parent, text='', width=size, height=size)
        self.label.configure(image=icons.icon(name, self.color, size, master=parent))

    # ------------------------------------------------------------------
    def set_icon(self, name: str) -> None:
        """Сменить саму иконку (например, при смене режима панели)."""
        if not name or name == self.name:
            return
        self.name = name
        self._cancel()
        try:
            self.label.configure(image=icons.icon(name, self.color, self.size,
                                                 master=self.label))
        except tk.TclError:
            pass

    def set_color(self, color: str) -> None:
        """Плавно перекрасить иконку в новый цвет."""
        color = str(color or theme.MUTED)
        if color.lower() == self._target.lower():
            return
        frames = icons.blend_frames(self.name, self.color, color, self.size)
        if not frames:
            return
        self._target = color
        self._cancel()
        if self.motion.reduced:
            self._apply(frames[-1])
            return
        self._animate(frames)

    def _cancel(self) -> None:
        if self._job is not None:
            self.motion.cancel(self._job)
            self._job = None

    def _apply(self, frame) -> None:
        color, image = frame
        self.color = color
        try:
            self.label.configure(image=image)
        except tk.TclError:
            pass

    def _animate(self, frames) -> None:
        steps = len(frames)

        def advance(remaining: int) -> None:
            self._job = None
            if not self.motion.alive:
                return
            fraction = motion_module.ease_out(1 - remaining / steps)
            index = min(steps - 1, int(round(fraction * (steps - 1))))
            self._apply(frames[index])
            if remaining > 0:
                self._job = self.motion.call(motion_module.FRAME,
                                             lambda: advance(remaining - 1))

        advance(steps - 1)

    def shutdown(self) -> None:
        """Отменить незавершённую анимацию."""
        self._cancel()
