"""Единая интерпретация данных сервиса для индикаторов интерфейса.

Правило модуля: каждый индикатор показывает только собственное состояние.
Сервис, камера и Telegram не влияют друг на друга: неисправность камеры не
делает индикатор сервиса жёлтым, а недоступность Telegram не красит в
красный весь заголовок.

Данные берутся только из существующего API (`GET /health`). Искусственных
состояний нет: если данных нет, состояние — «неизвестно», а не «ошибка».

Модуль не импортирует Tkinter: это чистые функции, их легко проверять
тестами и переиспользовать в оболочке и в панели управления.
"""
from datetime import datetime

from app.ui import theme

#: Цвета индикаторов (из общей палитры `app.ui.theme`).
SUCCESS = theme.SUCCESS
WARNING = theme.WARNING
DANGER = theme.DANGER
ACCENT = theme.ACCENT
MUTED = theme.MUTED

#: Прежние имена цветов — для совместимости с внешним кодом.
GREEN = SUCCESS
AMBER = WARNING
RED = DANGER

#: Состояния сервиса.
SERVICE_RUNNING = 'running'
SERVICE_STOPPED = 'stopped'
SERVICE_STARTING = 'starting'
SERVICE_STOPPING = 'stopping'
SERVICE_RESTARTING = 'restarting'
SERVICE_ERROR = 'error'

#: Состояния камеры.
CAMERA_CONNECTED = 'connected'
CAMERA_NO_LINK = 'no_link'
CAMERA_CONNECTING = 'connecting'
CAMERA_RECEIVING = CAMERA_CONNECTING
CAMERA_ERROR = 'error'
CAMERA_UNKNOWN = 'unknown'

#: Состояния Telegram (индикатор в шапке — доступность канала).
TELEGRAM_OK = 'ok'
TELEGRAM_UNAVAILABLE = 'unavailable'
TELEGRAM_NO_CONNECTION = TELEGRAM_UNAVAILABLE
TELEGRAM_DELIVERY_ERROR = 'delivery_error'
TELEGRAM_NOT_CONFIGURED = 'not_configured'
TELEGRAM_CHECKING = 'checking'
TELEGRAM_UNKNOWN = 'unknown'

#: Состояния приёма ANPR-событий — отдельный диагноз от RTSP.
CAMERA_EVENTS_RECEIVING = 'receiving'
CAMERA_EVENTS_NONE = 'none'
CAMERA_EVENTS_ERROR = 'error'

#: Подписи приёма ANPR-событий.
CAMERA_EVENTS_LABELS = {
    CAMERA_EVENTS_RECEIVING: 'принимаются',
    CAMERA_EVENTS_NONE: 'нет данных',
    CAMERA_EVENTS_ERROR: 'ошибка',
}

#: Состояния связи с Telegram — доступен ли API сейчас.
TELEGRAM_CONNECTED = 'connected'
TELEGRAM_CONNECTION_LOST = 'connection_lost'
TELEGRAM_CONNECTION_UNKNOWN = 'connection_unknown'

#: Состояния последней доставки — ушло ли сообщение.
TELEGRAM_DELIVERY_OK = 'delivery_ok'
TELEGRAM_DELIVERY_PARTIAL = 'delivery_partial'
TELEGRAM_DELIVERY_FAILED = 'delivery_failed'
TELEGRAM_DELIVERY_NONE = 'delivery_none'

#: Подписи состояний (единый словарь — источник истины для интерфейса).
SERVICE_LABELS = {
    SERVICE_RUNNING: 'Работает',
    SERVICE_STOPPED: 'Остановлен',
    SERVICE_STARTING: 'Запускается',
    SERVICE_STOPPING: 'Останавливается',
    SERVICE_RESTARTING: 'Перезапускается',
    SERVICE_ERROR: 'Ошибка',
}

CAMERA_LABELS = {
    CAMERA_CONNECTED: 'Подключена',
    CAMERA_NO_LINK: 'Нет связи',
    CAMERA_CONNECTING: 'Подключение',
    CAMERA_ERROR: 'Нет связи',
    CAMERA_UNKNOWN: 'Неизвестно',
}

