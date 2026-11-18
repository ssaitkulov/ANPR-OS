"""Совместимая точка входа локального HTTP-сервиса.

Исторический путь сохранён: `python app.py` (и запуск из панели управления)
работает как раньше. Сам сервис живёт в `app/services/http_api.py`.

Замечание об именах: `app` теперь имя пакета (`app/`), поэтому `import app`
загружает пакет, а не этот файл. На запуск сервиса это не влияет: файл
запускается как скрипт (`python app.py`), а имя модуля при этом — `__main__`.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.services import http_api  # noqa: E402

if __name__ == '__main__':
    http_api.run()
