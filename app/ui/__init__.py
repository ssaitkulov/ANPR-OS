"""Интерфейс CustomTkinter: единое окно приложения и его разделы.

`shell.py` — единое окно (Header + Content + StatusBar, один `mainloop()`);
`dashboard.py` и `settings.py` — разделы «Мониторинг» и «Настройки»;
`panel.py` — совместимый фасад с прежним именем класса `Panel`.

Точки входа в корне проекта (`control_panel.py`, `setup_anpr.py`) — лаунчеры
этих модулей.
"""
