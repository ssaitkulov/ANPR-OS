"""Единая точка входа в проект для служебных сценариев.

Сценарии лежат в `scripts/`, а код приложения — в пакете `app/`. Чтобы
импортировать `app.core.paths` (единственный источник путей), корень
проекта нужно один раз положить в `sys.path`. Это делается здесь — в
одном месте для всех сценариев, чтобы никто не вычислял корень проекта
самостоятельно.

Использование::

    from _bootstrap import PROJECT_ROOT
"""
import sys
from pathlib import Path

#: Корень проекта — нужен только для импорта модулей пакета `app`.
_BOOTSTRAP = Path(__file__).resolve().parents[1]
if str(_BOOTSTRAP) not in sys.path:
    sys.path.insert(0, str(_BOOTSTRAP))

from app.core import paths as app_paths  # noqa: E402  — после правки sys.path

#: Корень проекта — из модуля путей (единственный источник правды).
PROJECT_ROOT = app_paths.PROJECT_ROOT
