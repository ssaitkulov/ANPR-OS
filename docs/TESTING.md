# TESTING — тесты ANPR OS Fayz

Как устроен тестовый набор, что запускать при каком изменении и где проходит
граница между «быстрая проверка» и «полный прогон». Карта кода — в
[`CODEMAP.md`](CODEMAP.md), правила работы агентов — в
[`AGENTS.md`](../AGENTS.md), разовый аудит набора — в
[`TEST_AUDIT.md`](TEST_AUDIT.md).

## 1. Факты о наборе

| Показатель | Значение |
|---|---|
| Тестовых файлов | 35 (`tests/test_*.py`) + `tests/anpr_testenv.py` (изоляция) + `tests/ui_harness.py` (общая оснастка UI) |
| Тестов собрано | **475** (одинаково у pytest и `unittest discover`) |
| Уникальных имён тестов | 475 — совпадений имён нет (см. §4) |
| Полный прогон | ~1 мин 35 с: `python -m pytest -q` — 94 с, `python -m unittest discover` — 96 с |
| UI-часть | 80 с на 7 файлов (`test_service_panel.py`, `test_context_panel.py`, `test_stage2_ui.py`, `test_shell.py`, `test_dashboard.py`, `test_panel_status.py`, `test_segmented_tabs.py`) |
| Быстрый smoke | `python -m pytest -q tests/test_stream_latency.py tests/test_setup_anpr.py` — 0,3 с |
| Быстрая выборка для CI | `python -m unittest tests.test_safe_mode tests.test_control_auth tests.test_paths` — 62 теста, 0,9 с |
| Изоляция | `app/core/safe_mode.py`: рабочие данные уходят во временный каталог, сеть, процессы и запуск сервиса запрещены |

Полный набор отрабатывает за полторы минуты — это последний шаг перед релизом,
а не привычка перед каждым коммитом. Локальный коммит гоняет только тесты
затронутой области (§3, §4). Запустить полную проверку одной командой, включая
вспомогательные скрипты (зависимости, документация, сверка источников, приёмка
без сервиса), можно через `scripts\local_ci.bat` — это локальный аналог CI на
случай, когда GitHub Actions недоступен.

Где уходит время: UI-тесты поднимают настоящее окно Tk — на них приходится
~80 с из 94. Остальные 330 тестов укладываются в ~14 секунд. Ожидание в UI-тестах
идёт **по состоянию, а не по часам**: в `tests/ui_harness.py` есть
`pump_until(условие, таймаут)`, `wait_idle()` и `settle()` — они крутят цикл
событий Tk, пока состояние не изменится, с ограничением по времени. Стоит
проверять состояние сразу, как оно наступило: `pump(0.4)` в шапке теста означает
«ждать 0,4 с», даже если состояние изменилось через 5 мс.

## 2. Классификация тестов

| Класс | Что проверяет | Граница | Файлов | Тестов |
|---|---|---|---|---|
| **UNIT** | одну функцию или модуль в отрыве от системы | нет сети, нет живого сервиса, нет боевой базы; только временные каталоги | 14 | 61 |
| **INTEGRATION** | несколько модулей вместе: ядро + SQLite + очередь, HTTP-сервис + токен, интерфейс без окна | сеть замкана, потоки и процессы — заглушки | 19 | 388 |
| **ACCEPTANCE** | соответствие боевому регламенту: сверка источников, инструменты приёмки | идёт по рабочим данным, код возврата важен | 2 | 25 |

### 2.1 UNIT (14 файлов, 61 тест)

| Файл | Тестов | Предмет |
|---|---|---|
| `test_paths.py` | 17 | `app/core/paths.py`: пути от корня проекта, переносимость, прежние места данных |
| `test_camera_events.py` | 9 | разбор событий камеры: текст snapManager, группировка, отбор проездов (`is_countable`, направление, полоса) |
| `test_storage_and_outbox.py` | 5 | `app/storage/database.py`: SQLite и очередь во временной базе |
| `test_app_preferences.py` | 4 | `app/core/preferences.py`: настройки панели, реестр Windows |
| `test_event_images.py` | 4 | `app/storage/event_images.py`: вырезка номера |
| `test_directions.py` | 3 | направление движения, фильтр `DIRECTION` |
| `test_archive_time.py` | 3 | время записи архива камеры |
| `test_manual_snapshots.py` | 3 | фильтр ручных снимков камеры (`INCLUDE_MANUAL_SNAPSHOTS`) |
| `test_setup_anpr.py` | 3 | `app/ui/settings.py`: чтение, валидация, сохранение |
| `test_passage_photos.py` | 2 | `app/storage/passage_photos.py`: кэш снимков |
| `test_system_events.py` | 3 | `app/core/system_events.py`: журнал в памяти |
| `test_stream_latency.py` | 3 | задержка обработки потока камеры |
| `test_ai_and_photo_archive.py` | 1 | `app/storage/photo_archive.py` + путь фото в базе |
| `test_passages.py` | 1 | проезды: ключ и нормализация номера |

