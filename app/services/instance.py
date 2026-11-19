"""Межпроцессная блокировка: один экземпляр сервиса на машину.

Сервис держит именованный мютекс Windows (`Local\\EvrikaService_*`).
Второй экземпляр, запущенный на той же машине, мютекс не получит и
завершится с понятным сообщением — вместо того чтобы тихо портить рабочую
базу и бороться за порт.

Имя мютекса выводится из корня проекта: у другой копии программы — другое
имя, две разные установки не мешают друг другу. Формат имени прежний не
меняется: у панели уже есть свой мютекс (`app.services.panel_lock`).
"""
import ctypes
import hashlib
import os
from ctypes import wintypes

from app.core.paths import PROJECT_ROOT

#: Ошибка Windows: объект с таким именем уже существует.
ERROR_ALREADY_EXISTS = 183

#: Префикс имени мютекса.
PREFIX = 'EvrikaService'


def mutex_name(prefix: str = PREFIX) -> str:
    """Имя мютекса для текущей копии программы."""
    project = str(PROJECT_ROOT).casefold()
    suffix = hashlib.sha256(project.encode('utf-8')).hexdigest()[:24]
    return f'Local\\{prefix}Mutex_' + suffix


class ServiceInstance:
    """Мютекс одного экземпляра сервиса.

    Использование::

        instance = ServiceInstance()
        if not instance.acquire():
            print('Сервис уже запущен')
        ...
        instance.release()
    """

    def __init__(self, name: str | None = None):
        self.name = name or mutex_name()
        self.handle = None
        self.owned = False
        self._kernel = None

    def acquire(self) -> bool:
        """Захватить мютекс. False — экземпляр уже работает."""
        if os.name != 'nt':
            # Вне Windows именованных мютексов нет: защищаемся PID-файлом.
            return True
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.GetLastError.restype = wintypes.DWORD
        handle = kernel.CreateMutexW(None, True, self.name)
        error = kernel.GetLastError()
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if error == ERROR_ALREADY_EXISTS:
            # Мютекс уже есть: другой экземпляр держит его.
            kernel.CloseHandle(handle)
            return False
        self.handle = handle
        self.owned = True
        self._kernel = kernel
        return True

    def release(self) -> None:
        """Отпустить мютекс (повторный вызов безопасен)."""
        if self.handle and self._kernel is not None:
            try:
                if self.owned:
                    self._kernel.ReleaseMutex(self.handle)
                self._kernel.CloseHandle(self.handle)
            except Exception:  # pragma: no cover — освобождение не критично
                pass
        self.handle = None
        self.owned = False

    def __enter__(self):
        self.acquired = self.acquire()
        return self

    def __exit__(self, *args):
        self.release()
        return False
