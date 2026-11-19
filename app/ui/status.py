"""Единая интерпретация данных сервиса для индикаторов интерфейса.

Правило модуля: индикатор показывает только то, что реально пришло из
существующего API (`GET /health`). Искусственных состояний нет — если
данных нет, состояние «нет данных», а не «ошибка».

Модуль не импортирует Tkinter: это чистые функции, их легко проверять
тестами и переиспользовать в оболочке и в тестах панели.
"""
from datetime import datetime

#: Цвета индикаторов.
GREEN = '#56d8a0'
AMBER = '#f0c26b'
RED = '#ff8792'
MUTED = '#a5b1c2'

#: Состояния сервиса.
SERVICE_RUNNING = 'running'
SERVICE_STOPPED = 'stopped'
SERVICE_STARTING = 'starting'
SERVICE_STOPPING = 'stopping'
SERVICE_ERROR = 'error'

#: Состояния камеры.
CAMERA_CONNECTED = 'connected'
CAMERA_NO_LINK = 'no_link'
CAMERA_RECEIVING = 'receiving'
CAMERA_ERROR = 'error'

#: Состояния Telegram (индикатор в шапке).
TELEGRAM_OK = 'ok'
TELEGRAM_DELIVERY_ERROR = 'delivery_error'
TELEGRAM_NO_CONNECTION = 'no_connection'
TELEGRAM_NOT_CONFIGURED = 'not_configured'
TELEGRAM_CHECKING = 'checking'

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
    SERVICE_ERROR: 'Ошибка',
}

CAMERA_LABELS = {
    CAMERA_CONNECTED: 'Подключена',
    CAMERA_NO_LINK: 'Нет связи',
    CAMERA_RECEIVING: 'Получение данных',
    CAMERA_ERROR: 'Ошибка подключения',
}

TELEGRAM_LABELS = {
    TELEGRAM_OK: 'Работает',
    TELEGRAM_DELIVERY_ERROR: 'Ошибка доставки',
    TELEGRAM_NO_CONNECTION: 'Нет соединения',
    TELEGRAM_NOT_CONFIGURED: 'Не настроен',
    TELEGRAM_CHECKING: 'Проверяем…',
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
    SERVICE_ERROR: 'Ошибка',
}

CAMERA_SHORT = {
    CAMERA_CONNECTED: 'Подключена',
    CAMERA_NO_LINK: 'Нет связи',
    CAMERA_RECEIVING: 'Данные',
    CAMERA_ERROR: 'Ошибка',
}

