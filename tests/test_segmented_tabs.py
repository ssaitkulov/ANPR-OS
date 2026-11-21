"""Тесты сегментированного переключателя разделов в шапке.

Проверяется:
* переключение вкладок мышью и с клавиатуры;
* синхронизация активного состояния с открытым разделом;
* плавное перемещение подложки и отмена анимации при быстром переключении;
* закрытие контекстной панели диагностики при смене раздела;
* фокус по фону без белой рамки и отсутствие накопления отложенных вызовов Tk.

Живой сервис, камера и база не затрагиваются: общая оснастка UI
(`tests/ui_harness.py`) подменяет их заглушками.
"""
from tests import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД
import time
import unittest
from unittest.mock import patch

from app.ui import icons
from app.ui import theme
from app.ui.header import TAB_ICONS
from tests.ui_harness import ShellTestCase


class SegmentedTabsTestCase(ShellTestCase):
    """Общие условия для проверок навигации в шапке."""

    def setUp(self):
        super().setUp()
        self.tabs = self.header.tabs
        # Пока окно скрыто, Tk не доставляет события вкладкам и не
        # считает их размеры — окно на время проверки показано.
        self.root.deiconify()
        self.root.update_idletasks()
        self.root.update()

    def settle(self, timeout=2.0) -> bool:
        """Дождаться конца анимаций шапки (ожидание по состоянию)."""
        deadline = time.monotonic() + timeout
        while True:
            self.root.update()
            if self.header.motion.pending == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)

    def cover_of(self, key):
        """Положение подложки (x, y, ширина, высота) и вкладки."""
        slot = self.tabs._slot(key)
        cover = self.tabs._geometry
        self.assertIsNotNone(slot, 'геометрия вкладки не посчитана')
        self.assertIsNotNone(cover, 'подложка не поставлена на место')
        return cover, slot


