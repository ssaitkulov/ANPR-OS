"""SQLite storage for ANPR passages and the durable text outbox.

Images are stored separately as files; SQLite keeps only their paths.

`events` is the single source of truth for passages: one row == one passage
(live detection or archive/backfill record).

`event_key` identifies one concrete passage and is therefore UNIQUE. It is
content-derived: `sha256(day|iso|plate|lane|direction)`. Firmware identifiers
(`GroupID`, `IndexInGroup`) are never part of the key, because this firmware
reuses `GroupID` for different vehicles — a repeated passage must still create
a new row. The firmware key (`GroupID:IndexInGroup:plate:lane`) is kept in
`source_key`, the bare group id in `group_id`, and the camera archive record
number in `rec_no` — all for diagnostics only.

Deduplication of live events is done by the caller (time window + snapshot
fingerprint); the database only guarantees that the same passage is never
stored twice under the same key.
"""
import hashlib
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


def normalize_plate(value) -> str:
    """Plate without spaces and case — used for comparisons and keys."""
    return ''.join(str(value or '').split()).upper()


def event_key(day: str, captured_at: str, plate: str = '', lane: str = '',
              direction: str = '') -> str:
    """Content-unique key of one passage: sha256(day|iso|plate|lane|direction)."""
    parts = [str(day or '').strip(), str(captured_at or '').strip(),
             normalize_plate(plate), str(lane or '').strip(), str(direction or '').strip()]
    return hashlib.sha256('|'.join(parts).encode('utf-8')).hexdigest()


def passage_key(captured_at: str, plate: str, lane: str = '', direction: str = '') -> str:
    """Key of one passage; the day is taken from the timestamp."""
    return event_key(str(captured_at or '')[:10], captured_at, plate, lane, direction)


def is_event_key(value) -> bool:
    """True when the key is already in the current sha256 format."""
    text = str(value or '')
    return len(text) == 64 and all(ch in '0123456789abcdef' for ch in text)


def group_id_of(source_key) -> str:
    """Group id out of the firmware key `GroupID:IndexInGroup:plate:lane`."""
    text = str(source_key or '').strip()
    if not text or ':' not in text:
        return ''
    return text.split(':', 1)[0].strip()


def rec_no_of(event_key_value) -> int | None:
    """Archive record number out of the legacy key `backfill:rec:<n>`."""
    text = str(event_key_value or '')
    if text.startswith('backfill:rec:'):
        try:
            return int(text.rsplit(':', 1)[1])
        except ValueError:
            return None
    return None


CREATE_EVENTS_TABLE = '''
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_key TEXT UNIQUE,
        source_key TEXT,
        group_id TEXT,
        rec_no INTEGER,
        day TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        plate TEXT NOT NULL,
        direction TEXT,
        lane TEXT,
        speed TEXT,
        vehicle_color TEXT,
        plate_type TEXT,
        confidence TEXT,
        backfill INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
'''

CREATE_EVENTS_INDEXES = '''
    CREATE INDEX IF NOT EXISTS idx_events_day ON events(day, captured_at);
    CREATE INDEX IF NOT EXISTS idx_events_captured_at ON events(captured_at, plate);
    CREATE INDEX IF NOT EXISTS events_plate_idx ON events(plate, captured_at);
    CREATE INDEX IF NOT EXISTS events_source_idx ON events(source_key);
    CREATE INDEX IF NOT EXISTS events_group_idx ON events(group_id, captured_at);
    -- Индексы прошлой версии: те же самые под другими именами — держим один набор.
    DROP INDEX IF EXISTS events_day_idx;
    DROP INDEX IF EXISTS events_capture_idx;
'''

EVENT_FIELDS = ('id', 'event_key', 'source_key', 'group_id', 'rec_no', 'day', 'captured_at',
                'plate', 'direction', 'lane', 'speed', 'vehicle_color', 'plate_type',
                'confidence', 'backfill')

# -- политика повторов очереди недоставленных ------------------------------
# Значения по умолчанию; anpr_telegram.py переопределяет их из .env при импорте.
DEFAULT_MAX_ATTEMPTS = 10      # после стольких неудачных попыток запись становится 'dead'
RETRY_BASE_DELAY = 30.0        # первая пауза перед повтором, секунды
RETRY_MAX_DELAY = 3600.0       # потолок экспоненциальной паузы, секунды