### 2.2 INTEGRATION (19 файлов, 388 тестов)

| Файл | Тестов | Предмет |
|---|---|---|
| `test_service_panel.py` | 36 | панель управления сервисом: состояния, действия, подтверждения |
| `test_safe_mode.py` | 30 | изоляция: запрет сети и процессов, увод путей, учёт потоков, детектор тестового прогона, `ANPR_SAFE_MODE=0` |
| `test_stage2_ui.py` | 33 | шапка, навигация, жизненный цикл Tk, клавиатура и DPI |
| `test_stage2_resilience.py` | 29 | устойчивость ядра: ротация журналов, потоки, обрывы, ожидание потоков при остановке |
| `test_stage3_quality.py` | 27 | регрессии ядра: дедупликация, backfill, отчёт, очередь, маскировка секретов |
| `test_context_panel.py` | 23 | контекстная панель по выбранному индикатору |
| `test_audit_log.py` | 22 | `app/services/audit.py`: журналы управления, маскирование секретов, чтение битых строк |
| `test_passage_storage.py` | 19 | `app/storage/database.py`: проезды, миграции схемы, фото |
| `test_shell.py` | 21 | `app/ui/shell.py`: оболочка окна, разделы, состояние, health-опрос, дебаунс геометрии |
| `test_service_instance.py` | 22 | один экземпляр сервиса и панели, владелец порта, `describe` процесса, запуск сервиса |
| `test_control_auth.py` | 15 | `app/services/auth.py` + `POST /shutdown`: токен, `401`, защита процесса |
| `test_dashboard.py` | 56 | `app/ui/dashboard.py`: загрузка данных, лента проездов, график, видео, кэш фото и агрегатов, дебаунс ресайза, пустой график, календарь, режимы день/диапазон |
| `test_stage4_polish.py` | 12 | боевой режим: отчёт, фотоархив, получатели |
| `test_segmented_tabs.py` | 12 | сегментированный переключатель разделов в шапке |
| `test_outbox_reliability.py` | 10 | backoff, `dead`, доставка по чатам, фоновый поток |
| `test_http_api.py` | 8 | `app/services/http_api.py`: `/health`, `/events`, `/passages`, фото, `/status`, `/entries/today`, ручные команды |
| `test_delivery_queue.py` | 5 | очередь недоставленных на подменах: архивная рассылка, частичная доставка |
| `test_panel_status.py` | 5 | `app/ui/status.py` через `Panel.update_status` |
| `test_live_duplicates.py` | 3 | дедупликация живых событий |

### 2.3 ACCEPTANCE (2 файла, 25 тестов)

| Файл | Тестов | Предмет |
|---|---|---|
| `test_acceptance_tools.py` | 19 | `scripts/acceptance_check.py`, `scripts/check_lock.py`, `scripts/test_select.py`: код возврата, блокирующие замечания, выбор тестов по изменённым файлам |
| `test_verify_state.py` | 6 | `scripts/verify_state.py`: сверка JSON-зеркала ↔ SQLite |

Живая приёмка (24 ч работы, обрывы камеры и Telegram, доставка всем
получателям) — ручной чек-лист [`ACCEPTANCE.md`](ACCEPTANCE.md); его
автоматическая часть — `python scripts/acceptance_check.py` (код 0 — «в бой»).

## 3. Что запускать при каком изменении

| Изменение | Минимальный достаточный прогон |
|---|---|
| Только `*.md`, `.gitignore` | `python scripts/check_docs.py` — ссылки и структура. Полный прогон **не нужен** |
| Один модуль `app/core/*` | `python -m pytest tests/test_stage3_quality.py tests/test_stage2_resilience.py` (для `anpr_telegram.py` — плюс `test_camera_events.py`) |
| `app/core/safe_mode.py`, `tests/__init__.py`, `tests/conftest.py`, `tests/anpr_testenv.py`, `tests/ui_harness.py` | **полный набор** — изоляция и общая оснастка влияют на всё |
| `app/core/paths.py` | `tests/test_paths.py tests/test_passage_storage.py` |
| `app/storage/*` | `tests/test_passage_storage.py tests/test_storage_and_outbox.py tests/test_outbox_reliability.py tests/test_delivery_queue.py` |
| `app/services/*` (токен, аудит, управление) | `tests/test_control_auth.py tests/test_audit_log.py tests/test_service_instance.py tests/test_http_api.py` |
| `app/ui/*` | `tests/test_shell.py tests/test_panel_status.py tests/test_stage2_ui.py tests/test_service_panel.py tests/test_context_panel.py tests/test_dashboard.py tests/test_segmented_tabs.py` |
| Зависимости, `pyproject.toml` | `python scripts/check_lock.py` + полный прогон |
| Перед релизом | **полный прогон** + `check_lock.py` + `verify_state.py` + `acceptance_check.py` + чек-лист [`ACCEPTANCE.md`](ACCEPTANCE.md) |

