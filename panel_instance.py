"""Coordinate replacement of an existing control panel within this Windows session."""
import ctypes
from ctypes import wintypes
import hashlib
from pathlib import Path

kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
kernel.CreateMutexW.restype = wintypes.HANDLE
kernel.CreateEventW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
kernel.CreateEventW.restype = wintypes.HANDLE
kernel.SetEvent.argtypes = (wintypes.HANDLE,)
kernel.SetEvent.restype = wintypes.BOOL
kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
kernel.WaitForSingleObject.restype = wintypes.DWORD
kernel.ReleaseMutex.argtypes = (wintypes.HANDLE,)
kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
WAIT_OBJECT_0 = 0
WAIT_ABANDONED = 0x80
WAIT_TIMEOUT = 0x102


class PanelInstance:
    def __init__(self):
        project = str(Path(__file__).resolve().parent).casefold()
        suffix = hashlib.sha256(project.encode('utf-8')).hexdigest()[:24]
        self.mutex = kernel.CreateMutexW(None, False, 'Local\\EvrikaPanelMutex_' + suffix)
        self.event = kernel.CreateEventW(None, False, False, 'Local\\EvrikaPanelReplace_' + suffix)
        self.owned = False
        if not self.mutex or not self.event:
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def acquire(self, timeout_ms=8000):
        result = kernel.WaitForSingleObject(self.mutex, 0)
        if result == WAIT_TIMEOUT:
            if not kernel.SetEvent(self.event):
                raise ctypes.WinError(ctypes.get_last_error())
            result = kernel.WaitForSingleObject(self.mutex, timeout_ms)
        if result not in (WAIT_OBJECT_0, WAIT_ABANDONED):
            raise RuntimeError('Предыдущая панель не завершилась. Закройте её через трей и повторите запуск.')
        self.owned = True

    def replacement_requested(self):
        return kernel.WaitForSingleObject(self.event, 0) == WAIT_OBJECT_0

    def close(self):
        if self.owned and self.mutex:
            kernel.ReleaseMutex(self.mutex)
            self.owned = False
        if self.event:
            kernel.CloseHandle(self.event)
            self.event = None
        if self.mutex:
            kernel.CloseHandle(self.mutex)
            self.mutex = None
