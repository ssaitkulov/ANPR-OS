#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Локальный сервис камеры. Запуск и остановка — через run.bat. Порт 18749."""
import threading
import os
import time
from pathlib import Path

import anpr_telegram as M
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import Response

try:
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

state = M.load_state()
started_at = time.time()
_worker_started = False
_worker = None

app = FastAPI(title="ANPR FAYZ", version="1.0")


@app.on_event("startup")
def on_startup():
    global _worker_started, _worker
    if not _worker_started:
        M.EVENTS.add('started', 'Система запущена', M.now_local().isoformat())
        t = threading.Thread(target=M.subscribe_loop, args=(state,), kwargs={"dry_run": False}, daemon=True)
        t.start()
        _worker = t
        _worker_started = True


@app.get("/health")
def health():
    return {"service": "anpr-fayz", "pid": os.getpid(), "status": "ok" if _worker and _worker.is_alive() else "worker_stopped", "camera": M.DAHUA_HOST, "time": M.now_local().isoformat(), "started_at": started_at, **M.RUNTIME}


@app.get("/events")
def operational_events(after: int = 0):
    return {"service": "anpr-fayz", "pid": os.getpid(), **M.EVENTS.snapshot(max(0, after))}


@app.get("/passages")
def recent_passages():
    records = M.STORE.recent_passages()
    for record in records:
        record['plate'] = M.display_plate(record['plate'])
        record['direction'] = M.direction_label(record['direction'])
    return {"service": "anpr-fayz", "passages": records}


@app.get("/passages/{event_id}/photo")
def passage_photo(event_id: int):
    identity = M.STORE.passage_identity(event_id)
    photo = M.PHOTOS.get(*identity) if identity else None
    if photo is None and identity:
        path = M.STORE.get_photo_path(*identity)
        if path:
            try:
                photo = Path(path).read_bytes()
            except OSError:
                pass
    if photo is None:
        raise HTTPException(status_code=404, detail="Фото недоступно")
    return Response(content=photo, media_type='image/jpeg', headers={'Cache-Control': 'no-store'})


@app.get("/status")
def status():
    day = M.today_str()
    events = state.get("days", {}).get(day, [])
    return {"today": day, "entries": len(events), **M.STORE.stats(),
            "unique_plates": len({e.get("plate") for e in events}),
            "subscribers": len(M.load_subscribers()),
            "last_event_time": state.get("last_event_time", ""),
            "last_rec_no": state.get("last_rec_no", 0),
            "uptime_sec": int(time.time() - started_at)}


@app.get("/entries/today")
def entries_today():
    day = M.today_str()
    return {"date": day, "events": state.get("days", {}).get(day, [])}


@app.post("/report-now")
def report_now(background_tasks: BackgroundTasks):
    text = M.build_report(state)

    def _send():
        M.telegram_send(text)

    background_tasks.add_task(_send)
    return {"text": text}


@app.post("/backfill-now")
def backfill_now(background_tasks: BackgroundTasks):
    import requests
    from requests.auth import HTTPDigestAuth

    def _bf():
        sess = requests.Session()
        sess.auth = HTTPDigestAuth(M.DAHUA_USERNAME, M.DAHUA_PASSWORD)
        M.backfill_once(sess, state, dry_run=False)

    background_tasks.add_task(_bf)
    return {"status": "backfill queued"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=18749)
