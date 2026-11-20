# CODEMAP — карта кода ANPR OS Fayz

Навигация по проекту для ИИ-агентов и разработчиков: **где что лежит, кто что
вызывает, чем это покрыто**. Карта — не описание продукта (для этого
[`README.md`](../README.md)) и не история решений (для этого
`PROJECT_CONTEXT.md` — локальный файл, в Git не входит).

Правила работы с картой:

- начинай с неё, а не с чтения кода подряд: найди модуль → посмотри, кто его
  вызывает → открой только нужные файлы;
- карта описывает **текущее состояние кода**; если карта разошлась с исходником —
  правда в коде, исправь карту и заодно проверь, нет ли расхождения в документации;
- правки сюда вносятся только при содержательных изменениях в коде.

---

## 1. Слои и направление зависимостей

```
                     корневые точки входа (лаунчеры, алиасы в sys.modules)
                     app.py · control_panel.py · setup_anpr.py · anpr_telegram.py · service_control.py
                                        │
        ┌───────────────────────────────┼───────────────────────────────┐
        │                               │                               │
   app/core/  (правила, пути, ядро)  app/services/  (процессы, HTTP)  app/ui/  (окно)
        │                               │                               │
        └──────────────► app/storage/ ◄─┴───────────────────────────────┘
                     (SQLite, фото, outbox)
```

Правило слоёв: `ui → services → core → storage`; `core` не знает про `ui`,
`storage` не знает ни о ком, кроме стандартной библиотеки. Обратные зависимости
не заводим без причины.

| Слой | Каталог | Ответственность |
|---|---|---|
| core | `app/core/` | ядро сбора, пути, безопасный тестовый режим, настройки ОС, события |
| services | `app/services/` | HTTP-сервис, управление процессом, журналы, аудит, токен, блокировки |
| storage | `app/storage/` | SQLite (источник истины), очередь недоставленных, фотографии |
| ui | `app/ui/` | единое окно: мониторинг, настройки, шапка, панель управления |

## 2. Точки входа

Корневые файлы — **тонкие лаунчеры**. Они подставляют модуль из пакета `app/`
в `sys.modules`, поэтому `import anpr_telegram` и `import control_panel` дают
тот же объект, что и пакет, а подмены вида `patch('anpr_telegram.STORE')`
продолжают работать. Не добавляй логику в корневые файлы — она должна жить в `app/`.

| Файл | Запуск | Что запускает |
|---|---|---|
| `app.py` | `python app.py` | `app.services.http_api.run()` — HTTP-сервис `127.0.0.1:18749` |
| `control_panel.py` | `run.bat`, автозапуск Windows | `app.ui.panel.main()` → `AppShell` (единое окно) |
| `setup_anpr.py` | `settings.bat` | `app.ui.settings.main()` — окно настроек |
| `anpr_telegram.py` | CLI: `--check`, `--report-now`, `--dry-run`, `--parser-test`, `--backfill-now` | `app.core.anpr_telegram.main()` |
| `service_control.py` | `start_service.ps1`, `anpr-service` | `app.services.control.start()` |

## 3. Поток данных

```
Камера Dahua (RTSP + ANPR-события)
        │  auth.Digest, поток subscribe_loop / consume_camera_stream
        ▼
app/core/anpr_telegram.py ──► app/storage/database.py (Store) ──► data/anpr.sqlite3
        │                                │  таблица events (источник истины)
        │                                └─► таблица outbox (очередь недоставленных)
        │                                              │
        │  app/storage/passage_photos.py (кэш снимков)  │  flush_missed_messages / outbox_worker
        │  app/storage/photo_archive.py (файлы фото)     ▼
        └──────────────────────────────────►  Telegram (Bot API)
                                                   │
app/services/http_api.py ◄── /health /status /passages │ панель читает состояние
        │                                            ▼
app/ui/shell.py (AppShell) ─► header.py ─► service_panel.py / status.py ─► dashboard.py / settings.py
        │
        └─► app/services/control.py (start/stop сервиса) ─► auth.py (токен) + audit.py (журнал)
```

## 4. Карта модулей

