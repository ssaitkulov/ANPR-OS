#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Скачать все колёса из requirements-lock.txt в deploy/wheelhouse/.

Запускать на машине С ИНТЕРНЕТОМ, один раз перед копированием проекта
на рабочий ПК. Целевая машина интернета не видит и ставится из этой папки.

    python scripts/make_wheelhouse.py

Папка deploy/wheelhouse/ намеренно не попадает в Git (крупные бинарники):
её нужно скопировать вместе с проектом.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _bootstrap import PROJECT_ROOT  # noqa: E402

LOCKFILE = PROJECT_ROOT / 'requirements-lock.txt'
WHEELS = PROJECT_ROOT / 'deploy' / 'wheelhouse'


def pinned_packages() -> dict[str, str]:
    """Имя пакета -> версия из lock-файла."""
    pins = {}
    for line in LOCKFILE.read_text(encoding='utf-8').splitlines():
        line = line.split('#')[0].strip()
        if '==' not in line:
            continue
        name, version = line.split('==', 1)
        pins[name.strip().lower()] = version.strip()
    return pins


def wheel_name(name: str) -> str:
    return re.sub(r'[-_.]+', '_', name).lower()


def main() -> int:
    pins = pinned_packages()
    if not pins:
        print('[wheelhouse] lock-файл пуст', file=sys.stderr)
        return 1

    WHEELS.mkdir(parents=True, exist_ok=True)
    print(f'[wheelhouse] качаю {len(pins)} пакетов в {WHEELS}')

    result = subprocess.run(
        [sys.executable, '-m', 'pip', 'download',
         '--only-binary=:all:', '--dest', str(WHEELS), '-r', str(LOCKFILE)],
        cwd=PROJECT_ROOT,
    )
    if result.returncode:
        print('[wheelhouse] pip download не удался', file=sys.stderr)
        return 1

    # Нельзя '--no-deps': у click на Windows есть colorama по маркеру платформы.
    # Проверяем, что ВСЕ зависимости (включая маркерные) разрешаются из колёс.
    probe = subprocess.run(
        [sys.executable, '-m', 'pip', 'install', '--dry-run', '--ignore-installed',
         '--no-index', '--find-links', str(WHEELS), '-r', str(LOCKFILE)],
        cwd=PROJECT_ROOT,
    )
    if probe.returncode:
        print('[wheelhouse] не все зависимости разрешаются из колёс', file=sys.stderr)
        return 1

    files = [p.name.lower() for p in WHEELS.glob('*.whl')]
    missing = [f'{name}=={version}' for name, version in pins.items()
               if not any(f.startswith(wheel_name(name) + '-') for f in files)]
    if missing:
        print('[wheelhouse] нет колёс для:', ', '.join(missing), file=sys.stderr)
        return 1

    print(f'[wheelhouse] готово: {len(files)} колёс, все пакеты из lock-файла есть')
    return 0


if __name__ == '__main__':
    sys.exit(main())
