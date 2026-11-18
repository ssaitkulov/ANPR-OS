"""Сохранение размера и положения окна приложения.

Отдельный файл `ui_window.json` в корне проекта: формат настроек
приложения (`ui_preferences.json`) не меняется.

Формат файла:
    {"width": 1440, "height": 900, "x": 120, "y": 40, "maximized": false}
"""
import json
import os
import re
from pathlib import Path

from app.core.paths import PROJECT_ROOT

#: Файл состояния окна — рядом с рабочими данными, в корне проекта.
STATE_FILE = PROJECT_ROOT / 'ui_window.json'

#: Размер окна при первом запуске и минимальный допустимый размер.
DEFAULT_SIZE = (1440, 900)
MIN_SIZE = (1024, 650)

_GEOMETRY = re.compile(r'^(?P<width>\d+)x(?P<height>\d+)(?P<x>[+-]\d+)?(?P<y>[+-]\d+)?$')


def parse_geometry(text):
    """Разобрать строку геометрии Tk: '1440x900+120+40' → (1440, 900, 120, 40).

    Отрицательные смещения допустимы (окно на втором мониторе слева).
    Возвращает None, если строка не разбирается.
    """
    if not text:
        return None
    match = _GEOMETRY.match(str(text).strip())
    if not match:
        return None
    width = int(match.group('width'))
    height = int(match.group('height'))
    x = int(match.group('x')) if match.group('x') is not None else None
    y = int(match.group('y')) if match.group('y') is not None else None
    return width, height, x, y


def format_geometry(width, height, x=None, y=None):
    """Собрать строку геометрии Tk из размеров и смещения."""
    if x is None or y is None:
        return f'{int(width)}x{int(height)}'
    return f'{int(width)}x{int(height)}{int(x):+d}{int(y):+d}'


def state_from_geometry(text, maximized=False):
    """Собрать состояние окна из строки геометрии Tk."""
    parsed = parse_geometry(text)
    if not parsed:
        return None
    width, height, x, y = parsed
    return {'width': width, 'height': height, 'x': x, 'y': y, 'maximized': bool(maximized)}


def load_state(path=None):
    """Прочитать состояние окна. None — файла нет или он повреждён."""
    path = Path(path or STATE_FILE)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def save_state(state, path=None):
    """Записать состояние окна атомарно (через временный файл)."""
    path = Path(path or STATE_FILE)
    temporary = path.with_name(path.name + '.tmp')
    try:
        temporary.write_text(json.dumps(state, ensure_ascii=False) + '\n', encoding='utf-8')
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def within_bounds(state, bounds):
    """Попадает ли сохранённое положение в пределы экранов.

    bounds — (x, y, ширина, высота) виртуального экрана. Если окно было
    сохранено на мониторе, которого больше нет, положение не восстанавливается.
    """
    if not state:
        return False
    x, y = state.get('x'), state.get('y')
    if x is None or y is None:
        return False
    width = int(state.get('width') or 0)
    height = int(state.get('height') or 0)
    origin_x, origin_y, screen_width, screen_height = bounds
    return (x < origin_x + screen_width and x + width > origin_x
            and y < origin_y + screen_height and y + height > origin_y)
