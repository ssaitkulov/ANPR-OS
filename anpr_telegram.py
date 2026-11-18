#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ANPR Telegram counter — ANPR OS Fayz

Подключается к Dahua ANPR камере через HTTP API V3.91 (Digest-auth),
подписывается на live события TrafficJunction через snapManager.cgi
(гл. 4.4.3). Каждый въезд -> фото + время + номер в Telegram,
и в 20:00 итоги дня одним сообщением.

Зависимости: только requests (см. requirements.txt).
Настройки — через переменные окружения или файл .env (см. .env.example).

Использование:
    python anpr_telegram.py                 # основной режим (24/7, отчёт в 20:00)
    python anpr_telegram.py --check         # проверка камеры + Telegram
    python anpr_telegram.py --report-now    # отправить отчёт за сегодня сейчас
    python anpr_telegram.py --dry-run --report-now  # показать текст отчёта без отправки
"""

import argparse
import hashlib
import io
import json
import os
import sqlite3
import re
import queue
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from requests.auth import HTTPDigestAuth
from event_images import prepare_event_images
from storage import Store, event_key, passage_key, normalize_plate
import storage as storage_module
from system_events import EVENTS
from passage_photos import PHOTOS
from photo_archive import save_photo, cleanup_old_photos

# ---------------------------------------------------------------------------
# Конфиг (переменные окружения / .env)
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent


def load_env(path: Path = BASE_DIR / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


load_env()


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass  # pragma: no cover


def cfg(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


DAHUA_HOST = cfg("DAHUA_HOST", "192.168.1.108")
DAHUA_PORT = cfg("DAHUA_PORT", "80")
DAHUA_USERNAME = cfg("DAHUA_USERNAME", "admin")
DAHUA_PASSWORD = cfg("DAHUA_PASSWORD", "")
DAHUA_CHANNEL = cfg("DAHUA_CHANNEL", "1")          # 1 = первый канал
CAMERA_NAME = cfg("CAMERA_NAME", "Fayz kırısh")
ALLOWED_LANES = cfg("ALLOWED_LANES", "")           # пусто = все; напр. "0,1" (в событиях Lane с 0!)
DIRECTION = cfg("DIRECTION", "Approach")        # пусто = все; Approach=Приближается, Leave=Удаляется
DEBUG_RAW = cfg("DEBUG_RAW", "false").lower() in ("1", "true", "yes")
REPORT_TIME = cfg("REPORT_TIME", "20:00")          # HH:MM
TIMEZONE = cfg("TIMEZONE", "Europe/Moscow")
TELEGRAM_BOT_TOKEN = cfg("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = cfg("TELEGRAM_CHAT_ID", "")
STATE_FILE = Path(cfg("STATE_FILE", str(BASE_DIR / "anpr_state.json")))
DB_FILE = Path(cfg("DB_FILE", str(BASE_DIR / "anpr.sqlite3")))
IMAGE_DIR = Path(cfg("IMAGE_DIR", str(BASE_DIR / "images")))
SEND_EACH_ENTRY = cfg("TELEGRAM_SEND_EACH_ENTRY", "true").lower() in ("1", "true", "yes")
# Снимки на диск: true — архивировать (мониторинг читает их с диска), false — не писать.
SAVE_PHOTOS = cfg("SAVE_PHOTOS", "true").lower() in ("1", "true", "yes")
BACKFILL_ENABLED = cfg("BACKFILL_ENABLED", "true").lower() in ("1", "true", "yes")
BACKFILL_TIMESTAMP_MODE = cfg("BACKFILL_TIMESTAMP_MODE", "utc")
INCLUDE_MANUAL_SNAPSHOTS = cfg("INCLUDE_MANUAL_SNAPSHOTS", "true").lower() in ("1", "true", "yes")
BACKFILL_LOOKBACK_MINUTES = int(cfg("BACKFILL_LOOKBACK_MINUTES", "180"))
BACKFILL_MAX_MINUTES = int(cfg("BACKFILL_MAX_MINUTES", "1440"))
BACKFILL_SEND_PHOTOS = cfg("BACKFILL_SEND_PHOTOS", "false").lower() in ("1", "true", "yes")
BROADCAST_ENABLED = cfg("BROADCAST_ENABLED", "true").lower() in ("1", "true", "yes")
SUBSCRIBERS_FILE = Path(cfg("SUBSCRIBERS_FILE", str(BASE_DIR / "subscribers.json")))
ALLOWED_CHAT_IDS = cfg("ALLOWED_CHAT_IDS", "")  # пусто = доступ запрещён
RECONNECT_DELAY = int(cfg("RECONNECT_DELAY", "10"))
HEARTBEAT = cfg("HEARTBEAT", "5")
DEDUP_WINDOW_SECONDS = max(0.0, float(cfg('DEDUP_WINDOW_SECONDS', '15')))
STATE_KEEP_DAYS = max(1, int(cfg('STATE_KEEP_DAYS', '30')))  # сколько дней хранить JSON-зеркало
OUTBOX_FLUSH_INTERVAL = max(10, int(cfg('OUTBOX_FLUSH_INTERVAL', '15')))  # сек между попытками доставки очереди
OUTBOX_MAX_ATTEMPTS = max(1, int(cfg('OUTBOX_MAX_ATTEMPTS', '10')))  # попыток до статуса dead
OUTBOX_RETRY_BASE_SECONDS = max(1, int(cfg('OUTBOX_RETRY_BASE_SECONDS', '30')))  # первая пауза повтора
OUTBOX_RETRY_MAX_SECONDS = max(OUTBOX_RETRY_BASE_SECONDS,
                               int(cfg('OUTBOX_RETRY_MAX_SECONDS', '3600')))  # потолок паузы повтора
# Дневной отчёт: отдельный поток-планировщик, а не проверка в цикле камеры.
REPORT_CHECK_INTERVAL = max(15, int(cfg('REPORT_CHECK_INTERVAL', '60')))   # как часто проверять время отчёта, с
REPORT_WINDOW_MINUTES = max(0, int(cfg('REPORT_WINDOW_MINUTES', '240')))   # окно, в котором отчёт за день ещё можно отправить
REPORT_KEEP_DAYS = max(1, int(cfg('REPORT_KEEP_DAYS', '30')))             # сколько дней хранить отметки об отчётах
# Обслуживание: ротация фотографий и чекпоинт WAL.
MAINTENANCE_INTERVAL = max(60, int(cfg('MAINTENANCE_INTERVAL', '3600')))   # как часто чистить фотографии, с
IMAGE_RETENTION_DAYS = max(1, int(cfg('IMAGE_RETENTION_DAYS', '30')))     # сколько дней хранить фотографии
WAL_CHECKPOINT_INTERVAL = max(60, int(cfg('WAL_CHECKPOINT_INTERVAL', '900')))  # как часто сливать WAL, с
MAINTENANCE_TICK = 30  # как часто поток обслуживания просыпается, с

# Политика повторов очереди живёт в storage.py — передаём туда значения из .env.
storage_module.DEFAULT_MAX_ATTEMPTS = OUTBOX_MAX_ATTEMPTS
storage_module.RETRY_BASE_DELAY = float(OUTBOX_RETRY_BASE_SECONDS)
storage_module.RETRY_MAX_DELAY = float(OUTBOX_RETRY_MAX_SECONDS)

# Широковещательная запись очереди: получателей станет известно позже.
BROADCAST_CHAT = ''
BACKFILL_INTERVAL = max(15, int(cfg('BACKFILL_INTERVAL', '60')))  # сек между доборами архива
RUNTIME = {'camera_message': 'подключаюсь…', 'telegram_message': 'ожидаю первую отправку.', 'last_camera_data': ''}


class LazyStore:
    """Хранилище SQLite, открываемое при первом обращении.

    Импорт модуля не должен трогать рабочую базу: тесты и утилиты подключаются
    к модулю, не создавая anpr.sqlite3 и её WAL-файлов.
    """

    def __init__(self, path: Path):
        self._path = Path(path)
        self._store: Store | None = None
        self._lock = threading.Lock()

    def _get(self) -> Store:
        with self._lock:
            if self._store is None:
                self._store = Store(self._path)
            return self._store

    def __getattr__(self, name):
        return getattr(self._get(), name)

    def close(self):
        """Закрыть базу, если она была открыта (при остановке сервиса)."""
        with self._lock:
            store = self._store
            self._store = None
        if store is not None:
            store.close()


STORE = LazyStore(DB_FILE)

try:
    TZ = ZoneInfo(TIMEZONE)
except Exception:  # pragma: no cover
    TZ = ZoneInfo("Europe/Moscow")


# ---------------------------------------------------------------------------
# Хранилище состояния
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"days": {}, "seen": [], "last_report_date": "", "reported_days": [],
            "last_event_time": "", "last_rec_no": 0}


_state_save_lock = threading.RLock()
# Общее состояние читают и пишут несколько потоков (камера, добор архива,
# очередь недоставленных) — изменения сериализуются этим замком.
STATE_LOCK = _state_save_lock


def trim_state(state: dict) -> None:
    """Ограничить JSON-зеркало последними днями: постоянный архив лежит в SQLite."""
    days = state.get('days')
    if not isinstance(days, dict) or len(days) <= STATE_KEEP_DAYS:
        return
    keep = sorted(days)[-STATE_KEEP_DAYS:]
    state['days'] = {day: days[day] for day in keep}


def save_state(state: dict) -> None:
    with STATE_LOCK:
        trim_state(state)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(STATE_FILE)


def now_local() -> datetime:
    return datetime.now(TZ)


def today_str(dt: datetime | None = None) -> str:
    return (dt or now_local()).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Telegram: подписчики + рассылка
# ---------------------------------------------------------------------------

def load_subscribers() -> list:
    if SUBSCRIBERS_FILE.exists():
        try:
            data = json.loads(SUBSCRIBERS_FILE.read_text(encoding="utf-8"))
            return [str(x) for x in data]
        except Exception:
            return []
    return []


def save_subscribers(ids: list) -> None:
    tmp = SUBSCRIBERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(list(dict.fromkeys(ids)), ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(SUBSCRIBERS_FILE)


def get_recipients() -> list:
    ids = []
    if TELEGRAM_CHAT_ID:
        ids.append(TELEGRAM_CHAT_ID)
    if BROADCAST_ENABLED:
        ids.extend(load_subscribers())
    return [cid for cid in dict.fromkeys(ids) if _allowed(cid)]


def _tg_base() -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


# Недоставленный хвост последней отправки по чатам: доставленное не повторяем.
_LAST_TAIL: dict = {}
# Итог последней рассылки: кому доставлено, кому ушло в очередь.
_LAST_DELIVERY: dict = {'delivered': [], 'failed': [], 'seq': 0}
_DELIVERY_SEQ = {'n': 0}


def _set_delivery(delivered: list, failed: list) -> None:
    """Запомнить итог рассылки по чатам (пустые списки — отправки не было)."""
    _DELIVERY_SEQ['n'] += 1
    _LAST_DELIVERY.update({'delivered': list(delivered), 'failed': list(failed),
                            'seq': _DELIVERY_SEQ['n']})


def _telegram_post_message(chat_id: str, text: str) -> bool:
    """Один вызов sendMessage (текст не длиннее 4000 символов)."""
    try:
        r = requests.post(f"{_tg_base()}/sendMessage",
                          json={"chat_id": chat_id, "text": text,
                                "parse_mode": "HTML", "disable_web_page_preview": True},
                          timeout=30)
        r.raise_for_status()
        return True
    except Exception as e:  # pragma: no cover
        print(f"[telegram] {chat_id}: {e}", file=sys.stderr)
        return False


def telegram_send_to(chat_id: str, text: str) -> bool:
    """Отправить текст в чат. True — доставлено целиком.

    Длинный текст идёт частями по 4000 символов. Если сбой случился посередине,
    недоставленный хвост остаётся в `_LAST_TAIL[chat_id]`: в очередь кладётся
    только хвост, поэтому уже доставленное не дублируется.
    """
    parts = [text[i:i + 4000] for i in range(0, len(text), 4000)] or ['']
    for idx, part in enumerate(parts):
        if not _telegram_post_message(chat_id, part):
            _LAST_TAIL[chat_id] = ''.join(parts[idx:])
            return False
    _LAST_TAIL.pop(chat_id, None)
    return True


def _tail_of(chat_id: str) -> str:
    """Недоставленный хвост последней отправки в чат ('' — всё доставлено)."""
    return _LAST_TAIL.pop(chat_id, '')


def telegram_send(text: str, photo_path: Path | None = None, caption: str = "", kind: str = "text") -> bool:
    """Отправить текст всем получателям.

    Успех считается по каждому чату отдельно: чату, где отправка не удалась,
    текст кладётся в очередь с его chat_id — докачается, когда Telegram доступен.
    """
    if not TELEGRAM_BOT_TOKEN:
        print("[telegram] не задан TELEGRAM_BOT_TOKEN — сообщение ждёт в очереди", file=sys.stderr)
        queue_missed_text(text, kind=kind, chats=[])
        return False
    recs = get_recipients()
    if not recs:
        # Получателей ещё нет — сообщение не должно потеряться: ждёт в очереди.
        queue_missed_text(text, kind=kind, chats=[])
        print("[telegram] нет получателей — сообщение сохранено в очереди", file=sys.stderr)
        _set_delivery([], [])
        return False
    if photo_path and photo_path.exists():
        data = photo_path.read_bytes()
        return telegram_send_photo(data, caption, kind=kind)
    delivered, failed = [], []
    for cid in recs:
        if telegram_send_to(cid, text):
            delivered.append(cid)
        else:
            failed.append(cid)
    _set_delivery(delivered, failed)
    for cid in failed:
        # В очередь — только недоставленный хвост: доставленное не повторяем.
        queue_missed_text(_tail_of(cid) or text, kind=kind, chats=[cid])
    return bool(delivered)


def queue_missed_text(text: str, kind: str = 'text', chats: list | None = None) -> None:
    """Сохранить текст в очереди недоставленных.

    chats=None — всем текущим получателям; chats=[] — широковещательно
    (получателей пока нет): сообщение дойдёт, как только подписчики появятся.
    """
    targets = list(chats) if chats is not None else get_recipients()
    if not targets:
        targets = [BROADCAST_CHAT]
    for cid in targets:
        try:
            STORE.enqueue_text(None, cid, text, kind)
        except sqlite3.Error as error:
            print(f'[outbox] сообщение не сохранено: {error}', file=sys.stderr)


_outbox_lock = threading.Lock()


def _outbox_body(rows: list) -> str:
    """Текст накопившихся сообщений для одного чата."""
    kinds = {row[4] for row in rows}
    if kinds and kinds <= {'entry_without_photo'}:
        return ('📥 <b>Telegram был недоступен. Накопилось проездов: '
                f'{len(rows)}</b>\n\n' + '\n'.join(f'• {row[3]}' for row in rows) +
                '\n\nФотографии этих проездов недоступны: в момент сбоя они не сохранялись.')
    parts = [f'📥 <b>Telegram был недоступен. Накопилось сообщений: {len(rows)}</b>']
    parts.extend(row[3] for row in rows)
    return '\n\n'.join(parts)


def flush_missed_messages() -> bool:
    """Доставить накопленные сообщения. Замок не даёт двум потокам отправить одно и то же."""
    with _outbox_lock:
        return _flush_missed_messages()


def _mark_failed(row_id, error) -> str:
    """Отметить неудачную попытку; вернуть новый статус ('pending' или 'dead')."""
    try:
        status = STORE.mark_failed(row_id, error)
    except sqlite3.Error as failure:  # pragma: no cover
        print(f'[outbox] не удалось записать попытку: {failure}', file=sys.stderr)
        return 'pending'
    return status or 'pending'


def _report_dead(row_id, row, chat_id: str) -> None:
    """Попытки исчерпаны: запись в журнал событий (/events) и в stderr."""
    try:
        attempts = STORE.attempts_of(row_id)
    except Exception:  # pragma: no cover - хранилище без такого метода
        attempts = row[5] if len(row) > 5 else OUTBOX_MAX_ATTEMPTS
    body = row[3] if len(row) > 3 else ''
    text = (f'Сообщение не доставлено в чат {chat_id}: попыток {attempts}. '
            f'Текст: {str(body)[:120]}')
    print(f'[outbox] {text}', file=sys.stderr)
    EVENTS.add('telegram_dead', text, now_local().isoformat())


def _flush_broadcast(rows: list) -> bool:
    """Раздать широковещательное сообщение текущим получателям.

    Каждому получателю — своя копия; чату, где отправка не удалась, сообщение
    остаётся в очереди отдельной записью с его chat_id.
    """
    recipients = get_recipients()
    if not recipients:
        print('[outbox] получателей нет — сообщение ждёт в очереди', file=sys.stderr)
        return False
    body = _outbox_body(rows)
    kind = rows[0][4] if len(rows[0]) > 4 else 'text'
    delivered = False
    for cid in recipients:
        if telegram_send_to(cid, body):
            delivered = True
        else:
            # В очередь — только недоставленный хвост: доставленное не повторяем.
            queue_missed_text(_tail_of(cid) or body, kind=kind, chats=[cid])
    for row in rows:
        STORE.mark_sent(row[0])
    return delivered


def _flush_missed_messages() -> bool:
    """Доставка очереди по чатам: успех считается по каждому чату отдельно."""
    grouped = STORE.pending_grouped()
    if not grouped or not TELEGRAM_BOT_TOKEN:
        return False
    delivered = False
    for chat_id, rows in grouped.items():
        if not str(chat_id):
            delivered = _flush_broadcast(rows) or delivered
            continue
        body = _outbox_body(rows)
        if telegram_send_to(chat_id, body):
            for row in rows:
                STORE.mark_sent(row[0])
            delivered = True
        else:
            tail = _tail_of(chat_id)
            if tail:
                # Часть длинного сообщения дошла — её не повторяем, хвост ждёт отдельно.
                for row in rows:
                    STORE.mark_partial(row[0])
                queue_missed_text(tail, kind=rows[0][4] if len(rows[0]) > 4 else 'text',
                                  chats=[chat_id])
                continue
            for row in rows:
                if _mark_failed(row[0], 'Telegram недоступен') == 'dead':
                    _report_dead(row[0], row, chat_id)
    return delivered


def outbox_worker(stop_event: threading.Event) -> None:
    """Отдельный поток доставки очереди.

    Работает независимо от камеры: камера может быть недоступна часами,
    а накопленные сообщения всё равно уходят, как только Telegram доступен.
    Повторы идут с экспоненциальной паузой (next_attempt_at в базе).
    """
    while not stop_event.is_set():
        try:
            flush_missed_messages()
        except Exception as error:  # pragma: no cover
            print(f'[outbox] ошибка доставки: {error}', file=sys.stderr)
        stop_event.wait(OUTBOX_FLUSH_INTERVAL)


def telegram_send_media_group_to(chat_id: str, items: list) -> bool:
    """Один sendMediaGroup в конкретный чат."""
    try:
        files, data = [], []
        for idx, (img, cap) in enumerate(items):
            files.append((str(idx), (f"{idx}.jpg", io.BytesIO(img), "image/jpeg")))
            first = {"caption": cap[:1024], "parse_mode": "HTML"} if idx == 0 else {}
            data.append({"type": "photo", "media": f"attach://{idx}", **first})
        r = requests.post(f"{_tg_base()}/sendMediaGroup",
                          data={"chat_id": chat_id, "media": json.dumps(data)}, files=files, timeout=60)
        return r.status_code == 200
    except Exception as e:  # pragma: no cover
        print(f"[telegram] mediaGroup {chat_id}: {e}", file=sys.stderr)
        return False


def telegram_send_media_group(items: list, kind: str = 'entry_without_photo') -> bool:
    """items: [(image_bytes, caption), ...] — одним sendMediaGroup в каждый чат (макс 10 фото).

    Чату, где альбом не доставился, текст уходит в очередь с его chat_id:
    сообщение докачается позже отдельной записью очереди.
    """
    if not items:
        return False
    if not TELEGRAM_BOT_TOKEN:
        print("[telegram] не задан TELEGRAM_BOT_TOKEN — подпись ждёт в очереди", file=sys.stderr)
        queue_missed_text(items[0][1] if items else '', kind=kind, chats=[])
        return False
    items = items[:10]
    recs = get_recipients()
    caption = items[0][1] if items else ''
    if not recs:
        queue_missed_text(caption, kind=kind, chats=[])
        print("[telegram] нет получателей — подпись сохранена в очереди", file=sys.stderr)
        _set_delivery([], [])
        return False
    delivered, failed = [], []
    for cid in recs:
        if telegram_send_media_group_to(cid, items):
            delivered.append(cid)
        else:
            failed.append(cid)
    _set_delivery(delivered, failed)
    for cid in failed:
        queue_missed_text(caption, kind=kind, chats=[cid])
    return bool(delivered)


# ---------------------------------------------------------------------------
# Каждый въезд -> Telegram (фото + время)
# ---------------------------------------------------------------------------

import html as _html


def _esc(s) -> str:
    return _html.escape(str(s), quote=False)


def display_plate(value) -> str:
    """Format Uzbek plate text for people, keeping unknown formats unchanged."""
    raw = re.sub(r'\s+', '', str(value or '')).upper()
    match = re.fullmatch(r'(\d{2})(\d{4})([A-Z]{2})', raw)
    if match:
        return f'{match.group(1)} {match.group(2)} {match.group(3)}'
    match = re.fullmatch(r'(\d{2})([A-Z])(\d{3})([A-Z]{2})', raw)
    if match:
        return f'{match.group(1)} {match.group(2)} {match.group(3)} {match.group(4)}'
    return str(value or '—')


def build_entry_caption(rec: dict) -> str:
    """Компактный HTML-текст для альбома (caption первого фото, макс 1024 символа)."""
    plate = _esc(display_plate(rec.get("plate", "—")))
    iso = rec.get("iso", "")
    date = iso[:10] if len(iso) >= 10 else ""
    direction = rec.get("direction", "")
    dir_h = " · Приближается" if direction and is_approach_value(str(direction)) else ""
    conf = rec.get("confidence", "")
    direction_text = {'Approach': 'ENTRY', 'Leave': 'EXIT', '': 'UNKNOWN'}[direction_kind(direction)]
    lines = [
        f"🚧 <b>{_esc(CAMERA_NAME)}</b>",
        f"🔢 <b>{plate}</b>",
        f"🕐 {_esc(date)} {_esc(rec.get('time', ''))}",
        f"↔️ {direction_text}",
    ]
    color = rec.get("color", "")
    if color not in ("", "Unknown"):
        lines.append(f"🎨 {_esc(color)}")
    return "\n".join(lines)


def telegram_send_photo_to(chat_id: str, image_bytes: bytes | None, caption: str) -> bool:
    """Одно фото (или текст, если снимка нет) в конкретный чат."""
    try:
        if image_bytes:
            r = requests.post(
                f"{_tg_base()}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption[:1024], "parse_mode": "HTML"},
                files={"photo": ("event.jpg", io.BytesIO(image_bytes), "image/jpeg")},
                timeout=30)
        else:
            r = requests.post(
                f"{_tg_base()}/sendMessage",
                json={"chat_id": chat_id, "text": caption, "parse_mode": "HTML",
                      "disable_web_page_preview": True}, timeout=30)
        return r.status_code == 200
    except Exception as e:  # pragma: no cover
        print(f"[telegram] фото {chat_id}: {e}", file=sys.stderr)
        return False


def telegram_send_photo(image_bytes: bytes | None, caption: str, kind: str = 'entry_without_photo') -> bool:
    """Отправить снимок всем получателям; недоставленным чатам текст уходит в очередь."""
    if not TELEGRAM_BOT_TOKEN:
        print("[telegram] не задан TELEGRAM_BOT_TOKEN — подпись ждёт в очереди", file=sys.stderr)
        queue_missed_text(caption, kind=kind, chats=[])
        return False
    recs = get_recipients()
    if not recs:
        queue_missed_text(caption, kind=kind, chats=[])
        print("[telegram] нет получателей — подпись сохранена в очереди", file=sys.stderr)
        _set_delivery([], [])
        return False
    delivered, failed = [], []
    for cid in recs:
        if telegram_send_photo_to(cid, image_bytes, caption):
            delivered.append(cid)
        else:
            failed.append(cid)
    _set_delivery(delivered, failed)
    for cid in failed:
        queue_missed_text(caption, kind=kind, chats=[cid])
    return bool(delivered)


def send_entry_notification(rec: dict, images, dry_run: bool = False) -> None:
    """Оба снимка — одним сообщением (альбом). images: bytes | list[bytes] | None."""
    if images is None:
        image_list = []
    elif isinstance(images, (bytes, bytearray)):
        image_list = [bytes(images)]
    else:
        image_list = [bytes(x) for x in images if x]
    caption = build_entry_caption(rec)
    print(f"[entry] photos={len(image_list)}\n{caption}", file=sys.stderr)
    if dry_run:
        print(f"[dry-run] альбом из {len(image_list)} фото — не отправляю", file=sys.stderr)
        return
    if not SEND_EACH_ENTRY:
        return
    send_started = time.monotonic()
    seq_before = _DELIVERY_SEQ['n']
    if len(image_list) >= 2:
        ok = telegram_send_media_group([(image_list[0], caption)] + [(im, "") for im in image_list[1:]])
    elif len(image_list) == 1:
        ok = telegram_send_photo(image_list[0], caption)
    else:
        ok = telegram_send(caption, kind='entry_without_photo')
    delivered_at = now_local().isoformat()
    # Итог по чатам берём только если рассылка действительно была сейчас.
    if _LAST_DELIVERY.get('seq', 0) != seq_before:
        good = len(_LAST_DELIVERY.get('delivered') or [])
        bad = len(_LAST_DELIVERY.get('failed') or [])
    else:
        good, bad = (1, 0) if ok else (0, 1)
    RUNTIME['telegram_delivery'] = {'ok': ok, 'delivered': good, 'failed': bad, 'at': delivered_at}
    if ok and bad:
        EVENTS.add('telegram_partial',
                   f'Доставлено чатам: {good}, в очереди (Telegram недоступен): {bad}', delivered_at)
    elif ok:
        EVENTS.add('telegram_ok', 'Сообщение отправлено', delivered_at)
    else:
        EVENTS.add('telegram_error', 'Ошибка отправки Telegram', delivered_at)
    RUNTIME['telegram_message'] = ('последнее уведомление доставлено хотя бы в один разрешённый чат.' if ok else 'не удалось доставить уведомление (в очередь). Проверьте интернет, токен и ID чата.')
    elapsed = time.monotonic() - send_started
    RUNTIME['telegram_message'] += f' Отправка заняла {elapsed:.1f} с.'
    print(f'[delivery] started={rec.get("iso", "")} duration_seconds={elapsed:.3f} success={ok}', file=sys.stderr)
    print(f"[telegram] въезд отправлен: {ok}", file=sys.stderr)
    # Недоставленные чаты уже получили текст в очередь внутри telegram_send_* — здесь дублировать нельзя.


# ---------------------------------------------------------------------------
# Подписка: getUpdates (/start /stop /report)
# ---------------------------------------------------------------------------

def _allowed(chat_id: str) -> bool:
    return str(chat_id) in {x.strip() for x in ALLOWED_CHAT_IDS.split(",") if x.strip()}


def poll_telegram_updates(state: dict, stop_event=None) -> None:
    """Long-poll getUpdates: /start подписка, /stop отписка, /report отчёт дня."""
    if not TELEGRAM_BOT_TOKEN:
        return
    offset = 0
    while stop_event is None or not stop_event.is_set():
        try:
            r = requests.get(f"{_tg_base()}/getUpdates",
                             params={"timeout": 30, "offset": offset}, timeout=40)
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                cmd = (msg.get("text") or "").strip()
                if not chat_id or not _allowed(chat_id):
                    continue
                if cmd == "/start":
                    subs = load_subscribers()
                    if chat_id not in subs:
                        subs.append(chat_id)
                        save_subscribers(subs)
                    telegram_send_to(chat_id, "✅ Уведомления включены. Я буду присылать новые проезды в этот чат.\n\n/report — отчёт за сегодня\n/stop — выключить уведомления")
                elif cmd == "/stop":
                    save_subscribers([x for x in load_subscribers() if x != chat_id])
                    telegram_send_to(chat_id, "🔕 Уведомления выключены. Чтобы снова получать сообщения, отправьте /start")
                elif cmd == "/report":
                    telegram_send_to(chat_id, build_report(state))
                elif cmd.startswith("/"):
                    telegram_send_to(chat_id, "Я понимаю три команды:\n/start — включить уведомления\n/stop — выключить уведомления\n/report — получить отчёт за сегодня")
        except Exception as e:  # pragma: no cover
            print(f"[telegram] getUpdates: {e}", file=sys.stderr)
            if stop_event is not None:
                stop_event.wait(5)
            else:
                time.sleep(5)


# ---------------------------------------------------------------------------
# Парсинг Dahua multipart (snapManager)
# ---------------------------------------------------------------------------

def parse_kv_text(text: str) -> dict:
    """Строка вида 'Events[0].TrafficCar.PlateNumber=ABC123' -> dict."""
    out: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if "=" in line and not line.lower().startswith("heartbeat"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def extract_events(kv: dict) -> dict:
    """Сгруппировать Events[N].* по N: {0: {...}, 1: {...}}."""
    groups: dict = defaultdict(dict)
    for key, val in kv.items():
        m = re.match(r"Events\[(\d+)\]\.(.+)", key)
        if m:
            groups[int(m.group(1))][m.group(2)] = val
    return dict(groups)


def parse_boundary(content_type: str) -> bytes:
    m = re.search(r"boundary=([^;]+)", content_type)
    if not m:
        raise ValueError(f"Нет boundary в Content-Type: {content_type}")
    return m.group(1).strip().strip('"').encode()


class MultipartParser:
    """Инкрементальный парсер multipart/x-mixed-replace (server push)."""

    def __init__(self, boundary: bytes):
        self.marker = b"--" + boundary
        self.buf = b""

    def feed(self, data: bytes) -> list:
        self.buf += data
        parts = []
        while True:
            idx = self.buf.find(self.marker)
            if idx < 0:
                # Keep a tail that could be a split boundary
                if len(self.buf) > len(self.marker) - 1:
                    self.buf = self.buf[-(len(self.marker) - 1):]
                break
            self.buf = self.buf[idx:]  # drop preamble bytes
            raw = self.buf[len(self.marker):]
            if raw.startswith(b"--"):  # end of stream
                self.buf = b""
                break
            if raw.startswith(b"\r\n"):
                rest = raw[2:]
            elif raw.startswith(b"\n"):
                rest = raw[1:]
            elif len(raw) < 2:
                break  # CRLF ещё не пришёл, ждём (self.buf уже = marker+raw)
            else:
                break
            head_end = rest.find(b"\r\n\r\n")
            sep_len = 4
            if head_end < 0:
                head_end = rest.find(b"\n\n")
                sep_len = 2
            if head_end < 0:
                self.buf = self.marker + raw
                break  # headers incomplete, wait for more data
            headers = {}
            for line in rest[:head_end].decode("utf-8", "replace").splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            body_start = head_end + sep_len
            clen = headers.get("content-length")
            if clen is not None:
                body_len = int(clen)
                if len(rest) < body_start + body_len:
                    self.buf = self.marker + raw
                    break  # body incomplete
                body = rest[body_start:body_start + body_len]
                self.buf = rest[body_start + body_len:]
            else:
                nxt = rest.find(self.marker, body_start)
                if nxt < 0:
                    self.buf = self.marker + raw
                    break
                body = rest[body_start:nxt].rstrip(b"\r\n")
                self.buf = rest[nxt:]
            parts.append((headers, body))
        return parts


# ---------------------------------------------------------------------------
# Обработка событий
# ---------------------------------------------------------------------------

def plate_of(ev: dict) -> str:
    return (ev.get("TrafficCar.PlateNumber") or ev.get("TrafficCar.PlateNumber".lower()) or "").strip()


APPROACH_VALUES = {"approach", "approaching", "приближается", "приближение", "обverse", "obverse",
                   "in", "incoming", "въезд", "входящий", "entry", "enter", "0", "head"}
LEAVE_VALUES = {"leave", "leaving", "удаляется", "удаление", "reverse", "out", "outgoing",
                "выезд", "исходящий", "exit", "1", "tail"}
DIRECTION_KEYS = ("JunctionDirection", "VehicleDirection", "Direction", "TrafficCar.Direction", "Vehicle.Direction",
                  "SnapInfo.Direction", "DrivingDirection", "TrafficCar.DrivingDirection",
                  "Vehicle.DrivingDirection", "CommInfo.Direction")


def get_direction_value(ev: dict) -> tuple:
    """Найти направление в событии. Возвращает (значение, ключ)."""
    for key in DIRECTION_KEYS:
        if ev.get(key) not in ("", None):
            return str(ev[key]), key
    # TrafficFlowStat-style: DrivingDirection[0] лежит в DrivingDirection как строка-массив
    for key, val in ev.items():
        if "direction" in key.lower() and val:
            return str(val), key
    return "", ""


def is_approach_value(val: str) -> bool:
    low = (val or "").strip().lower()
    if low in LEAVE_VALUES or any(v in low for v in LEAVE_VALUES if len(v) > 3):
        return False
    return low in APPROACH_VALUES or any(v in low for v in APPROACH_VALUES if len(v) > 3)


def camera_group_id(ev: dict) -> str:
    """GroupID прошивки — хранится отдельной колонкой, в ключ не входит."""
    return str(ev.get("GroupID", "") or "").strip()


def is_countable(ev: dict) -> tuple:
    """Возвращает (считать ли, прошивный ключ события).

    Прошивный ключ не является признаком дубля: GroupID переиспользуется.
    Он только сохраняется в source_key для диагностики.
    """
    code = ev.get("EventBaseInfo.Code", "")
    if code != "TrafficJunction":
        return False, ""
    if ALLOWED_LANES:
        lanes = {x.strip() for x in ALLOWED_LANES.split(",") if x.strip()}
        if ev.get("Lane", "") not in lanes:
            return False, ""
    if not direction_allowed(get_direction_value(ev)[0]):
        return False, ""
    gid = ev.get("GroupID", "")
    idx = ev.get("IndexInGroup", "")
    plate = plate_of(ev)
    # GroupID alone is not unique on this firmware.
    key = f"{gid}:{idx}:{plate}:{ev.get('Lane','')}"
    return True, key


def direction_kind(value):
    value = str(value or '').strip().lower()
    if value in APPROACH_VALUES:
        return 'Approach'
    if value in LEAVE_VALUES:
        return 'Leave'
    return ''


def direction_allowed(value):
    return not DIRECTION or direction_kind(value).lower() == DIRECTION.lower()


def direction_label(value):
    return {'Approach': 'Въезд', 'Leave': 'Выезд', '': 'не определено'}[direction_kind(value)]


def store_event(rec: dict, key: str, source_key: str | None = None) -> int | None:
    """Записать проезд в SQLite по готовому ключу события."""
    try:
        return STORE.add_event(key, rec, source_key)
    except sqlite3.Error as error:
        print(f'[storage] проезд не сохранён: {error}', file=sys.stderr)
        return None


def store_passage(rec: dict, source_key: str | None = None) -> int | None:
    """Сохранить проезд в SQLite — единый источник истины для всех проездов.

    Ключ события — контентный (sha256 от дня, времени, номера, полосы и
    направления), поэтому повторный проезд с переиспользованным GroupID
    получает свою строку, а тот же самый проезд — не получает.
    """
    iso = rec.get('iso', '')
    key = event_key(iso[:10], iso, rec.get('plate', ''), rec.get('lane', ''),
                    rec.get('direction', ''))
    return store_event(rec, key, source_key)


def find_archive_row(plate: str, when: datetime, window: int = 120):
    """Запись того же проезда, пришедшая ранее из архива камеры."""
    day = when.strftime('%Y-%m-%d')
    wanted = normalize_plate(plate)
    for row in STORE.day_passages(day):
        if not row.get('backfill') or normalize_plate(row.get('plate')) != wanted:
            continue
        try:
            stamp = datetime.fromisoformat(row.get('captured_at') or '')
        except (ValueError, TypeError):
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=TZ)
        if abs((when - stamp).total_seconds()) <= window:
            return row
    return None


def replace_mirror_record(state: dict, archived: dict, rec: dict) -> bool:
    """Заменить архивную запись в JSON-зеркале на live-запись того же проезда."""
    for mirror in (state.get('days') or {}).values():
        for index, old in enumerate(mirror):
            if (old.get('backfill') and old.get('plate') == rec.get('plate')
                    and old.get('iso') == archived.get('captured_at')):
                mirror[index] = rec
                return True
    return False


def save_passage(rec: dict, source_key: str | None = None, archived: dict | None = None) -> int | None:
    """Сохранить проезд в SQLite: новая строка или обновление уже записанной."""
    try:
        if archived:
            return STORE.update_event(archived['id'], rec, source_key)
        return store_passage(rec, source_key)
    except sqlite3.Error as error:
        print(f'[storage] проезд не сохранён: {error}', file=sys.stderr)
        return None


def record_from_row(row: dict) -> dict:
    """Строка БД -> запись в том же формате, что и в JSON-состоянии."""
    iso = row.get('captured_at', '') or ''
    try:
        stamp = datetime.fromisoformat(iso).strftime('%H:%M:%S')
    except (ValueError, TypeError):
        stamp = iso[11:19]
    return {
        'plate': row.get('plate', '—'),
        'time': stamp,
        'iso': iso,
        'lane': row.get('lane', ''),
        'speed': row.get('speed', ''),
        'type': row.get('plate_type', ''),
        'color': row.get('vehicle_color', ''),
        'direction': row.get('direction', ''),
        'confidence': row.get('confidence', ''),
        'backfill': bool(row.get('backfill')),
        'group_id': row.get('group_id', ''),
        'rec_no': row.get('rec_no'),
    }


def reconcile_state_with_store(state: dict) -> int:
    """Перенести в SQLite проезды, которые пока есть только в JSON-состоянии."""
    with STATE_LOCK:
        return _reconcile_state_with_store(state)


def _reconcile_state_with_store(state: dict) -> int:
    items = []
    for records in (state.get('days') or {}).values():
        for rec in records:
            iso = rec.get('iso', '')
            plate = rec.get('plate', '')
            if not iso or not plate:
                continue
            if STORE.passage_exists(iso, plate):
                continue
            key = event_key(iso[:10], iso, plate, rec.get('lane', ''), rec.get('direction', ''))
            items.append((key, rec, None))
    if not items:
        return 0
    try:
        imported = STORE.add_events(items)
    except sqlite3.Error as error:
        print(f'[storage] перенос из JSON не удался: {error}', file=sys.stderr)
        return 0
    if imported:
        print(f'[storage] перенесено из JSON в SQLite: {imported}', file=sys.stderr)
    return imported


def record_event(state: dict, ev: dict, image: bytes | None) -> dict | None:
    """Добавить событие в состояние (сериализовано: состояние общее для потоков)."""
    with STATE_LOCK:
        return _record_event(state, ev, image)


def _record_event(state: dict, ev: dict, image: bytes | None) -> dict | None:
    """Добавить событие в состояние. Возвращает запись или None (дубль/не въезд)."""
    countable, source_key = is_countable(ev)
    if not countable:
        if ev.get("EventBaseInfo.Code") == "TrafficJunction":
            dval = get_direction_value(ev)[0]
            print(f"[filter] отброшен: plate={plate_of(ev) or '—'} dir={dval or '(нет поля)'} lane={ev.get('Lane','')}", file=sys.stderr)
        return None
    now = now_local()
    # Firmware reuses GroupID (including 1048576 for manual tests).
    # Never treat its presence in the historical list as a permanent duplicate.
    plate = plate_of(ev)
    normalized = re.sub(r'\s+', '', plate).upper()
    dedup_key = json.dumps([normalized, ev.get('Lane', ''), get_direction_value(ev)[0]], ensure_ascii=False)
    fingerprint = 'photo:' + hashlib.sha256(image).hexdigest() if image else None
    if fingerprint and fingerprint in state['seen']:
        print('[filter] повторная передача того же снимка', file=sys.stderr)
        return None
    recent = state.setdefault('recent_live', {})
    cutoff = now.timestamp() - DEDUP_WINDOW_SECONDS
    recent = {k: v for k, v in recent.items() if isinstance(v, (int, float)) and v > cutoff}
    state['recent_live'] = recent
    duplicate = bool(normalized and dedup_key in recent)
    if normalized:
        recent[dedup_key] = now.timestamp()
    if fingerprint:
        state['seen'] = (state['seen'] + [fingerprint])[-5000:]
    if duplicate:
        save_state(state)
        print(f'[filter] повторное распознавание в пределах {DEDUP_WINDOW_SECONDS:g} с', file=sys.stderr)
        return None
    rec = {
        "plate": plate or "—",
        "time": now.strftime("%H:%M:%S"),
        "iso": now.isoformat(),
        "lane": ev.get("Lane", ""),
        "speed": ev.get("Speed") or ev.get("TrafficCar.Speed", ""),
        "type": ev.get("TrafficCar.PlateType", ""),
        "color": ev.get("TrafficCar.PlateColor", ""),
        "direction": get_direction_value(ev)[0],
        "confidence": (ev.get("TrafficCar.Confidence") or ev.get("Confidence")
                       or ev.get("Object.Confidence") or ev.get("Vehicle.Confidence") or ""),
        "group_id": camera_group_id(ev),
    }
    day = today_str(now)
    archived = find_archive_row(plate, now)
    if archived:
        # Тот же проезд уже пришёл из архива камеры — обновляем ту же строку.
        save_passage(rec, source_key, archived)
        if not replace_mirror_record(state, archived, rec):
            state["days"].setdefault(day, []).append(rec)
    else:
        save_passage(rec, source_key)
        state["days"].setdefault(day, []).append(rec)
    state["last_event_time"] = rec["iso"]
    state["seen"].append(passage_key(rec["iso"], rec.get("plate", ""), rec.get("lane", ""),
                                     rec.get("direction", "")))
    state["seen"] = state["seen"][-5000:]  # не разрастаться
    # Images are stored separately after event metadata is recorded.
    save_state(state)
    return rec


def build_report(state: dict, day: str | None = None) -> str:
    day = day or today_str()
    events = [record_from_row(row) for row in STORE.day_passages(day)]
    if not events:
        # Резерв: проезды, которые ещё не попали в БД (запись в БД не удалась).
        events = state.get("days", {}).get(day, [])
    total = len(events)
    plates = [e.get("plate", "—") for e in events]
    unique = len(set(plates))
    speeds = [float(e["speed"]) for e in events if str(e.get("speed", "")).replace(".", "").isdigit()]
    avg_speed = f"{sum(speeds)/len(speeds):.0f} км/ч" if speeds else "—"
    lines = [
        f"📊 <b>Проезды за {day}</b>",
        f"Всего проездов: <b>{total}</b>",
        f"Разных номеров: <b>{unique}</b>",
        f"Средняя скорость: <b>{avg_speed}</b>",
        "",
        "Номера и время:",
    ]
    for e in events:
        _c, _t = e.get("color", ""), e.get("type", "")
        _c = "" if _c in ("", "Unknown") else f" · {_c}"
        _t = "" if _t in ("", "Unknown") else f" · {_t}"
        lines.append(f"• {e.get('time','')} — {display_plate(e.get('plate','—'))}{_c}{_t}")
    if not events:
        lines.append("• проездов не зафиксировано")
    return "\n".join(lines)


def report_time_minutes() -> int:
    """REPORT_TIME (ЧЧ:ММ) — минуты от начала суток."""
    try:
        hour, _, minute = str(REPORT_TIME).partition(':')
        return int(hour) * 60 + int(minute)
    except (TypeError, ValueError):
        return 20 * 60


def reported_days(state: dict) -> set:
    """Дни, отчёт по которым уже уходил."""
    days = state.get('reported_days')
    if isinstance(days, (list, tuple, set)):
        found = {str(day) for day in days}
    else:
        found = set()
    last = str(state.get('last_report_date') or '')
    if last:
        found.add(last)
    return found


def mark_reported(state: dict, day: str) -> None:
    """Отметить день как отправленный (отметка живёт в JSON-зеркале)."""
    days = reported_days(state)
    days.add(day)
    state['reported_days'] = sorted(days)[-REPORT_KEEP_DAYS:]
    state['last_report_date'] = day


def has_passages(state: dict, day: str) -> bool:
    """Были ли проезды за этот день: база — источник истины, зеркало — запасной вариант."""
    try:
        if STORE.count_day(day):
            return True
    except (sqlite3.Error, AttributeError):
        pass
    return bool((state.get('days') or {}).get(day))


def due_report_days(state: dict, now: datetime) -> list:
    """Дни, отчёт по которым пора отправить.

    Сегодня — как только наступило REPORT_TIME, и пока не вышло окно
    REPORT_WINDOW_MINUTES (отчёт не должен уйти на следующие сутки).
    Вчера — догоняющий отчёт: если за вчера отчёт не ушёл (сервис был
    выключен в это время), он уходит сейчас.
    """
    done = reported_days(state)
    minutes = now.hour * 60 + now.minute
    target = report_time_minutes()
    days = []
    yesterday = (now - timedelta(days=1)).strftime('%Y-%m-%d')
    today = now.strftime('%Y-%m-%d')
    if yesterday not in done and has_passages(state, yesterday):
        days.append(yesterday)
    if today not in done and target <= minutes <= target + REPORT_WINDOW_MINUTES:
        days.append(today)
    return days


def send_report_for_day(state: dict, day: str, dry_run: bool = False) -> bool:
    """Отправить отчёт за день и отметить день как отправленный."""
    text = build_report(state, day)
    print(text)
    if dry_run:
        return True
    ok = telegram_send(text, kind='report')
    # Дату отмечаем всегда: неудачная отправка уже легла в очередь и дойдёт
    # позже. Повтор набил бы очередь копиями отчёта.
    mark_reported(state, day)
    save_state(state)
    print("[report] отчёт отправлен в Telegram ✅" if ok else
          "[report] отчёт не доставлен — ждёт в очереди", file=sys.stderr)
    return True


def maybe_send_report(state: dict, dry_run: bool = False, handled: set | None = None) -> bool:
    """Отправить отчёт, если он пора. Вызывает поток-планировщик отчёта.

    handled — дни, о которых отчёт уже отправлен в этом запуске (dry-run:
    отметки в состоянии нет, отчёт печатается один раз).
    """
    now = now_local()
    sent = False
    for day in due_report_days(state, now):
        if handled is not None and day in handled:
            continue
        if send_report_for_day(state, day, dry_run=dry_run):
            sent = True
        if handled is not None:
            handled.add(day)
    return sent


def report_worker(state: dict, stop_event: threading.Event, dry_run: bool = False) -> None:
    """Поток-планировщик дневного отчёта.

    Отчёт не зависит от камеры: время проверяется раз в REPORT_CHECK_INTERVAL
    секунд, а не «точное совпадение минуты» в цикле камеры. Если сервис был
    выключен в момент отчёта, отчёт уходит позже — в том числе за вчера.
    """
    handled = set()
    while not stop_event.is_set():
        try:
            maybe_send_report(state, dry_run=dry_run, handled=handled)
        except Exception as error:  # pragma: no cover
            print(f'[report] отчёт не отправлен: {error}', file=sys.stderr)
        if stop_event.wait(REPORT_CHECK_INTERVAL):
            break


# ---------------------------------------------------------------------------
# Backfill: добор пропущенного одним списком
# ---------------------------------------------------------------------------

from datetime import timezone as _tz  # noqa: E402


def _backfill_range(state: dict):
    now = now_local()
    end = now
    last = state.get("last_event_time", "")
    if last:
        try:
            start = datetime.fromisoformat(last)
            if start.tzinfo is None:
                start = start.replace(tzinfo=TZ)
        except ValueError:
            start = now - _timedelta_minutes(BACKFILL_LOOKBACK_MINUTES)
    else:
        start = now - _timedelta_minutes(BACKFILL_LOOKBACK_MINUTES)
    max_start = now - _timedelta_minutes(BACKFILL_MAX_MINUTES)
    if start < max_start:
        start = max_start
    if end <= start:
        return None, None
    return start, end


def _timedelta_minutes(m: int):
    from datetime import timedelta
    return timedelta(minutes=m)


def is_duplicate_backfill(state: dict, plate: str, when: datetime, direction: str = '') -> bool:
    """Тот же номер ±120с от уже записанного события того же дня — дубль."""
    day = when.strftime("%Y-%m-%d")
    candidates = []
    for row in STORE.day_passages(day):
        candidates.append({'plate': row.get('plate'), 'iso': row.get('captured_at', ''),
                           'direction': row.get('direction') or '', 'backfill': bool(row.get('backfill'))})
    for rec in state.get("days", {}).get(day, []):
        candidates.append({'plate': rec.get('plate'), 'iso': rec.get('iso', ''),
                           'time': rec.get('time', ''),
                           'direction': rec.get('direction') or '', 'backfill': bool(rec.get('backfill'))})
    for cand in candidates:
        # Archive entries have their own RecNo; old imported records may use
        # an incorrect timezone and must never suppress a new archive record.
        if cand['backfill']:
            continue
        if cand['plate'] != plate:
            continue
        old_direction, new_direction = direction_kind(cand['direction']), direction_kind(direction)
        if old_direction and new_direction and old_direction != new_direction:
            continue
        iso = cand['iso']
        if iso:
            try:
                t = datetime.fromisoformat(iso)
                if t.tzinfo is None:
                    t = t.replace(tzinfo=TZ)
                if abs((when - t).total_seconds()) <= 120:
                    return True
            except ValueError:
                pass
        elif cand.get('time') and cand['time'] == when.strftime("%H:%M:%S"):
            return True
    return False


def record_backfill_event(state: dict, plate: str, when: datetime, speed: str = "", image: bytes | None = None,
                        lane: str = "", direction: str = "", color: str = "", category: str = "",
                        rec_no=None) -> dict | None:
    """Добавить проезд из архива камеры (сериализовано: состояние общее для потоков)."""
    with STATE_LOCK:
        return _record_backfill_event(state, plate, when, speed, image, lane, direction, color,
                                      category, rec_no)


def _record_backfill_event(state: dict, plate: str, when: datetime, speed: str = "", image: bytes | None = None,
                            lane: str = "", direction: str = "", color: str = "", category: str = "",
                            rec_no=None) -> dict | None:
    if is_duplicate_backfill(state, plate, when, direction):
        return None
    rec = {"plate": plate or "—", "time": when.strftime("%H:%M:%S"), "iso": when.isoformat(),
           "lane": lane, "speed": speed, "type": category, "color": color,
           "direction": direction, "backfill": True}
    if rec_no not in (None, ''):
        rec["rec_no"] = int(rec_no)
    day = when.strftime("%Y-%m-%d")
    # Проезд из архива пишется в SQLite с флагом backfill=1; ключ события
    # контентный — по времени и номеру (плюс полоса и направление).
    key = event_key(day, when.isoformat(), rec["plate"], lane, direction)
    store_event(rec, key)
    state["days"].setdefault(day, []).append(rec)
    state["last_event_time"] = when.isoformat()
    # Backfill has no image payload and stores metadata only.
    return rec


def _dir_human(direction) -> str:
    """JunctionDirection / Approach-Leave -> человекочитаемо. 0=Въезд, 1=Выезд."""
    if direction in ("", None):
        return ""
    d = str(direction).strip()
    if is_approach_value(d):
        return " · Въезд"
    if d.lower() in LEAVE_VALUES or any(v in d.lower() for v in LEAVE_VALUES if len(v) > 3):
        return " · Выезд"
    return f" · напр. {d}"


def build_backfill_text(records: list, start, end) -> str:
    lines = [f"📥 <b>Проезды из архива камеры: {len(records)}</b>",
             f"Период: {start.strftime('%d.%m %H:%M')} — {end.strftime('%d.%m %H:%M')}",
             ""]
    for rec in sorted(records, key=lambda r: r.get("iso", "")):
        day = (rec.get("iso") or "")[:10]
        _c, _t = rec.get("color", ""), rec.get("type", "")
        _c = "" if _c in ("", "Unknown") else f" · {_c}"
        _t = "" if _t in ("", "Unknown") else f" · {_t}"
        dir_h = _dir_human(rec.get("direction", ""))
        lines.append(f"• {day} {rec.get('time', '')} — {display_plate(rec.get('plate', '—'))}{_c}{_t}{dir_h}")
    lines.append("")
    lines.append("Фотографии этих проездов недоступны: в момент сбоя они не сохранялись.")
    return "\n".join(lines)


def parse_record_kv(text: str) -> tuple:
    """recordFinder key=value -> (found, list[dict])."""
    found, recs = 0, {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("found="):
            try:
                found = int(line.split("=", 1)[1])
            except ValueError:
                pass
        m = re.match(r"records\[(\d+)\]\.(.+)=(.*)", line)
        if m:
            recs.setdefault(int(m.group(1)), {})[m.group(2)] = m.group(3)
    return found, [recs[k] for k in sorted(recs)]


def archive_time(timestamp: int) -> datetime:
    value = datetime.fromtimestamp(timestamp, tz=_tz.utc)
    # This camera encodes local wall-clock fields in Time, rather than UTC.
    # Explicit device setting: never infer an offset from arbitrary old records.
    if BACKFILL_TIMESTAMP_MODE == 'local':
        return value.replace(tzinfo=TZ)
    return value.astimezone(TZ)


def backfill_once(sess, state: dict, dry_run: bool = False) -> int:
    """Добор через recordFinder TrafficSnapEventInfo по RecNo (текст, без фото).

    Сетевой запрос идёт без общего замка состояния: живая камера не должна
    ждать, пока скачивается архив. Запись найденного — уже под замком.
    Возвращает число новых проездов.
    """
    with STATE_LOCK:
        last_no = int(state.get("last_rec_no", 0) or 0)
        start, end = _backfill_range(state)
    print(f"[backfill] recordFinder TrafficSnapEventInfo last_rec_no={last_no}", file=sys.stderr)
    try:
        all_recs = _fetch_archive_records(sess)
    except Exception as e:
        print(f"[backfill] ошибка recordFinder: {e}", file=sys.stderr)
        return 0
    with STATE_LOCK:
        missed, changed = _apply_archive_records(state, all_recs, start, end, last_no)
    if changed:
        save_state(state)
    if missed:
        text = build_backfill_text(missed, start or now_local(), end or now_local())
        print(text, file=sys.stderr)
        if not dry_run:
            # ОДНО сообщение-список на всех подписчиков (фото в архиве нет).
            # Если отправка не удалась — список уходит в очередь и докачается позже.
            ok = telegram_send(text, kind='backfill')
            if not ok:
                print('[backfill] список не доставлен — сохранён в очереди', file=sys.stderr)
        print(f"[backfill] добавлено: {len(missed)}", file=sys.stderr)
    elif changed:
        print("[backfill] новых въездов нет", file=sys.stderr)
    else:
        print("[backfill] пропущенных нет", file=sys.stderr)
    return len(missed)


def _fetch_archive_records(sess) -> list:
    """Скачать записи архива камеры постранично (сеть — без замка состояния)."""
    base = f"http://{DAHUA_HOST}:{DAHUA_PORT}/cgi-bin"
    all_recs, begin = [], 0
    while True:
        r = sess.get(f"{base}/recordFinder.cgi",
                     params={"action": "find", "name": "TrafficSnapEventInfo",
                             "count": "200", "condition.QueryResultBegin": begin},
                     timeout=15)
        r.raise_for_status()
        found, page = parse_record_kv(r.text)
        if not page:
            break
        all_recs.extend(page)
        begin += len(page)
        if found == 0 or len(page) < 200:
            break
    return all_recs


def _apply_archive_records(state, all_recs, start, end, last_no) -> tuple:
    """Записать найденные архивные проезды. Возвращает (новые записи, состояние изменилось)."""
    missed = []
    for r in all_recs:
        try:
            rec_no = int(r.get("RecNo", 0) or 0)
            ts = int(r.get("Time", 0) or 0)
        except ValueError:
            continue
        if rec_no <= last_no:
            continue
        source = (r.get("SnapSource", "") or "").lower()
        if ("force" in source or "manual" in source) and not INCLUDE_MANUAL_SNAPSHOTS:
            state["last_rec_no"] = rec_no
            continue
        if ts <= 0:
            state["last_rec_no"] = rec_no
            continue
        when = archive_time(ts)
        if not direction_allowed(r.get('JunctionDirection', '')):
            state['last_rec_no'] = rec_no
            continue
        if not last_no and start and when < start:
            state["last_rec_no"] = rec_no
            continue  # первый запуск — только за lookback-окно
        rec = record_backfill_event(state, r.get("PlateNumber", ""), when,
                                    speed="", lane=r.get("Lane", ""),
                                    direction=r.get("JunctionDirection", ""),
                                    color=r.get("VehicleColor", ""), category=r.get("Category", ""),
                                    rec_no=rec_no)
        state["last_rec_no"] = rec_no
        if rec:
            missed.append(rec)
    changed = int(state.get("last_rec_no", 0) or 0) != last_no
    return missed, changed


# ---------------------------------------------------------------------------
# Подключение к Dahua
# ---------------------------------------------------------------------------

def check_connection() -> dict:
    """Проверка: камера отвечает, Telegram reachable. Возвращает словарь."""
    sess = requests.Session()
    sess.auth = HTTPDigestAuth(DAHUA_USERNAME, DAHUA_PASSWORD)
    base = f"http://{DAHUA_HOST}:{DAHUA_PORT}/cgi-bin"
    out = {}
    r = sess.get(f"{base}/magicBox.cgi", params={"action": "getDeviceType"}, timeout=10)
    r.raise_for_status()
    out["device"] = r.text.strip()
    if TELEGRAM_BOT_TOKEN:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe", timeout=10)
        out["telegram_bot"] = r.json().get("result", {}).get("username", "?") if r.ok else f"HTTP {r.status_code}"
    telegram_send("✅ ANPR бот подключён. Проверка: " + out.get("device", ""))
    return out


def flush_pending_events(state: dict, events: dict, images: list, dry_run: bool = False) -> None:
    """Отправить накопленные события с их снимками (альбомом). Снимки общие для группы."""
    for _, ev in events.items():
        first = images[0] if images else None
        rec = record_event(state, ev, first)
        if rec:
            if not dry_run:
                EVENTS.add('vehicle', 'Получено событие камеры', now_local().isoformat())
            print(f"[event] въезд: {rec['plate']} lane={rec['lane']} "
                  f"speed={rec['speed']} time={rec['time']} dir={rec.get('direction','')} photos={len(images)}",
                  file=sys.stderr)
            prepared = prepare_event_images(ev, images)
            if prepared and not dry_run:
                PHOTOS.put(rec.get('iso'), rec.get('plate'), prepared[0])
                if SAVE_PHOTOS:
                    try:
                        folder = IMAGE_DIR if IMAGE_DIR.is_absolute() else BASE_DIR / IMAGE_DIR
                        path = save_photo(folder, rec.get('iso'), rec.get('plate'), prepared[0])
                        if path:
                            STORE.set_photo_path(rec['iso'], rec['plate'], path)
                    except (OSError, ValueError, sqlite3.Error) as error:
                        print(f'[photo] Не удалось сохранить снимок: {error}', file=sys.stderr)
                else:
                    print('[photo] Снимок на диск не сохраняется (SAVE_PHOTOS=false)', file=sys.stderr)
            if len(prepared) < 2:
                print('[images] Вырезка номера недоступна: камера не передала подходящие координаты или второе фото.', file=sys.stderr)
            send_entry_notification(rec, prepared, dry_run=dry_run)


def stream_chunks_with_ticks(response, tick_seconds=0.25):
    """Read available bytes without filling 8 KiB; wake the consumer during silence."""
    chunks = queue.Queue(maxsize=32)
    cancelled = threading.Event()

    def put(item):
        while not cancelled.is_set():
            try:
                chunks.put(item, timeout=tick_seconds)
                return
            except queue.Full:
                pass

    def reader():
        try:
            while not cancelled.is_set():
                chunk = response.raw.read1(65536, decode_content=True)
                if not chunk:
                    break
                put(chunk)
        except Exception as error:
            put(error)
        finally:
            put(None)

    threading.Thread(target=reader, daemon=True).start()
    try:
        while True:
            try:
                item = chunks.get(timeout=tick_seconds)
            except queue.Empty:
                yield b''  # independent timer tick, even with no incoming traffic
                continue
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        cancelled.set()


def consume_camera_stream(response, state, dry_run=False, image_wait=1.0, no_image_wait=3.0, stop=None):
    parser = MultipartParser(parse_boundary(response.headers.get('Content-Type', '')))
    pending_events, pending_images = {}, []
    last_activity = time.monotonic()

    def flush():
        nonlocal pending_events, pending_images
        events, images = pending_events, pending_images
        pending_events, pending_images = {}, []
        if events:
            flush_pending_events(state, events, images, dry_run=dry_run)

    chunks = stream_chunks_with_ticks(response)
    next_outbox_check = 0.0
    try:
        for chunk in chunks:
            if stop is not None and stop.is_set():
                break  # сервис останавливается — дочитывать поток не нужно
            if chunk:
                last_activity = time.monotonic()
                RUNTIME['last_camera_data'] = now_local().strftime('%d.%m.%Y %H:%M:%S')
                RUNTIME['camera_message'] = 'подключена, получаю данные.'
            for headers, body in parser.feed(chunk):
                ctype = headers.get('content-type', '')
                if 'text/plain' in ctype:
                    text = body.decode('utf-8', 'replace')
                    flush()
                    if text.strip().lower() == 'heartbeat':
                        continue
                    if DEBUG_RAW:
                        print(f'[raw text {now_local().time()}]\n{text}', file=sys.stderr)
                    pending_events = extract_events(parse_kv_text(text))
                elif 'image/jpeg' in ctype and pending_events:
                    pending_images.append(body)
            wait = image_wait if pending_images else no_image_wait
            if pending_events and time.monotonic() - last_activity >= wait:
                flush()
            if not dry_run and time.monotonic() >= next_outbox_check:
                # Очередь недоставленных проверяем не чаще раза в 5 с, а не в каждом чанке.
                # Основную доставку ведёт отдельный поток outbox_worker — она не зависит от камеры.
                next_outbox_check = time.monotonic() + 5
                flush_missed_messages()
            # Отчёт за день отправляет отдельный поток — здесь только приём событий.
    finally:
        chunks.close()
        flush()  # retain complete events when the camera disconnects


# ---------------------------------------------------------------------------
# Фоновые потоки сервиса
# ---------------------------------------------------------------------------

_backfill_lock = threading.Lock()
_stop_event = threading.Event()
_workers = {}
_workers_lock = threading.Lock()
_active_response = None
_active_response_lock = threading.Lock()


def stop_event() -> threading.Event:
    """Общий сигнал остановки для фоновых потоков."""
    return _stop_event


def request_stop() -> None:
    """Мягкая остановка: потоки заканчивают работу, соединение с камерой закрывается."""
    _stop_event.set()
    close_camera_stream()


def set_active_response(response) -> None:
    """Запомнить открытое соединение с камерой, чтобы закрыть его при остановке."""
    global _active_response
    with _active_response_lock:
        _active_response = response


def close_camera_stream() -> None:
    """Закрыть живое соединение с камерой — поток подписки проснётся сразу."""
    global _active_response
    with _active_response_lock:
        response = _active_response
        _active_response = None
    if response is None:
        return
    try:
        response.close()
    except Exception:
        pass


def start_worker(name: str, target, args=(), kwargs=None) -> threading.Thread:
    """Запустить фоновый поток сервиса. Поток с таким именем создаётся один раз.

    Переподключение камеры не должно плодить потоки: добор архива, доставка
    очереди, отчёт и обслуживание работают на весь сервис.
    """
    with _workers_lock:
        thread = _workers.get(name)
        if thread is not None and thread.is_alive():
            return thread
        thread = threading.Thread(target=target, args=args, kwargs=kwargs or {},
                                 name=f'anpr-{name}', daemon=True)
        _workers[name] = thread
        thread.start()
        return thread


def run_backfill(state: dict, dry_run: bool = False, wait_seconds: float = 0.0) -> int:
    """Добор пропущенного из архива камеры. Одновременно может идти только один сеанс.

    wait_seconds > 0 — подождать освобождения замка (ручной добор из окна управления).
    """
    if wait_seconds > 0:
        if not _backfill_lock.acquire(timeout=wait_seconds):
            print("[backfill] предыдущий добор ещё выполняется — пропускаю", file=sys.stderr)
            return 0
    elif not _backfill_lock.acquire(blocking=False):
        print("[backfill] предыдущий добор ещё выполняется — пропускаю", file=sys.stderr)
        return 0
    try:
        sess = requests.Session()
        sess.auth = HTTPDigestAuth(DAHUA_USERNAME, DAHUA_PASSWORD)
        return backfill_once(sess, state, dry_run=dry_run)
    except Exception as e:  # pragma: no cover
        print(f"[backfill] ошибка: {e}", file=sys.stderr)
        return 0
    finally:
        _backfill_lock.release()


def backfill_worker(state: dict, stop: threading.Event, dry_run: bool = False) -> None:
    """Единственный поток добора архива: сразу при старте и далее раз в BACKFILL_INTERVAL.

    Добор идёт на ОТДЕЛЬНОЙ сессии — общая ломает live-стрим. Параллельный
    сеанс невозможен: run_backfill держит _backfill_lock.
    """
    while not stop.is_set():
        run_backfill(state, dry_run=dry_run)
        # Синхронизируем БД с JSON-зеркалом (страховка от потери записи в БД).
        try:
            reconcile_state_with_store(state)
        except Exception as error:
            print(f"[backfill] сверка с базой не удалась: {error}", file=sys.stderr)
        if stop.wait(BACKFILL_INTERVAL):
            break


def cleanup_images() -> int:
    """Удалить фотографии старше IMAGE_RETENTION_DAYS дней."""
    folder = IMAGE_DIR if IMAGE_DIR.is_absolute() else BASE_DIR / IMAGE_DIR
    return cleanup_old_photos(folder, keep_days=IMAGE_RETENTION_DAYS)


def maintenance_worker(state: dict, stop: threading.Event, dry_run: bool = False) -> None:
    """Поток обслуживания: ротация фотографий и чекпоинт WAL.

    Отдельный поток: обслуживание не зависит от камеры и не мешает приёму событий.
    """
    next_photos = 0.0
    next_checkpoint = 0.0
    while not stop.is_set():
        moment = time.monotonic()
        if moment >= next_photos:
            next_photos = moment + MAINTENANCE_INTERVAL
            try:
                removed = cleanup_images()
                if removed:
                    print(f"[maintenance] удалено каталогов фотографий: {removed}", file=sys.stderr)
            except Exception as error:
                print(f"[maintenance] ротация фотографий не выполнена: {error}", file=sys.stderr)
        if moment >= next_checkpoint:
            next_checkpoint = moment + WAL_CHECKPOINT_INTERVAL
            try:
                STORE.checkpoint()
            except Exception as error:
                print(f"[maintenance] чекпоинт WAL не выполнен: {error}", file=sys.stderr)
        if stop.wait(MAINTENANCE_TICK):
            break


def subscribe_loop(state: dict, dry_run: bool = False, stop: threading.Event | None = None) -> None:
    """Вечный цикл подписки snapManager с переподключением.

    Один поток на весь сервис: добор архива, доставка очереди, дневной отчёт и
    обслуживание живут отдельными потоками и не пересоздаются при переподключении.
    """
    sess = requests.Session()
    sess.auth = HTTPDigestAuth(DAHUA_USERNAME, DAHUA_PASSWORD)
    url = f"http://{DAHUA_HOST}:{DAHUA_PORT}/cgi-bin/snapManager.cgi"
    stop = stop if stop is not None else stop_event()
    camera_connected = False
    connection_failed = False
    if BROADCAST_ENABLED and TELEGRAM_BOT_TOKEN and not dry_run:
        start_worker('telegram', poll_telegram_updates, (state, stop))
        print("[telegram] подписчики: /start /stop /report (getUpdates)", file=sys.stderr)
    if not dry_run:
        # Очередь недоставленных развозит отдельный поток: доставка не зависит от камеры.
        start_worker('outbox', outbox_worker, (stop,))
        print(f"[outbox] доставка накопленного каждые {OUTBOX_FLUSH_INTERVAL} с", file=sys.stderr)
        # Обслуживание: ротация фотографий и чекпоинт WAL.
        start_worker('maintenance', maintenance_worker, (state, stop), {'dry_run': dry_run})
        print(f"[maintenance] фотографии старше {IMAGE_RETENTION_DAYS} дней и WAL раз в "
              f"{WAL_CHECKPOINT_INTERVAL} с", file=sys.stderr)
    # Отчёт за день — отдельный поток: не ждёт событий камеры. В dry-run отчёт
    # только печатается, отправки в Telegram нет.
    start_worker('report', report_worker, (state, stop), {'dry_run': dry_run})
    print(f"[report] отчёт за день в {REPORT_TIME}, проверка раз в {REPORT_CHECK_INTERVAL} с", file=sys.stderr)
    if BACKFILL_ENABLED and not dry_run:
        # Один поток добора на весь сервис: переподключения камеры плодить потоки не должны.
        start_worker('backfill', backfill_worker, (state, stop), {'dry_run': dry_run})
        print(f"[backfill] добор архива раз в {BACKFILL_INTERVAL} с", file=sys.stderr)
    while not stop.is_set():
        try:
            params = {
                "action": "attachFileProc",
                "channel": DAHUA_CHANNEL,
                "heartbeat": HEARTBEAT,
                "Flags[0]": "Event",
                "Events": "[TrafficJunction]",
            }
            print(f"[subscribe] GET {url} channel={DAHUA_CHANNEL} ...", file=sys.stderr)
            with sess.get(url, params=params, stream=True, timeout=(10, 60)) as r:
                r.raise_for_status()
                RUNTIME['camera_message'] = 'подключена. Ожидаю проезд автомобиля.'
                if not dry_run:
                    EVENTS.add('camera_connected', 'Камера подключена', now_local().isoformat())
                camera_connected = True
                connection_failed = False
                set_active_response(r)
                try:
                    consume_camera_stream(r, state, dry_run=dry_run, stop=stop)
                finally:
                    set_active_response(None)
                RUNTIME['camera_message'] = 'соединение закрыто. Подключаюсь снова.'
                if not dry_run and camera_connected:
                    EVENTS.add('camera_disconnected', 'Соединение с камерой потеряно', now_local().isoformat())
                camera_connected = False
                connection_failed = True
        except KeyboardInterrupt:
            print("\n[subscribe] остановлено пользователем", file=sys.stderr)
            raise
        except Exception as e:
            if stop.is_set():
                break
            RUNTIME['camera_message'] = 'связь прервалась. Программа попробует подключиться снова.'
            if not dry_run and not connection_failed:
                EVENTS.add('camera_disconnected', 'Соединение с камерой потеряно' if camera_connected else 'Камера недоступна', now_local().isoformat())
            camera_connected = False
            connection_failed = True
            print(f"[subscribe] ошибка: {e}. Переподключение через {RECONNECT_DELAY}с", file=sys.stderr)
            if stop.wait(RECONNECT_DELAY):
                break


def run_parser_test() -> int:
    """Офлайн-тест: multipart по кускам, группировка событий, дедупликация."""
    boundary = b"myboundary"
    jpeg = b"\xff\xd8fakejpegdata\xff\xd9"
    text_body = (b"Events[0].EventBaseInfo.Code=TrafficJunction\n"
                 b"Events[0].EventBaseInfo.Action=Pulse\n"
                 b"Events[0].GroupID=123\nEvents[0].IndexInGroup=1\n"
                 b"Events[0].Lane=0\nEvents[0].TrafficCar.PlateNumber=ABC123\n"
                 b"Events[0].TrafficCar.PlateColor=Blue\nEvents[0].Speed=55\n")
    wire = (b"--myboundary\r\nContent-Type: text/plain\r\nContent-Length: "
            + str(len(text_body)).encode() + b"\r\n\r\n" + text_body
            + b"\r\n--myboundary\r\nContent-Type: image/jpeg\r\nContent-Length: "
            + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg
            + b"\r\n--myboundary\r\nContent-Type: text/plain\r\nContent-Length: 9\r\n\r\nHeartbeat\r\n--myboundary--\r\n")
    for chunk in (len(wire), 8192, 17, 1):
        p, parts = MultipartParser(boundary), []
        for i in range(0, len(wire), chunk):
            parts.extend(p.feed(wire[i:i + chunk]))
        assert len(parts) == 3 and parts[0][1] == text_body and parts[1][1] == jpeg \
            and parts[2][1] == b"Heartbeat", f"chunk={chunk}: {len(parts)} parts"
    ev = extract_events(parse_kv_text(text_body.decode()))[0]
    st = {"days": {}, "seen": [], "last_report_date": ""}
    old_state_file = globals()["STATE_FILE"]
    old_store = globals()["STORE"]
    test_db = BASE_DIR / "_parser_test.sqlite3"
    globals()["STATE_FILE"] = BASE_DIR / "_parser_test_state.json"
    globals()["STORE"] = Store(test_db)
    try:
        rec = record_event(st, ev, jpeg)
        assert rec and rec["plate"] == "ABC123" and rec["speed"] == "55"
        assert record_event(st, ev, jpeg) is None  # дедупликация
    finally:
        globals()["STORE"].close()
        globals()["STORE"] = old_store
        globals()["STATE_FILE"] = old_state_file
        for tmp in (BASE_DIR / "_parser_test_state.json", test_db,
                    test_db.with_name(test_db.name + "-wal"), test_db.with_name(test_db.name + "-shm")):
            try:
                tmp.unlink()
            except OSError:
                pass
    print(build_entry_caption(rec))
    print("---")
    print(build_report(st))
    print("ALL TESTS PASSED")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Dahua ANPR -> Telegram (отчёт в 20:00)")
    ap.add_argument("--check", action="store_true", help="проверить камеру и Telegram")
    ap.add_argument("--report-now", action="store_true", help="отправить отчёт за сегодня сейчас")
    ap.add_argument("--dry-run", action="store_true", help="не отправлять в Telegram, только печатать")
    ap.add_argument("--parser-test", action="store_true", help="офлайн-тест парсера multipart/дедупликации без камеры")
    ap.add_argument("--backfill-now", action="store_true", help="один раз добрать пропущенное и выйти")
    args = ap.parse_args()

    if args.parser_test:
        return run_parser_test()

    if not DAHUA_PASSWORD:
        print("❌ Не задан DAHUA_PASSWORD (в .env или окружении)", file=sys.stderr)
        return 1

    state = load_state()
    reconcile_state_with_store(state)

    if args.check:
        info = check_connection()
        print(info)
        return 0

    if args.report_now:
        text = build_report(state)
        print(text)
        if not args.dry_run:
            telegram_send(text, kind='report')
        return 0

    if args.backfill_now:
        # Через run_backfill: добор не запустится параллельно фоновому потоку.
        return 0 if run_backfill(state, dry_run=args.dry_run) >= 0 else 1

    try:
        subscribe_loop(state, dry_run=args.dry_run)
    except KeyboardInterrupt:
        print("\nОстановлено пользователем", file=sys.stderr)
    finally:
        # Мягкая остановка: потоки заканчивают работу, состояние пишется на диск.
        request_stop()
        try:
            save_state(state)
        except Exception as error:
            print(f'[shutdown] состояние не сохранено: {error}', file=sys.stderr)
        STORE.close()
    return 0


if __name__ == "__main__":
    hashlib.md5()  # держим импорт для Digest (requests использует его внутри)
    raise SystemExit(main())
