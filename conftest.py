"""pytest: безопасный тестовый режим включается до сбора тестов.

Корневой `conftest.py` нужен для одной причины: pytest импортирует его
раньше тестовых модулей, поэтому защита от воздействия на рабочее
оборудование включается **до** импорта модулей приложения — иначе пути
базы, состояния и PID-файла уже были бы посчитаны по боевым адресам.

Тот же режим включается в `tests/__init__.py` — для `unittest discover`,
где `conftest.py` не загружается.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import safe_mode  # noqa: E402  — после правки sys.path

#: Защита рабочей системы: по умолчанию включена при тестовом прогоне.
safe_mode.activate(reason='pytest: безопасный тестовый режим')
