# Иконки (Tabler Icons, вариант Filled)

Источник: [Tabler Icons](https://github.com/tabler/tabler-icons), начертание Filled.
Лицензия MIT — текст сохранён в [LICENSE](LICENSE).

## Что лежит в репозитории

Только иконки, которые реально используются в интерфейсе:

| Файл | Где используется |
|---|---|
| `brand-messenger.svg` | иконка Telegram (шапка и панель управления) |
| `calendar-month.svg` | иконка календаря в окне мониторинга |
| `camera.svg` | иконка камеры |
| `chevron-down.svg` | стрелка раскрытия в шапке |
| `send.svg` | иконка отправки |
| `settings.svg` | иконка настроек |
| `video.svg` | иконка живого видео |
| `search.svg` | источник `assets/search-filled.png` |
| `chart-area-line.svg` | источник `assets/chart-statistics.png` |
| `alert-triangle.svg` | знак ошибки в панели управления |
| `circle-check.svg` | знак подтверждения в панели управления |
| `device-cctv.svg` | иконка камеры ANPR (шапка и панель управления) |
| `device-desktop.svg` | иконка ANPR-сервиса (шапка и панель управления) |
| `file-text.svg` | иконка журнала сервиса |
| `player-play.svg` | иконка запуска сервиса |
| `player-skip-back.svg` | иконка перезапуска сервиса |
| `player-stop.svg` | иконка остановки сервиса |
| `x.svg` | иконка закрытия панели управления |

Остальной набор (около 1050 иконок) в Git намеренно не попадает — он весит ~2,6 МБ
и в приложении не нужен: полный релиз Tabler Icons можно скачать отдельно.

## Как это работает с рантаймом

Python-интерфейс (Tkinter/CustomTkinter) не умеет SVG, поэтому иконки один раз
растеризуются в PNG и кладутся в `assets/`:

```bash
# Скрипт репозитория: SVGLib + ReportLab + pypdfium2 (утилиты для разработки,
# в самом приложении они не нужны)
python scripts/rasterize_icons.py
```

Скрипт пишет белые силуэты с альфа-каналом в `assets/icons/`. Цвет иконки задаётся
во время выполнения (кэш `app/ui/icons.py`), поэтому один и тот же PNG работает
для любого состояния.

Готовые PNG лежат рядом с исходниками и подключаются напрямую:

- `assets/search-filled.png` ← `filled/search.svg`
- `assets/chart-statistics.png` ← `filled/chart-area-line.svg`
- `assets/calendar-month-filled.png` ← `filled/calendar-month.svg`
- `assets/live-video-filled.png` ← `filled/video.svg`
- `assets/icons/<имя>-filled.png` ← `filled/<имя>.svg` (шапка и панель управления)

Чтобы добавить новую иконку в интерфейс: возьмите нужный SVG из полного набора Tabler,
положите его в `filled/` и добавьте строку с именем файла в `.gitignore` (белый список),
добавьте имя в список `ICONS` в `scripts/rasterize_icons.py`, растеризуйте и
подключите PNG через `app/ui/icons.py`. Файл SVG в рантайме не читается.
