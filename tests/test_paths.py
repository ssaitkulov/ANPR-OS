"""Единая система путей и переносимость проекта (Этап 2.3).

Проверяется именно то, о чём шла речь на этапе 2.3:

* все рабочие пути считаются от корня проекта, а не от текущей директории;
* ни один модуль не вычисляет корень проекта самостоятельно;
* фотографии в базе хранятся относительными путями и находятся по ним;
* проект переносится целиком: смена рабочей директории ничего не меняет;
* рабочие данные лежат в `data/`, а не в корне проекта.
"""
from tests import anpr_testenv  # noqa: F401  — изоляция тестов от боевых данных

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core import paths as app_paths
from app.core.paths import PROJECT_ROOT, DATA_DIR


class ProjectLayoutTests(unittest.TestCase):
    """Структура каталогов зафиксирована: `data/` — все рабочие данные."""

    def test_data_directory_is_inside_the_project(self):
        self.assertEqual(DATA_DIR, PROJECT_ROOT / 'data')
        self.assertEqual(app_paths.PHOTO_DIR, DATA_DIR / 'photos')
        self.assertEqual(app_paths.RUNTIME_DIR, DATA_DIR / 'runtime')
        self.assertEqual(app_paths.LOG_DIR, DATA_DIR / 'logs')

    def test_work_files_are_named_as_before_and_live_in_data(self):
        self.assertEqual(app_paths.DB_FILE, DATA_DIR / 'anpr.sqlite3')
        self.assertEqual(app_paths.STATE_FILE, DATA_DIR / 'anpr_state.json')
        self.assertEqual(app_paths.SUBSCRIBERS_FILE, DATA_DIR / 'subscribers.json')
        self.assertEqual(app_paths.UI_PREFERENCES_FILE, DATA_DIR / 'ui_preferences.json')
        self.assertEqual(app_paths.UI_WINDOW_FILE, DATA_DIR / 'ui_window.json')

    def test_project_root_is_not_the_current_directory(self):
        """Корень проекта определяется по файлу модуля, а не по рабочей папке."""
        self.assertTrue((PROJECT_ROOT / 'app' / 'core' / 'paths.py').exists())
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                self.assertEqual(PROJECT_ROOT, original)
                self.assertNotEqual(PROJECT_ROOT, Path.cwd())
            finally:
                os.chdir(original)


