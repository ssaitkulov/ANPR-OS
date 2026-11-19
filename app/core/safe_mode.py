"""Безопасный тестовый режим: тесты не могут повлиять на рабочую систему.

Модуль подключается **до** импорта модулей приложения (см. `tests/__init__.py`
и корневой `conftest.py`) и закрывает все каналы воздействия на боевое
оборудование и на работающий сервис:

* **Изоляция данных.** `.env`, SQLite, JSON-состояние, фотографии, файл
  подписчиков, PID-файл и журналы уводятся во временный каталог прогона.
* **Запрет опасных действий.** Перекрыты: запуск процессов (`subprocess.Popen`,
  в том числе запуск ANPR и `taskkill`), HTTP-запросы (рабочий сервис, камера,
  Telegram, любой внешний адрес), RTSP-подключения и прямая отправка в Telegram.
* **Учёт фоновых потоков.** Подмены снимаются только после того, как все
  запущенные в тесте потоки завершились (см. `finalize`).

Режим включается по умолчанию при запуске тестов (`pytest`, `unittest`) и
явно — переменной окружения `ANPR_SAFE_MODE=1`. Значение `ANPR_SAFE_MODE=0`
выключает защиту — это осознанное решение человека, а не поведение по
умолчанию; в журнал при этом пишется предупреждение.

Защита многослойная: даже если прикладной код забыл проверить режим,
сетевой уровень всё равно не даст уйти запросу наружу.
"""
from __future__ import annotations

import atexit
import contextlib
import ipaddress
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from pathlib import Path
from urllib.parse import urlsplit

#: Переменная окружения, включающая режим явно.
ENV_FLAG = 'ANPR_SAFE_MODE'

#: Порт локального сервиса ANPR (тот же, что и в `app.services.control`).
DEFAULT_SERVICE_PORT = 18749

#: Куда уводятся рабочие данные в изоляции.
ENV_ISOLATION_DIR = 'ANPR_ISOLATION_DIR'
ENV_ENV_FILE = 'ANPR_ENV_FILE'
ENV_RUNTIME_DIR = 'ANPR_RUNTIME_DIR'
ENV_SERVICE_PORT = 'ANPR_SERVICE_PORT'


class SafeModeError(RuntimeError):
    """Действие запрещено безопасным тестовым режимом."""


# ----------------------------------------------------------------------
# Состояние режима
# ----------------------------------------------------------------------

_lock = threading.RLock()
_active = False
_reason = ''
_isolation_dir: Path | None = None
_blocked: list[dict] = []
_patches: list[tuple] = []
_threads = weakref.WeakSet()
_finalized = False
_suspend_depth = 0


def _flag_value() -> bool | None:
    """Значение `ANPR_SAFE_MODE`: True / False / None (не задано)."""
    raw = os.environ.get(ENV_FLAG)
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if value in ('1', 'true', 'yes', 'on'):
        return True
    if value in ('0', 'false', 'no', 'off'):
        return False
    return None


def detect_test_run() -> bool:
    """Похоже ли окружение на тестовый прогон?

    Признаки: запущен pytest или unittest. Проверка выполняется по
    `sys.modules` и по аргументам командной строки — она работает и при
    `python -m unittest discover`, где `conftest.py` не загружается.
    """
    if 'pytest' in sys.modules:
        return True
    if os.environ.get('PYTEST_CURRENT_TEST'):
        return True
    main = sys.modules.get('__main__')
    if getattr(main, '__package__', '') == 'unittest':
        return True
    argv = ' '.join(sys.argv[:3]).lower()
    return 'unittest' in argv or 'pytest' in argv


def activate(reason: str | None = None, force: bool | None = None) -> bool:
    """Включить безопасный режим. Повторный вызов ничего не меняет.

    Возвращает `True`, если режим включён этим вызовом.
    """
    global _active, _reason, _finalized
    with _lock:
        if _active:
            return False
        if force is None:
            flag = _flag_value()
            if flag is False:
                _warn('ANPR_SAFE_MODE=0: защита рабочей системы отключена намеренно.')
                return False
            if flag is True:
                reason = reason or 'включён явно (ANPR_SAFE_MODE=1)'
            else:
                if not detect_test_run():
                    return False
                reason = reason or 'обнаружен тестовый запуск'
        _active = True
        _reason = reason or ''
        _finalized = False
        _setup_isolation()
        _install_patches()
        atexit.register(_cleanup)
        return True


def is_active() -> bool:
    """Включён ли безопасный режим."""
    return _active


