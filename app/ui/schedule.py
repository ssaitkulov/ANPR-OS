"""Безопасное планирование отложенных вызовов Tkinter.

Зачем это нужно
---------------

Tkinter регистрирует команду обратного вызова под именем, которое строится
из идентификатора одноразового объекта (``id`` временного bound-метода).
Идентификаторы освобождённых объектов переиспользуются, поэтому два
разных отложенных вызова иногда получают одно и то же имя. Тогда:

* старый таймер вызывает уже перезаписанную команду (чужой обработчик);
* а когда команда удаляется после выполнения, Tk печатает в stderr
  ``invalid command name "…<lambda>"``.

Такое сообщение — не только шум: потерянный вызов — это потерянное
обновление интерфейса или зависшая анимация.

Что сделано
-----------

Имя команды после регистрации переименовывается на уникальное. Таймеры
продолжают работать: в Tcl хранится именно имя команды, а не ссылка на
Python-объект. Удаление команды после выполнения тоже сохраняется —
оно происходит по имени, которое вернул ``_register``.

Подключается один раз за процесс: ``app.ui.schedule.install()`` вызывается
при импорте пакета ``app.ui``.
"""
from __future__ import annotations

import itertools
import tkinter
import tkinter as tk

#: Счётчик уникальных суффиксов имён команд.
_COUNTER = itertools.count(1)

#: Исходный метод Tkinter (сохраняем до подмены).
_ORIGINAL_REGISTER = tkinter.Misc._register

#: Подмена уже выполнена?
_PATCHED = False


def _register_unique(self, func, subst=None, needcleanup=1):
    """Зарегистрировать команду обратного вызова с уникальным именем."""
    name = _ORIGINAL_REGISTER(self, func, subst, needcleanup)
    unique = f'{name}_{next(_COUNTER)}'
    try:
        self.tk.call('rename', name, unique)
    except tk.TclError:
        # Команда не создалась — возвращаем исходное имя.
        return name
    commands = getattr(self, '_tclCommands', None)
    if commands and commands[-1] == name:
        # Имя команды поменялось — список команд виджета должен знать новое,
        # иначе команда не удалится при уничтожении виджета.
        commands[-1] = unique
    return unique


def install() -> bool:
    """Включить безопасные имена команд обратных вызовов.

    Возвращает ``True``, если подмена выполнена (первый вызов).
    """
    global _PATCHED
    if _PATCHED:
        return False
    tkinter.Misc._register = _register_unique
    _PATCHED = True
    return True
