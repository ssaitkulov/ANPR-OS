"""Мелкие помощники для виджетов CustomTkinter.

Один модуль — чтобы поведение кнопок и индикаторов (доступность с
клавиатуры, видимый фокус) не расходилось между шапкой и панелью.
"""
from __future__ import annotations

import tkinter as tk

from app.ui import theme


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

    try:
        target.bind('<FocusIn>', focus_in, add='+')
        target.bind('<FocusOut>', focus_out, add='+')
    except tk.TclError:
        pass
    return widget