def reason() -> str:
    """Почему включён режим (для диагностики и журналов)."""
    return _reason


def isolation_dir() -> Path:
    """Временный каталог изоляции (создаётся при включении режима)."""
    return _isolation_dir or Path(tempfile.gettempdir())


def snapshot() -> dict:
    """Сводка состояния режима — для тестов и журналов."""
    return {
        'active': _active,
        'reason': _reason,
        'isolation_dir': str(isolation_dir()),
        'blocked_attempts': len(_blocked),
        'alive_threads': [thread.name for thread in list(_threads) if thread.is_alive()],
        'finalized': _finalized,
    }


# ----------------------------------------------------------------------
# Изоляция рабочих данных
# ----------------------------------------------------------------------

def _setup_isolation() -> None:
    """Увести рабочие данные в отдельный временный каталог.

    Переменные окружения задаются присваиванием, а не `setdefault`:
    `.env` читается через `setdefault` и не должен вернуть боевые значения
    поверх изоляции.
    """
    global _isolation_dir
    _isolation_dir = Path(tempfile.mkdtemp(prefix='anpr-safemode-'))
    root = _isolation_dir

    # Пустой .env: настройки камеры и бота из боевого файла не читаются.
    env_file = root / '.env'
    env_file.write_text('# безопасный тестовый режим: боевые настройки не подключены\n',
                        encoding='utf-8')
    runtime_dir = root / 'runtime'
    runtime_dir.mkdir(parents=True, exist_ok=True)
    images = root / 'images'
    images.mkdir(parents=True, exist_ok=True)

    overrides = {
        ENV_ISOLATION_DIR: root,
        ENV_ENV_FILE: env_file,
        ENV_RUNTIME_DIR: runtime_dir,
        # Пути, которые читает ядро сбора.
        'DB_FILE': root / 'anpr.sqlite3',
        'STATE_FILE': root / 'anpr_state.json',
        'IMAGE_DIR': images,
        'SUBSCRIBERS_FILE': root / 'subscribers.json',
        # Путь PID-файла и журналов сервиса.
        'SERVICE_PID_FILE': runtime_dir / 'service.pid',
        'ANPR_SERVICE_PORT': os.environ.get(ENV_SERVICE_PORT, str(DEFAULT_SERVICE_PORT)),
        # Секреты обнуляются: с ними нельзя ни отправить сообщение, ни войти.
        'TELEGRAM_BOT_TOKEN': '',
        'TELEGRAM_CHAT_ID': '',
        'ALLOWED_CHAT_IDS': '',
        'BROADCAST_CHAT': '',
        'DAHUA_PASSWORD': '',
        # Конфигурация фиксируется явно: результат теста не должен зависеть
        # от того, что оператор написал в боевом `.env`.
        'DIRECTION': '',
        'ALLOWED_LANES': '',
        'DAHUA_CHANNEL': '1',
        'CAMERA_NAME': 'Тестовая камера',
        'TIMEZONE': 'Europe/Moscow',
    }
    for name, value in overrides.items():
        os.environ[name] = str(value)


# ----------------------------------------------------------------------
# Политика запретов
# ----------------------------------------------------------------------

def service_ports() -> set[int]:
    """Порты, на которых работает рабочий сервис ANPR."""
    ports = set()
    raw = os.environ.get(ENV_SERVICE_PORT) or str(DEFAULT_SERVICE_PORT)
    try:
        ports.add(int(raw))
    except (TypeError, ValueError):
        ports.add(DEFAULT_SERVICE_PORT)
    return ports


def _is_loopback(host: str) -> bool:
    value = (host or '').strip().strip('[]').lower()
    if value in ('localhost', '127.0.0.1', '::1', '0.0.0.0'):
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def blocked_endpoint(host: str | None, port: int | None, kind: str = 'network') -> str | None:
    """Причина запрета подключения к адресу (или `None`, если можно).

    Локальные адреса разрешены — тестам нужен свой тестовый сервер.
    Запрещены: рабочий сервис ANPR (даже на localhost) и любой внешний
    адрес — камера, RTSP, Telegram, интернет.
    """
    if host is None:
        return None
    value = str(host).strip().strip('[]')
    if _is_loopback(value):
        try:
            number = int(port or 0)
        except (TypeError, ValueError):
            number = 0
        if number in service_ports():
            return f'рабочий сервис ANPR ({value}:{number})'
        return None
    return f'внешний адрес ({value}:{port})'