TELEGRAM_LABELS = {
    TELEGRAM_OK: 'Работает',
    TELEGRAM_UNAVAILABLE: 'Недоступен',
    TELEGRAM_DELIVERY_ERROR: 'Ошибка доставки',
    TELEGRAM_NOT_CONFIGURED: 'Не настроен',
    TELEGRAM_CHECKING: 'Проверка',
    TELEGRAM_UNKNOWN: 'Неизвестно',
}

#: Подписи отдельных диагнозов Telegram.
TELEGRAM_CONNECTION_LABELS = {
    TELEGRAM_CONNECTED: 'Есть',
    TELEGRAM_CONNECTION_LOST: 'Нет',
    TELEGRAM_CONNECTION_UNKNOWN: 'Не проверялось',
}

TELEGRAM_DELIVERY_LABELS = {
    TELEGRAM_DELIVERY_OK: 'Доставлено',
    TELEGRAM_DELIVERY_PARTIAL: 'Частично',
    TELEGRAM_DELIVERY_FAILED: 'Не доставлено',
    TELEGRAM_DELIVERY_NONE: 'Отправок не было',
}

#: Короткие подписи — для узкого окна, где полные не помещаются.
SERVICE_SHORT = {
    SERVICE_RUNNING: 'Работает',
    SERVICE_STOPPED: 'Остановлен',
    SERVICE_STARTING: 'Запуск',
    SERVICE_STOPPING: 'Остановка',
    SERVICE_RESTARTING: 'Перезапуск',
    SERVICE_ERROR: 'Ошибка',
}

CAMERA_SHORT = {
    CAMERA_CONNECTED: 'Подключена',
    CAMERA_NO_LINK: 'Нет связи',
    CAMERA_CONNECTING: 'Подключение',
    CAMERA_ERROR: 'Нет связи',
    CAMERA_UNKNOWN: 'Неизвестно',
}

TELEGRAM_SHORT = {
    TELEGRAM_OK: 'Работает',
    TELEGRAM_UNAVAILABLE: 'Недоступен',
    TELEGRAM_DELIVERY_ERROR: 'Ошибка доставки',
    TELEGRAM_NOT_CONFIGURED: 'Не настроен',
    TELEGRAM_CHECKING: 'Проверка',
    TELEGRAM_UNKNOWN: 'Неизвестно',
}

TELEGRAM_CONNECTION_SHORT = {
    TELEGRAM_CONNECTED: 'Есть',
    TELEGRAM_CONNECTION_LOST: 'Нет',
    TELEGRAM_CONNECTION_UNKNOWN: 'Не знаю',
}

TELEGRAM_DELIVERY_SHORT = {
    TELEGRAM_DELIVERY_OK: 'Доставлено',
    TELEGRAM_DELIVERY_PARTIAL: 'Частично',
    TELEGRAM_DELIVERY_FAILED: 'Не доставлено',
    TELEGRAM_DELIVERY_NONE: 'Не было',
}

#: Подписи операций управления (для панели).
OPERATION_LABELS = {
    'start': 'Запуск сервиса',
    'stop': 'Остановка сервиса',
    'restart': 'Перезапуск сервиса',
}


def view(code, label, color, detail='', short=None):
    """Собрать описание состояния индикатора."""
    return {'code': code, 'label': label, 'color': color, 'detail': detail or '',
            'short': short or label}


