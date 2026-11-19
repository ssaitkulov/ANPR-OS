"""Общие помощники жизненного цикла Tkinter.

Главная задача — не оставлять «invalid command name …» в журнале при
закрытии окна. Отложенные вызовы (`after`) переживают уничтожение
виджетов: их нужно отменить явно.
"""
import tkinter as tk


def cancel_pending_scripts(widget):
    """Отменить все отложенные вызовы Tk, включая внутренние CustomTkinter.

    CustomTkinter планирует свои вызовы сам: трекер темы оформления
    (`update`, каждые 30 мс), установка иконки окна, анимация кнопок.
    После уничтожения окна они срабатывают и печатают в stderr
    «invalid command name». Перед закрытием окна отменяем их все.

    Список берётся у интерпретатора (`after info` без аргумента): все
    отложенные вызовы Tk принадлежат одному интерпретатору, поэтому
    отменяются и те, что запланированы другими виджетами.

    Возвращает количество отменённых вызовов.
    """
    try:
        scripts = widget.tk.call('after', 'info')
    except (tk.TclError, AttributeError):
        return 0
    cancelled = 0
    for script in scripts:
        try:
            widget.tk.call('after', 'cancel', script)
            cancelled += 1
        except tk.TclError:
            continue
    return cancelled