def blocked_url(url) -> str | None:
    """Причина запрета HTTP-запроса (или `None`, если можно)."""
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return 'неразбираемый адрес'
    scheme = (parts.scheme or '').lower()
    host = parts.hostname
    if not host:
        return 'адрес без узла'
    port = parts.port
    if port is None:
        port = 443 if scheme == 'https' else 80
    return blocked_endpoint(host, port, kind='http')


def blocked_command(command) -> str | None:
    """Причина запрета запуска процесса (или `None`, если можно)."""
    if isinstance(command, (str, bytes)):
        text = command.decode('utf-8', 'replace') if isinstance(command, bytes) else command
        parts = text.split()
    else:
        try:
            parts = [str(part) for part in command]
        except TypeError:
            parts = [str(command)]
    if not parts:
        return None
    lowered = [part.lower() for part in parts]
    executable = os.path.basename(lowered[0])
    joined = ' '.join(lowered)
    if executable in ('taskkill.exe', 'taskkill'):
        return 'завершение чужого процесса (taskkill)'
    if executable in ('python.exe', 'pythonw.exe', 'python', 'py.exe', 'uvicorn.exe'):
        return 'запуск интерпретатора Python'
    for part in lowered:
        name = os.path.basename(part)
        if name in ('app.py', 'service_control.py', 'anpr_telegram.py', 'control_panel.py'):
            return 'запуск приложения ANPR'
        if part.endswith('app.py') and 'anpr' in joined:
            return 'запуск приложения ANPR'
    return None


def _record(kind: str, detail: str) -> None:
    with _lock:
        _blocked.append({'at': time.time(), 'kind': kind, 'detail': detail})


def blocked_attempts() -> list[dict]:
    """Список заблокированных попыток (для тестов и журналов)."""
    with _lock:
        return list(_blocked)


def reset_attempts() -> None:
    """Очистить журнал заблокированных попыток (для тестов)."""
    with _lock:
        _blocked.clear()


def guard(action: str) -> None:
    """Запретить действие, если включён безопасный режим.

    Вызывается в опасных точках приложения (запуск/остановка сервиса,
    отправка в Telegram, подключение к камере) — понятная ошибка вместо
    тихого игнорирования.
    """
    if _active and not _suspend_depth:
        raise SafeModeError(f'{action} запрещено в безопасном тестовом режиме ({_reason}).')


@contextlib.contextmanager
def guards_suspended():
    """Временно снять прикладные запреты.

    Нужен модульным тестам, которые проверяют сам код управления сервисом
    с полными заглушками. Сетевые подмены при этом остаются включёнными:
    настоящий запрос к рабочему сервису всё равно не уйдёт.
    """
    global _suspend_depth
    _suspend_depth += 1
    try:
        yield
    finally:
        _suspend_depth = max(0, _suspend_depth - 1)


# ----------------------------------------------------------------------
# Подмены stdlib и библиотек
# ----------------------------------------------------------------------

def _guarded_popen(original):
    """Замена `subprocess.Popen` на подкласс.

    Именно подкласс, а не функция: код (например, `asyncio.windows_utils`)
    наследуется от `subprocess.Popen`, и подмена должна оставаться классом.
    """
    class GuardedPopen(original):
        def __init__(self, *args, **kwargs):
            command = args[0] if args else kwargs.get('args')
            problem = blocked_command(command)
            if problem:
                _record('process', f'{problem}: {command!r}')
                raise SafeModeError(f'Запуск процесса заблокирован ({problem}): {command!r}')
            super().__init__(*args, **kwargs)

    GuardedPopen.__name__ = getattr(original, '__name__', 'Popen')
    GuardedPopen.__qualname__ = GuardedPopen.__name__
    GuardedPopen.__doc__ = getattr(original, '__doc__', None)
    return GuardedPopen


def _guarded_session_request(original):
    def guarded(self, method, url, *args, **kwargs):
        problem = blocked_url(url)
        if problem:
            _record('http', f'{method} {url}: {problem}')
            raise SafeModeError(f'HTTP-запрос {method} {url} заблокирован ({problem}).')
        return original(self, method, url, *args, **kwargs)
    guarded.__name__ = getattr(original, '__name__', 'guarded')
    return guarded


def _guarded_connect(original, label: str):
    def guarded(self, address):
        host, port = _address_parts(address)
        problem = blocked_endpoint(host, port, kind='socket')
        if problem:
            _record('socket', f'{label} {host}:{port}: {problem}')
            raise SafeModeError(f'Подключение к {host}:{port} заблокировано ({problem}).')
        return original(self, address)
    guarded.__name__ = getattr(original, '__name__', 'guarded')
    return guarded


