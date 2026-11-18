"""One local service, with explicit ownership and plain-language status."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import requests

BASE = Path(__file__).resolve().parent
PORT = 18749
URL = f'http://127.0.0.1:{PORT}'
PID_FILE = BASE / 'service.pid'
BOOT_LOG = BASE / 'service.boot.log'   # вывод процесса до запуска журналов сервиса
STOP_TIMEOUT = 20                       # сколько ждём мягкую остановку, с
BOOT_LOG_MAX_BYTES = 1024 * 1024


def health():
    with requests.Session() as session:
        session.trust_env = False
        response = session.get(URL + '/health', timeout=2)
        response.raise_for_status()
        result = response.json()
    if result.get('service') != 'anpr-fayz':
        raise ValueError('Этот порт занят другой программой.')
    return result


def start():
    try:
        return health()
    except requests.ConnectionError:
        pass
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', PORT))
        except OSError:
            raise RuntimeError('Порт занят. Другие программы не остановлены.')
    if not (BASE / '.env').exists():
        raise RuntimeError('Сначала откройте «Настройки» и сохраните данные камеры и бота.')
    python = str(Path(sys.executable).with_name('python.exe')) if os.name == 'nt' else sys.executable
    _trim_boot_log()
    # Журналы сервиса пишет сам сервис (с ротацией) — в файле остаётся только
    # вывод до старта: ошибки запуска python или uvicorn.
    with BOOT_LOG.open('ab') as out:
        process = subprocess.Popen([python, str(BASE / 'app.py')], cwd=BASE, stdout=out, stderr=out,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    PID_FILE.write_text(str(process.pid), encoding='ascii')
    for _ in range(30):
        if process.poll() is not None:
            _copy_boot_log_to_stderr()
            raise RuntimeError('Не удалось запустить программу. Подробности в журнале service.stderr.log.')
        try:
            result = health()
            if result.get('pid') == process.pid:
                return result
        except requests.ConnectionError:
            pass
        time.sleep(0.3)
    raise RuntimeError('Запуск затянулся. Проверьте состояние через несколько секунд.')


def stop():
    try:
        result = health()
    except requests.ConnectionError:
        PID_FILE.unlink(missing_ok=True)
        return
    if not PID_FILE.exists() or int(PID_FILE.read_text()) != result.get('pid'):
        raise RuntimeError('Это другой экземпляр программы. Автоматическая остановка отменена.')
    stop_gracefully(result['pid'])
    PID_FILE.unlink(missing_ok=True)


def stop_gracefully(pid) -> None:
    """Мягкая остановка: сервис сам останавливает потоки и пишет состояние на диск.

    Жёсткое завершение — только если сервис не освободил порт за STOP_TIMEOUT
    секунд (зависший поток). Обычно этого не происходит.
    """
    try:
        with requests.Session() as session:
            session.trust_env = False
            session.post(URL + '/shutdown', timeout=5)
    except requests.RequestException:
        pass
    deadline = time.time() + STOP_TIMEOUT
    while time.time() < deadline:
        try:
            health()
        except requests.ConnectionError:
            return  # сервис остановился и порт освободил
        time.sleep(0.2)
    subprocess.run(['taskkill', '/PID', str(pid), '/F'], check=True, capture_output=True,
                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)


def _trim_boot_log():
    """Журнал запуска не должен расти без предела."""
    try:
        if BOOT_LOG.exists() and BOOT_LOG.stat().st_size > BOOT_LOG_MAX_BYTES:
            BOOT_LOG.unlink()
    except OSError:
        pass


def _copy_boot_log_to_stderr():
    """Перенести вывод запуска в журнал ошибок — его читают при разборе поломок."""
    try:
        text = BOOT_LOG.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return
    if not text.strip():
        return
    try:
        with (BASE / 'service.stderr.log').open('a', encoding='utf-8') as out:
            out.write(text)
            if not text.endswith('\n'):
                out.write('\n')
    except OSError:
        pass


if __name__ == '__main__':
    start()
