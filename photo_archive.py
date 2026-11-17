from pathlib import Path
import hashlib
import os
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
