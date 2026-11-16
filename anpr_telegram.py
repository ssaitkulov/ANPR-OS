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
import re
import queue
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from requests.auth import HTTPDigestAuth

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
ALLOWED_LANES = cfg("ALLOWED_LANES", "")           # пусто = все; напр. "0,1" (в событиях Lane с 0!)
DIRECTION = cfg("DIRECTION", "Approach")        # пусто = все; Approach=Приближается, Leave=Удаляется
DEBUG_RAW = cfg("DEBUG_RAW", "false").lower() in ("1", "true", "yes")
REPORT_TIME = cfg("REPORT_TIME", "20:00")          # HH:MM
TIMEZONE = cfg("TIMEZONE", "Europe/Moscow")
TELEGRAM_BOT_TOKEN = cfg("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = cfg("TELEGRAM_CHAT_ID", "")
STATE_FILE = Path(cfg("STATE_FILE", str(BASE_DIR / "anpr_state.json")))
IMAGE_DIR = Path(cfg("IMAGE_DIR", str(BASE_DIR / "images")))
SEND_EACH_ENTRY = cfg("TELEGRAM_SEND_EACH_ENTRY", "true").lower() in ("1", "true", "yes")
SAVE_PHOTOS = cfg("SAVE_PHOTOS", "false").lower() in ("1", "true", "yes")
BACKFILL_ENABLED = cfg("BACKFILL_ENABLED", "true").lower() in ("1", "true", "yes")
BACKFILL_TIMESTAMP_MODE = cfg("BACKFILL_TIMESTAMP_MODE", "utc")
BACKFILL_LOOKBACK_MINUTES = int(cfg("BACKFILL_LOOKBACK_MINUTES", "180"))
BACKFILL_MAX_MINUTES = int(cfg("BACKFILL_MAX_MINUTES", "1440"))
BACKFILL_SEND_PHOTOS = cfg("BACKFILL_SEND_PHOTOS", "false").lower() in ("1", "true", "yes")
BROADCAST_ENABLED = cfg("BROADCAST_ENABLED", "true").lower() in ("1", "true", "yes")
SUBSCRIBERS_FILE = Path(cfg("SUBSCRIBERS_FILE", str(BASE_DIR / "subscribers.json")))
ALLOWED_CHAT_IDS = cfg("ALLOWED_CHAT_IDS", "")  # пусто = доступ запрещён
RECONNECT_DELAY = int(cfg("RECONNECT_DELAY", "10"))
HEARTBEAT = cfg("HEARTBEAT", "5")
DEDUP_WINDOW_SECONDS = max(0.0, float(cfg('DEDUP_WINDOW_SECONDS', '15')))
RUNTIME = {'camera_message': 'подключаюсь…', 'telegram_message': 'ожидаю первую отправку.', 'last_camera_data': ''}

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
    return {"days": {}, "seen": [], "last_report_date": "", "last_event_time": "", "last_rec_no": 0}


_state_save_lock = threading.Lock()


def save_state(state: dict) -> None:
    with _state_save_lock:
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


def telegram_send_to(chat_id: str, text: str) -> bool:
    try:
        for i in range(0, len(text), 4000):
            r = requests.post(f"{_tg_base()}/sendMessage",
                              json={"chat_id": chat_id, "text": text[i:i + 4000],
                                    "parse_mode": "HTML", "disable_web_page_preview": True},
                              timeout=30)
            r.raise_for_status()
        return True
    except Exception as e:  # pragma: no cover
        print(f"[telegram] {chat_id}: {e}", file=sys.stderr)
        return False


def telegram_send(text: str, photo_path: Path | None = None, caption: str = "") -> bool:
    if not TELEGRAM_BOT_TOKEN:
        print("[telegram] не задан TELEGRAM_BOT_TOKEN — отправка пропущена", file=sys.stderr)
        return False
    recs = get_recipients()
    if not recs:
        print("[telegram] нет получателей (TELEGRAM_CHAT_ID + /start подписчиков)", file=sys.stderr)
        return False
    if photo_path and photo_path.exists():
        data = photo_path.read_bytes()
        return telegram_send_photo(data, caption)
    ok = False
    for cid in recs:
        ok = telegram_send_to(cid, text) or ok
    return ok


def telegram_send_media_group(items: list) -> bool:
    """items: [(image_bytes, caption), ...] — одним sendMediaGroup в каждый чат (макс 10 фото)."""
    if not TELEGRAM_BOT_TOKEN or not items:
        return False
    items = items[:10]
    recs = get_recipients()
    if not recs:
        return False
    ok = False
    for cid in recs:
        try:
            files, data = [], []
            for idx, (img, cap) in enumerate(items):
                files.append(("photo", (f"{idx}.jpg", io.BytesIO(img), "image/jpeg")))
                first = {"caption": cap[:1024], "parse_mode": "HTML"} if idx == 0 else {}
                data.append({"type": "photo", "media": f"attach://{idx}", **first})
            r = requests.post(f"{_tg_base()}/sendMediaGroup",
                              data={"chat_id": cid, "media": json.dumps(data)}, files=files, timeout=60)
            ok = (r.status_code == 200) or ok
        except Exception as e:  # pragma: no cover
            print(f"[telegram] mediaGroup {cid}: {e}", file=sys.stderr)
    return ok


# ---------------------------------------------------------------------------
# Каждый въезд -> Telegram (фото + время)
# ---------------------------------------------------------------------------

import html as _html


def _esc(s) -> str:
    return _html.escape(str(s), quote=False)


def build_entry_caption(rec: dict) -> str:
    """Компактный HTML-текст для альбома (caption первого фото, макс 1024 символа)."""
    plate = _esc(rec.get("plate", "—"))
    iso = rec.get("iso", "")
    date = iso[:10] if len(iso) >= 10 else ""
    direction = rec.get("direction", "")
    dir_h = " · Приближается" if direction and is_approach_value(str(direction)) else ""
    conf = rec.get("confidence", "")
    lines = [
        f"🚗 <b>ВЪЕЗД · {plate}</b>",
        f"🕐 {_esc(date)} {_esc(rec.get('time', ''))} · {_esc(TIMEZONE)}",
    ]
    if conf not in ("", None):
        c = str(conf).strip()
        if c.replace(".", "", 1).replace("-", "", 1).isdigit():
            num = float(c)
            conf_h = f"{num:.0f}%" if num <= 100 else f"{num:.0f}"
        else:
            conf_h = _esc(c)
        lines.append(f"🎯 Уверенность: {_esc(conf_h)}")
    else:
        lines.append("🎯 Уверенность: —")
    if rec.get("speed") not in ("", None):
        lines.append(f"🚙 {_esc(rec.get('speed'))} км/ч")
    extra = [_esc(x) for x in (rec.get("color", ""), rec.get("type", "")) if x]
    if extra:
        lines.append("🎨 " + " · ".join(extra))
    return "\n".join(lines)


def telegram_send_photo(image_bytes: bytes | None, caption: str) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        print("[telegram] не задан TELEGRAM_BOT_TOKEN — отправка пропущена", file=sys.stderr)
        return False
    recs = get_recipients()
    if not recs:
        print("[telegram] нет получателей", file=sys.stderr)
        return False
    ok = False
    for cid in recs:
        try:
            if image_bytes:
                r = requests.post(
                    f"{_tg_base()}/sendPhoto",
                    data={"chat_id": cid, "caption": caption[:1024], "parse_mode": "HTML"},
                    files={"photo": ("event.jpg", io.BytesIO(image_bytes), "image/jpeg")},
                    timeout=30)
            else:
                r = requests.post(
                    f"{_tg_base()}/sendMessage",
                    json={"chat_id": cid, "text": caption, "parse_mode": "HTML",
                          "disable_web_page_preview": True}, timeout=30)
            ok = (r.status_code == 200) or ok
        except Exception as e:  # pragma: no cover
            print(f"[telegram] фото {cid}: {e}", file=sys.stderr)
    return ok


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
    if len(image_list) >= 2:
        ok = telegram_send_media_group([(image_list[0], caption)] + [(im, "") for im in image_list[1:]])
    elif len(image_list) == 1:
        ok = telegram_send_photo(image_list[0], caption)
    else:
        ok = telegram_send(caption)
    RUNTIME['telegram_message'] = ('последнее уведомление доставлено хотя бы в один разрешённый чат.' if ok else 'не удалось доставить уведомление. Проверьте интернет, токен и ID чата.')
    elapsed = time.monotonic() - send_started
    RUNTIME['telegram_message'] += f' Отправка заняла {elapsed:.1f} с.'
    print(f'[delivery] started={rec.get("iso", "")} duration_seconds={elapsed:.3f} success={ok}', file=sys.stderr)
    print(f"[telegram] въезд отправлен: {ok}", file=sys.stderr)


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
                    telegram_send_to(chat_id, "✅ Подписан. Команды: /report — отчёт дня, /stop — отписка.")
                elif cmd == "/stop":
                    save_subscribers([x for x in load_subscribers() if x != chat_id])
                    telegram_send_to(chat_id, "Отписан.")
                elif cmd == "/report":
                    telegram_send_to(chat_id, build_report(state))
                elif cmd.startswith("/"):
                    telegram_send_to(chat_id, "Команды: /start /stop /report")
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


def is_countable(ev: dict) -> tuple:
    """Возвращает (считать ли, ключ дедупликации)."""
    code = ev.get("EventBaseInfo.Code", "")
    if code != "TrafficJunction":
        return False, ""
    if ALLOWED_LANES:
        lanes = {x.strip() for x in ALLOWED_LANES.split(",") if x.strip()}
        if ev.get("Lane", "") not in lanes:
            return False, ""
    # Направление: Approach (Приближается) по умолчанию. Если поля нет в событии — не фильтруем.
    if DIRECTION and DIRECTION.lower() in ("approach", "приближается"):
        dval, dkey = get_direction_value(ev)
        if dval:
            if not is_approach_value(dval):
                return False, ""
    elif DIRECTION and DIRECTION.lower() in ("leave", "удаляется"):
        dval, dkey = get_direction_value(ev)
        if dval and is_approach_value(dval):
            return False, ""
    gid = ev.get("GroupID", "")
    idx = ev.get("IndexInGroup", "")
    plate = plate_of(ev)
    # GroupID alone is not unique on this firmware.
    key = f"{gid}:{idx}:{plate}:{ev.get('Lane','')}"
    return True, key


def record_event(state: dict, ev: dict, image: bytes | None) -> dict | None:
    """Добавить событие в состояние. Возвращает запись или None (дубль/не въезд)."""
    countable, key = is_countable(ev)
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
    passage_key = json.dumps([normalized, ev.get('Lane', ''), get_direction_value(ev)[0]], ensure_ascii=False)
    fingerprint = 'photo:' + hashlib.sha256(image).hexdigest() if image else None
    if fingerprint and fingerprint in state['seen']:
        print('[filter] повторная передача того же снимка', file=sys.stderr)
        return None
    recent = state.setdefault('recent_live', {})
    cutoff = now.timestamp() - DEDUP_WINDOW_SECONDS
    recent = {k: v for k, v in recent.items() if isinstance(v, (int, float)) and v > cutoff}
    state['recent_live'] = recent
    duplicate = bool(normalized and passage_key in recent)
    if normalized:
        recent[passage_key] = now.timestamp()
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
    }
    day = today_str(now)
    state["days"].setdefault(day, []).append(rec)
    state["last_event_time"] = rec["iso"]
    state["seen"].append(key)
    state["seen"] = state["seen"][-5000:]  # не разрастаться
    if SAVE_PHOTOS and image:
        try:
            IMAGE_DIR.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9]+", "_", plate or "unknown")
            (IMAGE_DIR / f"{day}_{safe}_{rec['time'].replace(':','')}.jpg").write_bytes(image)
        except Exception:
            pass
    save_state(state)
    return rec


