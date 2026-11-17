"""Small SQLite metadata store and durable text outbox.

Images are deliberately absent from this module. They remain in memory only.
"""
import json
import sqlite3
import threading
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA busy_timeout=5000')
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT UNIQUE,
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
        self.conn.commit()

    def add_event(self, event_key, record):
        with self.lock:
            cur = self.conn.execute('''INSERT OR IGNORE INTO events
                (event_key,day,captured_at,plate,direction,lane,speed,vehicle_color,plate_type,confidence,backfill)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (event_key, record.get('iso','')[:10], record.get('iso',''),
                record.get('plate','—'), record.get('direction',''), record.get('lane',''), record.get('speed',''),
                record.get('color',''), record.get('type',''), record.get('confidence',''), int(record.get('backfill', False))))
            self.conn.commit()
            return cur.lastrowid or None

    def recent_passages(self, limit=100):
        """Return recent passage metadata only; no images or delivery data."""
        with self.lock:
            rows = self.conn.execute(
                """SELECT id, captured_at, plate, direction, lane, speed, backfill
                   FROM events ORDER BY captured_at DESC, id DESC LIMIT ?""", (limit,)
            ).fetchall()
        keys = ('id', 'captured_at', 'plate', 'direction', 'lane', 'speed', 'backfill')
        return [dict(zip(keys, row)) for row in rows]

    def passage_identity(self, event_id):
        with self.lock:
            return self.conn.execute('SELECT captured_at, plate FROM events WHERE id=?', (event_id,)).fetchone()

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
