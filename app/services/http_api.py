#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Локальный сервис камеры. Запуск и остановка — через run.bat. Порт 18749."""
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from app.services import logs as service_logs

import anpr_telegram as M
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import Response

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HOST = '127.0.0.1'
PORT = 18749

state = M.load_state()
started_at = time.time()
_worker_started = False
_worker = None
_server = None   # объект uvicorn — через него выполняется мягкая остановка


def shutdown_service() -> None:
    """Мягкая остановка: потоки, состояние на диск, база.

    Вызывается при завершении lifespan: и при остановке из окна управления,
    и при остановке самого процесса.
    """
    global _worker_started
    if not _worker_started:
        return
    _worker_started = False
    M.EVENTS.add('stopped', 'Система остановлена', M.now_local().isoformat())
    print('[shutdown] останавливаю фоновые потоки…', file=sys.stderr)
    M.request_stop()
    worker = _worker
    if worker is not None:
        worker.join(10)
    try:
        M.save_state(state)
    except Exception as error:
        print(f'[shutdown] состояние не сохранено: {error}', file=sys.stderr)
    try:
        M.STORE.close()
    except Exception as error:
        print(f'[shutdown] база не закрыта: {error}', file=sys.stderr)
    print('[shutdown] остановлено', file=sys.stderr)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _worker_started, _worker
    if not _worker_started:
        M.EVENTS.add('started', 'Система запущена', M.now_local().isoformat())
        # Проезды, которые остались только в JSON, переносим в БД — источник истины один.
        M.reconcile_state_with_store(state)
        t = threading.Thread(target=M.subscribe_loop, args=(state,), kwargs={"dry_run": False},
                             name='anpr-camera', daemon=True)
        t.start()
        _worker = t
        _worker_started = True
    try:
        yield
    finally:
        shutdown_service()


app = FastAPI(title="ANPR FAYZ", version="1.0", lifespan=lifespan)


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
    passages = M.STORE.day_passages(day)
    return {"today": day, "entries": len(passages), **M.STORE.stats(),
            "outbox": M.STORE.outbox_stats(),
            "unique_plates": len({row.get("plate") for row in passages}),
            "subscribers": len(M.load_subscribers()),
            "last_event_time": state.get("last_event_time", ""),
            "last_rec_no": state.get("last_rec_no", 0),
            "uptime_sec": int(time.time() - started_at)}


@app.get("/entries/today")
def entries_today():
    day = M.today_str()
    events = [M.record_from_row(row) for row in M.STORE.day_passages(day)]
    return {"date": day, "events": events}


@app.post("/report-now")
def report_now(background_tasks: BackgroundTasks):
    text = M.build_report(state)

    def _send():
        M.telegram_send(text, kind='report')

    background_tasks.add_task(_send)
    return {"text": text}


@app.post("/backfill-now")
def backfill_now(background_tasks: BackgroundTasks):
    def _bf():
        # Через run_backfill: ручной добор не пойдёт параллельно фоновому.
        # Если плановый добор как раз идёт — ждём его конца, а не отменяем.
        M.run_backfill(state, dry_run=False, wait_seconds=30)

    background_tasks.add_task(_bf)
    return {"status": "backfill queued"}


@app.post("/shutdown")
def shutdown():
    """Мягко остановить сервис.

    Потоки дорабатывают текущую операцию, состояние пишется на диск,
    база закрывается — после этого процесс завершается сам.
    """
    if _server is None:
        raise HTTPException(status_code=503, detail="Сервис запущен без управляющего сервера")
    _server.should_exit = True
    return {"service": "anpr-fayz", "status": "stopping"}


def run() -> None:
    """Запустить HTTP-сервис.

    Сервер запускается из кода (а не через CLI), чтобы мягкая остановка
    могла попросить uvicorn завершиться штатно.
    """
    global _server
    import uvicorn
    # Журналы с ротацией — весь вывод сервиса идёт в файлы, а не в консоль.
    service_logs.install()
    config = uvicorn.Config(app, host=HOST, port=PORT,
                            log_config=service_logs.uvicorn_log_config(), access_log=False)
    server = uvicorn.Server(config)
    _server = server
    server.run()


if __name__ == "__main__":
    run()
