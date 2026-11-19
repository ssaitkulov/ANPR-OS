"""Проверка процессов: жив ли PID, чей это процесс, кому принадлежит порт.

Задача модуля — не дать остановить чужой процесс. Совпадение порта само по
себе ничего не значит: порт может занять другая программа, и её трогать
нельзя. Поэтому управление сервисом опирается на три независимые проверки:

* PID из `/health` совпадает с PID из файла `service.pid`;
* процесс с этим PID существует и не завершён;
* командная строка процесса указывает на нашу программу (`app.py`).

Только после всех трёх проверок сервис можно останавливать.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes

#: Имя файла, по которому запускается сервис (маркер в командной строке).
SERVICE_MARKER = 'app.py'

#: Код возврата «процесс ещё работает».
STILL_ACTIVE = 259

#: Права доступа к процессу: достаточно запросить информацию.
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000


def pid_alive(pid: int) -> bool:
    """Жив ли процесс с таким PID."""
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    if os.name == 'nt':
        return _alive_windows(number)
    try:
        os.kill(number, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _alive_windows(pid: int) -> bool:
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def process_command_line(pid: int) -> str | None:
    """Командная строка процесса (None — не удалось получить)."""
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return None
    if os.name != 'nt' or number <= 0:
        return None
    return _command_line_windows(number)


class _PROCESS_COMMAND_LINE_INFORMATION(ctypes.Structure):
    """`UNICODE_STRING`: длина, максимальная длина, указатель на текст."""

    _fields_ = [('Length', wintypes.USHORT), ('MaximumLength', wintypes.USHORT),
                ('Buffer', ctypes.c_void_p)]


#: Размер буфера под командную строку (с запасом).
_COMMAND_LINE_BUFFER = 64 * 1024


def _command_line_windows(pid: int) -> str | None:
    """Командная строка через `NtQueryInformationProcess` (класс 60)."""
    try:
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        ntdll = ctypes.WinDLL('ntdll')
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        ntdll.NtQueryInformationProcess.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                                    ctypes.c_void_p, wintypes.DWORD,
                                                    ctypes.POINTER(wintypes.DWORD))
        ntdll.NtQueryInformationProcess.restype = ctypes.c_long
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            buffer = ctypes.create_string_buffer(_COMMAND_LINE_BUFFER)
            length = wintypes.DWORD()
            status = ntdll.NtQueryInformationProcess(handle, 60, buffer, _COMMAND_LINE_BUFFER,
                                                     ctypes.byref(length))
            if status != 0:
                return None
            info = ctypes.cast(buffer, ctypes.POINTER(_PROCESS_COMMAND_LINE_INFORMATION)).contents
            if not info.Buffer or not info.Length:
                return None
            raw = ctypes.string_at(info.Buffer, info.Length)
            return raw.decode('utf-16-le', errors='replace').rstrip('\x00')
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def looks_like_service(pid: int) -> bool:
    """Похож ли процесс на наш сервис.

    Основание — командная строка: среди аргументов должен быть файл запуска
    службы (`app.py`). Проверка строгая: имя файла должно совпасть целиком,
    чтобы чужой процесс случайно не прошёл за наш.

    Если командную строку получить не удалось, результат неизвестен, и
    проверка считается не пройденной: остановка не выполняется.
    """
    command = process_command_line(pid)
    if not command:
        return False
    for argument in command.split():
        name = argument.strip('"').replace('/', '\\').split('\\')[-1]
        if name == SERVICE_MARKER:
            return True
    return False


def port_owner_pid(port: int) -> int | None:
    """PID процесса, который слушает порт (None — неизвестно).

    Читается таблица соединений: `netstat` ничего не меняет в системе.
    """
    if os.name != 'nt':
        return None
    try:
        result = subprocess.run(['netstat', '-ano', '-p', 'TCP'], capture_output=True,
                                text=True, timeout=10,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (result.stdout or '').splitlines():
        parts = line.split()
        if len(parts) < 5 or 'LISTENING' not in line.upper():
            continue
        local = parts[1]
        if local.rsplit(':', 1)[-1] != str(port):
            continue
        try:
            return int(parts[-1])
        except ValueError:
            continue
    return None


def describe(pid: int) -> dict:
    """Сводка о процессе — для журнала и диагностики."""
    alive = pid_alive(pid)
    return {'pid': pid, 'alive': alive,
            'looks_like_service': looks_like_service(pid) if alive else False,
            'command': process_command_line(pid) if alive else None}
