"""Пути приложения — единственный источник правды о размещении файлов.

Программа запускается из разных мест: панель управления, `run.bat`,
планировщик Windows, служебные скрипты, тесты. Поэтому относительный путь
нельзя оставлять «как есть» — он разрешился бы от случайной рабочей
директории. Все пути считаются от корня проекта, и **ни один модуль не
вычисляет корень самостоятельно**: все берут пути отсюда.

Структура рабочих данных:

    data/
    ├── anpr.sqlite3        база проездов (SQLite, источник истины)
    ├── anpr_state.json     зеркало состояния
    ├── subscribers.json    список чатов-получателей
    ├── ui_preferences.json настройки панели
    ├── ui_window.json      положение и размер окна
    ├── photos/             фотографии проездов
    ├── runtime/            PID-файл, токен управления
    └── logs/               журналы сервиса и управления

Всё, что меняется во время работы, лежит внутри каталога `data/`.
Поэтому проект переносится целиком: скопировали папку на другой диск —
пути определились сами, потому что считаются от корня проекта, а не от
текущей директории и не от абсолютного адреса на диске.

Переопределение через переменные окружения (нужно в безопасном тестовом
режиме и при отладке):

* `ANPR_RUNTIME_DIR` — каталог PID-файла и токена управления;
* `ANPR_LOG_DIR` — каталог журналов;
* `IMAGE_DIR`, `DB_FILE`, `STATE_FILE`, `SUBSCRIBERS_FILE` — рабочие данные.
"""
from __future__ import annotations

import os
from pathlib import Path

#: Корень проекта: app/core/paths.py → app/core → app → корень.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Каталог изменяемых рабочих данных.
DATA_DIR = PROJECT_ROOT / 'data'

#: База проездов (SQLite — источник истины).
DB_FILE = DATA_DIR / 'anpr.sqlite3'

#: JSON-зеркало состояния (отчёты и восстановление).
STATE_FILE = DATA_DIR / 'anpr_state.json'

#: Список чатов-получателей Telegram.
SUBSCRIBERS_FILE = DATA_DIR / 'subscribers.json'

#: Фотографии проездов.
PHOTO_DIR = DATA_DIR / 'photos'

#: Каталог файлов времени выполнения: PID-файл и токен управления.
RUNTIME_DIR = DATA_DIR / 'runtime'

#: Каталог журналов: вывод сервиса и журналы управления.
LOG_DIR = DATA_DIR / 'logs'

#: Настройки панели управления (сворачивание в трей и т. п.).
UI_PREFERENCES_FILE = DATA_DIR / 'ui_preferences.json'

#: Положение и размер окна панели.
UI_WINDOW_FILE = DATA_DIR / 'ui_window.json'

#: Статические ресурсы: иконки и логотипы интерфейса.
ASSETS_DIR = PROJECT_ROOT / 'assets'

#: Служебные сценарии: сверка состояния, приёмка, проверка зависимостей.
SCRIPTS_DIR = PROJECT_ROOT / 'scripts'

#: Автоматические тесты.
TESTS_DIR = PROJECT_ROOT / 'tests'

#: Живой конфиг с секретами — остаётся в корне проекта.
ENV_FILE = PROJECT_ROOT / '.env'


# ----------------------------------------------------------------------
# Каталоги с учётом переопределений окружения
# ----------------------------------------------------------------------

def runtime_dir() -> Path:
    """Каталог для изменяемых файлов сервиса: PID-файл, токен управления.

    В безопасном тестовом режиме (`app.core.safe_mode`) каталог подменяется
    на временный — тест не должен трогать боевые PID-файлы и токен.
    """
    override = os.environ.get('ANPR_RUNTIME_DIR')
    return Path(override) if override else RUNTIME_DIR


def log_dir() -> Path:
    """Каталог журналов: вывод сервиса, журнал управления, журнал панели."""
    override = os.environ.get('ANPR_LOG_DIR')
    return Path(override) if override else LOG_DIR


