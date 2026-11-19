#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Перенос рабочих данных в каталог `data/` (Этап 2.3).

Зачем: приложение запускают из разных мест, а рабочие данные лежали
в корне проекта. Пути, посчитанные от текущей директории или вписанные
абсолютно, ломались при переносе папки на другой диск. Теперь все
рабочие данные лежат в `data/`, а пути считаются от корня проекта
(см. `app/core/paths.py`).

Скрипт выполняет перенос один раз и с проверками:

1. проверяет, что сервис остановлен (нет живого процесса и свободен порт);
2. делает резервную копию всех переносимых файлов;
3. сливает WAL, проверяет целостность SQLite и снимает счётчики;
4. копирует файлы в `data/` (оригиналы пока целы);
5. переписывает пути к фотографиям в базе на относительные;
6. обновляет `.env` (пути становятся относительными);
7. проверяет результат: структура, целостность, счётчики, фотографии;
8. и только после успешной проверки удаляет оригиналы.

Откат: `python scripts/migrate_data.py --rollback backup/migration-<метка>` —
файлы возвращаются на прежние места из резервной копии.

Запуск:
    python scripts/migrate_data.py --check      # что переносится (без изменений)
    python scripts/migrate_data.py --migrate    # перенос с проверками
    python scripts/migrate_data.py --rollback backup/migration-20261119_120000
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _bootstrap import PROJECT_ROOT as BASE_DIR  # noqa: E402  — корень проекта — из app.core.paths
from app.core import paths as app_paths  # noqa: E402  — единый источник путей
from app.services import auth  # noqa: E402  — ACL токена управления

#: Порт локального сервиса ANPR (тот же, что в `app/services/control.py`).
SERVICE_PORT = 18749

#: Куда складываются резервные копии (внутри проекта — проект переносится целиком).
BACKUP_ROOT = BASE_DIR / 'backup'

#: Таблицы, для которых сверяются счётчики до и после переноса.
COUNT_TABLES = ('events', 'outbox', 'passage_photo_paths', 'events_legacy')

#: Файлы, которые переносятся как есть (путь в корне → путь в `data/`).
FILE_MAP = {
    # База переносится после слияния WAL — отдельным шагом.
    'anpr.sqlite3': ('anpr.sqlite3', 'data'),
    'anpr_state.json': ('anpr_state.json', 'data'),
    'subscribers.json': ('subscribers.json', 'data'),
    'ui_preferences.json': ('ui_preferences.json', 'data'),
    'ui_window.json': ('ui_window.json', 'data'),
    'service.pid': ('service.pid', 'data/runtime'),
    'service.token': ('service.token', 'data/runtime'),
    'service.boot.log': ('service.boot.log', 'data/logs'),
    'service.stdout.log': ('service.stdout.log', 'data/logs'),
    'service.stderr.log': ('service.stderr.log', 'data/logs'),
    'uvicorn.log': ('uvicorn.log', 'data/logs'),
}

#: Каталог фотографий: `images/` → `data/photos/`.
PHOTOS_SOURCE = 'images'
PHOTOS_TARGET = 'data/photos'


class MigrationError(RuntimeError):
    """Перенос невозможен или не прошёл проверку."""


# ----------------------------------------------------------------------
# Отчёт
# ----------------------------------------------------------------------

class Report:
    """Ход переноса: шаги, проверки, итог."""

    def __init__(self) -> None:
        self.steps: list[str] = []
        self.checks: list[tuple[str, bool, str]] = []
        self.errors: list[str] = []

    def step(self, text: str) -> None:
        self.steps.append(text)
        print(f'[шаг] {text}', flush=True)

    def check(self, name: str, ok: bool, detail: str = '') -> bool:
        self.checks.append((name, ok, detail))
        mark = 'OK  ' if ok else 'ОШИБКА'
        print(f'[{mark}] {name}' + (f' — {detail}' if detail else ''), flush=True)
        if not ok:
            self.errors.append(f'{name}: {detail}')
        return ok

    @property
    def failed(self) -> int:
        return sum(1 for _name, ok, _detail in self.checks if not ok)


# ----------------------------------------------------------------------
# Проверки перед переносом
# ----------------------------------------------------------------------