Полный прогон: `python -m unittest discover` (или `python -m pytest -q`).

Локальный хук `.githooks/pre-commit` делает это сам: он спрашивает
`scripts/test_select.py`, какие файлы в индексе, и запускает тесты затронутой
области (для правки только документации — проверку ссылок и smoke-тест).
Полный набор перед релизом — обязателен.

Правила выбора (проверяются в `tests/test_acceptance_tools.py::TestSelectionTests`):

| Что попало в индекс | Что запускается |
|---|---|
| только `*.md`, `.gitignore`, конфигурация CI | `scripts/check_docs.py` + smoke-тест (`test_stream_latency.py`, `test_setup_anpr.py`) |
| один модуль приложения | тесты его области (см. таблицу выше) |
| один тестовый файл | только он |
| общая оснастка (`tests/ui_harness.py`, `tests/anpr_testenv.py`, `conftest.py`) или `app/core/safe_mode.py` | **полный набор** |
| файл, которому нет соответствия (картинка, конфиг) | **полный набор** |

## 4. Дублирование проверок

### 4.1 Что сделано

| Было | Стало | Итог |
|---|---|---|
| CI гонял и `unittest discover`, и `pytest` над одним набором: 8 полных прогонов в матрице 3.11–3.14 ≈ 24 мин | в CI один полный прогон (`pytest`) + быстрая проверка совместимости на `unittest` (62 теста, 0,9 с) | 4 полных прогона ≈ 12 мин |
| Хук гонял полный набор перед каждым коммитом (~3 мин) | хук запускает тесты затронутой области по файлам индекса | коммит с правкой UI — секунды вместо минут |
| 12 тестов имели одинаковое имя в двух файлах | 0 — совпадений имён в наборе нет (см. §4.2) | −12 |
| Дубли по поведению: доставка очереди, «попытки исчерпаны → dead», `SAVE_PHOTOS`, конкуренция добора, «чужой процесс», изоляция БД, `GroupID` | оставлено по одной проверке в более строгом месте (настоящая база вместо подмены) | −10 тестов |
| `tests/test_manual_snapshots.py` проверял выражение, переписанное в самом тесте | проверяет production-код `_apply_archive_records` | +1 полезный тест |
| `tests/test_safe_mode.py` проверял текст исходника `safe_mode.py` | проверяет поведение детектора тестового прогона | +1 полезный тест |
| `test_concurrent_backfill_does_not_duplicate_rows` зависел от того, как потоки разошлись по времени (голая `backfill_once` без сериализации) | тест идёт через `run_backfill` — с тем же мьютексом, что и в бою; гонки больше нет | тест перестал флакать |

Факт: было 373 теста и 177 с полного прогона — стало 368 тестов и 131 с.
Тестов меньше, а покрытых рисков больше (§5).

### 4.2 Повторный проход: оснастка UI и порядок выполнения

Отдельная работа по набору (шапка и контекстная панель): дубли имён, скорость
UI-тестов и зависимость результатов от порядка запуска.

| Было | Стало | Итог |
|---|---|---|
| `test_service_panel.py` и `test_stage2_ui.py` — по три теста с одинаковым именем (`test_stop_requires_confirmation`, `test_restart_requires_confirmation`, `test_start_does_not_ask_confirmation`) | сценарии различаются: в `test_service_panel.py` — диалог подтверждения (текст вопроса, отказ ничего не делает), в `test_stage2_ui.py` — путь переключателя в шапке. Тесты переименованы: `test_toggle_to_stop_asks_confirmation`, `test_restart_asks_confirmation`, `test_toggle_to_start_asks_no_confirmation` | 0 совпадений имён, обе проверки сохранены |
| `pump(0.4)` — фиксированная пауза на каждый шаг UI-теста (около 4 минут на UI-часть) | `pump_until(условие, таймаут)`, `wait_idle()`, `settle()`: ожидание по состоянию с ограничением по времени | UI-часть: 146 с → 80 с |
| Полный прогон под pytest в HEAD зависал на 105 с в `test_restart_requires_confirmation` и падал (код 1) | причина — PIL освобождал снимок Tk вызовом из фонового потока; вызов ждал главного окна. В `tests/anpr_testenv.py` освобождение снимка в чужом потоке пропускается | полный прогон 573 с с падением → 94 с, зелёный |
| Счётчики заглушки накапливались между тестами: `patch('app.ui.shell.service.start', ...)` возвращает подменённый атрибут как обычное значение, и родительская заглушка его больше не сбрасывает | `reset_calls()` сбрасывает сами атрибуты заглушки | тесты не зависят от порядка запуска |