def photo_dir() -> Path:
    """Каталог фотографий проездов.

    Путь можно задать настройкой `IMAGE_DIR`. Относительное значение
    считается от корня проекта — фотографии переезжают вместе с проектом.
    """
    return _env_path('IMAGE_DIR', PHOTO_DIR)


def db_file() -> Path:
    """Файл базы данных."""
    return _env_path('DB_FILE', DB_FILE)


def state_file() -> Path:
    """Файл JSON-зеркала состояния."""
    return _env_path('STATE_FILE', STATE_FILE)


def subscribers_file() -> Path:
    """Файл со списком чатов-получателей."""
    return _env_path('SUBSCRIBERS_FILE', SUBSCRIBERS_FILE)


def ui_preferences_file() -> Path:
    """Файл настроек панели управления."""
    return _env_path('UI_PREFERENCES_FILE', UI_PREFERENCES_FILE)


def ui_window_file() -> Path:
    """Файл положения и размера окна панели."""
    return _env_path('UI_WINDOW_FILE', UI_WINDOW_FILE)


def env_file() -> Path:
    """Файл настроек `.env`.

    В безопасном тестовом режиме указывает на пустую копию во временном
    каталоге: боевой пароль камеры и токен бота в тестах недоступны.
    """
    override = os.environ.get('ANPR_ENV_FILE')
    return Path(override) if override else ENV_FILE


def project_path(*parts) -> Path:
    """Путь внутри проекта: project_path('app', 'core')."""
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


def _env_path(name: str, default: Path) -> Path:
    """Путь из переменной окружения или значение по умолчанию."""
    value = os.environ.get(name)
    if value is None or not str(value).strip():
        return default
    return resolve_path(value, default)


# ----------------------------------------------------------------------
# Фотографии: путь в базе и путь на диске
# ----------------------------------------------------------------------

def resolve_photo_path(value, root=None) -> Path | None:
    """Абсолютный путь к фотографии по значению из базы.

    Поддерживаются три вида значения:

    * относительный путь от каталога фотографий (`2026-11-17/файл.jpg`) —
      основной вид: база не знает, где лежит проект, поэтому путь
      переносится вместе с папкой;
    * абсолютный путь — файл берётся как есть (если он есть);
    * абсолютный путь из прежней папки (`.../images/2026-11-17/файл.jpg`) —
      ищется по дате и имени в текущем каталоге фотографий.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    path = Path(text)
    folder = Path(root) if root is not None else photo_dir()
    if not path.is_absolute():
        return folder / path
    if path.exists():
        return path
    parts = path.parts
    if len(parts) >= 2:
        candidate = folder / parts[-2] / parts[-1]
        if candidate.exists():
            return candidate
    return path


def relative_photo_path(value, root=None) -> str:
    """Значение для базы: путь фотографии относительно каталога снимков.

    Так путь не зависит от диска и папки: проект можно перенести целиком.
    Если файл лежит вне каталога снимков, возвращается путь как есть.
    """
    path = resolve_photo_path(value, root)
    if path is None:
        return ''
    folder = Path(root) if root is not None else photo_dir()
    try:
        return path.relative_to(folder).as_posix()
    except ValueError:
        return str(path)


# ----------------------------------------------------------------------
# Каталоги и прежние места
# ----------------------------------------------------------------------

def ensure_data_dirs() -> None:
    """Создать структуру рабочих данных (если её ещё нет)."""
    for folder in (DATA_DIR, RUNTIME_DIR, LOG_DIR):
        folder.mkdir(parents=True, exist_ok=True)


def legacy_data() -> dict:
    """Рабочие данные, оставшиеся в прежних местах корня проекта.

    Используется защитой от «тихой» потери данных: если база ещё лежит
    в корне, а путь указывает в `data/`, приложение обязано сказать об
    этом, а не создать пустую базу рядом.
    """
    candidates = {
        'db': PROJECT_ROOT / 'anpr.sqlite3',
        'state': PROJECT_ROOT / 'anpr_state.json',
        'subscribers': PROJECT_ROOT / 'subscribers.json',
        'photos': PROJECT_ROOT / 'images',
    }
    return {name: path for name, path in candidates.items() if path.exists()}
