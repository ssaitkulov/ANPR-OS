"""One local service, with explicit ownership and plain-language status."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import requests

from app.core.paths import PROJECT_ROOT, runtime_dir, env_file
from app.core import safe_mode
from app.services import audit
from app.services import auth

#: Корень проекта: журналы, pid-файл и лаунчер сервиса лежат в корне.
BASE = PROJECT_ROOT
PORT = 18749
URL = f'http://127.0.0.1:{PORT}'
STOP_TIMEOUT = 20                       # сколько ждём мягкую остановку, с
BOOT_LOG_MAX_BYTES = 1024 * 1024


def pid_file() -> Path:
    """PID-файл сервиса.

    Обычно — `service.pid` в корне проекта. В безопасном тестовом режиме
    файл уводится во временный каталог: тест не должен трогать рабочий
    PID-файл (совместимость формата — одна строка с номером процесса).
    """
    override = os.environ.get('SERVICE_PID_FILE')
    if override:
        return Path(override)
    return runtime_dir() / 'service.pid'


def boot_log() -> Path:
    """Журнал вывода процесса до старта журналов сервиса."""
    override = os.environ.get('SERVICE_BOOT_LOG')
    if override:
        return Path(override)
    return runtime_dir() / 'service.boot.log'


#: Прежние имена — для совместимости с прежним кодом и скриптами.
PID_FILE = pid_file()
BOOT_LOG = boot_log()


def health():
    """Состояние сервиса.

    В безопасном тестовом режиме сеть не трогается: запрос к рабочему
    сервису запрещён, вместо него возвращается заведомо безопасный ответ.
    """
    if safe_mode.is_active():
        return {'service': 'anpr-fayz', 'pid': None, 'status': 'offline',
                'safe_mode': True, 'camera': '', 'time': '', 'started_at': 0}
    with requests.Session() as session:
        session.trust_env = False
        response = session.get(URL + '/health', timeout=2)
        response.raise_for_status()
        result = response.json()
    if result.get('service') != 'anpr-fayz':
        raise ValueError('Этот порт занят другой программой.')
    return result


def start(source: str = 'cli'):
    """Запустить сервис.

    Операция записывается в структурированный журнал `service.control.log`:
    запрос, длительность, PID запущенного процесса и причина ошибки.
    """
    try:
        safe_mode.guard('Запуск сервиса ANPR')
    except safe_mode.SafeModeError as error:
        audit.control('service.start', 'denied', source=source, error=str(error))
        raise
    with audit.operation('service.start', source=source) as op:
        try:
            return health()
        except requests.ConnectionError:
            pass
        with socket.socket() as probe:
            try:
                probe.bind(('127.0.0.1', PORT))
            except OSError:
                raise RuntimeError('Порт занят. Другие программы не остановлены.')
        if not env_file().exists():
            raise RuntimeError('Сначала откройте «Настройки» и сохраните данные камеры и бота.')
        python = str(Path(sys.executable).with_name('python.exe')) if os.name == 'nt' else sys.executable
        _trim_boot_log()
        # Журналы сервиса пишет сам сервис (с ротацией) — в файле остаётся только
        # вывод до старта: ошибки запуска python или uvicorn.
        boot = boot_log()
        with boot.open('ab') as out:
            process = subprocess.Popen([python, str(BASE / 'app.py')], cwd=BASE, stdout=out, stderr=out,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        pid_file().write_text(str(process.pid), encoding='ascii')
        op['target_pid'] = process.pid
        for _ in range(30):
            if process.poll() is not None:
                _copy_boot_log_to_stderr()
                raise RuntimeError('Не удалось запустить программу. Подробности в журнале service.stderr.log.')
            try:
                result = health()
                if result.get('pid') == process.pid:
                    op['target_pid'] = result.get('pid')
                    return result
            except requests.ConnectionError:
                pass
            time.sleep(0.3)
        raise RuntimeError('Запуск затянулся. Проверьте состояние через несколько секунд.')


def stop(source: str = 'cli'):
    """Остановить сервис (операция пишется в журнал управления)."""
    try:
        safe_mode.guard('Остановка сервиса ANPR')
    except safe_mode.SafeModeError as error:
        audit.control('service.stop', 'denied', source=source, error=str(error))
        raise
    with audit.operation('service.stop', source=source) as op:
        try:
            result = health()
        except requests.ConnectionError:
            pid_file().unlink(missing_ok=True)
            return
        store = pid_file()
        if not store.exists() or int(store.read_text()) != result.get('pid'):
            raise RuntimeError('Это другой экземпляр программы. Автоматическая остановка отменена.')
        target = result.get('pid')
        op['target_pid'] = target
        stop_gracefully(target, source=source)
        store.unlink(missing_ok=True)


def server_capabilities() -> dict:
    """Что умеет сервер: версия протокола и требование токена.

    Ответ старого сервиса (без полей `protocol`) — это протокол 1:
    остановка без токена. Смешивать схемы нельзя, поэтому решение принимается
    по версии сервера, а не по версии клиента.
    """
    try:
        data = health()
    except Exception:
        return {'protocol': 1, 'shutdown_auth': 'none', 'reachable': False}
    try:
        protocol = int(data.get('protocol') or 1)
    except (TypeError, ValueError):
        protocol = 1
    return {'protocol': protocol, 'shutdown_auth': str(data.get('shutdown_auth') or 'none'),
            'reachable': True}


def stop_gracefully(pid, source: str = 'cli') -> None:
    """Мягкая остановка: сервис сам останавливает потоки и пишет состояние на диск.

    Жёсткое завершение — только если сервис не освободил порт за STOP_TIMEOUT
    секунд (зависший поток). Обычно этого не происходит.
    """
    safe_mode.guard('Остановка сервиса ANPR')
    capabilities = server_capabilities()
    headers = {}
    scheme = 'legacy'
    if capabilities['shutdown_auth'] == 'token':
        token = auth.read_token()
        if not token:
            raise RuntimeError('Файл токена управления недоступен: остановка отменена.')
        headers[auth.TOKEN_HEADER] = token
        scheme = 'token'
    audit.record('service.shutdown.requested', 'requested', source=source, target_pid=pid,
                 transport='http', scheme=scheme, protocol=capabilities['protocol'])
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.post(URL + '/shutdown', timeout=5, headers=headers)
            if response.status_code == 401:
                audit.record('service.shutdown.requested', 'denied', source=source,
                             target_pid=pid, reason='сервер не принял токен управления')
                raise RuntimeError('Сервис не принял токен управления: остановка отменена.')
    except requests.RequestException as error:
        audit.record('service.shutdown.requested', 'error', source=source, target_pid=pid,
                     error=str(error))
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
        boot = boot_log()
        if boot.exists() and boot.stat().st_size > BOOT_LOG_MAX_BYTES:
            boot.unlink()
    except OSError:
        pass


def _copy_boot_log_to_stderr():
    """Перенести вывод запуска в журнал ошибок — его читают при разборе поломок."""
    try:
        text = boot_log().read_text(encoding='utf-8', errors='replace')
    except OSError:
        return
    if not text.strip():
        return
    try:
        with (runtime_dir() / 'service.stderr.log').open('a', encoding='utf-8') as out:
            out.write(text)
            if not text.endswith('\n'):
                out.write('\n')
    except OSError:
        pass


if __name__ == '__main__':
    start()