class PathResolutionTests(unittest.TestCase):
    """Пути из настроек считаются от корня проекта."""

    def test_relative_value_is_resolved_against_the_project_root(self):
        self.assertEqual(app_paths.resolve_path('data/photos'),
                         PROJECT_ROOT / 'data' / 'photos')
        self.assertEqual(app_paths.resolve_path('', DATA_DIR), DATA_DIR)

    def test_absolute_value_is_kept_as_is(self):
        absolute = Path('C:/example/data/photos')
        self.assertEqual(app_paths.resolve_path(str(absolute)), absolute)

    def test_environment_overrides_win(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(ANPR_RUNTIME_DIR=tmp, ANPR_LOG_DIR=tmp,
                      IMAGE_DIR='data/photos', DB_FILE='data/anpr.sqlite3',
                      STATE_FILE='data/anpr_state.json'):
                self.assertEqual(app_paths.runtime_dir(), Path(tmp))
                self.assertEqual(app_paths.log_dir(), Path(tmp))
                self.assertEqual(app_paths.photo_dir(), PROJECT_ROOT / 'data' / 'photos')
                self.assertEqual(app_paths.db_file(), PROJECT_ROOT / 'data' / 'anpr.sqlite3')
                self.assertEqual(app_paths.state_file(),
                                 PROJECT_ROOT / 'data' / 'anpr_state.json')


class PhotoPathTests(unittest.TestCase):
    """Путь к фотографии переживает перенос проекта."""

    def test_relative_path_is_resolved_against_the_photo_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            photo = root / '2026-11-17' / 'abc.jpg'
            photo.parent.mkdir(parents=True)
            photo.write_bytes(b'jpeg')
            stored = app_paths.relative_photo_path(photo, root)
            self.assertEqual(stored, '2026-11-17/abc.jpg')
            self.assertFalse(Path(stored).is_absolute())
            self.assertEqual(app_paths.resolve_photo_path(stored, root), photo)

    def test_absolute_path_outside_the_photo_folder_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = Path(tmp).parent / 'elsewhere.jpg'
            self.assertEqual(app_paths.relative_photo_path(outside, root), str(outside))

    def test_old_absolute_path_is_found_in_the_new_folder(self):
        """Путь из прежней папки (`.../images/дата/файл`) находится по дате и имени."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            photo = root / '2026-11-17' / 'abc.jpg'
            photo.parent.mkdir(parents=True)
            photo.write_bytes(b'jpeg')
            legacy = r'D:\PROJECT\ANPR OS Fayz\images\2026-11-17\abc.jpg'
            self.assertEqual(app_paths.resolve_photo_path(legacy, root), photo)


class PortabilityTests(unittest.TestCase):
    """Проект переносится целиком: смена рабочего каталога ничего не меняет."""

    def test_paths_do_not_depend_on_the_current_directory(self):
        """Смена рабочего каталога не меняет ни одного пути."""
        original = Path.cwd()
        with _working_environment():
            before = _working_paths()
            with tempfile.TemporaryDirectory() as tmp:
                os.chdir(tmp)
                try:
                    after = _working_paths()
                finally:
                    os.chdir(original)
        self.assertEqual(before, after)
        for path in after:
            self.assertTrue(str(path).startswith(str(PROJECT_ROOT)),
                            f'{path} — вне корня проекта')

    def test_configuration_files_hold_relative_paths_only(self):
        """В `.env` нет абсолютных путей: проект переносится без правки настроек."""
        env = app_paths.ENV_FILE
        if not env.exists():
            self.skipTest('нет файла .env')
        for line in env.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip()
            if key not in ('DB_FILE', 'STATE_FILE', 'SUBSCRIBERS_FILE', 'IMAGE_DIR'):
                continue
            value = value.strip().strip('"').strip("'")
            self.assertFalse(value.startswith('/'), f'{key} — абсолютный путь')
            self.assertNotIn(':', value, f'{key} — путь с указанием диска')
            self.assertFalse(value.startswith(str(PROJECT_ROOT)),
                             f'{key} — вписан абсолютный путь проекта')


class LegacyDataTests(unittest.TestCase):
    """Прежние места рабочих данных: о них нужно сказать, а не промолчать."""

    def test_legacy_files_in_the_project_root_are_reported(self):
        """База в корне — признак «тихой» потери данных: её надо увидеть."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'anpr.sqlite3').write_bytes(b'')
            (root / 'anpr_state.json').write_text('{}', encoding='utf-8')
            with patch.object(app_paths, 'PROJECT_ROOT', root):
                legacy = app_paths.legacy_data()
            self.assertEqual(legacy['db'], root / 'anpr.sqlite3')
            self.assertEqual(legacy['state'], root / 'anpr_state.json')
            self.assertNotIn('subscribers', legacy)

    def test_clean_root_has_no_legacy_data(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(app_paths, 'PROJECT_ROOT', Path(folder)):
                self.assertEqual(app_paths.legacy_data(), {})

    def test_data_directories_are_created(self):
        """Каталоги рабочих данных создаются — иначе сервис не стартует."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data, runtime, logs = root / 'data', root / 'runtime', root / 'logs'
            with patch.object(app_paths, 'DATA_DIR', data), \
                 patch.object(app_paths, 'RUNTIME_DIR', runtime), \
                 patch.object(app_paths, 'LOG_DIR', logs):
                app_paths.ensure_data_dirs()
            self.assertTrue(data.is_dir())
            self.assertTrue(runtime.is_dir())
            self.assertTrue(logs.is_dir())


class SingleSourceOfTruthTests(unittest.TestCase):
    """Корень проекта и пути к данным считает только `app/core/paths.py`."""

    #: Модуль с единственным источником путей и служебная загрузка скриптов.
    ALLOWED = {'app/core/paths.py', 'scripts/_bootstrap.py'}

    def _python_files(self):
        for folder in (PROJECT_ROOT / 'app', PROJECT_ROOT / 'scripts'):
            for path in sorted(folder.rglob('*.py')):
                relative = path.relative_to(PROJECT_ROOT).as_posix()
                if relative not in self.ALLOWED:
                    yield relative, path

    def test_no_module_computes_the_project_root_on_its_own(self):
        for relative, path in self._python_files():
            source = path.read_text(encoding='utf-8')
            self.assertNotIn('parents[1]', source,
                             f'{relative}: корень проекта вычисляется самостоятельно')
            self.assertNotIn('parent.parent', source,
                             f'{relative}: корень проекта вычисляется самостоятельно')

    def test_data_file_names_are_not_duplicated_outside_the_path_module(self):
        """Имена рабочих файлов живут в модуле путей, а не в разных модулях."""
        allowed_outside = {
            'app/core/anpr_telegram.py',    # формат состояния, не путь
            'app/services/audit.py',         # имена журналов
            'app/services/auth.py',          # имя файла токена
            'app/services/logs.py',          # имена файлов журналов
            'app/services/control.py',       # имена файлов запуска
            'scripts/migrate_data.py',       # сценарий переноса знает исходные места
            'scripts/acceptance_check.py',
            'scripts/verify_state.py',
            'app/core/safe_mode.py',         # изоляция: пути во временном каталоге
            'app/core/preferences.py',
            'app/core/window_state.py',
            'app/services/http_api.py',
            'app/ui/dashboard.py',
            'app/ui/settings.py',
            'app/ui/shell.py',
        }
        for relative, path in self._python_files():
            if relative in allowed_outside:
                continue
            source = path.read_text(encoding='utf-8')
            for name in ('anpr.sqlite3', 'anpr_state.json', 'subscribers.json'):
                self.assertNotIn(name, source,
                                 f'{relative}: путь к «{name}» собран вручную')

    def test_migration_script_covers_the_working_files(self):
        sys.path.insert(0, str(PROJECT_ROOT / 'scripts'))
        try:
            import migrate_data
        finally:
            sys.path.pop(0)
        sources = {str(source.relative_to(PROJECT_ROOT)).replace('\\', '/')
                   for source, _target in migrate_data.build_plan()}
        for name in ('anpr.sqlite3', 'anpr_state.json', 'subscribers.json', 'images'):
            self.assertIn(name, sources)


def _working_paths():
    """Рабочие пути приложения (без изоляции безопасного режима)."""
    return (app_paths.db_file(), app_paths.state_file(), app_paths.subscribers_file(),
            app_paths.photo_dir(), app_paths.runtime_dir(), app_paths.log_dir(),
            app_paths.env_file())


class _working_environment:
    """Временное снятие изоляции безопасного режима."""

    NAMES = ('DB_FILE', 'STATE_FILE', 'SUBSCRIBERS_FILE', 'IMAGE_DIR',
             'ANPR_RUNTIME_DIR', 'ANPR_LOG_DIR', 'ANPR_ENV_FILE')

    def __enter__(self):
        self.saved = {name: os.environ.pop(name, None) for name in self.NAMES}
        return self

    def __exit__(self, *args):
        for name, value in self.saved.items():
            if value is not None:
                os.environ[name] = value
        return False


class _env:
    """Временное переопределение переменных окружения."""

    def __init__(self, **values):
        self.values = values
        self.saved = {}

    def __enter__(self):
        for name, value in self.values.items():
            self.saved[name] = os.environ.get(name)
            os.environ[name] = value
        return self

    def __exit__(self, *args):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        return False


if __name__ == '__main__':
    unittest.main()
