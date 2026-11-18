"""Совместимая точка входа ядра сбора.

Исторический путь сохранён:

* `python anpr_telegram.py --check / --report-now / --dry-run /
  --parser-test / --backfill-now` — как раньше;
* `import anpr_telegram` — даёт **тот же объект модуля**, что и
  `app.core.anpr_telegram`.

Второе важно: тесты и служебные утилиты подменяют атрибуты этого модуля
(`bot.STORE`, `bot.telegram_send_to`, `bot.now_local` и другие). Простой
импорт функций из пакета такой подмены бы не дал — подменился бы один
модуль, а работал бы другой. Поэтому здесь ставится алиас в `sys.modules`.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.core import anpr_telegram as _module  # noqa: E402

sys.modules['anpr_telegram'] = _module

if __name__ == '__main__':
    _module.hashlib.md5()  # держим импорт для Digest (requests использует его внутри)
    raise SystemExit(_module.main())
