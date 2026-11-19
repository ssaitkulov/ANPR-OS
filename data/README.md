# data/ — изменяемые данные приложения

Здесь лежит всё, что меняется во время работы программы. Каталог находится
рядом с кодом, поэтому проект переносится целиком: скопировали папку на другой
диск — пути определились сами.

```
data/
├── anpr.sqlite3        база проездов (SQLite, источник истины)
├── anpr_state.json     зеркало состояния (дни, seen, last_rec_no, reported_days)
├── subscribers.json    список чатов-получателей
├── ui_preferences.json настройки панели (сворачивание в трей)
├── ui_window.json      положение и размер окна панели
├── photos/             фотографии проездов (photos/ГГГГ-ММ-ДД/*.jpg)
├── runtime/            service.pid (PID-файл), service.token (токен управления)
└── logs/               service.stdout.log, service.stderr.log, service.boot.log,
                        service.control.log, panel.log
```

## Кто что читает

| Файл | Кто |
|---|---|
| `anpr.sqlite3` | ядро сбора, HTTP-сервис, мониторинг, `scripts/verify_state.py`, `scripts/acceptance_check.py` |
| `anpr_state.json` | ядро сбора (зеркало), сверка состояния |
| `subscribers.json` | рассылка в Telegram, приёмка |
| `photos/` | ядро сбора (запись), мониторинг и HTTP-сервис (чтение) |
| `runtime/service.pid` | панель управления (старт/остановка) |
| `runtime/service.token` | авторизация `POST /shutdown` (права: только владелец) |
| `logs/` | панель управления («Открыть журнал»), приёмка |

Имена файлов прежние: `anpr.sqlite3`, `anpr_state.json`, `subscribers.json`,
`service.pid`, `service.token`. Изменилось только место — с корня проекта на
каталог `data/`.

## Пути

Все пути считаются от корня проекта в `app/core/paths.py` — это единственный
источник правды. Ни один модуль не вычисляет корень проекта самостоятельно и
не полагается на текущую рабочую директорию.

В `.env` пути **относительные** (`data/photos`, `data/subscribers.json`),
поэтому проект переносится без правки настроек. Путь к фотографии в базе
тоже относительный (`2026-11-17/файл.jpg` от каталога снимков): база не знает,
где лежит проект, — и не должна знать.

Переопределения через переменные окружения (нужны в безопасном тестовом
режиме): `ANPR_RUNTIME_DIR`, `ANPR_LOG_DIR`, `DB_FILE`, `STATE_FILE`,
`SUBSCRIBERS_FILE`, `IMAGE_DIR`.

## Содержимое в Git не попадает

`.gitignore` исключает всё, кроме этого файла. Резервные копии переноса —
в каталоге `backup/` (тоже вне Git).

## Откат

Перенос выполнялся один раз: `python scripts/migrate_data.py --migrate`.
Резервная копия осталась в `backup/migration-<метка>/`. Возврат на прежние
места (файлы, `.env` и проверка целостности):

```bash
python scripts/migrate_data.py --rollback backup/migration-20261119_131723
```
