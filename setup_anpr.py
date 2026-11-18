"""Совместимая точка входа окна настроек.

Запуск: `settings.bat` (`python setup_anpr.py`).
Сам код окна живёт в `app/ui/settings.py`; этот файл запускает его и
подставляет тот же объект модуля в `sys.modules`, чтобы `import setup_anpr`
давал именно окно настроек.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.ui import settings as _module  # noqa: E402

sys.modules['setup_anpr'] = _module

if __name__ == '__main__':
    _module.main()
