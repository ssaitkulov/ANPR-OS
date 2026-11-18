"""pytest: изоляция тестов от боевого окружения.

Вся изоляция живёт в `tests/anpr_testenv.py` — там же она подключается
для `python -m unittest discover`, где conftest не загружается.
"""
from tests import anpr_testenv  # noqa: F401  — изоляция от боевой БД и Telegram