def build_report(state: dict, day: str | None = None) -> str:
    day = day or today_str()
    events = state.get("days", {}).get(day, [])
    total = len(events)
    plates = [e.get("plate", "—") for e in events]
    unique = len(set(plates))
    speeds = [float(e["speed"]) for e in events if str(e.get("speed", "")).replace(".", "").isdigit()]
    avg_speed = f"{sum(speeds)/len(speeds):.0f} км/ч" if speeds else "—"
    lines = [
        f"🚗 <b>ANPR отчёт — {day}</b>",
        f"Камера: {DAHUA_HOST} (канал {DAHUA_CHANNEL})",
        f"━━━━━━━━━━━━━━━",
        f"Въездов за день: <b>{total}</b>",
        f"Уникальных номеров: <b>{unique}</b>",
        f"Средняя скорость: {avg_speed}",
        f"━━━━━━━━━━━━━━━",
        "Список (время — номер, авто):",
    ]
    for e in events:
        _c, _t = e.get("color", ""), e.get("type", "")
        _c = "" if _c in ("", "Unknown") else f" · {_c}"
        _t = "" if _t in ("", "Unknown") else f" · {_t}"
        lines.append(f"• {e.get('time','')} — {e.get('plate','—')}{_c}{_t}")
    if not events:
        lines.append("• въездов не зафиксировано")
    return "\n".join(lines)