def port_is_busy(port: int = SERVICE_PORT) -> bool:
    """Занят ли порт локального сервиса."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(('127.0.0.1', port)) == 0


def pid_is_alive(pid: int) -> bool:
    """Жив ли процесс с таким PID (Windows и Unix — по-разному)."""
    if pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def running_service_pid() -> int | None:
    """PID работающего сервиса ANPR (None — сервис не запущен)."""
    for candidate in (BASE_DIR / 'service.pid', app_paths.RUNTIME_DIR / 'service.pid'):
        try:
            pid = int(candidate.read_text(encoding='ascii').strip())
        except (OSError, ValueError):
            continue
        if pid_is_alive(pid):
            return pid
    return None


def assert_service_stopped(report: Report) -> None:
    """Перенос выполняется только при остановленном сервисе."""
    pid = running_service_pid()
    if pid:
        raise MigrationError(
            f'Сервис работает (PID {pid}). Сначала остановите его: '
            'панель управления → «Остановить», затем повторите перенос.'
        )
    busy = port_is_busy()
    if busy:
        raise MigrationError(
            f'Порт {SERVICE_PORT} занят — сервис или другая программа ещё работает. '
            'Перенос невозможен: работающая копия базы и её копия разойдутся.'
        )
    report.check('Сервис остановлен', True,
                 f'порт {SERVICE_PORT} свободен, живых процессов нет')


# ----------------------------------------------------------------------
# SQLite
# ----------------------------------------------------------------------

def open_database(path: Path) -> sqlite3.Connection:
    """Подключение к базе (обычное — нужен доступ на запись)."""
    return sqlite3.connect(str(path), timeout=10)


def table_counts(path: Path) -> dict:
    """Счётчики строк во всех таблицах базы."""
    counts: dict[str, int] = {}
    conn = open_database(path)
    try:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in COUNT_TABLES:
            counts[table] = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] \
                if table in names else 0
    finally:
        conn.close()
    return counts


def integrity_check(path: Path) -> str:
    """`PRAGMA integrity_check` — 'ok' значит, что база цела."""
    conn = open_database(path)
    try:
        rows = conn.execute('PRAGMA integrity_check').fetchall()
    finally:
        conn.close()
    return '; '.join(str(row[0]) for row in rows)


def checkpoint_database(path: Path) -> int:
    """Слить WAL в базу и вернуть остаток WAL в байтах.

    `wal_checkpoint(TRUNCATE)` переносит все несохранённые страницы из
    WAL в основной файл базы. После этого WAL пуст, и базу можно
    переносить одним файлом — вместе с ней ничего не пропадёт.
    """
    conn = open_database(path)
    try:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()
    finally:
        conn.close()
    wal = Path(str(path) + '-wal')
    return wal.stat().st_size if wal.exists() else 0


# ----------------------------------------------------------------------
# Резервная копия
# ----------------------------------------------------------------------

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def copy_preserving(source: Path, target: Path) -> None:
    """Копия файла с сохранением содержимого и прав."""
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def copy_tree_preserving(source: Path, target: Path) -> int:
    """Копия каталога. Возвращает число скопированных файлов."""
    copied = 0
    for item in sorted(source.rglob('*')):
        relative = item.relative_to(source)
        destination = target / relative
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, destination)
        copied += 1
    return copied


def create_backup(plan: list[tuple[Path, Path]]) -> tuple[Path, dict]:
    """Резервная копия переносимых файлов. Возвращает каталог и опись."""
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_dir = BACKUP_ROOT / f'migration-{stamp}'
    backup_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'project_root': str(BASE_DIR),
        'entries': [],
    }
    for source, target in plan:
        if not source.exists():
            continue
        relative = source.relative_to(BASE_DIR)
        copy = backup_dir / 'files' / relative
        if source.is_dir():
            count = copy_tree_preserving(source, copy)
        else:
            copy_preserving(source, copy)
            count = 1
        manifest['entries'].append({
            'source': str(relative).replace('\\', '/'),
            'target': str(target.relative_to(BASE_DIR)).replace('\\', '/'),
            'backup': str(copy.relative_to(backup_dir)).replace('\\', '/'),
            'kind': 'dir' if source.is_dir() else 'file',
            'files': count,
            'bytes': source.stat().st_size if source.is_file() else 0,
            'sha256': sha256(source) if source.is_file() else '',
        })
    env_file = BASE_DIR / '.env'
    if env_file.exists():
        copy_preserving(env_file, backup_dir / 'files' / '.env')
        manifest['env_backup'] = 'files/.env'
    (backup_dir / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding='utf-8')
    return backup_dir, manifest


# ----------------------------------------------------------------------
# .env
# ----------------------------------------------------------------------

ENV_KEYS = {
    'DB_FILE': 'data/anpr.sqlite3',
    'STATE_FILE': 'data/anpr_state.json',
    'SUBSCRIBERS_FILE': 'data/subscribers.json',
    'IMAGE_DIR': 'data/photos',
}


def read_env_values(path: Path) -> dict:
    """Текущие значения путей из `.env` (до переноса) — для отката."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#') or '=' not in stripped:
            continue
        key, value = stripped.split('=', 1)
        key = key.strip()
        if key in ENV_KEYS:
            values[key] = value.strip().strip('"').strip("'")
    return values