def service_view(data=None, busy=False, operation=None, phase=None, error=''):
    """Состояние сервиса ANPR.

    `data` — ответ `/health` (None, если сервис не отвечает);
    `busy`/`operation`/`phase` — выполняется ли сейчас операция управления.
    """
    if busy and operation in ('start', 'stop', 'restart'):
        if operation == 'start':
            return view(SERVICE_STARTING, SERVICE_LABELS[SERVICE_STARTING], ACCENT,
                        'Запускаем ANPR-мониторинг…', SERVICE_SHORT[SERVICE_STARTING])
        if operation == 'stop':
            return view(SERVICE_STOPPING, SERVICE_LABELS[SERVICE_STOPPING], ACCENT,
                        'Останавливаем ANPR-мониторинг…', SERVICE_SHORT[SERVICE_STOPPING])
        return view(SERVICE_RESTARTING, SERVICE_LABELS[SERVICE_RESTARTING], ACCENT,
                    'Перезапускаем ANPR-мониторинг…', SERVICE_SHORT[SERVICE_RESTARTING])
    if data and data.get('status') == 'ok':
        return view(SERVICE_RUNNING, SERVICE_LABELS[SERVICE_RUNNING], SUCCESS,
                    'Сервис отвечает и выполняет сбор данных.')
    if data:
        # Сервис ответил, но рабочий поток остановлен.
        return view(SERVICE_ERROR, SERVICE_LABELS[SERVICE_ERROR], DANGER,
                    'Сервис отвечает, но сбор данных не выполняется.', SERVICE_SHORT[SERVICE_ERROR])
    if error:
        return view(SERVICE_ERROR, SERVICE_LABELS[SERVICE_ERROR], DANGER, str(error),
                    SERVICE_SHORT[SERVICE_ERROR])
    return view(SERVICE_STOPPED, SERVICE_LABELS[SERVICE_STOPPED], MUTED, 'Сервис не запущен.',
                SERVICE_SHORT[SERVICE_STOPPED])


def camera_view(data=None, running=False):
    """Состояние камеры по данным `/health`.

    Состояние камеры не зависит от состояния сервиса и Telegram. Если сервис
    остановлен, камера не опрашивается — состояние неизвестно, а не «нет
    связи»: подтверждённого обрыва связи у нас нет.
    """
    if not running:
        return view(CAMERA_UNKNOWN, CAMERA_LABELS[CAMERA_UNKNOWN], MUTED,
                    'Сервис остановлен. Состояние камеры неизвестно.',
                    CAMERA_SHORT[CAMERA_UNKNOWN])
    data = data or {}
    message = str(data.get('camera_message') or '').strip()
    lowered = message.lower()
    last_data = str(data.get('last_camera_data') or '').strip()
    if lowered.startswith('подключена'):
        if last_data:
            return view(CAMERA_CONNECTED, CAMERA_LABELS[CAMERA_CONNECTED], SUCCESS,
                        f'{message} Последние данные: {last_data}.' if message else last_data,
                        CAMERA_SHORT[CAMERA_CONNECTED])
        return view(CAMERA_CONNECTED, CAMERA_LABELS[CAMERA_CONNECTED], SUCCESS,
                    message or 'Камера подключена.', CAMERA_SHORT[CAMERA_CONNECTED])
    if 'прервалась' in lowered or 'закрыто' in lowered or 'недоступна' in lowered:
        return view(CAMERA_NO_LINK, CAMERA_LABELS[CAMERA_NO_LINK], DANGER, message,
                    CAMERA_SHORT[CAMERA_NO_LINK])
    if 'подключаюсь' in lowered or 'подключение' in lowered:
        return view(CAMERA_CONNECTING, CAMERA_LABELS[CAMERA_CONNECTING], WARNING, message,
                    CAMERA_SHORT[CAMERA_CONNECTING])
    if 'ошиб' in lowered or 'не удалось' in lowered or 'отказ' in lowered:
        return view(CAMERA_ERROR, CAMERA_LABELS[CAMERA_ERROR], DANGER, message,
                    CAMERA_SHORT[CAMERA_ERROR])
    return view(CAMERA_UNKNOWN, CAMERA_LABELS[CAMERA_UNKNOWN], MUTED,
                message or 'Нет данных о состоянии камеры.', CAMERA_SHORT[CAMERA_UNKNOWN])


