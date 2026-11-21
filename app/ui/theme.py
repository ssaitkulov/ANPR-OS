"""Единые токены оформления интерфейса.

Модуль — источник истины для цветов, шрифтов и размеров. Раньше цвета
были размазаны по модулям (`#1a2432` в одном месте, `#1B293D` — в другом),
из-за чего окно выглядело собранным из разных приложений.

Палитра намеренно сдержанная: тёмный фон, один акцент, один CTA-цвет и
четыре состояния. Правило состояний:

* `SUCCESS` — подтверждённая работоспособность;
* `WARNING` — промежуточное состояние или ограниченная функциональность;
* `DANGER` — подтверждённая ошибка;
* `ACCENT` — процесс выполняется;
* `MUTED` — остановлено или состояние неизвестно.

Цвет всегда дополнен текстом: цвет — это второй канал, а не единственный.
"""
from __future__ import annotations

#: Фон окна.
BG = '#101827'
#: Фон карточек и панелей.
CARD = '#1B293D'
#: Фон вложенных элементов (кнопки, поля, выделение).
CARD_ALT = '#26364C'
#: Основной текст.
TEXT = '#F2F9FF'
#: Вторичный текст: подписи, пояснения.
MUTED = '#A9BEDB'
#: Акцент: активные элементы, ссылки, ход операции.
ACCENT = '#0089FF'
#: Глубокий акцент: фон активной вкладки с белым текстом (контраст AA).
ACCENT_DEEP = '#0068C9'
#: CTA-цвет: главное действие в безопасном состоянии (запуск).
CTA = '#FEE46F'

#: Состояния.
SUCCESS = '#06C35D'
WARNING = '#FFBA02'
DANGER = '#F23F3A'

#: Шрифт по умолчанию.
FONT_FAMILY = 'Segoe UI'

#: Основной текст, px.
SIZE_BODY = 13
#: Подписи, px.
SIZE_CAPTION = 12
#: Заголовок панели, px.
SIZE_TITLE = 16
#: Мелкие подписи (PID, время), px.
SIZE_MICRO = 11

#: Высота интерактивной строки в шапке, px.
ROW_HEIGHT = 38
#: Отступ между группами в шапке, px.
GROUP_GAP = 8
#: Цвет элемента при наведении (кнопки, индикаторы).
ROW_HOVER = '#31435D'


def font(size: int = SIZE_BODY, weight: str = 'normal') -> tuple:
    """Шрифт интерфейса: `(family, size[, weight])`."""
    if weight in ('', 'normal'):
        return (FONT_FAMILY, size)
    return (FONT_FAMILY, size, weight)


#: Готовые шрифты — чтобы не собирать кортежи вручную в разных модулях.
FONT_BODY = font(SIZE_BODY)
FONT_BODY_BOLD = font(SIZE_BODY, 'bold')
FONT_CAPTION = font(SIZE_CAPTION)
FONT_CAPTION_BOLD = font(SIZE_CAPTION, 'bold')
FONT_TITLE = font(SIZE_TITLE, 'bold')
FONT_MICRO = font(SIZE_MICRO)
FONT_MICRO_BOLD = font(SIZE_MICRO, 'bold')


def _channels(color: str) -> tuple:
    """Разбор цвета `#RRGGBB` в каналы 0..255."""
    value = str(color or '').strip().lstrip('#')
    if len(value) == 3:
        value = ''.join(char * 2 for char in value)
    if len(value) != 6:
        return 255, 255, 255
    try:
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    except ValueError:
        return 255, 255, 255


def to_hex(color: tuple) -> str:
    """Сборка цвета из каналов 0..255 в `#RRGGBB`."""
    return '#{:02x}{:02x}{:02x}'.format(*[max(0, min(255, int(value))) for value in color])


def blend(start: str, end: str, ratio: float) -> str:
    """Смешать два цвета: `ratio` — доля `end` (0..1).

    Нужен для плавной смены цвета индикаторов: резкая перекраска мигает,
    а плавный переход читается как «состояние изменилось».
    """
    weight = max(0.0, min(1.0, float(ratio)))
    first = _channels(start)
    second = _channels(end)
    return to_hex(tuple(first[index] + (second[index] - first[index]) * weight
                        for index in range(3)))
