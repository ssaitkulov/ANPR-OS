"""pytest: изоляция тестов от боевого окружения.

Вся изоляция живёт в `anpr_testenv` — там же она подключается для
`python -m unittest discover`, где conftest не загружается.
"""
import anpr_testenv  # noqa: F401  — изоляция от боевой БД и Telegram