def camera_events_view(data=None, running=False):
    """Принимаются ли ANPR-события — отдельный диагноз от RTSP.

    Камера может быть подключена, но события не приходить: это разные
    вещи, и путать их нельзя. Если сервис остановлен, камера не
    опрашивается — событий нет, но и обрыва связи не зафиксировано.
    """
    if not running:
        return view(CAMERA_EVENTS_NONE, CAMERA_EVENTS_LABELS[CAMERA_EVENTS_NONE], MUTED,
                    'Сервис остановлен. События не опрашиваются.',
                    CAMERA_EVENTS_LABELS[CAMERA_EVENTS_NONE])
    data = data or {}
    message = str(data.get('camera_message') or '').strip().lower()
    if 'прервалась' in message or 'недоступна' in message or 'ошиб' in message \
            or 'не удалось' in message or 'отказ' in message:
        return view(CAMERA_EVENTS_ERROR, CAMERA_EVENTS_LABELS[CAMERA_EVENTS_ERROR], DANGER,
                    'Камера недоступна. События не приходят.',
                    CAMERA_EVENTS_LABELS[CAMERA_EVENTS_ERROR])
    if str(data.get('last_camera_data') or '').strip():
        return view(CAMERA_EVENTS_RECEIVING, CAMERA_EVENTS_LABELS[CAMERA_EVENTS_RECEIVING],
                    SUCCESS, 'События ANPR приходят.', CAMERA_EVENTS_LABELS[CAMERA_EVENTS_RECEIVING])
    return view(CAMERA_EVENTS_NONE, CAMERA_EVENTS_LABELS[CAMERA_EVENTS_NONE], MUTED,
                'События ANPR ещё не приходили.', CAMERA_EVENTS_LABELS[CAMERA_EVENTS_NONE])


def mark_stale(value) -> str:
    """Пометить последние известные данные как устаревшие.

    Когда компонент недоступен, его последние данные правдивы только
    исторически — показывать их без отметки нельзя, иначе они читаются
    как актуальные.
    """
    text = str(value or '').strip()
    if not text or text in ('Нет данных', '—'):
        return text or 'Нет данных'
    return f'{text} (устарело)'


def telegram_connection_view(data=None, running=False, configured=True):
    """Связь с Telegram: доступен ли API сейчас.

    Отдельный диагноз от доставки: сообщение могло уйти час назад, а
    Telegram сейчас недоступен — и наоборот, доставка может сорваться
    при живой связи (чат заблокировал бота).
    """
    if not configured:
        return view(TELEGRAM_NOT_CONFIGURED, 'Не настроен', MUTED,
                    'Не задан токен бота или список разрешённых чатов.', 'Не задано')
    if not running:
        return view(TELEGRAM_CONNECTION_UNKNOWN, 'Сервис остановлен', MUTED,
                    'Сервис остановлен. Обращения к Telegram не идут.', 'Остановлен')
    data = data or {}
    connection = data.get('telegram_connection') or {}
    at = str(connection.get('at') or '').strip()
    failed_at = str(connection.get('failed_at') or '').strip()
    error = str(connection.get('error') or '').strip()
    if connection.get('ok'):
        detail = (f'Последнее обращение к Telegram прошло успешно ({at}).' if at
                  else 'Telegram отвечает.')
        return view(TELEGRAM_CONNECTED, TELEGRAM_CONNECTION_LABELS[TELEGRAM_CONNECTED], SUCCESS,
                    detail, TELEGRAM_CONNECTION_SHORT[TELEGRAM_CONNECTED])
    if failed_at or error:
        detail = f'Последнее обращение к Telegram не прошло: {error}' if error \
            else f'Последнее обращение к Telegram не прошло ({failed_at}).'
        return view(TELEGRAM_CONNECTION_LOST, TELEGRAM_CONNECTION_LABELS[TELEGRAM_CONNECTION_LOST], DANGER,
                    detail, TELEGRAM_CONNECTION_SHORT[TELEGRAM_CONNECTION_LOST])
    return view(TELEGRAM_CONNECTION_UNKNOWN, TELEGRAM_CONNECTION_LABELS[TELEGRAM_CONNECTION_UNKNOWN], WARNING,
                'Обращений к Telegram ещё не было. Состояние неизвестно.',
                TELEGRAM_CONNECTION_SHORT[TELEGRAM_CONNECTION_UNKNOWN])


