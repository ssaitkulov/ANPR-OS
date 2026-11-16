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
    with (BASE / 'service.stdout.log').open('ab') as out, (BASE / 'service.stderr.log').open('ab') as err:
        process = subprocess.Popen([python, '-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', str(PORT), '--workers', '1'],
                                   cwd=BASE, stdout=out, stderr=err,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    PID_FILE.write_text(str(process.pid), encoding='ascii')
    for _ in range(30):
        if process.poll() is not None:
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
        return
    if not PID_FILE.exists() or int(PID_FILE.read_text()) != result.get('pid'):
        raise RuntimeError('Это другой экземпляр программы. Автоматическая остановка отменена.')
    subprocess.run(['taskkill', '/PID', str(result['pid']), '/F'], check=True, capture_output=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    PID_FILE.unlink(missing_ok=True)


if __name__ == '__main__':
    start()
