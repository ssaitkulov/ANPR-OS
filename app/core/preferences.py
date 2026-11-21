"""Per-user Windows startup and panel close preferences."""
import json
import os
from pathlib import Path
import subprocess
import sys
import winreg

from app.core.paths import PROJECT_ROOT, ui_preferences_file

#: Корень проекта. Путь к панели в автозапуске Windows остаётся прежним:
#: лаунчер `control_panel.py` лежит в корне.
BASE = PROJECT_ROOT
#: Настройки панели — рядом с рабочими данными, в `data/`.
PREFERENCES = ui_preferences_file()
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_NAME = 'EvrikaANPR'


def startup_command():
    executable = Path(sys.executable)
    pythonw = executable.with_name('pythonw.exe')
    if not pythonw.exists():
        raise OSError('Не найден pythonw.exe для автозапуска.')
    return subprocess.list2cmdline([str(pythonw), str(BASE / 'control_panel.py')])


def get_autostart():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, RUN_NAME)
    except FileNotFoundError:
        return False
    return value == startup_command()


def set_autostart(enabled):
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
        try:
            existing, _ = winreg.QueryValueEx(key, RUN_NAME)
        except FileNotFoundError:
            existing = None
        command = startup_command()
        if enabled and existing is not None and existing != command:
            raise OSError('Запись автозапуска Evrika принадлежит другой установке.')
        if enabled:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, command)
        elif existing is not None:
            # Выключение должно работать и после переноса папки: чужую
            # (устаревшую) запись всё равно нужно удалить.
            winreg.DeleteValue(key, RUN_NAME)


def get_minimize_to_tray(path=PREFERENCES):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        value = data.get('minimize_to_tray', True)
        return value if isinstance(value, bool) else True
    except (OSError, ValueError, AttributeError):
        return True


def set_minimize_to_tray(enabled, path=PREFERENCES):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps({'minimize_to_tray': bool(enabled)}, ensure_ascii=False) + '\n', encoding='utf-8')
    os.replace(temporary, path)
