"""Совместимый фасад панели управления.

Окно приложения теперь единое — `app/ui/shell.py`, класс `AppShell`.
Модуль сохранён как публичная точка входа: корневой лаунчер
`control_panel.py` подставляет его в `sys.modules`, поэтому
`import control_panel` даёт прежний модуль, а подмены вида
`patch('control_panel.service.stop')` и `patch('control_panel.messagebox.…')`
продолжают работать (модули `service` и `messagebox` — те же объекты).
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tkinter import messagebox  # noqa: F401  — публичная точка для подмен в тестах

from app.services import control as service  # noqa: F401  — публичная точка для подмен в тестах
from app.ui.shell import AppShell

#: Прежнее имя класса панели — тот же объект, что и `AppShell`.
Panel = AppShell


def main() -> None:
    """Запуск единого окна приложения."""
    from app.ui.shell import main as _main
    _main()


if __name__ == '__main__':
    main()
