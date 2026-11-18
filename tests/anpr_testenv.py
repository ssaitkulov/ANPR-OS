"""Изоляция тестов от боевого окружения (Этап 3).

Модуль нужно импортировать **первым** в каждом тестовом файле — до
``import anpr_telegram``: пути базы, JSON-состояния, фотографий и подписчиков
перенаправляются во временный каталог, а доступы к Telegram обнуляются.

Без этого ``python -m unittest discover`` мог бы изменить рабочие
``anpr.sqlite3`` / ``anpr_state.json`` или отправить сообщение подписчикам.

Пользоваться можно и как фикстура:

    class MyTest(anpr_testenv.IsolatedTestCase):
        def test_something(self):
            self.store    # временная база, подставленная вместо bot.STORE
            self.state    # состояние сервиса (JSON-зеркало в tmp)
"""
import atexit
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

#: Корень проекта: рабочие файлы сервиса лежат в корне, а не рядом с тестами.
BASE_DIR = Path(__file__).resolve().parents[1]

#: Временный каталог тестового прогона — база, состояние, фото, подписчики.
ISOLATION_DIR = Path(tempfile.mkdtemp(prefix='anpr-tests-'))
atexit.register(shutil.rmtree, ISOLATION_DIR, True)

#: Рабочие файлы сервиса — их прогон тестов не должен менять.
WORKING_FILES = {
    'db': BASE_DIR / 'anpr.sqlite3',
    'state': BASE_DIR / 'anpr_state.json',
    'subscribers': BASE_DIR / 'subscribers.json',
}


def _redirect(name: str, value) -> None:
    """Перенаправить переменную окружения.

    Именно присваивание, а не ``setdefault``: ``.env`` загружается через
    ``setdefault`` и не должен вернуть боевые значения поверх изоляции.
    """
    os.environ[name] = str(value)


for _name, _value in {
    'DB_FILE': ISOLATION_DIR / 'anpr.sqlite3',
    'STATE_FILE': ISOLATION_DIR / 'anpr_state.json',
    'IMAGE_DIR': ISOLATION_DIR / 'images',
    'SUBSCRIBERS_FILE': ISOLATION_DIR / 'subscribers.json',
}.items():
    _redirect(_name, _value)

# Настоящий бот в тестах не нужен и не должен получать сообщения.
os.environ['TELEGRAM_BOT_TOKEN'] = ''
os.environ['TELEGRAM_CHAT_ID'] = ''
os.environ['ALLOWED_CHAT_IDS'] = ''


def is_isolated() -> bool:
    """True, если рабочие пути уведены во временный каталог."""
    return all(str(WORKING_FILES[key]) != os.environ.get(env, '')
               for key, env in (('db', 'DB_FILE'), ('state', 'STATE_FILE'),
                                ('subscribers', 'SUBSCRIBERS_FILE')))


def _close_module_store():
    """Закрыть модульную базу перед выходом (иначе остаётся «unclosed database»)."""
    try:
        import anpr_telegram as bot
        bot.STORE.close()
    except Exception:  # pragma: no cover — закрытие не должно ломать выход
        pass


atexit.register(_close_module_store)


def fingerprint(path) -> dict | None:
    """Слепок файла: есть ли он, когда менялся, сколько весит."""
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return None
    return {'exists': True, 'mtime_ns': stat.st_mtime_ns, 'size': stat.st_size}


def working_fingerprint() -> dict:
    """Слепки рабочих файлов сервиса — для проверки «тесты ничего не меняли»."""
    return {name: fingerprint(path) for name, path in WORKING_FILES.items()}


def working_db_rows(plate: str) -> list:
    """Строки рабочей базы по номеру (только чтение). Пусто, если базы нет.

    Нужно, чтобы доказать: запись, сделанная тестом, не попала в боевую БД,
    даже если живой сервис пишет в неё одновременно.
    """
    db = WORKING_FILES['db']
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return []
    try:
        return conn.execute('SELECT id, plate, captured_at FROM events WHERE plate=?',
                            (plate,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()


class IsolatedTestCase(unittest.TestCase):
    """Фикстура: временная база, временное состояние, никакого Telegram.

    Патчится ``bot.STORE``, ``bot.STATE_FILE``, ``bot.IMAGE_DIR``,
    ``bot.SUBSCRIBERS_FILE`` и токен Telegram — тест работает с настоящим
    кодом сервиса, но данные остаются во временном каталоге.
    """

    def setUp(self):
        import anpr_telegram as bot
        from app.storage.database import Store

        # Предыдущий тест не должен оставить в модуле подмены: если оставил —
        # падать нужно сразу и с понятным сообщением, а не с загадочным «True».
        for name in ('telegram_send', 'telegram_send_to', 'backfill_once',
                     '_fetch_archive_records', 'now_local'):
            self.assertNotIsInstance(getattr(bot, name), Mock,
                                     f'модуль загрязнён подменой: {name}')

        self._folder = tempfile.TemporaryDirectory(prefix='anpr-case-')
        self.folder = Path(self._folder.name)
        self.store = Store(self.folder / 'events.sqlite3')
        self.state_path = self.folder / 'state.json'
        self.image_dir = self.folder / 'images'
        self.subscribers_path = self.folder / 'subscribers.json'

        self._patches = [
            patch.object(bot, 'STORE', self.store),
            patch.object(bot, 'STATE_FILE', self.state_path),
            patch.object(bot, 'IMAGE_DIR', self.image_dir),
            patch.object(bot, 'SUBSCRIBERS_FILE', self.subscribers_path),
            patch.object(bot, 'TELEGRAM_BOT_TOKEN', ''),
            patch.object(bot, 'TELEGRAM_CHAT_ID', ''),
            patch.object(bot, 'ALLOWED_CHAT_IDS', ''),
            patch.object(bot, 'BROADCAST_CHAT', ''),
        ]
        for item in self._patches:
            item.start()
        self.bot = bot
        self.state = {'days': {}, 'seen': [], 'last_report_date': '', 'reported_days': [],
                      'last_event_time': '', 'last_rec_no': 0}

    def tearDown(self):
        for item in reversed(self._patches):
            item.stop()
        try:
            # Модульная база (LazyStore) могла открыться во время теста —
            # закрываем, чтобы не осталось «unclosed database».
            self.bot.STORE.close()
        finally:
            try:
                self.store.close()
            finally:
                self._folder.cleanup()

    # -- помощники --------------------------------------------------------

    def rows(self, day: str) -> list:
        """Строки базы за день."""
        return self.store.day_passages(day)

    def outbox_rows(self) -> list:
        """Все записи очереди недоставленных."""
        with self.store.lock:
            return self.store.conn.execute(
                'SELECT id, chat_id, body, kind, status, attempts FROM outbox ORDER BY id'
            ).fetchall()
