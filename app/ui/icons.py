"""Централизованный кэш иконок Tabler Filled.

Python-интерфейс не умеет SVG, поэтому иконки один раз растеризованы в
`assets/icons/` (см. `scripts/rasterize_icons.py`). Здесь они:

* читаются из PNG-силуэтов (белый с альфа-каналом);
* перекрашиваются в нужный цвет;
* кэшируются по паре «иконка + цвет + размер».

Кэш общий для всего приложения: иконка не пересоздаётся при каждом
обновлении индикатора, а цвет меняется без обращения к диску.

Кэш привязан к интерпретатору Tk: `CTkImage` хранит готовые снимки Tk, а
снимок живёт в одном интерпретаторе. В процессе может быть несколько
окон (тесты), поэтому ключ кэша включает идентификатор интерпретатора.
"""
from __future__ import annotations

import weakref

from PIL import Image
import customtkinter as ctk

from app.core.paths import ASSETS_DIR

#: Каталог с растеризованными иконками.
ICON_DIR = ASSETS_DIR / 'icons'

#: Шаг плавной перекраски иконки (кадров на переход).
BLEND_STEPS = 8

#: Кэш: (интерпретатор, имя, цвет, размер, поворот) → (ссылка, CTkImage).
_cache: dict = {}
#: Кэш исходных силуэтов: имя → Image.
_sources: dict = {}


def source(name: str) -> Image:
    """Белый силуэт иконки (RGBA) из кэша."""
    if name in _sources:
        return _sources[name]
    path = ICON_DIR / f'{name}-filled.png'
    if not path.exists():
        raise FileNotFoundError(f'Иконка не растеризована: {path}')
    with Image.open(path) as image:
        picture = image.convert('RGBA')
    _sources[name] = picture
    return picture


def tinted(name: str, color: str) -> Image:
    """Силуэт иконки, перекрашенный в `color`."""
    picture = source(name)
    from app.ui import theme

    red, green, blue = theme._channels(color)
    filled = Image.new('RGBA', picture.size, (red, green, blue, 255))
    filled.putalpha(picture.getchannel('A'))
    return filled


def _interpreter(master):
    """Объект интерпретатора Tk, которому принадлежит виджет."""
    if master is None:
        return None
    return getattr(master, 'tk', None)


def icon(name: str, color: str = '#F2F9FF', size: int = 16, master=None,
         flip: bool = False) -> ctk.CTkImage:
    """Иконка Tabler Filled нужного цвета и размера (из кэша)."""
    interpreter = _interpreter(master)
    key = (id(interpreter), name, str(color).lower(), int(size), bool(flip))
    entry = _cache.get(key)
    if entry is not None:
        reference, image = entry
        if interpreter is None or (reference() is not None and reference() is interpreter):
            return image
    picture = tinted(name, color)
    if flip:
        picture = picture.rotate(180)
    picture = picture.resize((int(size), int(size)), Image.LANCZOS)
    image = ctk.CTkImage(light_image=picture, dark_image=picture, size=(int(size), int(size)))
    reference = None
    if interpreter is not None:
        try:
            reference = weakref.ref(interpreter)
        except TypeError:
            reference = lambda interpreter=interpreter: interpreter
    _cache[key] = (reference, image)
    return image


def blend_frames(name: str, start: str, end: str, size: int = 19,
                 steps: int = BLEND_STEPS) -> list:
    """Кадры плавной перекраски иконки: список пар «цвет, изображение».

    Цвет иконки — это состояние компонента, и он должен меняться плавно:
    резкая перекраска читается как мигание. Кадры собираются один раз на
    переход и намеренно не попадают в общий кэш — иначе кэш накапливал бы
    промежуточные оттенки и рос без ограничений.

    Переход выполняется только при реальном изменении цвета: обычный
    опрос состояния (раз в несколько секунд) картинок не создаёт.
    """
    from app.ui import theme

    picture = source(name)
    frames = []
    for index in range(1, max(1, int(steps)) + 1):
        color = theme.blend(start, end, index / max(1, int(steps)))
        red, green, blue = theme._channels(color)
        frame = Image.new('RGBA', picture.size, (red, green, blue, 255))
        frame.putalpha(picture.getchannel('A'))
        frame = frame.resize((int(size), int(size)), Image.LANCZOS)
        frames.append((color, ctk.CTkImage(light_image=frame, dark_image=frame,
                                            size=(int(size), int(size)))))
    return frames


def available() -> list:
    """Имена растеризованных иконок (для диагностики и тестов)."""
    if not ICON_DIR.exists():
        return []
    return sorted(path.stem.replace('-filled', '') for path in ICON_DIR.glob('*-filled.png'))