### 4.1 `app/core/`

| Модуль | Назначение | Ключевые функции и классы | Кто использует |
|---|---|---|---|
| `anpr_telegram.py` | ядро сбора: камера, разбор событий, запись проездов, отправка в Telegram, добор из архива | `main`, `subscribe_loop`, `consume_camera_stream`, `stream_chunks_with_ticks`, `parse_kv_text`, `extract_events`, `MultipartParser`, `plate_of`, `get_direction_value`, `direction_kind`/`direction_allowed`/`direction_label`, `record_event`/`save_passage`/`store_event`, `send_entry_notification`, `telegram_send`, `telegram_send_photo`, `telegram_send_media_group`, `flush_missed_messages`, `outbox_worker`, `build_report`, `maybe_send_report`, `report_worker`, `backfill_once`, `run_backfill`, `backfill_worker`, `maintenance_worker`, `cleanup_images`, `check_connection`, `load_state`/`save_state`, `load_subscribers`/`get_recipients`, `LazyStore`, `now_local`, `today_str` | `services/http_api.py` (через алиас `import anpr_telegram as M`), корневые лаунчеры, `scripts/`, тесты |
| `paths.py` | единственный источник правды о путях; считает всё от корня проекта | `PROJECT_ROOT`, `DATA_DIR`, `runtime_dir`, `log_dir`, `photo_dir`, `db_file`, `state_file`, `subscribers_file`, `ui_preferences_file`, `ui_window_file`, `env_file`, `resolve_path`, `resolve_photo_path`, `relative_photo_path`, `ensure_data_dirs`, `legacy_data` | все слои; переопределяется `ANPR_RUNTIME_DIR`, `ANPR_LOG_DIR`, `IMAGE_DIR`, `DB_FILE`, `STATE_FILE`, `SUBSCRIBERS_FILE` |
| `safe_mode.py` | безопасный тестовый режим: изоляция рабочих данных, запрет сети/процессов, учёт потоков | `activate`, `is_active`, `reason`, `isolation_dir`, `snapshot`, `detect_test_run`, `guard`, `guards_suspended`, `blocked_url`/`blocked_command`/`blocked_endpoint`, `track_thread`, `join_threads`, `finalize`, `restore_patches`, `SafeModeError` | `tests/__init__.py`, корневой `conftest.py`, `tests/conftest.py`; проверки в `control.py`, `core/anpr_telegram.py`, `ui/settings.py`, `ui/dashboard.py` |
| `preferences.py` | настройки панели и автозапуск Windows (реестр) | `get_autostart`/`set_autostart`, `startup_command`, `get_minimize_to_tray`/`set_minimize_to_tray` | `ui/shell.py`, `ui/settings.py` |
| `window_state.py` | положение и размер окна между запусками | `parse_geometry`, `format_geometry`, `state_from_geometry`, `load_state`, `save_state`, `within_bounds` | `ui/shell.py` |
| `system_events.py` | операционный журнал в памяти (кольцевой буфер) | `EventLog.add`, `EventLog.snapshot`, `EVENTS` | `core/anpr_telegram.py`, `services/http_api.py` (`GET /events`) |

### 4.2 `app/services/`

