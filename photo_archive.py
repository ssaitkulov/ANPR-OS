from pathlib import Path
from datetime import datetime, timedelta
import hashlib
import os
import shutil
import uuid


def save_photo(folder, captured_at, plate, image):
    """Save a real event JPEG outside SQLite and return its absolute path."""
    if not captured_at or not image:
        return None
    folder = Path(folder).expanduser().resolve()
    day = captured_at[:10]
    if len(day) != 10 or any(c not in '0123456789-' for c in day):
        raise ValueError('Некорректная дата снимка')
    target_dir = folder / day
    target_dir.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256((captured_at + '\0' + (plate or '')).encode('utf-8')).hexdigest() + '.jpg'
    destination = target_dir / name
    if destination.exists():
        return str(destination)
    temp = target_dir / ('.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('xb') as out:
            out.write(image)
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)
    return str(destination)


def cleanup_old_photos(folder, keep_days: int = 30, today=None) -> int:
    """Удалить каталоги фотографий старше keep_days дней. Возвращает число удалённых.

    Каталоги называются по дате (ГГГГ-ММ-ДД). Всё, что не похоже на дату
    (например, `diagnostic`), остаётся на месте — это не архив проездов.
    """
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        return 0
    cutoff = (today or datetime.now()).date() - timedelta(days=max(1, int(keep_days)))
    removed = 0
    try:
        children = list(folder.iterdir())
    except OSError:
        return 0
    for child in children:
        if not child.is_dir():
            continue
        try:
            day = datetime.strptime(child.name, '%Y-%m-%d').date()
        except ValueError:
            continue
        if day > cutoff:
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed += 1
    return removed
