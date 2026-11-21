#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка установки ANPR OS Fayz на целевом ПК.

Работает до первого запуска сервиса: база и .env могут отсутствовать —
это не ошибка установки, а этап настройки через settings.bat.

    python scripts/verify_install.py

Код возврата: 0 — окружение готово, 1 — что-то не так.
"""
from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _bootstrap import PROJECT_ROOT  # noqa: E402

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:  # pragma: no cover
    pass

MODULES = (
    'requests', 'certifi', 'urllib3', 'PIL', 'tzdata', 'fastapi',
    'starlette', 'pydantic', 'anyio', 'uvicorn', 'click', 'h11',
    'customtkinter', 'darkdetect', 'pystray', 'cv2', 'numpy', 'tkinter',
)


def main() -> int:
    ok = True

    if sys.version_info[:2] != (3, 14):
        print(f'[install-check] Python {sys.version_info.major}.{sys.version_info.minor} '
              '— ожидался 3.14', file=sys.stderr)
        ok = False
    else:
        print(f'[install-check] Python {sys.version.split()[0]} — OK')

    failed = []
    for name in MODULES:
        try:
            importlib.import_module(name)
        except Exception as error:
            failed.append(f'{name}: {error}')
    if failed:
        ok = False
        print('[install-check] не импортируются:', file=sys.stderr)
        for item in failed:
            print(f'  {item}', file=sys.stderr)
    else:
        print(f'[install-check] импортируются все {len(MODULES)} модулей')

    try:
        result = subprocess.run(
            [sys.executable, '-m', 'pip', 'check'],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60,
        )
        if result.returncode:
            ok = False
            print(f'[install-check] pip check: {result.stdout.strip()}', file=sys.stderr)
        else:
            print('[install-check] pip check — OK')
    except Exception as error:
        print(f'[install-check] pip check недоступен: {error}', file=sys.stderr)

    env = PROJECT_ROOT / '.env'
    if env.exists():
        print('[install-check] .env найден')
    else:
        print('[install-check] .env нет — заполните его через settings.bat')

    data = PROJECT_ROOT / 'data'
    try:
        data.mkdir(exist_ok=True)
        print('[install-check] каталог data доступен')
    except OSError as error:
        ok = False
        print(f'[install-check] каталог data недоступен: {error}', file=sys.stderr)

    for script in ('run.bat', 'settings.bat', 'app.py', 'control_panel.py'):
        if not (PROJECT_ROOT / script).exists():
            ok = False
            print(f'[install-check] нет файла {script}', file=sys.stderr)

    print('[install-check] ГОТОВО К ЗАПУСКУ' if ok else '[install-check] ЕСТЬ ПРОБЛЕМЫ')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