| Модуль | Назначение | Ключевые функции и классы | Кто использует |
|---|---|---|---|
| `http_api.py` | локальный HTTP-сервис (FastAPI) | `app` (эндпоинты), `lifespan` (старт рабочего потока), `run`, `shutdown_service`, `health`, `status`, `operational_events`, `recent_passages`, `passage_photo`, `entries_today`, `report_now`, `backfill_now`, `shutdown` | `app.py`, панель (`ui/shell.py`), `scripts/acceptance_check.py` |
| `control.py` | запуск и остановка сервиса как отдельного процесса | `start`, `stop`, `stop_gracefully`, `health`, `server_capabilities`, `pid_file`, `_verify_service_process`, `_trim_boot_log` | `ui/shell.py`, `ui/panel.py` (публичный `service`), `service_control.py` |
| `auth.py` | токен управления сервисом | `TOKEN_HEADER` (`X-Avrika-Control-Token`), `PROTOCOL_VERSION`, `load_or_create_token`, `create_token`, `verify`, `register_secret`, `restrict_acl`, `read_sddl`, `acl_state`, `token_path` | `http_api.py` (проверка `POST /shutdown`), `control.py` |
| `audit.py` | структурированные журналы управления (JSONL) с маскированием секретов | `record`, `control`, `panel`, `operation` (контекстный менеджер), `read`, `journal_path`, `writer_for`, `JournalWriter`, `register_secret`, `redact_text`/`redact_detail`, `timestamp` | `control.py`, `http_api.py`, `ui/shell.py` |
| `logs.py` | вывод сервиса в файлы с ротацией | `install`, `uvicorn_log_config`, `RotatingFileWriter`, `LogStream`, `write_log`, `writer_for` | `http_api.py` |
| `instance.py` | один экземпляр сервиса на машину (мьютекс Windows) | `ServiceInstance.acquire`/`release`, `mutex_name` | `http_api.py` |
| `panel_lock.py` | один экземпляр окна панели | `PanelInstance.acquire`/`close`, `replacement_requested` | `ui/shell.py` |
| `process_info.py` | проверка «это наш процесс» по PID/командной строке/порту | `pid_alive`, `process_command_line`, `looks_like_service`, `port_owner_pid`, `describe` | `control.py`, `ui/shell.py` |

### 4.3 `app/storage/`

| Модуль | Назначение | Ключевые функции и классы | Кто использует |
|---|---|---|---|
| `database.py` | SQLite: проезды (источник истины) и очередь недоставленных | `event_key` (sha256 от `day|iso|plate|lane|direction`), `passage_key`, `normalize_plate`, `is_event_key`, `group_id_of`, `rec_no_of`, `backoff_delay`, класс `Store`: `add_event`/`add_events`, `update_event`, `day_passages`, `recent_passages`, `count_day`, `passage_exists`, `passage_identity`, `set_photo_path`/`get_photo_path`/`find_photo_path`, `enqueue_text`, `pending`/`pending_grouped`, `mark_sent`/`mark_partial`/`mark_failed`, `dead_messages`, `outbox_stats`, `stats`, `checkpoint`, `close`; миграции `_upgrade_events`, `_upgrade_outbox` | `core/anpr_telegram.py`, `ui/dashboard.py`, `services/http_api.py` (через `M.STORE`), `scripts/` |
| `passage_photos.py` | кэш последних снимков в памяти | `PhotoCache.put`/`get`, `PHOTOS` | `core/anpr_telegram.py`, `services/http_api.py` (`/passages/{id}/photo`) |
| `event_images.py` | подготовка снимков события (кадрирование по номеру) | `prepare_event_images`, `plate_box` | `core/anpr_telegram.py` |
| `photo_archive.py` | файлы фотографий на диске и их ротация | `save_photo`, `cleanup_old_photos` | `core/anpr_telegram.py`, `scripts/migrate_data.py` |

Модуль `database.py` — самый критичный: в нём ключ проезда, дедупликация и
политика повторов очереди.

### 4.4 `app/ui/`

