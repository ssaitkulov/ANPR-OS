"""Опциональная инструментация главного потока Tk.

Включается переменной окружения ``EVRIKA_PERF_LOG=1``. В обычном режиме
`timed()` возвращает callback как есть, `heartbeat()` ничего не планирует —
накладных расходов и лишних after() нет.
"""
import os
import sys
import time

THRESHOLD_MS = 16.0
HEARTBEAT_MS = 100
LAG_MS = 50.0


def _enabled():
    return os.environ.get('EVRIKA_PERF_LOG', '') == '1'


def timed(label, callback):
    """Обернуть callback замером времени; медленные вызовы идут в stderr."""
    if not _enabled():
        return callback

    def wrapped(*args, **kwargs):
        started = time.perf_counter()
        try:
            return callback(*args, **kwargs)
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            if elapsed > THRESHOLD_MS:
                print(f'[perf] {label}: {elapsed:.1f} ms', file=sys.stderr)

    return wrapped


def heartbeat(root):
    """Запланировать замер задержки event loop (только при EVRIKA_PERF_LOG=1)."""
    if not _enabled():
        return
    state = {'expected': time.monotonic() + HEARTBEAT_MS / 1000}

    def tick():
        now = time.monotonic()
        lag = (now - state['expected']) * 1000
        if lag > LAG_MS:
            print(f'[perf] event-loop lag: {lag:.1f} ms', file=sys.stderr)
        state['expected'] = now + HEARTBEAT_MS / 1000
        try:
            root.after(HEARTBEAT_MS, tick)
        except Exception:  # окно закрыто
            return

    try:
        root.after(HEARTBEAT_MS, tick)
    except Exception:
        return
