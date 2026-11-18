# Иконки (Tabler Icons, вариант Filled)

Источник: [Tabler Icons](https://github.com/tabler/tabler-icons), начертание Filled.
Лицензия MIT — текст сохранён в [LICENSE](LICENSE).

## Что лежит в репозитории

Только иконки, которые реально используются в интерфейсе:

| Файл | Где используется |
|---|---|
| `brand-messenger.svg` | иконка Telegram в уведомлениях |
| `calendar-month.svg` | иконка календаря в окне мониторинга |
| `camera.svg` | иконка камеры |
| `send.svg` | иконка отправки |
| `settings.svg` | иконка настроек |
| `video.svg` | иконка живого видео |
| `search.svg` | источник `assets/search-filled.png` |
| `chart-area-line.svg` | источник `assets/chart-statistics.png` |

Остальной набор (около 1050 иконок) в Git намеренно не попадает — он весит ~2,6 МБ
и в приложении не нужен: полный релиз Tabler Icons можно скачать отдельно.

## Как это работает с рантаймом

Python-интерфейс (Tkinter/CustomTkinter) не умеет SVG, поэтому иконки один раз
растеризуются в PNG и кладутся в `assets/`:

```bash
# Любым SVG-конвертером, например Inkscape или cairosvg (отдельная утилита,
# в зависимости приложения она не нужна)
```

Готовые PNG лежат рядом с исходниками и подключаются напрямую:

- `assets/search-filled.png` ← `filled/search.svg`
- `assets/chart-statistics.png` ← `filled/chart-area-line.svg`
- `assets/calendar-month-filled.png` ← `filled/calendar-month.svg`
- `assets/live-video-filled.png` ← `filled/video.svg`

Чтобы добавить новую иконку в интерфейс: возьмите нужный SVG из полного набора Tabler,
положите его в `filled/` и добавьте строку с именем файла в `.gitignore` (белый список),
растеризуйте в `assets/` и подключите PNG в коде. Файл SVG в рантайме не читается.
