#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка документации: ссылки на файлы и обязательные разделы.

Лёгкая проверка для правок, которые не трогают код: относительные ссылки
в markdown должны вести в существующие файлы, упомянутые тесты и модули —
существовать, а ключевые документы — содержать нужные разделы.

Запуск:
    python scripts/check_docs.py           # человекочитаемый отчёт
    python scripts/check_docs.py --json    # отчёт в JSON

Код возврата: 0 — проблем нет, 1 — найдена проблема.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _bootstrap import PROJECT_ROOT  # noqa: E402  — корень проекта — из app.core.paths

#: Консоль Windows может быть в cp1251/cp866 — печатаем отчёт в UTF-8.
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

ROOT = PROJECT_ROOT

#: Документы, которые проверяем поимённо (остальные markdown — только ссылки).
DOCS = (
    'AGENTS.md',
    'README.md',
    'PROJECT_CONTEXT.md',
    'docs/CODEMAP.md',
    'docs/MAINTENANCE.md',
    'docs/ACCEPTANCE.md',
    'docs/TESTING.md',
)

#: Обязательные разделы ключевых документов: документ -> (раздел, подстрока).
REQUIRED = {
    'AGENTS.md': (
        '## 0. Карта документации',
        '## 1. Перед правкой',
        '## 2. Наименьшее достаточное изменение',
        '## 3. Хирургические изменения',
        '## 4. Проверка',
        '## 5. Что нельзя ослаблять',
        '## 6. Документация',
        '## 7. Зависимости, плагины, хуки',
    ),
    'docs/CODEMAP.md': (
        '## 2. Точки входа',
        '## 4. Карта модулей',
        '## 5. Данные',
        '## 7. Карта тестов',
    ),
    'docs/TESTING.md': (
        '## 2. Классификация тестов',
        '## 4. Дублирование проверок',
    ),
}

#: Упоминания файлов в тексте: `tests/test_x.py`, `app/core/paths.py`.
FILE_REF = re.compile(r'`((?:tests|app|scripts|data|docs)/[\w./-]+\.(?:py|md|json|toml|txt))`')
LINK = re.compile(r'\[[^\]]+\]\(([^)\s]+)\)')

#: Каталоги, из которых markdown не проверяем (набор иконок, шаблоны).
SKIP_DIRS = {'filled', 'assets', '.git', 'backup', '__pycache__', 'packages'}


def markdown_files() -> list[Path]:
    """Документы проекта: отслеживаемые git плюс новые неигнорируемые файлы.

    Локальные заметки вне Git (например, личный SKILL.md) не проверяются:
    они не входят в поставку проекта.
    """
    tracked: set[Path] = set()
    try:
        for args in (['ls-files', '*.md'], ['ls-files', '--others', '--exclude-standard', '*.md']):
            result = subprocess.run(['git', '-C', str(ROOT), *args],
                                    capture_output=True, text=True, encoding='utf-8')
            if result.returncode == 0:
                tracked |= {ROOT / line for line in result.stdout.splitlines() if line}
    except OSError:
        tracked = set()
    if tracked:
        return sorted(path for path in tracked if path.exists())
    files = []
    for path in ROOT.rglob('*.md'):
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        files.append(path)
    return sorted(files)


def check_links() -> list[str]:
    problems = []
    for path in markdown_files():
        text = path.read_text(encoding='utf-8')
        for target in LINK.findall(text):
            if target.startswith(('http://', 'https://', '#', 'mailto:')):
                continue
            clean = target.split('#')[0]
            if not clean:
                continue
            if not (path.parent / clean).resolve().exists():
                problems.append(f"{path.relative_to(ROOT)}: битая ссылка -> {target}")
    return problems


def check_file_refs() -> list[str]:
    problems = []
    for path in markdown_files():
        text = path.read_text(encoding='utf-8')
        for ref in FILE_REF.findall(text):
            if not (ROOT / ref).exists():
                problems.append(f"{path.relative_to(ROOT)}: упомянут несуществующий файл -> {ref}")
    return problems


def check_sections() -> list[str]:
    problems = []
    for name, sections in REQUIRED.items():
        path = ROOT / name
        if not path.exists():
            problems.append(f"{name}: файл отсутствует")
            continue
        text = path.read_text(encoding='utf-8')
        for section in sections:
            if section not in text:
                problems.append(f"{name}: нет обязательного раздела -> {section}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description='Проверка документации проекта')
    parser.add_argument('--json', action='store_true', help='отчёт в JSON')
    args = parser.parse_args()

    problems = check_links() + check_file_refs() + check_sections()

    if args.json:
        print(json.dumps({'ok': not problems, 'problems': problems}, ensure_ascii=False, indent=2))
        return 1 if problems else 0

    if problems:
        print(f'Проблем в документации: {len(problems)}')
        for item in problems:
            print(f'  - {item}')
        return 1
    print('Документация в порядке: ссылки, упомянутые файлы и разделы на месте.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