def _address_parts(address):
    if isinstance(address, (tuple, list)) and len(address) >= 2:
        return address[0], address[1]
    return None, None


def _guarded_urlopen(original):
    def guarded(url, *args, **kwargs):
        problem = blocked_url(url)
        if problem:
            _record('http', f'urlopen {url}: {problem}')
            raise SafeModeError(f'HTTP-запрос {url} заблокирован ({problem}).')
        return original(url, *args, **kwargs)
    guarded.__name__ = getattr(original, '__name__', 'guarded')
    return guarded


def _guarded_thread_start(original):
    def guarded(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if _active:
            with _lock:
                _threads.add(self)
        return result
    guarded.__name__ = getattr(original, '__name__', 'guarded')
    return guarded


def _install_patches() -> None:
    """Установить защитные подмены. Повторная установка невозможна."""
    if _patches:
        return

    original_popen = subprocess.Popen
    subprocess.Popen = _guarded_popen(original_popen)
    _patches.append((subprocess, 'Popen', original_popen))

    original_connect = socket.socket.connect
    socket.socket.connect = _guarded_connect(original_connect, 'connect')
    _patches.append((socket.socket, 'connect', original_connect))

    original_connect_ex = socket.socket.connect_ex
    socket.socket.connect_ex = _guarded_connect(original_connect_ex, 'connect_ex')
    _patches.append((socket.socket, 'connect_ex', original_connect_ex))

    original_start = threading.Thread.start
    threading.Thread.start = _guarded_thread_start(original_start)
    _patches.append((threading.Thread, 'start', original_start))

    try:
        import urllib.request as urllib_request
    except ImportError:  # pragma: no cover — urllib есть всегда
        urllib_request = None
    if urllib_request is not None:
        original_urlopen = urllib_request.urlopen
        urllib_request.urlopen = _guarded_urlopen(original_urlopen)
        _patches.append((urllib_request, 'urlopen', original_urlopen))

    try:
        from requests.sessions import Session as _RequestsSession
    except Exception:  # pragma: no cover — requests может отсутствовать
        _RequestsSession = None
    if _RequestsSession is not None:
        original_request = _RequestsSession.request
        _RequestsSession.request = _guarded_session_request(original_request)
        _patches.append((_RequestsSession, 'request', original_request))


def restore_patches() -> None:
    """Снять защитные подмены (только если потоки завершены)."""
    global _finalized
    with _lock:
        if not _patches:
            return
        for owner, name, original in reversed(_patches):
            try:
                setattr(owner, name, original)
            except Exception:  # pragma: no cover — снятие подмены не должно падать
                pass
        _patches.clear()
        _finalized = True


# ----------------------------------------------------------------------
# Фоновые потоки
# ----------------------------------------------------------------------

def track_thread(thread) -> None:
    """Запомнить поток, чтобы дождаться его перед снятием подмен."""
    with _lock:
        _threads.add(thread)


def alive_threads() -> list[str]:
    """Имена ещё работающих потоков."""
    return [thread.name for thread in list(_threads) if thread.is_alive()]


def join_threads(timeout: float = 5.0) -> list[str]:
    """Дождаться завершения фоновых потоков.

    Возвращает имена потоков, которые не завершились за отведённое время.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    for thread in list(_threads):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            thread.join(remaining)
        except RuntimeError:  # pragma: no cover — поток не запускался
            continue
    return alive_threads()


def finalize(timeout: float = 5.0) -> bool:
    """Завершить фоновые потоки и только потом снять подмены.

    Если поток не завершился — подмены остаются: защита рабочей системы
    важнее чистого состояния процесса.
    """
    global _finalized
    alive = join_threads(timeout)
    if alive:
        _record('threads', f'потоки не завершились: {alive}')
        return False
    restore_patches()
    return True


def _cleanup() -> None:
    """Выход из процесса: дождаться потоков, снять подмены, убрать временный каталог."""
    global _finalized
    try:
        finalize(timeout=2.0)
    except Exception:  # pragma: no cover — выход не должен падать
        pass
    if _isolation_dir is not None:
        shutil.rmtree(_isolation_dir, ignore_errors=True)


def _warn(message: str) -> None:
    try:
        print(f'[safe-mode] {message}', file=sys.stderr)
    except Exception:  # pragma: no cover
        pass
