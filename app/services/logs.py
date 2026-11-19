"""Журналы сервиса: один писатель на файл, ротация по размеру.

Сервис пишет в `service.stdout.log` и `service.stderr.log`. Файлы не растут
бесконечно: текущий файл при превышении предела уходит в `.1`, предыдущая
копия — в `.2` и так далее. Механизм ротации — `RotatingFileHandler`
(`logging.handlers`): он сам переименовывает текущий файл и открывает новый.

Один файл — один писатель: и `print()` сервиса, и журнал uvicorn пишут
в один и тот же экземпляр, поэтому ротация не раскрывает файл на полпути.
"""
import logging
import logging.handlers
import os
import atexit
import sys
import threading
from datetime import datetime
from pathlib import Path

from app.core.paths import PROJECT_ROOT, runtime_dir

DEFAULT_MAX_MB = 10          # размер одного файла журнала, МБ
DEFAULT_BACKUPS = 3          # сколько старых копий хранить
STDOUT_NAME = 'service.stdout.log'
STDERR_NAME = 'service.stderr.log'


def _stamp() -> str:
    """Временная метка строки журнала: ISO 8601 с часовым поясом."""
    return datetime.now().astimezone().isoformat(timespec='milliseconds')


def _max_bytes() -> int:
    """Предел размера файла журнала (LOG_MAX_MB в .env, по умолчанию 10 МБ)."""
    try:
        megabytes = float(os.environ.get('LOG_MAX_MB') or DEFAULT_MAX_MB)
    except (TypeError, ValueError):
        megabytes = DEFAULT_MAX_MB
    return max(1, int(megabytes * 1024 * 1024))


def _backups() -> int:
    try:
        return max(0, int(os.environ.get('LOG_BACKUPS') or DEFAULT_BACKUPS))
    except (TypeError, ValueError):
        return DEFAULT_BACKUPS


class RotatingFileWriter:
    """Текстовый поток в файл с ротацией по размеру.

    Запись идёт через `RotatingFileHandler`: он держит открытый файл,
    а при переполнении переименовывает его в `.1` и открывает новый.

    Каждая строка получает временную метку ISO 8601 с часовым поясом —
    по журналу видно, когда что произошло. Незаконченная строка ждёт
    продолжения: метка ставится только когда строка дописана.
    """

    def __init__(self, path, max_bytes=None, backups=None):
        self.path = Path(path)
        self._handler = logging.handlers.RotatingFileHandler(
            str(self.path), maxBytes=max_bytes or _max_bytes(), backupCount=_backups() if backups is None else backups,
            encoding='utf-8', delay=False)
        self._lock = threading.RLock()
        self._pending = ''

    def write(self, text: str) -> None:
        """Записать текст. Ошибка диска не должна ронять сервис."""
        if not text:
            return
        with self._lock:
            try:
                stream = self._handler.stream
                if stream is None:
                    # Файл закрывали — открываем заново, журнал продолжает жить.
                    stream = self._handler._open()
                    self._handler.stream = stream
                self._pending += text
                lines = self._pending.split('\n')
                self._pending = lines.pop()
                payload = ''.join(f'{_stamp()} {line}\n' for line in lines)
                if payload:
                    stream.write(payload)
                    stream.flush()
                if self._size(stream) >= self._handler.maxBytes:
                    self._handler.doRollover()
            except (OSError, ValueError, AttributeError):
                pass

    def flush_pending(self) -> None:
        """Дописать недописанную строку (при остановке сервиса)."""
        with self._lock:
            if not self._pending:
                return
            text = self._pending
            self._pending = ''
            try:
                stream = self._handler.stream
                if stream is None:
                    return
                stream.write(f'{_stamp()} {text}\n')
                stream.flush()
            except (OSError, ValueError, AttributeError):
                pass

    def _size(self, stream=None) -> int:
        stream = stream if stream is not None else self._handler.stream
        try:
            return os.fstat(stream.fileno()).st_size
        except (OSError, ValueError, AttributeError):
            try:
                return stream.tell()
            except (OSError, ValueError, AttributeError):
                return 0

    def fileno(self) -> int:
        return self._handler.stream.fileno()

    def close(self) -> None:
        with self._lock:
            try:
                self.flush_pending()
            except Exception:
                pass
            try:
                self._handler.close()
            except Exception:
                pass