def maybe_send_report(state: dict, dry_run: bool = False) -> bool:
    """Если сейчас время REPORT_TIME и отчёт сегодня ещё не уходил — отправить."""
    now = now_local()
    if now.strftime("%H:%M") != REPORT_TIME:
        return False
    day = today_str(now)
    if state.get("last_report_date") == day:
        return False
    text = build_report(state, day)
    print(text)
    if not dry_run:
        ok = telegram_send(text)
        if ok:
            state["last_report_date"] = day
            save_state(state)
            print("[report] отчёт отправлен в Telegram ✅", file=sys.stderr)
    return True


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


def is_duplicate_backfill(state: dict, plate: str, when: datetime) -> bool:
    """Тот же номер ±120с от уже записанного события того же дня — дубль."""
    day = when.strftime("%Y-%m-%d")
    for rec in state.get("days", {}).get(day, []):
        if rec.get("plate") != plate:
            continue
        iso = rec.get("iso", "")
        if iso:
            try:
                t = datetime.fromisoformat(iso)
                if t.tzinfo is None:
                    t = t.replace(tzinfo=TZ)
                if abs((when - t).total_seconds()) <= 120:
                    return True
            except ValueError:
                pass
        elif rec.get("time") == when.strftime("%H:%M:%S"):
            return True
    return False


