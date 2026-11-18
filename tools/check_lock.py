#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка согласованности файлов зависимостей.

`requirements.txt` — прямые зависимости с зафиксированными версиями,
`requirements-lock.txt` — полный набор с транзитивными. Версии прямых
зависимостей в этих файлах должны совпадать: иначе сборка «поехала»
(одна машина ставит один набор, другая — другой).

Запуск:
    python tools/check_lock.py            # человекочитаемый отчёт
    python tools/check_lock.py --json     # отчёт в JSON

Код возврата: 0 — расхождений нет, 1 — расхождение найдено.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
REQUIREMENTS = BASE_DIR / 'requirements.txt'
LOCKFILE = BASE_DIR / 'requirements-lock.txt'
PYPROJECT = BASE_DIR / 'pyproject.toml'

try:  # консоль Windows по умолчанию не печатает кириллицу
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:  # pragma: no cover
    pass


def parse_pins(path: Path) -> dict:
    """Версии из файла зависимостей: {'requests': '2.32.5', ...}."""
    pins = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.split('#')[0].strip()
        if not line or '==' not in line:
            continue
        name, version = line.split('==', 1)
        pins[name.strip().lower()] = version.strip()
    return pins


def pyproject_pins(path: Path) -> dict:
    """Версии из секции dependencies файла pyproject.toml."""
    pins = {}
    in_section = False
    for line in path.read_text(encoding='utf-8').splitlines():
        stripped = line.strip()
        if stripped.startswith('dependencies'):
            in_section = True
            continue
        if in_section:
            if stripped.startswith(']'):
                break
            if stripped.startswith('"'):
                name, _, version = stripped.strip('",').partition('==')
                pins[name.strip().lower()] = version.strip()
    return pins


def check() -> dict:
    direct = parse_pins(REQUIREMENTS)
    locked = parse_pins(LOCKFILE)
    project = pyproject_pins(PYPROJECT)

    missing_in_lock = sorted(name for name in direct if name not in locked)
    different = sorted(name for name, version in direct.items()
                       if name in locked and locked[name] != version)
    missing_in_project = sorted(name for name in direct if name not in project)
    different_project = sorted(name for name, version in direct.items()
                                if name in project and project[name] != version)
    extra_in_lock = sorted(name for name in locked if name not in direct)

    problems = {
        'missing_in_lock': missing_in_lock,
        'different_in_lock': different,
        'missing_in_pyproject': missing_in_project,
        'different_in_pyproject': different_project,
    }
    ok = not any(problems.values())
    return {
        'requirements': direct,
        'lock': locked,
        'pyproject': project,
        'transitive_only_in_lock': extra_in_lock,
        **problems,
        'ok': ok,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Проверка согласованности requirements*.txt и pyproject.toml')
    parser.add_argument('--json', action='store_true', help='вывести отчёт в JSON')
    args = parser.parse_args(argv)

    report = check()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0 if report['ok'] else 1

    print(f'[lock] прямых зависимостей: {len(report["requirements"])}')
    print(f'[lock] всего в lock-файле: {len(report["lock"])} '
          f'(транзитивных дополнительно: {len(report["transitive_only_in_lock"])})')
    if report['ok']:
        print('[lock] версии в requirements.txt, requirements-lock.txt и pyproject.toml совпадают')
        return 0
    for label, items in (('нет в lock-файле', report['missing_in_lock']),
                         ('отличается в lock-файле', report['different_in_lock']),
                         ('нет в pyproject.toml', report['missing_in_pyproject']),
                         ('отличается в pyproject.toml', report['different_in_pyproject'])):
        for name in items:
            print(f'[lock] {label}: {name}', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
