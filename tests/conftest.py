"""pytest: изоляция тестов от боевого окружения.

Вся изоляция живёт в `app/core/safe_mode.py`; она включается ещё в
`tests/__init__.py` и в корневом `conftest.py` — до импорта модулей
приложения. Здесь — только порядок завершения: фоновые потоки дожидаются
окончания **до** снятия тестовых подмен.
"""
import pytest

from tests import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД и Telegram
from app.core import safe_mode


@pytest.fixture(autouse=True)
def _safe_mode_threads():
    """После каждого теста — дождаться фоновых потоков."""
    yield
    safe_mode.join_threads(timeout=3.0)


@pytest.fixture(scope='session', autouse=True)
def _safe_mode_finalize():
    """В конце прогона — завершить потоки и снять подмены.

    Если какой-то поток не завершился, подмены остаются: защита рабочей
    системы важнее чистого состояния процесса.
    """
    yield
    safe_mode.finalize(timeout=5.0)
