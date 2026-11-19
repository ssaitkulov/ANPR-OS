"""Пути приложения — от расположения проекта, а не от текущей директории.

Программа запускается из разных мест: панель управления, `run.bat`,
планировщик Windows, служебные скрипты. Поэтому относительный путь нельзя
оставлять «как есть» — он разрешился бы от случайной рабочей директории.
Здесь все пути абсолютные и считаются от корня проекта.

Рабочие данные (база, зеркало состояния, фотографии, журналы) пока лежат
в корне проекта — как и раньше. Каталог `data/` создан заранее, но перенос
рабочих данных в него выполняется отдельной согласованной процедурой
(см. `data/README.md`).
"""
import os
from pathlib import Path

#: Корень проекта: app/core/paths.py → app/core → app → корень.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Изменяемые данные приложения (целевое место для базы, фото, журналов).
DATA_DIR = PROJECT_ROOT / 'data'

#: Каталог журналов сервиса (используется при переносе данных в data/).
LOGS_DIR = DATA_DIR / 'logs'

#: Статические ресурсы: иконки и логотипы интерфейса.
ASSETS_DIR = PROJECT_ROOT / 'assets'

#: Служебные сценарии: сверка состояния, приёмка, проверка зависимостей.
SCRIPTS_DIR = PROJECT_ROOT / 'scripts'

#: Автоматические тесты.
TESTS_DIR = PROJECT_ROOT / 'tests'

#: Живой конфиг с секретами — остаётся в корне проекта.
ENV_FILE = PROJECT_ROOT / '.env'


def runtime_dir() -> Path:
    """Каталог для изменяемых файлов сервиса: PID-файл, журналы запуска.

    Обычно это корень проекта. В безопасном тестовом режиме
    (`app.core.safe_mode`) каталог подменяется на временный — тест не
    должен трогать боевые PID-файлы и журналы.
    """
    override = os.environ.get('ANPR_RUNTIME_DIR')
    return Path(override) if override else PROJECT_ROOT


def env_file() -> Path:
    """Файл настроек `.env`.

    В безопасном тестовом режиме указывает на пустую копию во временном
    каталоге: боевые пароль камеры и токен бота в тестах недоступны.
    """
    override = os.environ.get('ANPR_ENV_FILE')
    return Path(override) if override else ENV_FILE


def project_path(*parts) -> Path:
    """Путь внутри проекта: project_path('anpr.sqlite3')."""
    return PROJECT_ROOT.joinpath(*parts)


def resolve_path(value, default=None) -> Path:
    """Привести путь из настроек к абсолютному.

    Абсолютный путь берётся как есть; относительный считается от корня
    проекта. Раньше относительный путь разрешался от текущей директории —
    из-за этого, например, `SUBSCRIBERS_FILE=subscribers.json` зависел от
    того, откуда запустили сервис.
    """
    text = str(value or '').strip()
    if not text:
        return Path(default) if default is not None else PROJECT_ROOT
    path = Path(text).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path)
