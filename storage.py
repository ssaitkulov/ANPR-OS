"""SQLite storage for ANPR passages and the durable text outbox.

Images are stored separately as files; SQLite keeps only their paths.

`events` is the single source of truth for passages: one row == one passage
(live detection or archive/backfill record).

`event_key` identifies one concrete passage and is therefore UNIQUE. It is
derived from the passage itself (timestamp + plate + lane), never from the
firmware identifiers, because firmware reuses `GroupID` for different
vehicles. The firmware key (`GroupID:IndexInGroup:plate:lane`) is kept
separately in `source_key` for diagnostics only.

Deduplication of live events is done by the caller (time window + snapshot
fingerprint); the database only guarantees that the same passage is never
stored twice under the same key.
"""
import sqlite3
import threading
from pathlib import Path


def passage_key(captured_at: str, plate: str, lane: str = '', backfill: bool = False) -> str:
    """Deterministic key of one passage (live or archive)."""
    stamp = (captured_at or '').strip()
    prefix = 'backfill' if backfill else 'live'
    return f"{prefix}:{stamp}|{plate or ''}|{lane or ''}"


def backfill_key(rec_no, captured_at: str, plate: str, lane: str = '') -> str:
    """Key of an archive record; the camera RecNo wins when it is known."""
    if rec_no not in (None, '', 0):
        return f"backfill:rec:{int(rec_no)}"
    return passage_key(captured_at, plate, lane, backfill=True)


CREATE_EVENTS_TABLE = '''
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_key TEXT UNIQUE,
        source_key TEXT,
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
    CREATE INDEX IF NOT EXISTS events_day_idx ON events(day, captured_at);
    CREATE INDEX IF NOT EXISTS events_plate_idx ON events(plate, captured_at);
    CREATE INDEX IF NOT EXISTS events_capture_idx ON events(captured_at, plate);
    CREATE INDEX IF NOT EXISTS events_source_idx ON events(source_key);
'''

EVENT_FIELDS = ('id', 'event_key', 'source_key', 'day', 'captured_at', 'plate', 'direction',
                'lane', 'speed', 'vehicle_color', 'plate_type', 'confidence', 'backfill')


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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                sent_at TEXT
            );
            CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(status, id);
        ''')
        self._upgrade_events()
        self.conn.commit()

    # -- schema ------------------------------------------------------------

    def _upgrade_events(self):
        """Bring an older events table to the current layout.

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
        conn.executescript(CREATE_EVENTS_TABLE)
        conn.executescript(CREATE_EVENTS_INDEXES)

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
            backfill = bool(data.get('backfill'))
            key = passage_key(captured, plate, lane, backfill)
            if key in used:
                key = f"{key}#{data.get('id')}"
            used.add(key)
            conn.execute('''INSERT OR IGNORE INTO events
                (event_key, source_key, day, captured_at, plate, direction, lane, speed,
                 vehicle_color, plate_type, confidence, backfill, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                         (key, data.get('event_key'), data.get('day') or captured[:10], captured,
                          plate, data.get('direction'), lane, data.get('speed'),
                          data.get('vehicle_color'), data.get('plate_type'), data.get('confidence'),
                          int(backfill), data.get('created_at')))

    # -- passages ----------------------------------------------------------

    def add_event(self, event_key, record, source_key=None):
        """Store one passage. Returns its row id (existing id if already stored)."""
        iso = record.get('iso', '') or ''
        key = event_key or passage_key(iso, record.get('plate', ''), record.get('lane', ''),
                                       bool(record.get('backfill')))
        with self.lock:
            cur = self.conn.execute('''INSERT OR IGNORE INTO events
                (event_key, source_key, day, captured_at, plate, direction, lane, speed,
                 vehicle_color, plate_type, confidence, backfill)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (key, source_key, iso[:10], iso, record.get('plate', '—'),
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
            event_key, record = item[0], item[1]
            source_key = item[2] if len(item) > 2 else None
            iso = record.get('iso', '') or ''
            key = event_key or passage_key(iso, record.get('plate', ''), record.get('lane', ''),
                                           bool(record.get('backfill')))
            rows.append((key, source_key, iso[:10], iso, record.get('plate', '—'),
                         record.get('direction', ''), record.get('lane', ''), record.get('speed', ''),
                         record.get('color', ''), record.get('type', ''), record.get('confidence', ''),
                         int(bool(record.get('backfill')))))
        if not rows:
            return 0
        with self.lock:
            before = self.conn.total_changes
            self.conn.executemany('''INSERT OR IGNORE INTO events
                (event_key, source_key, day, captured_at, plate, direction, lane, speed,
                 vehicle_color, plate_type, confidence, backfill)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', rows)
            self.conn.commit()
            return self.conn.total_changes - before

    def update_event(self, event_id, record, source_key=None):
        """Обновить существующую запись проезда (live-событие поверх архивной)."""
        iso = record.get('iso', '') or ''
        key = passage_key(iso, record.get('plate', ''), record.get('lane', ''),
                          bool(record.get('backfill')))
        with self.lock:
            self.conn.execute('''UPDATE events SET source_key=?, day=?, captured_at=?, plate=?, direction=?,
                lane=?, speed=?, vehicle_color=?, plate_type=?, confidence=?, backfill=? WHERE id=?''',
                (source_key, iso[:10], iso, record.get('plate', '—'), record.get('direction', ''),
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

    # -- outbox ------------------------------------------------------------

    def enqueue_text(self, event_id, chat_id, body, kind='text'):
        with self.lock:
            self.conn.execute('INSERT INTO outbox(event_id,chat_id,body,kind) VALUES(?,?,?,?)',
                              (event_id, str(chat_id), body, kind))
            self.conn.commit()

    def pending(self, limit=100):
        with self.lock:
            return self.conn.execute('''SELECT id,event_id,chat_id,body,kind,attempts FROM outbox
                                        WHERE status='pending' ORDER BY id LIMIT ?''', (limit,)).fetchall()

    def pending_grouped(self, limit=1000):
        grouped = {}
        for row in self.pending(limit):
            grouped.setdefault(row[2], []).append(row)
        return grouped

    def mark_sent(self, outbox_id):
        with self.lock:
            self.conn.execute("UPDATE outbox SET status='sent',sent_at=CURRENT_TIMESTAMP WHERE id=?", (outbox_id,))
            self.conn.commit()

    def mark_failed(self, outbox_id, error):
        with self.lock:
            self.conn.execute("UPDATE outbox SET attempts=attempts+1,last_error=? WHERE id=?", (str(error)[:500], outbox_id))
            self.conn.commit()

    def stats(self):
        with self.lock:
            events = self.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0]
            pending = self.conn.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0]
            return {'events': events, 'pending_messages': pending}

    def close(self):
        with self.lock:
            self.conn.close()
