#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Приёмка перед боевым режимом: автоматическая часть чек-листа.

Проверяет то, что можно проверить по данным сервиса:

* JSON-зеркало и SQLite не расходятся (0 расхождений);
* в очереди доставки нет «мёртвых» и зависших сообщений;
* дневной отчёт за проверенный день ушёл;
* фотоархив согласован с базой: у живых проездов снимок есть на диске;
* все получатели (основной чат + подписчики) получили сообщения;
* локальный сервис отвечает и его рабочий поток жив.

Запуск:
    python scripts/acceptance_check.py                 # сегодня
    python scripts/acceptance_check.py --day 2026-11-18
    python scripts/acceptance_check.py --json          # отчёт в JSON

Код возврата: 0 — замечаний нет (FAIL нет), 1 — есть блокирующие замечания.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _bootstrap import PROJECT_ROOT  # noqa: E402  — корень проекта — из app.core.paths
from app.core import paths as app_paths  # noqa: E402  — единый источник путей

#: Рабочие данные — в `data/` (см. `app/core/paths.py`).
DEFAULT_STATE = app_paths.state_file()
DEFAULT_DB = app_paths.db_file()
SERVICE_URL = 'http://127.0.0.1:18749'

try:  # консоль Windows по умолчанию не печатает кириллицу
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:  # pragma: no cover
    pass

OK = 'OK'
WARN = 'WARN'
FAIL = 'FAIL'


class Report:
    """Список проверок: у каждой — статус и пояснение."""

    def __init__(self):
        self.checks = []

    def add(self, name: str, status: str, detail: str = '') -> None:
        self.checks.append({'name': name, 'status': status, 'detail': detail})

    @property
    def failed(self) -> int:
        return sum(1 for check in self.checks if check['status'] == FAIL)

    @property
    def warned(self) -> int:
        return sum(1 for check in self.checks if check['status'] == WARN)


def load_env(path: Path) -> dict:
    """Разбор .env — те же значения, что видит сервис."""
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def open_readonly(path: Path) -> sqlite3.Connection:
    """База только для чтения: проверка не должна ничего менять."""
    uri = path.resolve().as_uri() + '?mode=ro'
    try:
        return sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error:
        return sqlite3.connect(str(path), timeout=5)