def restore_env_values(path: Path, values: dict) -> None:
    """ Вернуть в `.env` прежние значения путей (откат). """
    if not values:
        return
    lines = path.read_text(encoding='utf-8-sig').splitlines() if path.exists() else []
    output: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = line.split('=', 1)[0].strip() if '=' in line and not line.lstrip().startswith('#') else ''
        if key in values:
            if key not in seen:
                seen.add(key)
                output.append(f'{key}={values[key]}')
            continue
        output.append(line)
    for key, value in values.items():
        if key not in seen:
            output.append(f'{key}={value}')
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text('\n'.join(output) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def update_env(path: Path) -> dict:
    """Прописать относительные пути рабочих данных. Возвращает изменения."""
    lines = path.read_text(encoding='utf-8-sig').splitlines() if path.exists() else []
    changes: dict[str, str] = {}
    output: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = line.split('=', 1)[0].strip() if '=' in line and not line.lstrip().startswith('#') else ''
        if key in ENV_KEYS:
            if key not in seen:
                seen.add(key)
                output.append(f'{key}={ENV_KEYS[key]}')
                changes[key] = ENV_KEYS[key]
            continue
        output.append(line)
    for key, value in ENV_KEYS.items():
        if key not in seen:
            output.append(f'{key}={value}')
            changes[key] = value
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text('\n'.join(output) + '\n', encoding='utf-8')
    os.replace(temporary, path)
    return changes


# ----------------------------------------------------------------------
# Перенос
# ----------------------------------------------------------------------

def build_plan() -> list[tuple[Path, Path]]:
    """План переноса: (источник, назначение)."""
    plan: list[tuple[Path, Path]] = []
    for source_name, (target_name, target_dir) in FILE_MAP.items():
        plan.append((BASE_DIR / source_name, BASE_DIR / target_dir / target_name))
    plan.append((BASE_DIR / PHOTOS_SOURCE, BASE_DIR / PHOTOS_TARGET))
    return plan


def existing_sources(plan: list[tuple[Path, Path]]) -> list[tuple[Path, Path]]:
    """Только те пары, где источник существует."""
    return [(source, target) for source, target in plan if source.exists()]


def describe_plan(plan: list[tuple[Path, Path]]) -> None:
    """Напечатать план переноса."""
    print('План переноса:')
    for source, target in plan:
        relative_source = source.relative_to(BASE_DIR)
        relative_target = target.relative_to(BASE_DIR)
        kind = 'каталог' if source.is_dir() else 'файл'
        size = '' if source.is_dir() else f' ({source.stat().st_size} байт)'
        print(f'  {relative_source}  →  {relative_target}   [{kind}{size}]')


def migrate(report: Report) -> int:
    """Полный перенос с проверками."""
    plan = build_plan()
    sources = existing_sources(plan)
    if not sources:
        print('Переносить нечего: в корне проекта нет рабочих данных.')
        return 0

    assert_service_stopped(report)
    describe_plan(sources)

    # -- 1. SQLite: чекпоинт, целостность, счётчики ------------------------
    # Слияние WAL идёт до резервной копии: копия должна содержать всю базу.
    source_db = BASE_DIR / 'anpr.sqlite3'
    before_counts: dict[str, int] = {}
    if source_db.exists():
        report.step('Слияние WAL и проверка целостности исходной базы')
        wal_left = checkpoint_database(source_db)
        report.check('WAL слит в базу', wal_left == 0,
                     f'остаток WAL: {wal_left} байт')
        integrity = integrity_check(source_db)
        report.check('Целостность исходной базы', integrity == 'ok', integrity)
        before_counts = table_counts(source_db)
        report.step('Счётчики исходной базы: ' + json.dumps(before_counts, ensure_ascii=False))

    # -- 2. Резервная копия ------------------------------------------------
    report.step('Резервная копия')
    backup_dir, manifest = create_backup(sources)
    report.check('Резервная копия создана', backup_dir.exists(),
                 str(backup_dir.relative_to(BASE_DIR)))

    # -- 3. Копирование в data/ -------------------------------------------
    report.step('Копирование файлов в data/ (оригиналы пока на месте)')
    for source, target in sources:
        if source.is_dir():
            count = copy_tree_preserving(source, target)
            print(f'  {source.relative_to(BASE_DIR)} → {target.relative_to(BASE_DIR)} '
                  f'({count} файлов)')
        else:
            copy_preserving(source, target)
            print(f'  {source.relative_to(BASE_DIR)} → {target.relative_to(BASE_DIR)}')

    # -- 4. Пути к фотографиям в базе -------------------------------------
    target_db = BASE_DIR / 'data' / 'anpr.sqlite3'
    if target_db.exists():
        report.step('Переписывание путей к фотографиям (относительные)')
        rewritten = rewrite_photo_paths(target_db, BASE_DIR / PHOTOS_SOURCE,
                                        BASE_DIR / PHOTOS_TARGET)
        report.check('Пути к фотографиям переписаны', True,
                     f'обновлено записей: {rewritten}')

    # -- 5. .env -----------------------------------------------------------
    report.step('Обновление .env (относительные пути)')
    previous_env = read_env_values(BASE_DIR / '.env')
    changes = update_env(BASE_DIR / '.env')
    manifest['env_previous'] = previous_env
    (backup_dir / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding='utf-8')
    report.check('Настройки путей обновлены', True,
                 ', '.join(f'{key}={value}' for key, value in changes.items()))

    # -- 6. Проверка результата -------------------------------------------
    report.step('Проверка результата')
    verify_result(report, target_db, before_counts, source_db)

    if report.failed:
        report.step('Проверка не пройдена — оригиналы оставлены на месте, '
                    'резервная копия: ' + str(backup_dir.relative_to(BASE_DIR)))
        (backup_dir / 'report.json').write_text(
            json.dumps({'ok': False, 'steps': report.steps,
                        'checks': [{'name': n, 'ok': ok, 'detail': d}
                                   for n, ok, d in report.checks]},
                       ensure_ascii=False, indent=1), encoding='utf-8')
        return 1

    # -- 7. Оригиналы удаляются только после успешной проверки -------------
    report.step('Удаление оригиналов (после успешной проверки)')
    removed = remove_sources(sources)
    report.check('Оригиналы удалены', True, ', '.join(removed) if removed else 'нечего удалять')

    (backup_dir / 'report.json').write_text(
        json.dumps({'ok': True, 'steps': report.steps,
                    'checks': [{'name': n, 'ok': ok, 'detail': d}
                               for n, ok, d in report.checks]},
                   ensure_ascii=False, indent=1), encoding='utf-8')
    report.step('Готово. Откат: python scripts/migrate_data.py --rollback '
                + str(backup_dir.relative_to(BASE_DIR)))
    return 0


def rewrite_photo_paths(db_path: Path, old_root: Path, new_root: Path) -> int:
    """Заменить абсолютные пути к фотографиям на относительные.

    В баке путь хранится относительно каталога снимков
    (`2026-11-17/файл.jpg`): база не знает, где лежит проект, — поэтому
    проект переносится целиком на другой диск.
    """
    conn = open_database(db_path)
    updated = 0
    try:
        rows = conn.execute('SELECT captured_at, plate, path FROM passage_photo_paths').fetchall()
        for captured_at, plate, stored in rows:
            text = str(stored or '')
            if not text:
                continue
            candidate = Path(text)
            relative = None
            if candidate.is_absolute():
                try:
                    relative = candidate.relative_to(old_root).as_posix()
                except ValueError:
                    continue
            else:
                relative = candidate.as_posix()
            conn.execute(
                'UPDATE passage_photo_paths SET path=? WHERE captured_at=? AND plate=?',
                (relative, captured_at, plate))
            updated += 1
        conn.commit()
    finally:
        conn.close()
    return updated


def remove_sources(sources: list[tuple[Path, Path]]) -> list[str]:
    """Удалить оригиналы после успешной проверки."""
    removed: list[str] = []
    for source, _target in sources:
        try:
            if source.is_dir():
                shutil.rmtree(source)
            else:
                source.unlink()
            removed.append(str(source.relative_to(BASE_DIR)))
        except OSError as error:
            print(f'  ! не удалось удалить {source}: {error}')
    # Служебные файлы WAL/SHM остались от прежней базы — они уже пусты.
    for suffix in ('-wal', '-shm'):
        stale = BASE_DIR / f'anpr.sqlite3{suffix}'
        if stale.exists():
            try:
                stale.unlink()
                removed.append(str(stale.relative_to(BASE_DIR)))
            except OSError:
                pass
    return removed


def verify_result(report: Report, target_db: Path, before_counts: dict,
                  source_db: Path) -> None:
    """Проверить результат переноса."""
    # 1. Структура каталогов.
    expected = [
        BASE_DIR / 'data' / 'anpr.sqlite3',
        BASE_DIR / 'data' / 'anpr_state.json',
        BASE_DIR / 'data' / 'subscribers.json',
        BASE_DIR / 'data' / 'photos',
        BASE_DIR / 'data' / 'runtime',
        BASE_DIR / 'data' / 'logs',
    ]
    missing = [str(path.relative_to(BASE_DIR)) for path in expected if not path.exists()]
    report.check('Структура data/ создана', not missing,
                 'нет: ' + ', '.join(missing) if missing else 'все каталоги и файлы на месте')

    # 2. Целостность базы.
    if target_db.exists():
        integrity = integrity_check(target_db)
        report.check('Целостность перенесённой базы', integrity == 'ok', integrity)
        after_counts = table_counts(target_db)
        report.check('Количество записей не изменилось', after_counts == before_counts,
                     f'до: {before_counts}; после: {after_counts}')
    elif source_db.exists():
        report.check('Перенесённая база на месте', False, 'anpr.sqlite3 не найден в data/')

    # 3. JSON-файлы читаются.
    for name in ('anpr_state.json', 'subscribers.json'):
        path = BASE_DIR / 'data' / name
        if not path.exists():
            continue
        try:
            json.loads(path.read_text(encoding='utf-8'))
            report.check(f'{name} читается', True, '')
        except (OSError, ValueError) as error:
            report.check(f'{name} читается', False, str(error))

    # 4. Фотографии доступны по путям из базы.
    if target_db.exists():
        conn = open_database(target_db)
        try:
            rows = conn.execute('SELECT captured_at, plate, path FROM passage_photo_paths').fetchall()
        finally:
            conn.close()
        photo_root = BASE_DIR / 'data' / 'photos'
        broken = []
        for captured_at, plate, stored in rows:
            path = app_paths.resolve_photo_path(stored, photo_root)
            if path is None or not path.exists():
                broken.append(f'{captured_at} {plate}')
        report.check('Фотографии доступны', not broken,
                     f'записей: {len(rows)}, недоступных: {len(broken)}'
                     + (f' (например: {", ".join(broken[:3])})' if broken else ''))

    # 5. ACL токена управления сохранён.
    token = BASE_DIR / 'data' / 'runtime' / 'service.token'
    if token.exists():
        state = auth.acl_state(token)
        sddl = state.get('sddl')
        report.check('Токен управления доступен только владельцу',
                     bool(sddl) and 'OW' in str(sddl), str(sddl))
    else:
        report.check('Токен управления', True,
                     'файла нет — сервис создаст его при следующем запуске')

    # 6. В базе не осталось путей на прежнюю папку проекта.
    if target_db.exists():
        conn = open_database(target_db)
        try:
            rows = conn.execute('SELECT path FROM passage_photo_paths').fetchall()
        finally:
            conn.close()
        legacy = 0
        foreign = 0
        for (stored,) in rows:
            text = str(stored or '')
            if not text:
                continue
            candidate = Path(text)
            if not candidate.is_absolute():
                continue
            try:
                candidate.relative_to(BASE_DIR)
                legacy += 1
            except ValueError:
                foreign += 1
        report.check('В базе нет путей на прежние папки проекта', legacy == 0,
                     f'записей с путём внутрь проекта: {legacy}')
        if foreign:
            report.step(f'Внимание: {foreign} записей указывают на файлы вне проекта — '
                        'они оставлены как есть')


# ----------------------------------------------------------------------
# Откат
# ----------------------------------------------------------------------

def rollback(report: Report, backup_dir: Path) -> int:
    """Вернуть файлы на прежние места из резервной копии."""
    manifest_path = backup_dir / 'manifest.json'
    if not manifest_path.exists():
        raise MigrationError(f'Опись переноса не найдена: {manifest_path}')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))

    report.step('Возврат файлов из резервной копии ' + str(backup_dir))
    restored: list[str] = []
    for entry in manifest.get('entries', []):
        source = backup_dir / entry['backup']
        target = BASE_DIR / entry['source']
        if not source.exists():
            print(f'  ! нет в копии: {entry["source"]}')
            continue
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry['kind'] == 'dir':
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
        restored.append(entry['source'])
        print(f'  {entry["source"]} ← {source.relative_to(backup_dir)}')

    env_backup = backup_dir / 'files' / '.env'
    previous = manifest.get('env_previous') or {}
    if previous:
        restore_env_values(BASE_DIR / '.env', previous)
        print('  .env ← прежние значения путей: '
              + ', '.join(f'{key}={value}' for key, value in previous.items()))
    elif env_backup.exists():
        shutil.copy2(env_backup, BASE_DIR / '.env')
        print('  .env ← files/.env')

    # Перенесённые копии в data/ убираем — рабочие данные снова в корне.
    removed = []
    for entry in manifest.get('entries', []):
        relative_target = entry.get('target')
        if not relative_target:
            continue
        target = BASE_DIR / relative_target
        for suffix in ('', '-wal', '-shm'):
            item = Path(str(target) + suffix)
            if not item.exists():
                continue
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
            removed.append(str(item.relative_to(BASE_DIR)))

    report.check('Файлы возвращены на прежние места', True,
                 f'восстановлено: {len(restored)}')
    report.check('Копии в data/ удалены', True,
                 ', '.join(removed) if removed else 'нечего удалять')

    db = BASE_DIR / 'anpr.sqlite3'
    if db.exists():
        integrity = integrity_check(db)
        report.check('Целостность восстановленной базы', integrity == 'ok', integrity)
        report.check('Счётчики восстановленной базы', True,
                     json.dumps(table_counts(db), ensure_ascii=False))
    return 0 if not report.failed else 1


# ----------------------------------------------------------------------
# Точка входа
# ----------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='Перенос рабочих данных в каталог data/')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--check', action='store_true',
                       help='показать, что переносится, ничего не меняя')
    group.add_argument('--migrate', action='store_true',
                       help='выполнить перенос с резервной копией и проверками')
    group.add_argument('--rollback', type=Path,
                       help='вернуть файлы из резервной копии на прежние места')
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    report = Report()
    try:
        if args.check:
            plan = build_plan()
            sources = existing_sources(plan)
            if not sources:
                print('Переносить нечего: в корне проекта нет рабочих данных.')
                return 0
            describe_plan(sources)
            print(f'\nВсего объектов: {len(sources)}. Для переноса: --migrate')
            return 0
        if args.migrate:
            return migrate(report)
        return rollback(report, args.rollback)
    except MigrationError as error:
        print(f'Ошибка: {error}', file=sys.stderr)
        return 2
    except Exception as error:  # pragma: no cover — неожиданная ошибка
        print(f'Непредвиденная ошибка: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