class SegmentedTabsTests(SegmentedTabsTestCase):
    """Сегментированный переключатель разделов в шапке."""

    def test_click_switches_sections(self):
        """Клик по вкладке открывает её раздел."""
        self.click(self.tabs.tabs['settings'].static.frame)
        self.assertEqual(self.shell.current_section, 'settings')
        self.assertEqual(self.tabs.active, 'settings')
        self.click(self.tabs.tabs['monitor'].static.frame)
        self.assertEqual(self.shell.current_section, 'monitor')
        self.assertEqual(self.tabs.active, 'monitor')

    def test_active_state_follows_the_open_section(self):
        """Активна та вкладка, чей раздел открыт."""
        self.shell.show_section('settings')
        self.assertEqual(self.tabs.active, 'settings')
        self.shell.show_section('monitor')
        self.assertEqual(self.tabs.active, 'monitor')

    def test_repeated_click_on_the_active_tab_does_nothing(self):
        """Повторный клик по активной вкладке раздел не перезапускает."""
        calls = []
        original = self.shell.show_section

        def counted(name):
            calls.append(name)
            original(name)

        self.shell.show_section = counted
        self.click(self.tabs.tabs['monitor'].static.frame)
        self.assertEqual(calls, [])
        self.assertEqual(self.shell.current_section, 'monitor')

    def test_switching_closes_the_diagnostics_panel(self):
        """Смена раздела закрывает открытую контекстную панель."""
        self.header.close_details()
        self.header.toggle_details('service')
        self.assertTrue(self.header.details.is_open)
        self.click(self.tabs.tabs['settings'].static.frame)
        self.assertFalse(self.header.details.is_open)

    def test_keyboard_switches_the_section(self):
        """Enter и пробел переключают раздел (как и клик мышью)."""
        frame = self.tabs.tabs['settings'].static.frame
        frame.focus_set()
        self.root.update()
        frame.event_generate('<Return>')
        self.root.update()
        self.assertEqual(self.shell.current_section, 'settings')
        self.click(self.tabs.tabs['monitor'].static.frame)
        frame.focus_set()
        self.root.update()
        frame.event_generate('<space>')
        self.root.update()
        self.assertEqual(self.shell.current_section, 'settings')

    def test_tabs_are_reachable_by_the_tab_key(self):
        """Вкладки получают фокус с клавиатуры (Tab)."""
        for key in ('monitor', 'settings'):
            frame = self.tabs.tabs[key].static.frame
            frame.focus_set()
            self.root.update()
            self.assertIs(self.root.focus_get(), frame)

    def test_active_label_has_no_extra_padding(self):
        """Копия подписи в подложке не получает лишний padx и не обрезается."""
        tab = self.tabs.tabs['monitor']
        with patch.object(tab.slider.frame, 'pack') as pack:
            self.tabs._show_in_pill(tab)
        pack.assert_called_once_with(side='left', padx=0)

    def test_focus_is_visible_without_a_border(self):
        """Фокус виден по фону вкладки, без белой рамки вокруг подложки."""
        frame = self.tabs.tabs['settings'].static.frame
        before = str(frame.cget('fg_color'))
        frame.focus_set()
        frame.event_generate('<FocusIn>')
        self.root.update()
        self.assertIs(self.root.focus_get(), frame)
        self.assertEqual(str(frame.cget('border_width')), '0')
        self.assertNotEqual(str(frame.cget('fg_color')), before)
        self.shell.show_section('monitor')
        self.assertEqual(str(self.tabs.pill.cget('border_width')), '0')

    def test_cover_covers_the_active_tab(self):
        """Подложка совпадает с активной вкладкой и залита акцентом."""
        self.shell.show_section('settings')
        self.settle()
        cover, slot = self.cover_of('settings')
        self.assertEqual(round(cover[0]), round(slot[0]))
        self.assertEqual(round(cover[2]), round(slot[2]))
        # Заливку активной вкладки даёт акцент темы, у неактивной — нет.
        self.assertEqual(self.tabs.pill.cget('fg_color'), theme.ACCENT_DEEP)
        for tab in self.tabs.tabs.values():
            self.assertEqual(tab.static.frame.cget('fg_color'), 'transparent')

    def test_fast_switching_ends_on_the_chosen_tab(self):
        """Быстрое переключение не оставляет подложку не там."""
        for key in ('settings', 'monitor', 'settings', 'monitor', 'settings'):
            self.tabs.select(key)
            self.root.update()
            time.sleep(0.03)
        self.settle()
        cover, slot = self.cover_of('settings')
        self.assertEqual(self.tabs.active, 'settings')
        self.assertEqual(round(cover[0]), round(slot[0]))
        self.assertEqual(round(cover[2]), round(slot[2]))
        # Анимация не оставляет отложенных вызовов Tk.
        self.assertEqual(self.header.motion.pending, 0)

    def test_closing_while_animating_is_clean(self):
        """Закрытие окна во время анимации не даёт ошибок Tk."""
        self.tabs.select('settings')
        self.root.update()
        time.sleep(0.05)
        self.shell._quit()
        self.pump(0.3)
        self.assertEqual(self.errors, [])

    def test_icons_and_colors_come_from_the_design_system(self):
        """Иконки — Tabler Filled, цвета — из палитры темы."""
        available = icons.available()
        for name in TAB_ICONS.values():
            self.assertIn(name, available)
        self.assertEqual(self.tabs.frame.cget('fg_color'), theme.CARD)
        self.assertEqual(self.tabs.pill.cget('fg_color'), theme.ACCENT_DEEP)
        self.settle()
        # Неактивная вкладка — приглушённая подпись, активную закрывает
        # подложка, поэтому её подпись совпадает с фоном контейнера.
        self.assertEqual(self.tabs.tabs['settings'].static.label.cget('text_color'),
                         theme.MUTED)
        self.assertEqual(self.tabs.tabs['monitor'].static.label.cget('text_color'),
                         theme.CARD)


class HeaderHeightTests(SegmentedTabsTestCase):
    """Шапка по высоте не изменилась."""

    def test_header_row_height_is_unchanged(self):
        """Высота строки шапки задаётся индикаторами, а не вкладками."""
        header = self.header
        self.assertEqual(header.frame.winfo_height(), header.control.winfo_height())
        self.assertGreaterEqual(header.frame.winfo_height(),
                                header.tabs.frame.winfo_height())


if __name__ == '__main__':
    unittest.main()