def backoff_delay(attempts: int, base: float = RETRY_BASE_DELAY,
                  cap: float = RETRY_MAX_DELAY) -> float:
    """Пауза перед следующей попыткой: base, 2*base, 4*base ... (не больше cap)."""
    try:
        attempts = int(attempts)
    except (TypeError, ValueError):
        attempts = 1
    attempts = max(1, attempts)
    return float(min(cap, base * (2 ** (attempts - 1))))


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA busy_timeout=5000')
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS passage_photo_paths (
                captured_at TEXT NOT NULL,
                plate TEXT NOT NULL,
                path TEXT NOT NULL,
                PRIMARY KEY (captured_at, plate)
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER,
                chat_id TEXT NOT NULL,
                body TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'text',
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                next_attempt_at REAL,
                photo_path TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                sent_at TEXT
            );
            CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(status, id);
        ''')
        self._upgrade_events()
        self._upgrade_outbox()
        self.conn.executescript('''
            CREATE INDEX IF NOT EXISTS outbox_due ON outbox(status, next_attempt_at);
        ''')
        self.conn.commit()

    # -- schema ------------------------------------------------------------

    def _upgrade_events(self):
        """Bring an older events table to the current layout (hot migration).

        Two things happen here, both idempotent:
          * missing columns (`group_id`, `rec_no`) are added;
          * legacy `event_key` values are replaced by content-derived sha256 keys.

        Legacy databases kept the firmware key in a UNIQUE column, so every
        repeated passage (same GroupID, same plate, same lane) was silently
        dropped by INSERT OR IGNORE. Such a table is rebuilt once: rows are
        copied with passage-derived keys.
        """
        conn = self.conn
        table = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='events'").fetchone()
        if table:
            columns = {row[1] for row in conn.execute('PRAGMA table_info(events)')}
            if 'source_key' not in columns:
                self._rebuild_events(columns)
            else:
                self._add_missing_columns(columns)
        conn.executescript(CREATE_EVENTS_TABLE)
        conn.executescript(CREATE_EVENTS_INDEXES)
        self._migrate_event_keys()

    def _upgrade_outbox(self):
        """Добавить недостающие колонки в старую очередь (идемпотентно)."""
        columns = {row[1] for row in self.conn.execute('PRAGMA table_info(outbox)')}
        if 'next_attempt_at' not in columns:
            self.conn.execute('ALTER TABLE outbox ADD COLUMN next_attempt_at REAL')
        if 'photo_path' not in columns:
            # Путь к уже сохранённому на диске снимку: при досылке после
            # недоступности Telegram сообщение уходит с фото, а не текстом.
            self.conn.execute('ALTER TABLE outbox ADD COLUMN photo_path TEXT')

    def _add_missing_columns(self, columns):
        conn = self.conn
        for name, ddl in (('group_id', 'ALTER TABLE events ADD COLUMN group_id TEXT'),
                          ('rec_no', 'ALTER TABLE events ADD COLUMN rec_no INTEGER')):
            if name not in columns:
                conn.execute(ddl)

    def _rebuild_events(self, columns):
        conn = self.conn
        wanted = ('id', 'event_key', 'day', 'captured_at', 'plate', 'direction', 'lane',
                  'speed', 'vehicle_color', 'plate_type', 'confidence', 'backfill', 'created_at')
        available = [name for name in wanted if name in columns]
        rows = conn.execute(f"SELECT {', '.join(available)} FROM events ORDER BY id").fetchall()
        positions = {name: pos for pos, name in enumerate(available)}
        conn.execute('DROP TABLE IF EXISTS events_legacy')
        conn.execute('ALTER TABLE events RENAME TO events_legacy')
        conn.execute(CREATE_EVENTS_TABLE)
        used = set()
        for row in rows:
            data = {name: row[positions[name]] for name in available}
            captured = data.get('captured_at') or ''
            plate = data.get('plate') or ''
            lane = data.get('lane') or ''
            direction = data.get('direction') or ''
            backfill = bool(data.get('backfill'))
            day = data.get('day') or captured[:10]
            key = event_key(day, captured, plate, lane, direction)
            if key in used:
                # Two rows describe the same passage — keep both, disambiguate.
                key = f"{key}#{data.get('id')}"
            used.add(key)
            conn.execute('''INSERT OR IGNORE INTO events
                (event_key, source_key, group_id, rec_no, day, captured_at, plate, direction, lane,
                 speed, vehicle_color, plate_type, confidence, backfill, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                         (key, data.get('event_key'), group_id_of(data.get('event_key')),
                          rec_no_of(data.get('event_key')), day, captured, plate, direction, lane,
                          data.get('speed'), data.get('vehicle_color'), data.get('plate_type'),
                          data.get('confidence'), int(backfill), data.get('created_at')))

    def _migrate_event_keys(self):
        """Replace legacy keys with content-derived ones; fill the new columns."""
        conn = self.conn
        rows = conn.execute('''SELECT id, event_key, source_key, group_id, rec_no,
                                      day, captured_at, plate, direction, lane
                               FROM events ORDER BY id''').fetchall()
        if not rows:
            return
        owner = {}
        for row in rows:
            owner.setdefault(row[1], row[0])
        migrated = 0
        for (rid, key, source_key, group_id, rec_no, day, captured_at,
             plate, direction, lane) in rows:
            gid = group_id or group_id_of(source_key)
            rec = rec_no if rec_no is not None else rec_no_of(key)
            if gid != (group_id or '') or (rec is not None and rec_no is None):
                conn.execute('UPDATE events SET group_id=?, rec_no=? WHERE id=?', (gid, rec, rid))
            if is_event_key(key):
                continue
            fresh = event_key(day, captured_at, plate, lane, direction)
            if owner.get(fresh, rid) != rid:
                # Another row already owns that key (same content) — keep this row as is.
                print(f'[storage] дубликат содержимого, ключ строки {rid} оставлен прежним',
                      file=sys.stderr)
                continue
            conn.execute('UPDATE events SET event_key=? WHERE id=?', (fresh, rid))
            owner[fresh] = rid
            owner.pop(key, None)
            migrated += 1
        if migrated:
            conn.commit()
            print(f'[storage] ключи событий пересчитаны (sha256): {migrated}', file=sys.stderr)

    # -- passages ----------------------------------------------------------

    def add_event(self, key, record, source_key=None):
        """Store one passage. Returns its row id (existing id if already stored)."""
        iso = record.get('iso', '') or ''
        key = key or passage_key(iso, record.get('plate', ''), record.get('lane', ''),
                                 record.get('direction', ''))
        with self.lock:
            cur = self.conn.execute('''INSERT OR IGNORE INTO events
                (event_key, source_key, group_id, rec_no, day, captured_at, plate, direction, lane,
                 speed, vehicle_color, plate_type, confidence, backfill)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (key, source_key, record.get('group_id', ''), _rec_no(record),
                 iso[:10], iso, record.get('plate', '—'),
                 record.get('direction', ''), record.get('lane', ''), record.get('speed', ''),
                 record.get('color', ''), record.get('type', ''), record.get('confidence', ''),
                 int(bool(record.get('backfill')))))
            self.conn.commit()
            if cur.rowcount:
                return cur.lastrowid
            row = self.conn.execute('SELECT id FROM events WHERE event_key=?', (key,)).fetchone()
            return row[0] if row else None

    def add_events(self, items):
        """Store many passages at once. items: [(event_key, record, source_key), ...]."""
        rows = []
        for item in items:
            key, record = item[0], item[1]
            source_key = item[2] if len(item) > 2 else None
            iso = record.get('iso', '') or ''
            key = key or passage_key(iso, record.get('plate', ''), record.get('lane', ''),
                                     record.get('direction', ''))
            rows.append((key, source_key, record.get('group_id', ''), _rec_no(record),
                         iso[:10], iso, record.get('plate', '—'),
                         record.get('direction', ''), record.get('lane', ''), record.get('speed', ''),
                         record.get('color', ''), record.get('type', ''), record.get('confidence', ''),
                         int(bool(record.get('backfill')))))
        if not rows:
            return 0
        with self.lock:
            before = self.conn.total_changes
            self.conn.executemany('''INSERT OR IGNORE INTO events
                (event_key, source_key, group_id,rec_no, day, captured_at, plate, direction, lane,
                 speed, vehicle_color, plate_type, confidence, backfill)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', rows)
            self.conn.commit()
            return self.conn.total_changes - before

    def update_event(self, event_id, record, source_key=None):
        """Обновить существующую запись проезда (live-событие поверх архивной)."""
        iso = record.get('iso', '') or ''
        key = event_key(iso[:10], iso, record.get('plate', ''), record.get('lane', ''),
                        record.get('direction', ''))
        with self.lock:
            self.conn.execute('''UPDATE events SET source_key=?, group_id=COALESCE(NULLIF(?,''), group_id),
                rec_no=COALESCE(?, rec_no), day=?, captured_at=?, plate=?, direction=?,
                lane=?, speed=?, vehicle_color=?, plate_type=?, confidence=?, backfill=? WHERE id=?''',
                (source_key, record.get('group_id', ''), _rec_no(record),
                 iso[:10], iso, record.get('plate', '—'), record.get('direction', ''),
                 record.get('lane', ''), record.get('speed', ''), record.get('color', ''),
                 record.get('type', ''), record.get('confidence', ''),
                 int(bool(record.get('backfill'))), event_id))
            try:
                self.conn.execute('UPDATE events SET event_key=? WHERE id=?', (key, event_id))
            except sqlite3.IntegrityError:
                pass  # ключ занят другой записью — оставляем прежний
            self.conn.commit()
        return event_id

    def recent_passages(self, limit=100):
        """Return recent passage metadata only; no images or delivery data."""
        with self.lock:
            rows = self.conn.execute(
                """SELECT id, captured_at, plate, direction, lane, speed, backfill
                   FROM events ORDER BY captured_at DESC, id DESC LIMIT ?""", (limit,)
            ).fetchall()
        keys = ('id', 'captured_at', 'plate', 'direction', 'lane', 'speed', 'backfill')
        return [dict(zip(keys, row)) for row in rows]

    def day_passages(self, day):
        """All passages of one day, oldest first."""
        with self.lock:
            rows = self.conn.execute(
                f"SELECT {', '.join(EVENT_FIELDS)} FROM events WHERE day=? ORDER BY captured_at, id", (day,)
            ).fetchall()
        return [dict(zip(EVENT_FIELDS, row)) for row in rows]

    def count_day(self, day) -> int:
        """Сколько проездов за день (быстрая проверка «были ли проезды»)."""
        with self.lock:
            row = self.conn.execute('SELECT COUNT(*) FROM events WHERE day=?', (day,)).fetchone()
        return int(row[0]) if row else 0

    def passage_exists(self, captured_at, plate):
        """True when a passage with the same timestamp and plate is already stored."""
        with self.lock:
            return self.conn.execute(
                'SELECT 1 FROM events WHERE captured_at=? AND plate=? LIMIT 1', (captured_at, plate)
            ).fetchone() is not None

    def passage_identity(self, event_id):
        with self.lock:
            return self.conn.execute('SELECT captured_at, plate FROM events WHERE id=?', (event_id,)).fetchone()

    # -- photos ------------------------------------------------------------

    def set_photo_path(self, captured_at, plate, path):
        with self.lock:
            self.conn.execute('INSERT OR REPLACE INTO passage_photo_paths (captured_at, plate, path) VALUES (?, ?, ?)',
                              (captured_at, plate, path))
            self.conn.commit()

    def get_photo_path(self, captured_at, plate):
        with self.lock:
            row = self.conn.execute('SELECT path FROM passage_photo_paths WHERE captured_at=? AND plate=?',
                                    (captured_at, plate)).fetchone()
        return row[0] if row else None

    def find_photo_path(self, captured_at, plate, window: int = 120):
        """Путь к фото проезда: сначала точное совпадение, иначе тот же номер в ±window секунд.

        Номер из архива камеры и номер из live-события могут отличаться
        пробелами и регистром, а время — секундой. Поэтому сравнение идёт
        по нормализованному номеру и близкому времени.
        """
        day = str(captured_at or '')[:10]
        target = normalize_plate(plate)
        if not day or not target:
            return None
        with self.lock:
            rows = self.conn.execute(
                'SELECT captured_at, plate, path FROM passage_photo_paths WHERE captured_at LIKE ?',
                (day + '%',)).fetchall()
        if not rows:
            return None
        try:
            moment = datetime.fromisoformat(str(captured_at))
        except (TypeError, ValueError):
            moment = None
        fallback = None
        for stamp, stored_plate, path in rows:
            if normalize_plate(stored_plate) != target:
                continue
            if str(stamp) == str(captured_at):
                return path
            if moment is None:
                fallback = fallback or path
                continue
            try:
                other = datetime.fromisoformat(str(stamp))
            except (TypeError, ValueError):
                continue
            if abs((moment - other).total_seconds()) <= max(0, int(window)):
                return path
            fallback = fallback or path
        return fallback

    # -- outbox ------------------------------------------------------------

    def enqueue_text(self, event_id, chat_id, body, kind='text', photo_path=None):
        """Положить сообщение в очередь. Повтор разрешён сразу.

        photo_path — путь к уже сохранённому снимку, чтобы досылка после
        недоступности Telegram ушла с фото, а не голым текстом.
        """
        with self.lock:
            self.conn.execute(
                'INSERT INTO outbox(event_id,chat_id,body,kind,next_attempt_at,photo_path) '
                'VALUES(?,?,?,?,?,?)',
                (event_id, str(chat_id), body, kind, time.time(), photo_path))
            self.conn.commit()

    def pending(self, limit=100):
        """Записи, готовые к отправке: статус pending и пауза повтора истекла."""
        with self.lock:
            return self.conn.execute('''SELECT id,event_id,chat_id,body,kind,attempts,next_attempt_at,photo_path FROM outbox
                                        WHERE status='pending'
                                          AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                                        ORDER BY id LIMIT ?''', (time.time(), limit)).fetchall()

    def pending_grouped(self, limit=1000):
        grouped = {}
        for row in self.pending(limit):
            grouped.setdefault(row[2], []).append(row)
        return grouped

    def mark_sent(self, outbox_id):
        with self.lock:
            self.conn.execute("UPDATE outbox SET status='sent',sent_at=CURRENT_TIMESTAMP WHERE id=?", (outbox_id,))
            self.conn.commit()

    def mark_partial(self, outbox_id):
        """Запись доставлена частично (длинное сообщение) — хвост ждёт отдельной строки."""
        with self.lock:
            self.conn.execute("UPDATE outbox SET status='partial' WHERE id=?", (outbox_id,))
            self.conn.commit()

    def attempts_of(self, outbox_id) -> int:
        """Сколько попыток доставки было у записи."""
        with self.lock:
            row = self.conn.execute('SELECT attempts FROM outbox WHERE id=?', (outbox_id,)).fetchone()
            return int(row[0]) if row else 0

    def mark_failed(self, outbox_id, error, max_attempts=None, base_delay=None, max_delay=None):
        """Записать неудачную попытку.

        Возвращает новый статус записи: 'pending' (ждёт следующего повтора) или
        'dead' (попытки исчерпаны — сообщение больше не отправляется автоматически).
        """
        max_attempts = DEFAULT_MAX_ATTEMPTS if max_attempts is None else max_attempts
        base_delay = RETRY_BASE_DELAY if base_delay is None else base_delay
        max_delay = RETRY_MAX_DELAY if max_delay is None else max_delay
        with self.lock:
            self.conn.execute('UPDATE outbox SET attempts=attempts+1 WHERE id=?', (outbox_id,))
            row = self.conn.execute('SELECT attempts FROM outbox WHERE id=?', (outbox_id,)).fetchone()
            attempts = int(row[0]) if row else 0
            if attempts >= max_attempts:
                self.conn.execute("UPDATE outbox SET status='dead',last_error=? WHERE id=?",
                                  (str(error)[:500], outbox_id))
                status = 'dead'
            else:
                delay = backoff_delay(attempts, base_delay, max_delay)
                self.conn.execute('UPDATE outbox SET last_error=?,next_attempt_at=? WHERE id=?',
                                  (str(error)[:500], time.time() + delay, outbox_id))
                status = 'pending'
            self.conn.commit()
            return status

    def dead_messages(self, limit=100):
        """Записи, которые не удалось доставить (статус dead)."""
        with self.lock:
            return self.conn.execute('''SELECT id,chat_id,body,kind,attempts,last_error,created_at
                                        FROM outbox WHERE status='dead' ORDER BY id DESC LIMIT ?''',
                                      (limit,)).fetchall()

    def outbox_stats(self):
        """Счётчики очереди по статусам: pending / sent / partial / dead."""
        with self.lock:
            rows = self.conn.execute('SELECT status, COUNT(*) FROM outbox GROUP BY status').fetchall()
        counts = {'pending': 0, 'sent': 0, 'partial': 0, 'dead': 0}
        for status, count in rows:
            counts[str(status)] = int(count)
        return counts

    def stats(self):
        with self.lock:
            events = self.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]
            pending = self.conn.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0]
            return {'events': events, 'pending_messages': pending}

    def checkpoint(self, mode: str = 'TRUNCATE') -> bool:
        """Слить WAL в базу — файл -wal не растёт бесконечно.

        Возвращает True, если чекпоинт прошёл. WAL-файл уменьшается до нуля,
        база остаётся цельной при следующем открытии.
        """
        with self.lock:
            try:
                row = self.conn.execute(f'PRAGMA wal_checkpoint({mode})').fetchone()
            except sqlite3.Error as error:
                print(f'[storage] чекпоинт WAL не выполнен: {error}', file=sys.stderr)
                return False
            return bool(row and row[0] == 0)

    def close(self):
        with self.lock:
            try:
                self.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            except sqlite3.Error:
                pass
            self.conn.close()


def _rec_no(record):
    """Archive record number of a passage record (None when it is unknown)."""
    value = record.get('rec_no')
    if value in (None, ''):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
