"""Совместимая точка входа управления локальным сервисом.

Запуск: `start_service.ps1` (`python service_control.py`) и команда
`anpr-service` из `pyproject.toml`.

Сам код — в `app/services/control.py`; этот файл запускает его и
подставляет тот же объект модуля в `sys.modules`, чтобы
`import service_control` давал прежний модуль.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.services import control as _module  # noqa: E402

sys.modules['service_control'] = _module

if __name__ == '__main__':
    _module.start()