def check_sources(report: Report, state_path: Path, db_path: Path) -> None:
    """JSON-зеркало ↔ SQLite: расхождений быть не должно."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        from verify_state import verify
    except Exception as error:  # pragma: no cover
        report.add('Сверка JSON ↔ SQLite', FAIL, f'не удалось запустить сверку: {error}')
        return
    try:
        result = verify(state_path, db_path)
    except (FileNotFoundError, ValueError) as error:
        report.add('Сверка JSON ↔ SQLite', FAIL, str(error))
        return
    issues = result.get('issues') or []
    if issues:
        first = issues[0]
        report.add('Сверка JSON ↔ SQLite', FAIL,
                   f"расхождений: {len(issues)} (например, {first.get('kind')} "
                   f"{first.get('day')} {first.get('plate')})")
    else:
        report.add('Сверка JSON ↔ SQLite', OK,
                   f"расхождений нет; проездов в JSON: {result.get('json_records')}, "
                   f"в SQLite: {result.get('db_total')}")


def check_outbox(report: Report, conn: sqlite3.Connection, stale_minutes: int) -> None:
    """Очередь доставки: без dead и без зависших сообщений."""
    counts = dict(conn.execute('SELECT status, COUNT(*) FROM outbox GROUP BY status').fetchall())
    dead = int(counts.get('dead', 0))
    pending = int(counts.get('pending', 0))
    if dead:
        rows = conn.execute('SELECT id, chat_id, kind, attempts, last_error FROM outbox '
                            'WHERE status=? ORDER BY id DESC LIMIT 5', ('dead',)).fetchall()
        detail = '; '.join(f'#{row[0]} чат {row[1]} ({row[2]}), попыток {row[3]}: {str(row[4])[:80]}'
                            for row in rows)
        report.add('Очередь доставки: нет dead', FAIL, f'недоставленных: {dead} — {detail}')
    else:
        report.add('Очередь доставки: нет dead', OK, 'недоставленных сообщений нет')

    stale = conn.execute(
        "SELECT COUNT(*) FROM outbox WHERE status='pending' AND created_at < ?",
        ((datetime.now() - timedelta(minutes=stale_minutes)).strftime('%Y-%m-%d %H:%M:%S'),)
    ).fetchone()[0]
    if stale:
        report.add('Очередь доставки: нет зависших', WARN,
                   f'в очереди {stale} сообщений старше {stale_minutes} мин')
    else:
        report.add('Очередь доставки: нет зависших', OK,
                   f'в очереди {pending} сообщений, все свежие')


def check_report(report: Report, conn: sqlite3.Connection, state: dict, day: str,
                 now: datetime, report_time: str, env: dict) -> None:
    """Дневной отчёт за день должен быть отправлен (когда уже пора)."""
    passages = conn.execute('SELECT COUNT(*) FROM events WHERE day=?', (day,)).fetchone()[0]
    reported = set(state.get('reported_days') or [])
    sent = conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='report' AND status='sent'").fetchone()[0]
    if not passages:
        report.add(f'Отчёт за {day}', WARN, 'за день нет проездов — отчёт не требовался')
        return
    if day in reported:
        report.add(f'Отчёт за {day}', OK,
                   f'отчёт отмечен в состоянии (отправленных отчётов в очереди: {sent})')
        return
    # Отчёт уходит в REPORT_TIME; до этого момента его отсутствие — не замечание.
    try:
        hour, minute = (int(part) for part in (report_time or '20:00').split(':')[:2])
    except (TypeError, ValueError):
        hour, minute = 20, 0
    due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    window = int(env.get('REPORT_WINDOW_MINUTES') or 240)
    if day == now.strftime('%Y-%m-%d'):
        if now < due:
            report.add(f'Отчёт за {day}', WARN,
                       f'время отчёта ещё не наступило ({report_time}), отчёт не требовался')
            return
        if (now - due) <= timedelta(minutes=10):
            report.add(f'Отчёт за {day}', WARN,
                       f'отчёт должен уйти в течение минуты после {report_time} — проверьте поток отчёта')
            return
    report.add(f'Отчёт за {day}', FAIL,
               f'отчёт за день не отправлен (проездов: {passages}, '
               f'отправленных отчётов в очереди: {sent})')


def check_photo_archive(report: Report, conn: sqlite3.Connection, day: str, image_dir: Path) -> None:
    """Фотоархив: у живых проездов снимок должен лежать на диске."""
    rows = conn.execute('''SELECT e.id, e.captured_at, e.plate, e.backfill, p.path
                           FROM events e
                           LEFT JOIN passage_photo_paths p
                             ON p.captured_at=e.captured_at AND p.plate=e.plate
                           WHERE e.day=?''', (day,)).fetchall()
    live_total = live_with_photo = 0
    live_missing = []
    archive_total = archive_with_photo = 0
    for _id, _captured, _plate, backfill, path in rows:
        if backfill:
            archive_total += 1
            if path:
                archive_with_photo += 1
            continue
        live_total += 1
        photo = app_paths.resolve_photo_path(path, image_dir) if path else None
        if photo and photo.exists():
            live_with_photo += 1
        else:
            live_missing.append(f'{_captured} {_plate}')
    if live_total == 0:
        report.add(f'Фотоархив за {day}', WARN, 'живых проездов за день нет')
    elif live_missing:
        report.add(f'Фотоархив за {day}', FAIL,
                   f'без снимка или с исчезнувшим файлом: {len(live_missing)} из {live_total} '
                   f'(например, {", ".join(live_missing[:3])})')
    else:
        report.add(f'Фотоархив за {day}', OK,
                   f'живых проездов: {live_total}, у всех есть фото; '
                   f'проездов из архива: {archive_total}, из них с фото: {archive_with_photo} '
                   '(камера снимки из архива не отдаёт)')


def expected_recipients(env: dict) -> list:
    """Получатели рассылки: основной чат + подписчики, прошедшие список разрешённых."""
    chat_ids = []
    if env.get('TELEGRAM_CHAT_ID'):
        chat_ids.append(str(env['TELEGRAM_CHAT_ID']))
    subscribers_file = Path(env.get('SUBSCRIBERS_FILE') or app_paths.subscribers_file())
    if not subscribers_file.is_absolute():
        subscribers_file = PROJECT_ROOT / subscribers_file
    subscribers = []
    if subscribers_file.exists():
        try:
            data = json.loads(subscribers_file.read_text(encoding='utf-8'))
            subscribers = data if isinstance(data, list) else (data.get('subscribers') or [])
        except (OSError, ValueError):
            subscribers = []
    for item in subscribers:
        if isinstance(item, dict):
            item = item.get('chat_id') or item.get('id')
        if item:
            chat_ids.append(str(item))
    allowed = {x.strip() for x in (env.get('ALLOWED_CHAT_IDS') or '').split(',') if x.strip()}
    return sorted({cid for cid in chat_ids if not allowed or cid in allowed})


def check_recipients(report: Report, conn: sqlite3.Connection, env: dict, health: dict | None) -> None:
    """Все получатели должны получить сообщения."""
    expected = expected_recipients(env)
    queued = {row[0]: row[1] for row in conn.execute(
        "SELECT chat_id, COUNT(*) FROM outbox WHERE status IN ('pending','dead') GROUP BY chat_id")}

    delivery = (health or {}).get('telegram_delivery') or {}
    delivered = int(delivery.get('delivered') or 0)
    failed = int(delivery.get('failed') or 0)
    detail = f'получателей: {len(expected)} (основной чат + подписчики)'
    if health and 'telegram_delivery' not in health:
        report.add('Все получатели получили сообщения', WARN,
                   f'{detail}; сервис не отдаёт счётчики доставки — перезапустите его, '
                   'чтобы он работал с текущим кодом, и повторите проверку')
    elif health:
        detail += (f'; последняя рассылка: доставлено {delivered}, не доставлено {failed}'
                   f' (время {delivery.get("at", "—")})')
        if failed:
            report.add('Все получатели получили сообщения', WARN,
                       f'{detail}; не доставлено чатов: {failed} — их текст ждёт в очереди')
        elif delivered and delivered < len(expected):
            report.add('Все получатели получили сообщения', WARN,
                       f'{detail}; последняя рассылка покрыла {delivered} из {len(expected)}')
        elif delivered:
            report.add('Все получатели получили сообщения', OK, detail)
        else:
            report.add('Все получатели получили сообщения', WARN,
                       f'{detail}; последней рассылки ещё не было')
    else:
        delivered_chats = {row[0] for row in conn.execute(
            "SELECT DISTINCT chat_id FROM outbox WHERE status IN ('sent','partial')")}
        undelivered = sorted(cid for cid in expected if cid not in delivered_chats)
        detail += f'; с доставленными сообщениями в очереди: {len(expected) - len(undelivered)}'
        if undelivered:
            report.add('Все получатели получили сообщения', WARN,
                       f'{detail}; без доставки: {", ".join(undelivered)}')
        else:
            report.add('Все получатели получили сообщения', OK, detail)
    if queued:
        report.add('Сообщения в очереди по чатам', WARN,
                   '; '.join(f'{cid}: {count}' for cid, count in sorted(queued.items())))


def check_service(report: Report) -> dict | None:
    """Локальный сервис должен отвечать, а его рабочий поток — быть жив."""
    try:
        with urllib.request.urlopen(f'{SERVICE_URL}/health', timeout=5) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except (urllib.error.URLError, OSError, ValueError) as error:
        report.add('Локальный сервис', WARN,
                   f'сервис на {SERVICE_URL} не отвечает ({error}) — проверьте, запущен ли он')
        return None
    status = payload.get('status')
    if status == 'ok':
        report.add('Локальный сервис', OK,
                   f"статус: {status}, камера: {payload.get('camera')}, "
                   f"telegram: {payload.get('telegram_message', '')}")
    else:
        report.add('Локальный сервис', FAIL, f'сервис отвечает, но рабочий поток: {status}')
    return payload


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Автоматическая часть приёмочного чек-листа')
    parser.add_argument('--day', default='', help='день проверки (ГГГГ-ММ-ДД), по умолчанию сегодня')
    parser.add_argument('--state', type=Path, default=DEFAULT_STATE)
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    parser.add_argument('--env', type=Path, default=PROJECT_ROOT / '.env')
    parser.add_argument('--stale-minutes', type=int, default=30,
                        help='через сколько минут сообщение в очереди считается зависшим')
    parser.add_argument('--skip-service', action='store_true',
                        help='не проверять локальный сервис (он может быть не запущен)')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)

    env = load_env(args.env)
    timezone = env.get('TIMEZONE') or 'Asia/Tashkent'
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        tz = ZoneInfo('Asia/Tashkent')
    day = args.day or datetime.now(tz).strftime('%Y-%m-%d')
    state = load_state(args.state)

    report = Report()
    if not args.db.exists():
        report.add('База данных', FAIL, f'файл не найден: {args.db}')
        _print(report, args.json)
        return 1

    health = None
    if not args.skip_service:
        health = check_service(report)

    conn = open_readonly(args.db)
    try:
        check_sources(report, args.state, args.db)
        check_outbox(report, conn, args.stale_minutes)
        check_report(report, conn, state, day, datetime.now(tz),
                     env.get('REPORT_TIME') or '20:00', env)
        image_dir = Path(env.get('IMAGE_DIR') or app_paths.photo_dir())
        if not image_dir.is_absolute():
            image_dir = PROJECT_ROOT / image_dir
        check_photo_archive(report, conn, day, image_dir)
        check_recipients(report, conn, env, health)
    finally:
        conn.close()

    return _print(report, args.json)


def _print(report: Report, as_json: bool) -> int:
    if as_json:
        print(json.dumps({'checks': report.checks, 'failed': report.failed,
                          'warned': report.warned, 'ok': report.failed == 0},
                         ensure_ascii=False, indent=1))
        return 0 if report.failed == 0 else 1
    for check in report.checks:
        print(f"[{check['status']:4}] {check['name']}"
              + (f" — {check['detail']}" if check['detail'] else ''))
    print(f"[итог] блокирующих замечаний: {report.failed}, предупреждений: {report.warned}")
    return 0 if report.failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
