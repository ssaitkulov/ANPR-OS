"""Секретный токен управления локальным сервисом.

`/shutdown` — единственная операция, которая останавливает сервис, поэтому
она защищена токеном: без знания токена запрос не выполняется, даже если он
пришёл с localhost.

Устройство:

* токен — 32 байта из `secrets.token_urlsafe(32)`; сравнение — только
  `hmac.compare_digest` (сравнение за постоянное время);
* токен лежит в файле `data/runtime/service.token`, права доступа ограничены: на Windows
  через SDDL `D:P(A;;FA;;;OW)` — доступ имеет только владелец файла, то
  есть тот же локальный пользователь, от имени которого запущен сервис;
* основание для доступа — сам секрет, а не localhost, Origin, Referer или
  имя HTTP-заголовка: заголовок лишь переносит значение секрета;
* значение токена регистрируется в `app.services.audit` как секрет — в
  журнал оно не попадает никогда.

Схема работает при разных версиях клиента и сервера: версия протокола
объявляется в `/health` (`protocol`, `shutdown_auth`), и клиент выбирает
схему по версии *сервера* — новая схема не применяется наполовину.
"""
from __future__ import annotations

import ctypes
import os
import secrets
import sys
from ctypes import wintypes
from pathlib import Path

from app.core.paths import runtime_dir

#: Заголовок, в котором клиент передаёт токен.
TOKEN_HEADER = 'X-Avrika-Control-Token'

#: Имя файла с токеном.
TOKEN_FILE_NAME = 'service.token'

#: Версия протокола управления: 2 — токен обязателен, 1 — старый сервис.
PROTOCOL_VERSION = 2

#: SDDL: только владелец файла, наследование отключено.
OWNER_ONLY_SDDL = 'D:P(A;;FA;;;OW)'

#: Ошибки Windows, которые нужно различать.
ERROR_ALREADY_EXISTS = 183
ERROR_FILE_EXISTS = 80


def token_path() -> Path:
    """Путь к файлу с токеном (`data/runtime/service.token`)."""
    override = os.environ.get('SERVICE_TOKEN_FILE')
    if override:
        return Path(override)
    return runtime_dir() / TOKEN_FILE_NAME


def read_token() -> str | None:
    """Прочитать токен из файла (None — файла нет или он пуст)."""
    try:
        value = token_path().read_text(encoding='utf-8').strip()
    except OSError:
        return None
    return value or None


def write_token(token: str) -> Path:
    """Записать токен в файл и ограничить права доступа."""
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token.strip() + '\n', encoding='utf-8')
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    restrict_acl(path)
    register_secret(token)
    return path


def create_token() -> str:
    """Создать новый токен и сохранить его."""
    token = secrets.token_urlsafe(32)
    write_token(token)
    return token


def load_or_create_token() -> str:
    """Токен сервиса: из файла либо новый (файл создаётся при первом запуске)."""
    token = read_token()
    if not token:
        token = create_token()
    _register(token)
    return token


def verify(presented: str | None, expected: str | None = None) -> bool:
    """Проверить предъявленный токен.

    `expected` — токен, который сервер держит в памяти; без него токен
    читается из файла. Сравнение — только за постоянное время.
    """
    if expected is None:
        expected = read_token()
    if not expected or not presented:
        return False
    return secrets.compare_digest(str(presented), str(expected))


def _register(token: str) -> None:
    """Сообщить журналу, что это значение — секрет."""
    register_secret(token)


def register_secret(token: str) -> None:
    """Отметить значение как секрет: в журнал оно не попадёт."""
    try:
        from app.services import audit
        audit.register_secret(token)
    except Exception:  # pragma: no cover — журнал не критичен
        pass


# ----------------------------------------------------------------------
# Права доступа к файлу (Windows)
# ----------------------------------------------------------------------

def _advapi32():
    return ctypes.WinDLL('advapi32', use_last_error=True)


def _apply_sddl(path: Path, sddl: str) -> bool:
    """Назначить дескриптор безопасности по SDDL-строке."""
    advapi32 = _advapi32()
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL)]
    advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p]
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1,
                                                                       ctypes.byref(descriptor), None):
        return False
    try:
        dacl_present = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        defaulted = wintypes.BOOL()
        if not advapi32.GetSecurityDescriptorDacl(descriptor, ctypes.byref(dacl_present),
                                                  ctypes.byref(dacl), ctypes.byref(defaulted)):
            return False
        # SE_FILE_OBJECT = 1, DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION
        result = advapi32.SetNamedSecurityInfoW(str(path), 1, 0x4 | 0x80000000,
                                                 None, None, dacl, None)
        return result == 0
    finally:
        kernel32.LocalFree(descriptor)


def read_sddl(path: Path) -> str | None:
    """Прочитать ACL файла в виде SDDL-строки (для проверки)."""
    advapi32 = _advapi32()
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(ctypes.c_uint)]
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    descriptor = ctypes.c_void_p()
    result = advapi32.GetNamedSecurityInfoW(str(path), 1, 0x4, None, None, None, None,
                                             ctypes.byref(descriptor))
    if result != 0 or not descriptor:
        return None
    text = wintypes.LPWSTR()
    length = ctypes.c_uint()
    try:
        ok = advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, 1, 0x4, ctypes.byref(text), ctypes.byref(length))
        return text.value if ok and text.value else None
    finally:
        if text:
            kernel32.LocalFree(text)
        kernel32.LocalFree(descriptor)


def restrict_acl(path: Path) -> str:
    """Ограничить доступ к файлу: только владелец.

    Возвращает результат: `applied`, `failed` или `unsupported`.
    """
    if sys.platform != 'win32':
        try:
            os.chmod(path, 0o600)
        except OSError:
            return 'failed'
        return 'unsupported'
    try:
        if _apply_sddl(Path(path), OWNER_ONLY_SDDL):
            return 'applied'
    except Exception:
        pass
    return 'failed'


def acl_state(path: Path) -> dict:
    """Состояние прав файла (для диагностики и журнала)."""
    path = Path(path)
    if not path.exists():
        return {'exists': False}
    info = {'exists': True, 'path': str(path)}
    if sys.platform == 'win32':
        try:
            info['sddl'] = read_sddl(path)
        except Exception:
            info['sddl'] = None
    return info
