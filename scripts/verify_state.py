#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сверка источников: JSON-зеркало состояния ↔ SQLite.

Проезды хранятся в двух местах: в `anpr_state.json` (зеркало для отчётов и
восстановления) и в `anpr.sqlite3` (единственный источник истины). Расхождение
между ними означает либо потерю данных, либо задвоение — оба случая нужно
видеть до эксплуатации.

Запуск:
    python scripts/verify_state.py                    # сверка по умолчанию
    python scripts/verify_state.py --json             # отчёт в JSON
    python scripts/verify_state.py --state X --db Y   # другие файлы

Код возврата: 0 — расхождений нет, 1 — расхождения найдены (или данные не
прочитались).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _bootstrap import PROJECT_ROOT  # noqa: E402  — корень проекта — из app.core.paths

# Консоль Windows по умолчанию печатает в cp866/cp1251 — кириллица в отчёте
# превратилась бы в «кракозябры». Как в acceptance_check.py и check_lock.py.
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from app.storage.database import Store, normalize_plate  # noqa: E402
from app.core import paths as app_paths  # noqa: E402

#: Рабочие данные — в `data/` (см. `app/core/paths.py`).
DEFAULT_STATE = app_paths.state_file()
DEFAULT_DB = app_paths.db_file()

# Поля, которые должны совпадать в обоих источниках.
REPORTED_FIELDS = (
    ('lane', 'lane'),
    ('direction', 'direction'),
    ('speed', 'speed'),
    ('color', 'vehicle_color'),
    ('type', 'plate_type'),
)


def record_key(record: dict) -> tuple:
    """Ключ сопоставления проезда: время + номер.

    JSON-зеркало хранит время в поле `iso`, SQLite — в `captured_at`.
    """
    stamp = record.get('iso') or record.get('captured_at') or ''
    return (stamp, normalize_plate(record.get('plate')))


def load_state(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f'файл состояния не найден: {path}')
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise ValueError(f'не удалось прочитать {path}: {error}') from error


def compare_day(day: str, json_records: list, db_rows: list) -> dict:
    """Сравнить проезды одного дня. Возвращает список расхождений."""
    issues = []
    json_by_key = {}
    for record in json_records:
        json_by_key.setdefault(record_key(record), []).append(record)
    db_by_key = {}
    for row in db_rows:
        db_by_key.setdefault(record_key(row), []).append(row)

    for key, records in json_by_key.items():
        rows = db_by_key.get(key, [])
        if not rows:
            issues.append({'kind': 'missing_in_db', 'day': day, 'iso': key[0], 'plate': key[1],
                           'detail': 'проезд есть в JSON, но нет в SQLite'})
            continue
        record = records[0]
        row = rows[0]
        if bool(record.get('backfill')) != bool(row.get('backfill')):
            issues.append({'kind': 'flag_mismatch', 'day': day, 'iso': key[0], 'plate': key[1],
                           'detail': f"backfill: json={bool(record.get('backfill'))} "
                                     f"db={bool(row.get('backfill'))}"})
        for json_field, db_field in REPORTED_FIELDS:
            left = (record.get(json_field) or '')
            right = (row.get(db_field) or '')
            if str(left) != str(right):
                issues.append({'kind': 'field_mismatch', 'day': day, 'iso': key[0], 'plate': key[1],
                               'detail': f'{json_field}: json={left!r} db={right!r}'})

    for key, rows in db_by_key.items():
        if key not in json_by_key:
            issues.append({'kind': 'missing_in_json', 'day': day, 'iso': key[0], 'plate': key[1],
                           'detail': f'проезд есть в SQLite, но нет в JSON (backfill='
                                     f'{bool(rows[0].get("backfill"))})'})
    return issues


def verify(state_path: Path, db_path: Path) -> dict:
    state = load_state(state_path)
    days = state.get('days') or {}
    store = Store(db_path)
    try:
        issues = []
        json_total = db_total = 0
        for day in sorted(days):
            json_records = days.get(day) or []
            db_rows = store.day_passages(day)
            json_total += len(json_records)
            db_total += len(db_rows)
            issues.extend(compare_day(day, json_records, db_rows))
        conn = store.conn
        backfill = conn.execute('SELECT COUNT(*) FROM events WHERE backfill=1').fetchone()[0]
        live = conn.execute('SELECT COUNT(*) FROM events WHERE backfill=0').fetchone()[0]
        max_id = conn.execute('SELECT MAX(id) FROM events').fetchone()[0]
        total = conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    finally:
        store.close()

    return {
        'state_file': str(state_path),
        'db_file': str(db_path),
        'days': len(days),
        'json_records': json_total,
        'db_rows_in_state_days': db_total,
        'db_total': total,
        'db_live': live,
        'db_backfill': backfill,
        'db_max_id': max_id,
        'issues': issues,
        'ok': not issues,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Сверка JSON-состояния и SQLite')
    parser.add_argument('--state', type=Path, default=DEFAULT_STATE)
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    parser.add_argument('--json', action='store_true', help='вывести отчёт в JSON')
    args = parser.parse_args(argv)

    try:
        report = verify(args.state, args.db)
    except (FileNotFoundError, ValueError) as error:
        print(f'[verify] {error}', file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0 if report['ok'] else 1

    print(f"[verify] состояние: {report['state_file']}")
    print(f"[verify] база:      {report['db_file']}")
    print(f"[verify] дней в JSON: {report['days']}, проездов в JSON: {report['json_records']}, "
          f"проездов в SQLite за эти дни: {report['db_rows_in_state_days']}")
    print(f"[verify] всего строк в SQLite: {report['db_total']} "
          f"(live={report['db_live']}, backfill={report['db_backfill']}, max(id)={report['db_max_id']})")
    if report['ok']:
        print('[verify] расхождений нет')
        return 0
    print(f"[verify] расхождений: {len(report['issues'])}", file=sys.stderr)
    for issue in report['issues'][:50]:
        print(f"  - [{issue['kind']}] {issue['day']} {issue['iso']} {issue['plate']}: {issue['detail']}",
              file=sys.stderr)
    if len(report['issues']) > 50:
        print(f"  ... и ещё {len(report['issues']) - 50}", file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