_writers = {}
_writers_lock = threading.Lock()


def writer_for(path) -> RotatingFileWriter:
    """Писатель для файла. Один файл — один писатель на весь процесс."""
    key = os.path.normcase(os.path.abspath(str(path)))
    with _writers_lock:
        writer = _writers.get(key)
        if writer is None:
            writer = RotatingFileWriter(path)
            _writers[key] = writer
        return writer


def write_log(path, text: str) -> None:
    """Записать текст в журнал (файл выбирается по пути)."""
    writer_for(path).write(text)


class LogStream:
    """Замена sys.stdout/sys.stderr: всё печатается в журнал с ротацией."""

    def __init__(self, path):
        self.path = str(Path(path))

    def write(self, text):
        write_log(self.path, text)
        return len(text)

    def flush(self):
        try:
            writer_for(self.path).flush_pending()
        except Exception:
            pass

    def isatty(self):
        return False

    def readable(self):
        return False

    def writable(self):
        return True

    def seekable(self):
        return False

    def fileno(self):
        return writer_for(self.path).fileno()

    def reconfigure(self, **kwargs):
        """Совместимость с sys.stdout.reconfigure(): параметры не применяются."""
        return None

    @property
    def encoding(self):
        return 'utf-8'

    @property
    def errors(self):
        return 'replace'


_installed = False


def install(base_dir=None, stdout_name: str = STDOUT_NAME, stderr_name: str = STDERR_NAME):
    """Перенаправить print() сервиса в журналы с ротацией.

    Возвращает пару путей (stdout, stderr). Повторный вызов ничего не меняет.
    """
    global _installed
    base = Path(base_dir) if base_dir else runtime_dir()
    out_path = base / stdout_name
    err_path = base / stderr_name
    if not _installed:
        sys.stdout = LogStream(out_path)
        sys.stderr = LogStream(err_path)
        _installed = True
        atexit.register(_flush_all)
    return out_path, err_path


def _flush_all() -> None:
    """Дописать недописанные строки при остановке сервиса."""
    with _writers_lock:
        writers = list(_writers.values())
    for writer in writers:
        try:
            writer.flush_pending()
        except Exception:
            pass


class BridgeHandler(logging.Handler):
    """Обработчик журналирования uvicorn: пишет в тот же файл, что и print()."""

    def __init__(self, filename=None, **kwargs):
        super().__init__()
        self._path = str(Path(filename)) if filename else None

    def emit(self, record):
        if not self._path:
            return
        try:
            write_log(self._path, self.format(record) + '\n')
        except Exception:
            pass


def uvicorn_log_config(base_dir=None, stdout_name: str = STDOUT_NAME,
                       stderr_name: str = STDERR_NAME) -> dict:
    """Конфигурация журналирования uvicorn с ротацией по размеру.

    Access-журнал (строки на каждый опрос панели) оставлен на уровне WARNING:
    иначе файл растёт на сотни килобайт в час без единой полезной записи.
    """
    base = Path(base_dir) if base_dir else runtime_dir()
    out_path = str(base / stdout_name)
    err_path = str(base / stderr_name)
    return {
        'version': 1,
        'disable_existing_loggers': False,
        'formatters': {
            'default': {'format': '%(levelname)s: %(message)s'},
        },
        'handlers': {
            'app_out': {'class': 'app.services.logs.BridgeHandler', 'filename': out_path,
                        'level': 'INFO', 'formatter': 'default'},
            'app_err': {'class': 'app.services.logs.BridgeHandler', 'filename': err_path,
                        'level': 'INFO', 'formatter': 'default'},
        },
        'root': {'handlers': ['app_out', 'app_err'], 'level': 'INFO'},
        'loggers': {
            'uvicorn': {'handlers': ['app_out', 'app_err'], 'level': 'INFO', 'propagate': False},
            'uvicorn.error': {'handlers': ['app_err'], 'level': 'INFO', 'propagate': False},
            'uvicorn.access': {'handlers': ['app_out'], 'level': 'WARNING', 'propagate': False},
        },
    }