def record_backfill_event(state: dict, plate: str, when: datetime, speed: str = "", image: bytes | None = None,
                        lane: str = "", direction: str = "", color: str = "", category: str = "") -> dict | None:
    if is_duplicate_backfill(state, plate, when):
        return None
    rec = {"plate": plate or "—", "time": when.strftime("%H:%M:%S"), "iso": when.isoformat(),
           "lane": lane, "speed": speed, "type": category, "color": color,
           "direction": direction, "backfill": True}
    day = when.strftime("%Y-%m-%d")
    state["days"].setdefault(day, []).append(rec)
    state["last_event_time"] = when.isoformat()
    if SAVE_PHOTOS and image:
        try:
            IMAGE_DIR.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9]+", "_", plate or "unknown")
            (IMAGE_DIR / f"{day}_{safe}_{rec['time'].replace(':', '')}.jpg").write_bytes(image)
        except Exception:
            pass
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
    lines = [f"📥 <b>Добор пропущенных: {len(records)}</b>",
             f"Период: {start.strftime('%d.%m %H:%M')} — {end.strftime('%d.%m %H:%M')} ({TIMEZONE})",
             "━━━━━━━━━━━━━━━"]
    for rec in sorted(records, key=lambda r: r.get("iso", "")):
        day = (rec.get("iso") or "")[:10]
        _c, _t = rec.get("color", ""), rec.get("type", "")
        _c = "" if _c in ("", "Unknown") else f" · {_c}"
        _t = "" if _t in ("", "Unknown") else f" · {_t}"
        dir_h = _dir_human(rec.get("direction", ""))
        lines.append(f"• {day} {rec.get('time', '')} — {rec.get('plate', '—')}{_c}{_t}{dir_h}")
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
    """Добор через recordFinder TrafficSnapEventInfo по RecNo (текст, без фото). Возвращает кол-во новых."""
    start, end = _backfill_range(state)
    base = f"http://{DAHUA_HOST}:{DAHUA_PORT}/cgi-bin"
    print(f"[backfill] recordFinder TrafficSnapEventInfo last_rec_no={state.get('last_rec_no', 0)}", file=sys.stderr)
    try:
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
    except Exception as e:
        print(f"[backfill] ошибка recordFinder: {e}", file=sys.stderr)
        return 0
    last_no = state.get("last_rec_no", 0) or 0
    max_no = max([int(r.get("RecNo", 0) or 0) for r in all_recs], default=last_no)
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
        if "force" in source or "manual" in source:
            state["last_rec_no"] = rec_no
            continue  # ручные тестовые снимки — не въезды
        if ts <= 0:
            state["last_rec_no"] = rec_no
            continue
        when = archive_time(ts)
        if not last_no and start and when < start:
            state["last_rec_no"] = rec_no
            continue  # первый запуск — только за lookback-окно
        rec = record_backfill_event(state, r.get("PlateNumber", ""), when,
                                    speed="", lane=r.get("Lane", ""),
                                    direction=r.get("JunctionDirection", ""),
                                    color=r.get("VehicleColor", ""), category=r.get("Category", ""))
        state["last_rec_no"] = rec_no
        if rec:
            missed.append(rec)
    if missed:
        save_state(state)
        text = build_backfill_text(missed, start or now_local(), end or now_local())
        print(text, file=sys.stderr)
        if not dry_run:
            telegram_send(text)  # ОДНО сообщение-список на всех подписчиков (фото в архиве нет)
        print(f"[backfill] добавлено: {len(missed)}", file=sys.stderr)
    elif max_no != last_no:
        save_state(state)
        print("[backfill] новых въездов нет", file=sys.stderr)
    else:
        print("[backfill] пропущенных нет", file=sys.stderr)
    return len(missed)


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
            print(f"[event] въезд: {rec['plate']} lane={rec['lane']} "
                  f"speed={rec['speed']} time={rec['time']} dir={rec.get('direction','')} photos={len(images)}",
                  file=sys.stderr)
            send_entry_notification(rec, images, dry_run=dry_run)


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


