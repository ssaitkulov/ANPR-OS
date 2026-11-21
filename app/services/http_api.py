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
from app.services import audit
from app.services import auth
from app.services import instance as service_instance

import anpr_telegram as M
from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
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


def _control_token() -> str:
    """Токен управления сервисом (создаётся при первом запуске).

    Токен живёт в памяти процесса: файл с ним нужен только клиенту.
    Если файл удалили, он восстанавливается — иначе управление стало бы
    невозможным до перезапуска сервиса.
    """
    global _token
    if _token is None:
        _token = auth.load_or_create_token()
    elif not auth.token_path().exists():
        auth.write_token(_token)
    return _token


_token: str | None = None


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
    return {"service": "anpr-fayz", "pid": os.getpid(), "status": "ok" if _worker and _worker.is_alive() else "worker_stopped", "camera": M.DAHUA_HOST, "time": M.now_local().isoformat(), "started_at": started_at, "protocol": auth.PROTOCOL_VERSION, "shutdown_auth": "token", **M.snapshot_runtime()}


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
        stored = M.STORE.get_photo_path(*identity)
        path = M.photo_file(stored) if stored else None
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
    snapshot = M.snapshot_state(state)
    return {"today": day, "entries": len(passages), **M.STORE.stats(),
            "outbox": M.STORE.outbox_stats(),
            "unique_plates": len({row.get("plate") for row in passages}),
            "subscribers": len(M.load_subscribers()),
            "last_event_time": snapshot.get("last_event_time", ""),
            "last_rec_no": snapshot.get("last_rec_no", 0),
            "uptime_sec": int(time.time() - started_at)}


@app.get("/entries/today")
def entries_today():
    day = M.today_str()
    events = [M.record_from_row(row) for row in M.STORE.day_passages(day)]
    return {"date": day, "events": events}


@app.post("/report-now")
def report_now(background_tasks: BackgroundTasks):
    text = M.build_report(M.snapshot_state(state))

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
def shutdown(request: Request):
    """Мягко остановить сервис.

    Запрос принимается только с секретным токеном управления: без него
    сервис не останавливается. Отказ записывается в журнал аудита — без
    раскрытия самого токена.

    Потоки дорабатывают текущую операцию, состояние пишется на диск,
    база закрывается — после этого процесс завершается сам.
    """
    presented = request.headers.get(auth.TOKEN_HEADER, '') if request is not None else ''
    if not auth.verify(presented, expected=_control_token()):
        client = request.client.host if (request is not None and request.client) else ''
        audit.control('service.shutdown', 'denied', source='service', target_pid=os.getpid(),
                      reason='токен управления не предъявлен или неверен', client=str(client))
        raise HTTPException(status_code=401, detail="Требуется секретный токен управления")
    if _server is None:
        audit.control('service.shutdown', 'error', source='service',
                      target_pid=os.getpid(), error='сервис запущен без управляющего сервера')
        raise HTTPException(status_code=503, detail="Сервис запущен без управляющего сервера")
    audit.control('service.shutdown', 'ok', source='service', target_pid=os.getpid(),
                  client=str(request.client.host if request.client else ''))
    _server.should_exit = True
    return {"service": "anpr-fayz", "status": "stopping"}


def run() -> None:
    """Запустить HTTP-сервис.

    Сервер запускается из кода (а не через CLI), чтобы мягкая остановка
    могла попросить uvicorn завершиться штатно.
    """
    global _server
    import uvicorn
    # Один экземпляр сервиса на машину: второй не должен портить базу
    # и бороться за порт.
    guard = service_instance.ServiceInstance()
    if not guard.acquire():
        print('[start] сервис уже запущен: второй экземпляр не нужен', file=sys.stderr)
        raise SystemExit(2)
    # Токен управления создаётся при старте: файл с ним должен быть готов
    # до первого запроса остановки.
    _control_token()
    # Журналы с ротацией — весь вывод сервиса идёт в файлы, а не в консоль.
    service_logs.install()
    config = uvicorn.Config(app, host=HOST, port=PORT,
                            log_config=service_logs.uvicorn_log_config(), access_log=False)
    server = uvicorn.Server(config)
    _server = server
    try:
        server.run()
    finally:
        guard.release()


if __name__ == "__main__":
    run()