| Модуль | Назначение | Ключевые функции и классы | Кто использует |
|---|---|---|---|
| `shell.py` | единое окно приложения: разделы, состояние, управление сервисом, сбор данных для панели | класс `AppShell`: `show_section`, `_create_section`, `refresh`/`refresh_now`/`_health_worker`, `update_status`, `panel_data` (строки для каждого режима панели: сервис, камера, Telegram), `details_data`, `merge_events`/`render_events`, `toggle_service`/`start_service`/`stop_service`/`restart_service`, `work`, `open_log`, `show_message`, `_start_tray`, `save_window_state`, `shutdown`-цикл; `main` | `ui/panel.py` |
| `panel.py` | совместимый фасад панели (`Panel = AppShell`) | `main`, `Panel` | `control_panel.py`, тесты (`patch('control_panel.…')`) |
| `header.py` | верхняя панель: сегментированный переключатель разделов, три компактных индикатора (сервис, камера, Telegram), вход в контекстную панель | класс `SystemHeader` (`tabs` — сегментированный переключатель, `nav_buttons` — совместимая карта «раздел → вкладка», `chips` — три `StatusChip`, `toggle_details(key)` — открыть/переключить/закрыть панель по индикатору, `toggle_panel` — совместимость, `close_details`, `set_state`, `refresh_panel`, `set_running`, `set_busy`, `sync_message`, `shutdown`), класс `SegmentedTabs` (`add_tab`, `select`, `activate`, `sync`, `hover`, `focus_in`/`focus_out`), класс `SectionTab` (`configure` — совместимость с прежней кнопкой навигации), класс `TabLabel` (иконка + подпись вкладки), класс `StatusChip` (`set_state`, `set_active`, `set_compact`, `shutdown`), константы `INDICATOR_CAPTIONS`, `INDICATOR_ICONS`, `TAB_ICONS`, `ICON_SIZE`, `CHIP_HEIGHT`, `TABS_HEIGHT`, `TABS_TRANSITION` | `ui/shell.py` |
| `service_panel.py` | контекстная панель: содержимое только по выбранному индикатору | класс `ServicePanel` (`open(anchor, section)`, `close`, `toggle`, `refresh`, `_update_actions`, `_update_message`, `shutdown`), `ModeBlock` (режим `service`/`camera`/`telegram`), `DetailRows` (`update`, `captions`), константы `MODES`, `DEFAULT_MODE`, `MODE_TITLES`, `MODE_ICONS`, `PANEL_WIDTH` | `ui/header.py` |
| `status.py` | единый словарь состояний → цвет/подпись | `service_view`, `camera_view`, `camera_events_view`, `mark_stale`, `telegram_connection_view`, `telegram_delivery_view`, `telegram_view`, `telegram_configured`, `format_duration`, `format_time`, `view` | `ui/header.py`, `ui/service_panel.py`, `ui/shell.py` |
| `dashboard.py` | раздел «Мониторинг»: видео, метрики, лента проездов, график | класс `MonitoringSection` (`schedule`/`schedule_idle`/`cancel_after`, `data_worker`, `video_worker`, `render_passages`, `draw_chart`, `poll`, `shutdown`), `load_dashboard`, `open_database`, `aggregate_hours`, `chart_last_hour`, `archived_photo` | `ui/shell.py` |
| `settings.py` | раздел «Настройки»: камера, Telegram, мониторинг, приложение | класс `SettingsSection` (`select_tab`, `values`, `save`, `save_application`, `check`, `poll`, `shutdown`), `read_settings`, `camera_settings`, `validate`, `save_settings`, `check_camera`, `check_bot`, `default_photo_dir`, `main` | `ui/shell.py`, `ui/dashboard.py` (читает настройки), `setup_anpr.py` |
| `widgets.py` | переиспользуемые элементы: иконка с цветом, tooltip, фокус, hover | `TintedIcon`, `Tooltip`, `animated_hover`, `focusable`, `bind_widget` | `ui/header.py`, `ui/service_panel.py` |
| `motion.py` | анимации и отложенные вызовы Tk | класс `Motion` (`call`, `cancel`, `cancel_all`, `tween`, `slide`, `slide_out`, `animate_color`, `pulse`), `reduced_motion`, `ease_out`, `ease_in_out`; `animate_color` доводит цвет до целевого, `slide`/`slide_out` принимают `width`/`height`, `tween` — кривую `easing` | `ui/widgets.py`, `ui/header.py`, `ui/service_panel.py` |
| `icons.py` | кэш иконок из `assets/icons` (PNG, tint по цвету) | `icon`, `source`, `tinted`, `blend_frames` (кадры плавной перекраски, `BLEND_STEPS`), `available` | `ui/widgets.py`, `ui/service_panel.py` |
| `theme.py` | цвета, шрифты, смешивание цветов | `font`, `blend`, `to_hex`, константы палитры | все модули `ui/` |
| `tklife.py` | отмена отложенных вызовов Tk при уничтожении виджета | `cancel_pending_scripts` | `ui/shell.py`, `ui/dashboard.py`, `ui/settings.py` |
| `schedule.py` | безопасная переопределка `after` в Tk (чистка call'ов) | `install` | `app/ui/__init__.py` |

## 5. Данные

### 5.1 База `data/anpr.sqlite3`

| Таблица | Что хранит | Ключевые моменты |
|---|---|---|
| `events` | проезды: одна строка = один проезд (живой или из архива) | `event_key` UNIQUE = `sha256(day|captured_at|plate|lane|direction)`; `source_key`, `group_id`, `rec_no` — только диагностика; индексы по `day`, `captured_at`, `plate`, `source_key`, `group_id` |
| `outbox` | недоставленные сообщения | статусы `pending`/`sent`/`partial`/`dead`, `attempts`, `next_attempt_at`, экспоненциальный backoff (база 30 с, потолок 3600 с) |
| `passage_photo_paths` | путь к снимку проезда | ключ `(captured_at, plate)` |

Схема создаётся и обновляется на месте в `Store.__init__` (`_upgrade_events`,
`_upgrade_outbox`) — миграции идемпотентны, при изменении схемы правь их там.

### 5.2 Файлы `data/`

| Путь | Содержимое | Кто пишет |
|---|---|---|
| `data/anpr_state.json` | зеркало состояния (отчёты, восстановление) | `core/anpr_telegram.py` |
| `data/subscribers.json` | чаты-получатели | `core/anpr_telegram.py` |
| `data/ui_preferences.json`, `data/ui_window.json` | настройки и геометрия окна | `core/preferences.py`, `core/window_state.py` |
| `data/photos/` | фотографии проездов | `storage/photo_archive.py` |
| `data/runtime/service.pid`, `data/runtime/service.token` | PID процесса и токен управления | `services/control.py`, `services/auth.py` |
| `data/logs/service.*.log`, `service.control.log`, `panel.log` | вывод сервиса, журналы управления | `services/logs.py`, `services/audit.py` |

### 5.3 Конфигурация `.env`

Загружается в `core/anpr_telegram.load_env` / `cfg`. Основные группы:
`DAHUA_*` (камера), `TELEGRAM_*` (бот и чат), `ALLOWED_CHAT_IDS`, `ALLOWED_LANES`,
`DIRECTION`, `TIMEZONE`, `REPORT_TIME`/`REPORT_WINDOW_MINUTES`, `BACKFILL_*`,
`OUTBOX_*`, `SAVE_PHOTOS`/`IMAGE_RETENTION_DAYS`, `DB_FILE`/`STATE_FILE`/
`SUBSCRIBERS_FILE`/`IMAGE_DIR`, `LOG_MAX_MB`/`LOG_BACKUPS`. Полный перечень —
в `.env.example`; секреты (`DAHUA_PASSWORD`, `TELEGRAM_BOT_TOKEN`) хранятся только
в `.env` и маскируются в журналах.

## 6. HTTP-эндпоинты → обработчики

Семантика и примеры запросов — в [`README.md`](../README.md) (раздел «Локальные
эндпоинты»). Здесь только соответствие «путь → код».

| Метод и путь | Обработчик | Модуль |
|---|---|---|
| `GET /health` | `health` | `services/http_api.py` |
| `GET /events` | `operational_events` | `services/http_api.py` |
| `GET /passages` | `recent_passages` | `services/http_api.py` |
| `GET /passages/{event_id}/photo` | `passage_photo` | `services/http_api.py` |
| `GET /status` | `status` | `services/http_api.py` |
| `GET /entries/today` | `entries_today` | `services/http_api.py` |
| `POST /report-now` | `report_now` | `services/http_api.py` → `core/anpr_telegram.build_report` |
| `POST /backfill-now` | `backfill_now` | `services/http_api.py` → `core/anpr_telegram.run_backfill` |
| `POST /shutdown` | `shutdown` | `services/http_api.py` + `services/auth.verify` |

## 7. Карта тестов

Изоляция включается до импорта модулей приложения (`tests/__init__.py`,
корневой `conftest.py`): рабочие данные, сеть и процессы уводятся в
безопасный режим. База для новых тестов — `tests/anpr_testenv.IsolatedTestCase`;
общая оснастка UI (заглушки, базовый класс) — `tests/ui_harness.py`.
Классификация набора и что запускать при каком изменении — в
[`TESTING.md`](TESTING.md).

| Файл теста | Что покрывает |
|---|---|
| `test_safe_mode.py` | изоляция: запрет сети/процессов, увод путей, учёт потоков, детектор тестового прогона |
| `test_camera_events.py` | разбор событий камеры: текст snapManager, группировка по индексу, отбор проездов (`is_countable`, полоса, направление) |
| `test_paths.py` | единая система путей, переносимость проекта |
| `test_passage_storage.py`, `test_passages.py`, `test_storage_and_outbox.py` | `storage/database.py`: проезды, ключи, outbox |
| `test_outbox_reliability.py`, `test_delivery_queue.py` | backoff, `dead`, доставка по чатам, фоновый поток |
| `test_live_duplicates.py`, `test_directions.py` | дедупликация живых событий, направление движения |
| `test_archive_time.py`, `test_manual_snapshots.py`, `test_ai_and_photo_archive.py` | добор из архива камеры, время архива, фотоархив |
| `test_event_images.py`, `test_passage_photos.py` | подготовка снимков, кэш фото |
| `test_stream_latency.py` | задержка потока камеры (быстрый smoke-тест) |
| `test_audit_log.py` | журналы управления, маскирование секретов |
| `test_control_auth.py` | токен остановки, `POST /shutdown`, защита процесса |
| `test_service_instance.py` | один экземпляр сервиса, устойчивость запуска |
| `test_stage2_resilience.py` | ротация журналов, потоки, устойчивость ядра |
| `test_stage3_quality.py`, `test_stage4_polish.py` | регрессии ядра и боевого режима |
| `test_setup_anpr.py` | окно настроек: чтение, валидация, сохранение |
| `test_app_preferences.py` | настройки панели, автозапуск Windows |
| `test_shell.py`, `test_panel_status.py` | оболочка окна, словарь состояний |
| `test_stage2_ui.py`, `test_service_panel.py`, `test_context_panel.py` | шапка с тремя индикаторами, контекстная панель по выбранному компоненту |
| `test_segmented_tabs.py` | сегментированный переключатель разделов: переключение мышью и с клавиатуры, подложка, фокус, закрытие панели диагностики |
| `test_dashboard.py` | мониторинг: загрузка данных, лента, график |
| `test_system_events.py` | операционный журнал в памяти |
| `test_verify_state.py` | `scripts/verify_state.py` — сверка JSON ↔ SQLite |
| `test_acceptance_tools.py` | `scripts/acceptance_check.py`, `scripts/check_lock.py` |

## 8. Скрипты обслуживания

| Скрипт | Назначение | Код возврата |
|---|---|---|
| `scripts/verify_state.py` | сверка JSON-зеркала ↔ SQLite | 1 при расхождениях |
| `scripts/acceptance_check.py` | автоматическая часть приёмки в бой | 1 при блокирующих замечаниях |
| `scripts/check_lock.py` | согласованность версий зависимостей в трёх файлах | 1 при расхождении |
| `scripts/migrate_data.py` | перенос рабочих данных в `data/` с резервной копией и проверками | — |
| `scripts/rasterize_icons.py` | разработческая растеризация SVG → PNG в `assets/icons` (в runtime не нужен) | — |
| `scripts/_bootstrap.py` | единое добавление корня проекта в `sys.path` для скриптов | — |

## 9. Что менять осторожно

| Область | Почему |
|---|---|
| `storage/database.py` (ключи, миграции, outbox) | источник истины, риск потери и задвоения проездов |
| `core/safe_mode.py` | единственная защита рабочей системы при тестах |
| `services/auth.py`, `services/http_api.py` (`/shutdown`) | управление сервисом по секретному токену |
| `services/audit.py` (маскирование) | секреты не должны попасть в журналы |
| `core/paths.py` | переносимость проекта и изоляция тестовых данных |
| `core/anpr_telegram.py` (сеть камеры, разбор событий) | качество распознавания и direction/lanes |