def consume_camera_stream(response, state, dry_run=False, image_wait=1.0, no_image_wait=3.0):
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
    try:
        for chunk in chunks:
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
            maybe_send_report(state, dry_run=dry_run)
    finally:
        chunks.close()
        flush()  # retain complete events when the camera disconnects


def subscribe_loop(state: dict, dry_run: bool = False) -> None:
    """Вечный цикл подписки snapManager с переподключением + backfill на (ре)старте."""
    import threading
    sess = requests.Session()
    sess.auth = HTTPDigestAuth(DAHUA_USERNAME, DAHUA_PASSWORD)
    url = f"http://{DAHUA_HOST}:{DAHUA_PORT}/cgi-bin/snapManager.cgi"
    stop = threading.Event()
    if BROADCAST_ENABLED and TELEGRAM_BOT_TOKEN and not dry_run:
        threading.Thread(target=poll_telegram_updates, args=(state, stop), daemon=True).start()
        print("[telegram] подписчики: /start /stop /report (getUpdates)", file=sys.stderr)
    if BACKFILL_ENABLED and not dry_run:
        # Страховка: даже если live-молчит (прошивка шлёт heartbeat редко), добор каждые 60с.
        def _periodic_backfill():
            while not stop.is_set():
                time.sleep(60)
                try:
                    s3 = requests.Session()
                    s3.auth = HTTPDigestAuth(DAHUA_USERNAME, DAHUA_PASSWORD)
                    backfill_once(s3, state, dry_run=dry_run)
                except Exception as e:
                    print(f"[backfill-periodic] ошибка: {e}", file=sys.stderr)
        threading.Thread(target=_periodic_backfill, daemon=True).start()
    while True:
        try:
            if BACKFILL_ENABLED:
                # Backfill в фоне на ОТДЕЛЬНОЙ сессии — shared Session ломает live-стрим.
                def _bf():
                    s2 = requests.Session()
                    s2.auth = HTTPDigestAuth(DAHUA_USERNAME, DAHUA_PASSWORD)
                    backfill_once(s2, state, dry_run=dry_run)
                threading.Thread(target=_bf, daemon=True).start()
                print("[backfill] добор пропущенного в фоне, live стартует сразу", file=sys.stderr)
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
                consume_camera_stream(r, state, dry_run=dry_run)
                RUNTIME['camera_message'] = 'соединение закрыто. Подключаюсь снова.'
        except KeyboardInterrupt:
            print("\n[subscribe] остановлено пользователем", file=sys.stderr)
            raise
        except Exception as e:
            RUNTIME['camera_message'] = 'связь прервалась. Программа попробует подключиться снова.'
            print(f"[subscribe] ошибка: {e}. Переподключение через {RECONNECT_DELAY}с", file=sys.stderr)
            time.sleep(RECONNECT_DELAY)


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
    globals()["STATE_FILE"] = BASE_DIR / "_parser_test_state.json"
    try:
        rec = record_event(st, ev, jpeg)
        assert rec and rec["plate"] == "ABC123" and rec["speed"] == "55"
        assert record_event(st, ev, jpeg) is None  # дедупликация
    finally:
        globals()["STATE_FILE"] = old_state_file
        try:
            Path(BASE_DIR / "_parser_test_state.json").unlink()
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

    if args.check:
        info = check_connection()
        print(info)
        return 0

    if args.report_now:
        text = build_report(state)
        print(text)
        if not args.dry_run:
            telegram_send(text)
        return 0

    if args.backfill_now:
        sess = requests.Session()
        sess.auth = HTTPDigestAuth(DAHUA_USERNAME, DAHUA_PASSWORD)
        return 0 if backfill_once(sess, state, dry_run=args.dry_run) >= 0 else 1

    subscribe_loop(state, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    hashlib.md5()  # держим импорт для Digest (requests использует его внутри)
    raise SystemExit(main())
