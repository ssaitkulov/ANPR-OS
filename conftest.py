"""Изоляция тестов от боевого окружения.

Импорт anpr_telegram не должен трогать рабочую базу, JSON-зеркало и настоящий
Telegram. Поэтому ещё до импорта модуля пути перенаправляются во временный
каталог, а секреты вычищаются из окружения.
"""
import os
import tempfile
from pathlib import Path

_TEMP = Path(tempfile.mkdtemp(prefix='anpr-tests-'))

os.environ.setdefault('DB_FILE', str(_TEMP / 'events.sqlite3'))
os.environ.setdefault('STATE_FILE', str(_TEMP / 'state.json'))
os.environ.setdefault('IMAGE_DIR', str(_TEMP / 'images'))
os.environ.setdefault('SUBSCRIBERS_FILE', str(_TEMP / 'subscribers.json'))

# Настоящий бот в тестах не нужен и не должен получать сообщения.
os.environ['TELEGRAM_BOT_TOKEN'] = ''
os.environ['TELEGRAM_CHAT_ID'] = ''
os.environ['ALLOWED_CHAT_IDS'] = ''