def telegram_delivery_view(data=None):
    """Последняя доставка: ушло ли сообщение и когда.

    Считает только факт отправки; связь с Telegram — отдельный диагноз.
    """
    data = data or {}
    delivery = data.get('telegram_delivery') or {}
    at = str(delivery.get('at') or '').strip()
    delivered = int(delivery.get('delivered') or 0)
    failed = int(delivery.get('failed') or 0)
    if not at:
        return view(TELEGRAM_DELIVERY_NONE, TELEGRAM_DELIVERY_LABELS[TELEGRAM_DELIVERY_NONE], MUTED,
                    'Сообщения ещё не отправлялись.', TELEGRAM_DELIVERY_SHORT[TELEGRAM_DELIVERY_NONE])
    detail = f'{at}: доставлено {delivered}, не доставлено {failed}.'
    if not delivery.get('ok'):
        return view(TELEGRAM_DELIVERY_FAILED, TELEGRAM_DELIVERY_LABELS[TELEGRAM_DELIVERY_FAILED], DANGER,
                    detail, TELEGRAM_DELIVERY_SHORT[TELEGRAM_DELIVERY_FAILED])
    if failed:
        return view(TELEGRAM_DELIVERY_PARTIAL, TELEGRAM_DELIVERY_LABELS[TELEGRAM_DELIVERY_PARTIAL], WARNING,
                    detail, TELEGRAM_DELIVERY_SHORT[TELEGRAM_DELIVERY_PARTIAL])
    return view(TELEGRAM_DELIVERY_OK, TELEGRAM_DELIVERY_LABELS[TELEGRAM_DELIVERY_OK], SUCCESS,
                detail, TELEGRAM_DELIVERY_SHORT[TELEGRAM_DELIVERY_OK])


def telegram_view(data=None, running=False, configured=True):
    """Состояние Telegram по данным `/health` и настройкам (.env).

    Индикатор в шапке отвечает на вопрос «доступен ли Telegram сейчас».
    Успешность последней отправки — другой вопрос: она показывается в
    панели управления отдельной строкой и не красит индикатор в красный.
    """
    if not configured:
        return view(TELEGRAM_NOT_CONFIGURED, TELEGRAM_LABELS[TELEGRAM_NOT_CONFIGURED], MUTED,
                    'Не задан токен бота или список разрешённых чатов.',
                    TELEGRAM_SHORT[TELEGRAM_NOT_CONFIGURED])
    if not running:
        return view(TELEGRAM_UNKNOWN, TELEGRAM_LABELS[TELEGRAM_UNKNOWN], MUTED,
                    'Сервис остановлен. Состояние Telegram неизвестно.',
                    TELEGRAM_SHORT[TELEGRAM_UNKNOWN])
    connection = telegram_connection_view(data, running, configured)
    if connection['code'] == TELEGRAM_CONNECTION_LOST:
        return view(TELEGRAM_UNAVAILABLE, TELEGRAM_LABELS[TELEGRAM_UNAVAILABLE], DANGER,
                    connection['detail'], TELEGRAM_SHORT[TELEGRAM_UNAVAILABLE])
    if connection['code'] == TELEGRAM_CONNECTED:
        message = str((data or {}).get('telegram_message') or '').strip()
        return view(TELEGRAM_OK, TELEGRAM_LABELS[TELEGRAM_OK], SUCCESS,
                    message or connection['detail'], TELEGRAM_SHORT[TELEGRAM_OK])
    # Контакта с Telegram ещё не было: «Проверка», а не «Нет соединения».
    return view(TELEGRAM_CHECKING, TELEGRAM_LABELS[TELEGRAM_CHECKING], WARNING,
                connection['detail'] or 'Ожидание первой отправки.',
                TELEGRAM_SHORT[TELEGRAM_CHECKING])


def telegram_configured(settings=None):
    """Настроен ли Telegram: нужен токен бота и хотя бы один разрешённый чат."""
    settings = settings or {}
    token = str(settings.get('TELEGRAM_BOT_TOKEN') or '').strip()
    allowed = [item.strip() for item in str(settings.get('ALLOWED_CHAT_IDS') or '').split(',') if item.strip()]
    return bool(token) and bool(allowed)


def format_duration(seconds):
    """Человекочитаемая длительность: «1 ч 05 мин»."""
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return ''
    if total < 0:
        return ''
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f'{hours} ч {minutes:02d} мин'
    if minutes:
        return f'{minutes} мин {secs:02d} с'
    return f'{secs} с'


def format_time(value):
    """Время «ЧЧ:ММ:СС» из ISO-строки или числа (время запуска сервиса)."""
    if value in (None, ''):
        return ''
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value).astimezone().strftime('%H:%M:%S')
        except (OSError, OverflowError, ValueError):
            return ''
    try:
        return datetime.fromisoformat(str(value)).astimezone().strftime('%H:%M:%S')
    except (TypeError, ValueError):
        return str(value)
