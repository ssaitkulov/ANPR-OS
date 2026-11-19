"""Структурированный журнал управления сервисом (JSONL).

Два журнала, по одному JSON-объекту на строку:

* `service.control.log` — операции управления сервисом: запуск, остановка,
  перезапуск, запросы остановки от клиента, отказы авторизации;
* `panel.log` — события панели: запрос подтверждения и ответ пользователя,
  завершение операции, завершение панели.

Каждая запись несёт:

* `ts` — время ISO 8601 с часовым поясом;
* `action` — что произошло;
* `source` — кто инициировал (панель, CLI, сервис, система);
* `initiator_pid` — PID процесса, инициировавшего операцию;
* `target_pid` — PID целевого процесса;
* `result` — `requested`, `ok`, `error` или `denied`;
* `duration_ms` — длительность операции;
* `error` — причина ошибки (пусто, если ошибки не было).

Секреты в журнал не попадают: значения полей с именами вроде `token`,
`password`, `secret` маскируются, а из текста ошибок вычищаются токены
бота, пароли и учётные данные из адресов.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app.core.paths import runtime_dir

#: Имена журналов и файлы, в которых они живут.
JOURNALS = {
    'control': 'service.control.log',
    'panel': 'panel.log',
}

#: Размер файла журнала до ротации, МБ.
MAX_MB = 5
#: Сколько старых копий хранить.
BACKUPS = 3

#: Поля, значение которых никогда не пишется в журнал.
SECRET_KEYS = {'token', 'bot_token', 'control_token', 'password', 'passwd',
               'secret', 'api_key', 'apikey', 'authorization', 'auth', 'key',
               'chat_id', 'access_token', 'refresh_token'}

#: Значения, которые маскируются в любом месте текста (например, токен
#: управления сервисом): регистрируются при создании.
_secrets: set[str] = set()
_secrets_lock = threading.Lock()

#: Шаблоны «секретов» в свободном тексте.
_TEXT_RULES = (
    (re.compile(r'bot\d{4,}:[A-Za-z0-9_\-]{10,}'), 'bot<скрыто>'),
    (re.compile(r'(?i)\b(token|password|passwd|secret|api[_-]?key|authorization)'
                r'(\s*[:=]\s*)([^\s,;"\']+)'), r'\1\2<скрыто>'),
    (re.compile(r'(?i)\b(Authorization|X-Avrika-Control-Token)(\s*[:=]\s*)([^\s,;"\']+)'),
     r'\1\2<скрыто>'),
    (re.compile(r'(?<=://)[^/\s:@]+:[^/\s@]+@'), '<скрыто>@'),
)

#: Писатели журналов: один на файл на весь процесс.
_writers: dict[str, 'JournalWriter'] = {}
_writers_lock = threading.Lock()


def register_secret(value) -> None:
    """Запомнить значение, которое нельзя писать в журнал (токен управления)."""
    text = str(value or '').strip()
    if len(text) < 8:
        return
    with _secrets_lock:
        _secrets.add(text)


def redact_text(text) -> str:
    """Убрать секреты из свободного текста."""
    result = str(text or '')
    with _secrets_lock:
        secrets = list(_secrets)
    for secret in secrets:
        if secret and secret in result:
            result = result.replace(secret, '<скрыто>')
    for pattern, replacement in _TEXT_RULES:
        result = pattern.sub(replacement, result)
    return result


def redact_detail(value):
    """Привести значение к безопасному виду (словари и списки — рекурсивно)."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if str(key).lower() in SECRET_KEYS:
                result[key] = '<скрыто>'
            else:
                result[key] = redact_detail(item)
        return result
    if isinstance(value, (list, tuple)):
        return [redact_detail(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def timestamp() -> str:
    """Текущее время ISO 8601 с часовым поясом."""
    return datetime.now().astimezone().isoformat(timespec='milliseconds')


class JournalWriter:
    """Писатель одного журнала: дописывает строки, ротирует по размеру."""

    def __init__(self, path: Path, max_bytes: int | None = None, backups: int | None = None):
        self.path = Path(path)
        self.max_bytes = max_bytes if max_bytes is not None else int(MAX_MB * 1024 * 1024)
        self.backups = backups if backups is not None else BACKUPS
        self._lock = threading.Lock()

    def write(self, payload: dict) -> None:
        line = json.dumps(payload, ensure_ascii=False, sort_keys=False) + '\n'
        with self._lock:
            try:
                self._rotate_if_needed()
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open('a', encoding='utf-8') as out:
                    out.write(line)
            except OSError:
                # Журнал не должен ронять сервис.
                pass

    def _rotate_if_needed(self) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self.max_bytes:
            return
        for index in range(self.backups, 0, -1):
            source = self.path.with_name(self.path.name + f'.{index}')
            target = self.path.with_name(self.path.name + f'.{index + 1}')
            if source.exists():
                try:
                    if target.exists():
                        target.unlink()
                    source.replace(target)
                except OSError:
                    pass
        first = self.path.with_name(self.path.name + '.1')
        try:
            if first.exists():
                first.unlink()
            self.path.replace(first)
        except OSError:
            pass

    def read(self) -> list[dict]:
        """Прочитать записи журнала (для проверок и диагностики)."""
        try:
            text = self.path.read_text(encoding='utf-8')
        except OSError:
            return []
        records = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        return records


def journal_path(journal: str) -> Path:
    """Путь к файлу журнала."""
    name = JOURNALS.get(journal, journal)
    return runtime_dir() / name


def writer_for(journal: str) -> JournalWriter:
    """Писатель журнала (один на файл на процесс)."""
    with _writers_lock:
        writer = _writers.get(journal)
        if writer is None:
            writer = JournalWriter(journal_path(journal))
            _writers[journal] = writer
        return writer


def record(action: str, result: str = 'ok', *, journal: str = 'control', source: str = '',
           initiator_pid: int | None = None, target_pid: int | None = None,
           error: str | None = None, duration_ms: float | None = None, **extra) -> dict:
    """Записать событие в журнал.

    Возвращает записанный объект — удобно для проверок в тестах.
    """
    payload = {
        'ts': timestamp(),
        'action': str(action),
        'source': str(source or ''),
        'initiator_pid': int(initiator_pid) if initiator_pid else os.getpid(),
        'target_pid': int(target_pid) if target_pid else None,
        'result': str(result),
        'duration_ms': round(float(duration_ms), 3) if duration_ms is not None else None,
        'error': redact_text(error) if error else None,
    }
    details = redact_detail(extra)
    if details:
        payload['details'] = details
    writer_for(journal).write(payload)
    return payload


def control(action: str, result: str = 'ok', **kwargs) -> dict:
    """Записать событие в журнал управления сервисом."""
    kwargs.setdefault('source', 'service')
    return record(action, result, journal='control', **kwargs)


def panel(action: str, result: str = 'ok', **kwargs) -> dict:
    """Записать событие в журнал панели."""
    kwargs.setdefault('source', 'panel')
    return record(action, result, journal='panel', **kwargs)


@contextmanager
def operation(action: str, *, journal: str = 'control', source: str = '',
              target_pid: int | None = None, **extra):
    """Обернуть операцию: записать запрос, длительность и результат.

    Использование::

        with audit.operation('service.start', source='panel') as op:
            ...
    """
    started = time.perf_counter()
    record(action, 'requested', journal=journal, source=source, target_pid=target_pid, **extra)
    holder = {'target_pid': target_pid, 'details': extra}
    try:
        yield holder
    except Exception as error:
        record(action, 'error', journal=journal, source=source,
               target_pid=holder.get('target_pid') or target_pid,
               error=str(error), duration_ms=(time.perf_counter() - started) * 1000, **extra)
        raise
    else:
        record(action, 'ok', journal=journal, source=source,
               target_pid=holder.get('target_pid') or target_pid,
               duration_ms=(time.perf_counter() - started) * 1000, **extra)


def read(journal: str) -> list[dict]:
    """Прочитать записи журнала."""
    return writer_for(journal).read()