### 4.3 Что осталось

| Что | Где | Почему не трогаем сейчас |
|---|---|---|
| ~200 строк копипастой оснастки UI | уже вынесены в `tests/ui_harness.py` | сделано |
| «Ядро + очередь + доставка» в 5 файлах | `test_outbox_reliability.py`, `test_delivery_queue.py`, `test_storage_and_outbox.py`, `test_stage3_quality.py`, `test_stage4_polish.py` | это разные уровни (подмены против настоящей базы) — сводить без потери проверок нельзя |

## 5. Пробелы покрытия

Аудит ([`TEST_AUDIT.md`](TEST_AUDIT.md)) нашёл 14 пробелов, 7 — в критичной
логике. Закрыты:

| Пробел | Чем закрыт |
|---|---|
| Миграция очереди `_upgrade_outbox` (колонка `next_attempt_at`) | `test_passage_storage.py::LegacyOutboxMigrationTests` |
| Пересборка старой `events` с одинаковым содержимым | `test_passage_storage.py::LegacyMigrationTests` |
| Миграция ключа при совпадении содержимого | `test_passage_storage.py::DuplicateContentMigrationTests` |
| Разбор событий камеры: `is_countable`, полоса, направление, `parse_kv_text`/`extract_events`, boundary | `test_camera_events.py` |
| Ручные снимки при `INCLUDE_MANUAL_SNAPSHOTS=0` | `test_manual_snapshots.py` (через `_apply_archive_records`) |
| Детектор тестового прогона вместо проверки текста исходника | `test_safe_mode.py` |

Закрыты повторным проходом (список пробелов — в [`TEST_AUDIT.md`](TEST_AUDIT.md) §4):

| Пробел | Чем закрыт |
|---|---|
| HTTP-эндпоинты кроме `/health` и `/shutdown` | `test_http_api.py` — восемь эндпоинтов через `TestClient` |
| `panel_lock.py` — один экземпляр окна панели | `test_service_instance.py::PanelInstanceTests` |
| `paths.legacy_data()` и `paths.ensure_data_dirs()` | `test_paths.py::LegacyDataTests` |
| Ветка `ANPR_SAFE_MODE=0` | `test_safe_mode.py::SafeModeFlagTests` (отдельный экземпляр модуля, защита рабочего процесса не трогается) |
| `process_info.port_owner_pid` и `describe` | `test_service_instance.py::PortOwnerTests` |
| Чтение журнала аудита при битых строках | `test_audit_log.py::JournalReadTests` |
| График и лента «Мониторинга» | `test_dashboard.py::MonitoringWidgetsTests` |
| Запуск сервиса: занятый порт, нет `.env`, процесс упал | `test_service_instance.py::StartTests` |

Не закрыты (план — в аудите): низкий приоритет — `app/ui/motion.py`,
`app/ui/widgets.py`, `app/ui/settings.py`.

## 6. Правила, которые нельзя ослаблять

- Изоляция включена **до** импорта модулей приложения (`tests/__init__.py`, корневой
  `conftest.py`). Правка этого порядка ломает безопасность рабочей системы.
- Любой новый тест обязан работать без сети и без живого сервиса; «а у меня локально
  прошло» — не довод.
- Регрессионное покрытие критичной логики (`event_key`, очередь доставки, токен
  остановки, маскирование секретов, изоляция) не сокращается ради скорости.
- Тест проверяет поведение production-кода, а не текст исходника и не
  переписанное в тесте выражение.
- Новый тест — не «ещё один файл на всякий случай», а проверка конкретного
  поведения; дубли имён и предметов — повод пересмотреть набор, а не повод
  добавить ещё один.

## 7. Аудит набора тестов

Разовый аудит проведён и лежит в [`TEST_AUDIT.md`](TEST_AUDIT.md): таблицы
дубликатов и пробелов, измеренное покрытие, приоритетный план. Промпт для
повторного аудита (если набор снова изменится) — там же, раздел «Промпт».