TELEGRAM_SHORT = {
    TELEGRAM_OK: 'Работает',
    TELEGRAM_DELIVERY_ERROR: 'Ошибка',
    TELEGRAM_NO_CONNECTION: 'Нет связи',
    TELEGRAM_NOT_CONFIGURED: 'Не задано',
    TELEGRAM_CHECKING: 'Проверка',
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


def view(code, label, color, detail='', short=None):
    """Собрать описание состояния индикатора."""
    return {'code': code, 'label': label, 'color': color, 'detail': detail or '',
            'short': short or label}


def service_view(data=None, busy=False, operation=None, phase=None, error=''):
    """Состояние сервиса ANPR.

    `data` — ответ `/health` (None, если сервис не отвечает);
    `busy`/`operation`/`phase` — выполняется ли сейчас операция управления.
    """
    if busy:
        starting = operation == 'start' or (operation == 'restart' and phase == 'start')
        if starting:
            return view(SERVICE_STARTING, SERVICE_LABELS[SERVICE_STARTING], AMBER,
                        'Запускаем ANPR-мониторинг…', SERVICE_SHORT[SERVICE_STARTING])
        return view(SERVICE_STOPPING, SERVICE_LABELS[SERVICE_STOPPING], AMBER,
                    'Останавливаем ANPR-мониторинг…', SERVICE_SHORT[SERVICE_STOPPING])
    if data and data.get('status') == 'ok':
        return view(SERVICE_RUNNING, SERVICE_LABELS[SERVICE_RUNNING], GREEN,
                    'Сервис отвечает и выполняет сбор данных.')
    if data:
        # Сервис ответил, но рабочий поток остановлен.
        return view(SERVICE_ERROR, SERVICE_LABELS[SERVICE_ERROR], RED,
                    'Сервис отвечает, но сбор данных не выполняется.', SERVICE_SHORT[SERVICE_ERROR])
    if error:
        return view(SERVICE_ERROR, SERVICE_LABELS[SERVICE_ERROR], RED, str(error),
                    SERVICE_SHORT[SERVICE_ERROR])
    return view(SERVICE_STOPPED, SERVICE_LABELS[SERVICE_STOPPED], MUTED, 'Сервис не запущен.',
                SERVICE_SHORT[SERVICE_STOPPED])


def camera_view(data=None, running=False):
    """Состояние камеры по данным `/health`."""
    if not running:
        return view(CAMERA_NO_LINK, CAMERA_LABELS[CAMERA_NO_LINK], MUTED,
                    'Сервис остановлен — камера не опрашивается.', CAMERA_SHORT[CAMERA_NO_LINK])
    data = data or {}
    message = str(data.get('camera_message') or '').strip()
    lowered = message.lower()
    last_data = str(data.get('last_camera_data') or '').strip()
    if lowered.startswith('подключена'):
        if last_data:
            return view(CAMERA_CONNECTED, CAMERA_LABELS[CAMERA_CONNECTED], GREEN,
                        f'{message} Последние данные: {last_data}.' if message else last_data,
                        CAMERA_SHORT[CAMERA_CONNECTED])
        return view(CAMERA_CONNECTED, CAMERA_LABELS[CAMERA_CONNECTED], GREEN,
                    message or 'Камера подключена.', CAMERA_SHORT[CAMERA_CONNECTED])
    if 'прервалась' in lowered or 'закрыто' in lowered or 'недоступна' in lowered:
        return view(CAMERA_NO_LINK, CAMERA_LABELS[CAMERA_NO_LINK], RED, message,
                    CAMERA_SHORT[CAMERA_NO_LINK])
    if 'подключаюсь' in lowered or 'подключение' in lowered:
        return view(CAMERA_RECEIVING, CAMERA_LABELS[CAMERA_RECEIVING], AMBER, message,
                    CAMERA_SHORT[CAMERA_RECEIVING])
    if 'ошиб' in lowered or 'не удалось' in lowered or 'отказ' in lowered:
        return view(CAMERA_ERROR, CAMERA_LABELS[CAMERA_ERROR], RED, message,
                    CAMERA_SHORT[CAMERA_ERROR])
    return view(CAMERA_NO_LINK, CAMERA_LABELS[CAMERA_NO_LINK], MUTED,
                message or 'Нет данных о состоянии камеры.', CAMERA_SHORT[CAMERA_NO_LINK])


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
                    'Сервис остановлен — обращения к Telegram не идут.', 'Остановлен')
    data = data or {}
    connection = data.get('telegram_connection') or {}
    at = str(connection.get('at') or '').strip()
    failed_at = str(connection.get('failed_at') or '').strip()
    error = str(connection.get('error') or '').strip()
    if connection.get('ok'):
        detail = (f'Последнее обращение к Telegram прошло успешно ({at}).' if at
                  else 'Telegram отвечает.')
        return view(TELEGRAM_CONNECTED, TELEGRAM_CONNECTION_LABELS[TELEGRAM_CONNECTED], GREEN,
                    detail, TELEGRAM_CONNECTION_SHORT[TELEGRAM_CONNECTED])
    if failed_at or error:
        detail = f'Последнее обращение к Telegram не прошло: {error}' if error \
            else f'Последнее обращение к Telegram не прошло ({failed_at}).'
        return view(TELEGRAM_CONNECTION_LOST, TELEGRAM_CONNECTION_LABELS[TELEGRAM_CONNECTION_LOST], RED,
                    detail, TELEGRAM_CONNECTION_SHORT[TELEGRAM_CONNECTION_LOST])
    return view(TELEGRAM_CONNECTION_UNKNOWN, TELEGRAM_CONNECTION_LABELS[TELEGRAM_CONNECTION_UNKNOWN], AMBER,
                'Обращений к Telegram ещё не было — состояние неизвестно.',
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
    detail = f'{at}: доставлено чатов — {delivered}, не доставлено — {failed}.'
    if not delivery.get('ok'):
        return view(TELEGRAM_DELIVERY_FAILED, TELEGRAM_DELIVERY_LABELS[TELEGRAM_DELIVERY_FAILED], RED,
                    detail, TELEGRAM_DELIVERY_SHORT[TELEGRAM_DELIVERY_FAILED])
    if failed:
        return view(TELEGRAM_DELIVERY_PARTIAL, TELEGRAM_DELIVERY_LABELS[TELEGRAM_DELIVERY_PARTIAL], AMBER,
                    detail, TELEGRAM_DELIVERY_SHORT[TELEGRAM_DELIVERY_PARTIAL])
    return view(TELEGRAM_DELIVERY_OK, TELEGRAM_DELIVERY_LABELS[TELEGRAM_DELIVERY_OK], GREEN,
                detail, TELEGRAM_DELIVERY_SHORT[TELEGRAM_DELIVERY_OK])


def telegram_view(data=None, running=False, configured=True):
    """Состояние Telegram по данным `/health` и настройкам (.env).

    Индикатор в шапке отвечает на вопрос «доступен ли Telegram сейчас»,
    поэтому основной признак — связь; ошибка последней доставки тоже
    красит его в красный, но это отдельный диагноз. Подробности — в
    `telegram_connection_view` и `telegram_delivery_view`.
    """
    if not configured:
        return view(TELEGRAM_NOT_CONFIGURED, TELEGRAM_LABELS[TELEGRAM_NOT_CONFIGURED], MUTED,
                    'Не задан токен бота или список разрешённых чатов.',
                    TELEGRAM_SHORT[TELEGRAM_NOT_CONFIGURED])
    if not running:
        return view(TELEGRAM_NO_CONNECTION, TELEGRAM_LABELS[TELEGRAM_NO_CONNECTION], MUTED,
                    'Сервис остановлен — уведомления не отправляются.',
                    TELEGRAM_SHORT[TELEGRAM_NO_CONNECTION])
    connection = telegram_connection_view(data, running, configured)
    delivery = telegram_delivery_view(data)
    if connection['code'] == TELEGRAM_CONNECTION_LOST:
        return view(TELEGRAM_NO_CONNECTION, 'Нет связи', RED,
                    connection['detail'], 'Нет связи')
    if delivery['code'] == TELEGRAM_DELIVERY_FAILED:
        message = str((data or {}).get('telegram_message') or '').strip()
        return view(TELEGRAM_DELIVERY_ERROR, TELEGRAM_LABELS[TELEGRAM_DELIVERY_ERROR], RED,
                    message or delivery['detail'], TELEGRAM_SHORT[TELEGRAM_DELIVERY_ERROR])
    if connection['code'] == TELEGRAM_CONNECTED:
        message = str((data or {}).get('telegram_message') or '').strip()
        return view(TELEGRAM_OK, TELEGRAM_LABELS[TELEGRAM_OK], GREEN,
                    message or connection['detail'], TELEGRAM_SHORT[TELEGRAM_OK])
    return view(TELEGRAM_CHECKING, TELEGRAM_LABELS[TELEGRAM_CHECKING], AMBER,
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
