#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Выбор тестов по изменённым файлам — для быстрого локального прогона.

Хук `.githooks/pre-commit` спрашивает здесь, что запускать:

* `FULL` — нужен полный набор (изоляция или непонятная область правки);
* `DOCS` — менялась только документация: проверка ссылок и быстрый smoke;
* иначе — список тестовых модулей затронутой области.

Полный набор всегда гоняют CI и релиз — этот скрипт только убирает
трёхминутный прогон перед каждым коммитом.

Запуск:
    python scripts/test_select.py --staged            # по файлам в индексе git
    python scripts/test_select.py app/ui/header.py     # по своим файлам
    python scripts/test_select.py --staged --json

Код возврата: 0 — план собран (в stdout), 1 — не удалось определить файлы.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _bootstrap import PROJECT_ROOT  # noqa: E402  — корень проекта — из app.core.paths

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

#: Тесты по областям кода. Пути к файлам — pytest принимает их как есть,
#: unittest требует имён модулей (хук переводит пути в модули сам).
GROUPS = {
    'core': (
        'tests/test_stage3_quality.py', 'tests/test_stage4_polish.py', 'tests/test_stage2_resilience.py',
        'tests/test_delivery_queue.py', 'tests/test_outbox_reliability.py', 'tests/test_storage_and_outbox.py',
        'tests/test_live_duplicates.py', 'tests/test_directions.py', 'tests/test_archive_time.py',
        'tests/test_manual_snapshots.py', 'tests/test_event_images.py', 'tests/test_passage_photos.py',
        'tests/test_stream_latency.py', 'tests/test_system_events.py', 'tests/test_camera_events.py',
    ),
    'storage': (
        'tests/test_passage_storage.py', 'tests/test_passages.py', 'tests/test_storage_and_outbox.py',
        'tests/test_outbox_reliability.py', 'tests/test_delivery_queue.py', 'tests/test_ai_and_photo_archive.py',
        'tests/test_passage_photos.py', 'tests/test_event_images.py',
    ),
    'services': (
        'tests/test_control_auth.py', 'tests/test_audit_log.py', 'tests/test_service_instance.py',
        'tests/test_stage2_resilience.py',
    ),
    'ui': (
        'tests/test_shell.py', 'tests/test_panel_status.py', 'tests/test_stage2_ui.py',
        'tests/test_service_panel.py', 'tests/test_context_panel.py', 'tests/test_dashboard.py',
        'tests/test_setup_anpr.py',
    ),
    'scripts': ('tests/test_acceptance_tools.py', 'tests/test_verify_state.py'),
    'paths': ('tests/test_paths.py',),
    'preferences': ('tests/test_app_preferences.py',),
}

#: Изменение изоляции или самого набора тестов затрагивает всё.
FULL_MARKERS = (
    'app/core/safe_mode.py',
    'tests/conftest.py',
    'tests/__init__.py',
    'tests/anpr_testenv.py',
    'conftest.py',
)

#: Файлы, не требующие прогона тестов: документация и конфигурация CI.
DOC_SUFFIXES = ('.md', '.txt', '.rst')
DOC_NAMES = ('.gitignore', '.gitattributes', 'LICENSE')

#: Зависимости и версии — только полный набор.
FULL_NAMES = ('pyproject.toml', 'requirements.txt', 'requirements-lock.txt')


def staged_files() -> list[str]:
    result = subprocess.run(['git', '-C', str(PROJECT_ROOT), 'diff', '--cached', '--name-only'],
                            capture_output=True, text=True, encoding='utf-8')
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def collect(paths: list[str]) -> list[str]:
    """Разложить изменённые файлы по областям кода."""
    modules: set[str] = set()
    docs_only = True
    for raw in paths:
        name = raw.replace('\\', '/')
        if name in FULL_MARKERS:
            return ['FULL']
        if name in FULL_NAMES:
            return ['FULL']
        if name in DOC_NAMES or name.endswith(DOC_SUFFIXES):
            continue
        if name.startswith('.github/') and name.endswith(('.yml', '.yaml')):
            continue  # конфигурация CI — код приложения не меняется
        docs_only = False
        if name.startswith('tests/') and name.endswith('.py'):
            modules.add(name)
            continue
        if name.startswith('app/core/anpr_telegram.py'):
            modules.update(GROUPS['core'])
        elif name.startswith('app/core/paths.py'):
            modules.update(GROUPS['paths'])
            modules.update(GROUPS['storage'])
        elif name.startswith('app/core/preferences.py'):
            modules.update(GROUPS['preferences'])
            modules.update(GROUPS['ui'])
        elif name.startswith('app/core/window_state.py'):
            modules.update(GROUPS['ui'])
        elif name.startswith('app/core/system_events.py'):
            modules.update(GROUPS['core'])
        elif name.startswith('app/core/'):
            modules.update(GROUPS['core'])
        elif name.startswith('app/storage/'):
            modules.update(GROUPS['storage'])
        elif name.startswith('app/services/'):
            modules.update(GROUPS['services'])
        elif name.startswith('app/ui/'):
            modules.update(GROUPS['ui'])
        elif name.startswith('scripts/'):
            modules.update(GROUPS['scripts'])
        elif name.endswith('.py'):
            # Корневые точки входа и прочее — влияет на всё понятное.
            modules.update(GROUPS['services'])
            modules.update(GROUPS['ui'])
            modules.update(GROUPS['core'])
        else:
            return ['FULL']
    if not modules:
        return ['DOCS'] if docs_only else ['FULL']
    return sorted(modules)


def main() -> int:
    parser = argparse.ArgumentParser(description='Выбор тестов по изменённым файлам')
    parser.add_argument('--staged', action='store_true', help='взять файлы из индекса git')
    parser.add_argument('--json', action='store_true', help='вывести план в JSON')
    parser.add_argument('paths', nargs='*', help='свои файлы вместо индекса git')
    args = parser.parse_args()

    if args.staged:
        paths = staged_files()
        if not paths:
            if args.json:
                print(json.dumps({'plan': 'FULL', 'reason': 'нет файлов в индексе'}, ensure_ascii=False))
            else:
                print('FULL')
            return 0
    else:
        paths = args.paths
        if not paths:
            parser.error('нужны --staged или список файлов')

    plan = collect(paths)
    if args.json:
        print(json.dumps({'plan': plan, 'files': paths}, ensure_ascii=False))
    else:
        print(' '.join(plan))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
