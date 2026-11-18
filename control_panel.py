"""Совместимая точка входа панели управления.

Запуск: `run.bat` (`pythonw.exe control_panel.py`), автозапуск Windows.
Сам код панели живёт в `app/ui/panel.py`; этот файл запускает его и
подставляет тот же объект модуля в `sys.modules` — чтобы `import control_panel`
давал именно панель (от этого зависят подмены вида `patch('control_panel.…')`).
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.ui import panel as _module  # noqa: E402

sys.modules['control_panel'] = _module

if __name__ == '__main__':
    _module.main()
