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

#: Состояния Telegram.
TELEGRAM_OK = 'ok'
TELEGRAM_DELIVERY_ERROR = 'delivery_error'
TELEGRAM_NO_CONNECTION = 'no_connection'
TELEGRAM_NOT_CONFIGURED = 'not_configured'

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
}


def view(code, label, color, detail=''):
    """Собрать описание состояния индикатора."""
    return {'code': code, 'label': label, 'color': color, 'detail': detail or ''}


def service_view(data=None, busy=False, operation=None, phase=None, error=''):
    """Состояние сервиса ANPR.

    `data` — ответ `/health` (None, если сервис не отвечает);
    `busy`/`operation`/`phase` — выполняется ли сейчас операция управления.
    """
    if busy:
        starting = operation == 'start' or (operation == 'restart' and phase == 'start')
        if starting:
            return view(SERVICE_STARTING, SERVICE_LABELS[SERVICE_STARTING], AMBER,
                        'Запускаем ANPR-мониторинг…')
        return view(SERVICE_STOPPING, SERVICE_LABELS[SERVICE_STOPPING], AMBER,
                    'Останавливаем ANPR-мониторинг…')
    if data and data.get('status') == 'ok':
        return view(SERVICE_RUNNING, SERVICE_LABELS[SERVICE_RUNNING], GREEN,
                    'Сервис отвечает и выполняет сбор данных.')
    if data:
        # Сервис ответил, но рабочий поток остановлен.
        return view(SERVICE_ERROR, SERVICE_LABELS[SERVICE_ERROR], RED,
                    'Сервис отвечает, но сбор данных не выполняется.')
    if error:
        return view(SERVICE_ERROR, SERVICE_LABELS[SERVICE_ERROR], RED, str(error))
    return view(SERVICE_STOPPED, SERVICE_LABELS[SERVICE_STOPPED], MUTED, 'Сервис не запущен.')


def camera_view(data=None, running=False):
    """Состояние камеры по данным `/health`."""
    if not running:
        return view(CAMERA_NO_LINK, CAMERA_LABELS[CAMERA_NO_LINK], MUTED,
                    'Сервис остановлен — камера не опрашивается.')
    data = data or {}
    message = str(data.get('camera_message') or '').strip()
    lowered = message.lower()
    last_data = str(data.get('last_camera_data') or '').strip()
    if lowered.startswith('подключена'):
        if last_data:
            return view(CAMERA_CONNECTED, CAMERA_LABELS[CAMERA_CONNECTED], GREEN,
                        f'{message} Последние данные: {last_data}.' if message else last_data)
        return view(CAMERA_CONNECTED, CAMERA_LABELS[CAMERA_CONNECTED], GREEN,
                    message or 'Камера подключена.')
    if 'прервалась' in lowered or 'закрыто' in lowered or 'недоступна' in lowered:
        return view(CAMERA_NO_LINK, CAMERA_LABELS[CAMERA_NO_LINK], RED, message)
    if 'подключаюсь' in lowered or 'подключение' in lowered:
        return view(CAMERA_RECEIVING, CAMERA_LABELS[CAMERA_RECEIVING], AMBER, message)
    if 'ошиб' in lowered or 'не удалось' in lowered or 'отказ' in lowered:
        return view(CAMERA_ERROR, CAMERA_LABELS[CAMERA_ERROR], RED, message)
    return view(CAMERA_NO_LINK, CAMERA_LABELS[CAMERA_NO_LINK], MUTED,
                message or 'Нет данных о состоянии камеры.')


def telegram_view(data=None, running=False, configured=True):
    """Состояние Telegram по данным `/health` и настройкам (.env)."""
    if not configured:
        return view(TELEGRAM_NOT_CONFIGURED, TELEGRAM_LABELS[TELEGRAM_NOT_CONFIGURED], MUTED,
                    'Не задан токен бота или список разрешённых чатов.')
    if not running:
        return view(TELEGRAM_NO_CONNECTION, TELEGRAM_LABELS[TELEGRAM_NO_CONNECTION], MUTED,
                    'Сервис остановлен — уведомления не отправляются.')
    data = data or {}
    delivery = data.get('telegram_delivery') or {}
    attempted = bool(delivery.get('at')) or bool(delivery.get('delivered')) or bool(delivery.get('failed'))
    message = str(data.get('telegram_message') or '').strip()
    if attempted:
        if delivery.get('ok'):
            return view(TELEGRAM_OK, TELEGRAM_LABELS[TELEGRAM_OK], GREEN, message)
        return view(TELEGRAM_DELIVERY_ERROR, TELEGRAM_LABELS[TELEGRAM_DELIVERY_ERROR], RED, message)
    if 'не удалось' in message.lower():
        return view(TELEGRAM_DELIVERY_ERROR, TELEGRAM_LABELS[TELEGRAM_DELIVERY_ERROR], RED, message)
    return view(TELEGRAM_NO_CONNECTION, TELEGRAM_LABELS[TELEGRAM_NO_CONNECTION], AMBER,
                message or 'Ожидание первой отправки.')


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
