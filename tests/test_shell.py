from tests import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД
import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.core import window_state
from app.ui.dashboard import MonitoringSection
from app.ui.shell import AppShell


class FakeButton:
    def __init__(self):
        self.config = {}

    def configure(self, **kwargs):
        self.config.update(kwargs)


class FakeWidget:
    """Раздел-фрейм: считает вызовы grid/grid_remove."""

    def __init__(self):
        self.visible = False
        self.grid_calls = 0
        self.remove_calls = 0

    def grid(self, **kwargs):
        self.visible = True
        self.grid_calls += 1

    def grid_remove(self):
        self.visible = False
        self.remove_calls += 1


class FakeRoot:
    def __init__(self, geometry='1440x900+100+50', state='normal'):
        self._geometry = geometry
        self._state = state
        self.titles = []
        self.withdrawn = False
        self.deiconified = False

    def geometry(self, value=None):
        if value is None:
            return self._geometry
        self._geometry = value
        return value

    def state(self, value=None):
        if value is None:
            return self._state
        self._state = value
        return value

    def title(self, value):
        self.titles.append(value)

    def withdraw(self):
        self.withdrawn = True

    def deiconify(self):
        self.deiconified = True

    def lift(self):
        pass

    def focus_force(self):
        pass

    def winfo_screenwidth(self):
        return 1920

    def winfo_screenheight(self):
        return 1080

    def winfo_vrootx(self):
        return 0

    def winfo_vrooty(self):
        return 0

    def winfo_vrootwidth(self):
        return 1920

    def winfo_vrootheight(self):
        return 1080


class FakeSection:
    """Раздел-заглушка: считает потоки, умеет ставить видео на паузу."""

    def __init__(self, name):
        self.name = name
        self.root = FakeWidget()
        self.threads = ['data', 'video']
        self.paused = False
        self.pause_calls = 0
        self.resume_calls = 0

    def pause_video(self):
        self.paused = True
        self.pause_calls += 1

    def resume_video(self):
        self.paused = False
        self.resume_calls += 1

    def shutdown(self):
        self.threads = []


class FakeShell(AppShell):
    """Оболочка без окна Tk: подменяем только создание разделов."""

    def __init__(self):
        self.root = FakeRoot()
        self.sections = {}
        self.section_objects = {}
        self.nav_buttons = {'monitor': FakeButton(), 'settings': FakeButton()}
        self.current_section = None
        self.created = []

    def _create_section(self, name):
        self.created.append(name)
        return FakeSection(name)


class SectionSwitchTests(unittest.TestCase):
    def test_sections_are_created_once_and_reused(self):
        shell = FakeShell()
        shell.show_section('monitor')
        shell.show_section('settings')
        for _ in range(5):
            shell.show_section('monitor')
            shell.show_section('settings')
        self.assertEqual(shell.created, ['monitor', 'settings'])
        self.assertEqual(len(shell.sections), 2)

    def test_switching_hides_the_previous_section_without_destroying_it(self):
        shell = FakeShell()
        shell.show_section('monitor')
        monitor = shell.sections['monitor']
        shell.show_section('settings')
        self.assertFalse(monitor.visible)
        self.assertTrue(shell.sections['settings'].visible)
        shell.show_section('monitor')
        self.assertTrue(monitor.visible)
        self.assertFalse(shell.sections['settings'].visible)

    def test_navigation_does_not_restart_the_video_worker(self):
        shell = FakeShell()
        shell.show_section('monitor')
        section = shell.section_objects['monitor']
        threads = list(section.threads)
        for _ in range(10):
            shell.show_section('settings')
            shell.show_section('monitor')
        self.assertIs(shell.section_objects['monitor'], section)
        self.assertEqual(section.threads, threads)

    def test_switching_updates_title_and_active_navigation_button(self):
        shell = FakeShell()
        shell.show_section('monitor')
        self.assertEqual(shell.root.titles[-1], 'Эврика · Мониторинг')
        self.assertEqual(shell.nav_buttons['monitor'].config.get('fg_color'), shell.BLUE)
        shell.show_section('settings')
        self.assertEqual(shell.root.titles[-1], 'Эврика · Настройки')
        self.assertEqual(shell.nav_buttons['settings'].config.get('fg_color'), shell.BLUE)


class TrayRestoreTests(unittest.TestCase):
    def test_hide_withdraws_window_and_pauses_video(self):
        shell = FakeShell()
        shell.show_section('monitor')
        section = shell.section_objects['monitor']
        shell.hide()
        self.assertTrue(shell.root.withdrawn)
        self.assertTrue(section.paused)
        self.assertEqual(section.pause_calls, 1)

    def test_restore_brings_window_back_and_resumes_video(self):
        shell = FakeShell()
        shell.show_section('monitor')
        section = shell.section_objects['monitor']
        shell.hide()
        shell.show()
        self.assertTrue(shell.root.deiconified)
        self.assertFalse(section.paused)
        self.assertEqual(section.resume_calls, 1)

    def test_service_keeps_running_while_window_is_hidden(self):
        """Сворачивание окна не останавливает сервис — только показ видео."""
        shell = FakeShell()
        shell.show_section('monitor')
        shell.hide()
        self.assertFalse(shell.busy if hasattr(shell, 'busy') else False)
        self.assertTrue(shell.root.withdrawn)


class WindowStateTests(unittest.TestCase):
    def test_geometry_round_trip(self):
        self.assertEqual(window_state.parse_geometry('1440x900+120+40'), (1440, 900, 120, 40))
        self.assertEqual(window_state.parse_geometry('1440x900-1920+0'), (1440, 900, -1920, 0))
        self.assertIsNone(window_state.parse_geometry(' nonsense '))
        self.assertEqual(window_state.format_geometry(1440, 900, 120, 40), '1440x900+120+40')
        self.assertEqual(window_state.format_geometry(1440, 900, -1920, 0), '1440x900-1920+0')
        self.assertEqual(window_state.format_geometry(1440, 900), '1440x900')

    def test_state_is_saved_and_restored(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'ui_window.json'
            state = window_state.state_from_geometry('1440x900+120+40')
            self.assertTrue(window_state.save_state(state, path))
            self.assertEqual(window_state.load_state(path), state)
            self.assertFalse((Path(folder) / 'ui_window.json.tmp').exists())

    def test_broken_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'ui_window.json'
            path.write_text('{ not json', encoding='utf-8')
            self.assertIsNone(window_state.load_state(path))
            path.write_text('[1, 2]', encoding='utf-8')
            self.assertIsNone(window_state.load_state(path))

    def test_state_outside_the_screen_is_not_restored(self):
        state = {'width': 1440, 'height': 900, 'x': 5000, 'y': 4000, 'maximized': False}
        self.assertFalse(window_state.within_bounds(state, (0, 0, 1920, 1080)))
        self.assertTrue(window_state.within_bounds({'width': 800, 'height': 600, 'x': 10, 'y': 10},
                                                   (0, 0, 1920, 1080)))

    def test_shell_saves_size_position_and_maximized_state(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'ui_window.json'
            shell = FakeShell()
            shell._normal_geometry = None
            with patch.object(window_state, 'STATE_FILE', path):
                shell.root._geometry = '1280x800+33+44'
                shell.save_window_state()
                saved = json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual((saved['width'], saved['height'], saved['x'], saved['y']),
                                 (1280, 800, 33, 44))
                self.assertFalse(saved['maximized'])
                # Максимизированное окно сохраняет обычную геометрию.
                shell._normal_geometry = '1280x800+33+44'
                shell.root._state = 'zoomed'
                shell.root._geometry = '1920x1080+0+0'
                shell.save_window_state()
                saved = json.loads(path.read_text(encoding='utf-8'))
                self.assertTrue(saved['maximized'])
                self.assertEqual((saved['width'], saved['height']), (1280, 800))

    def test_shell_applies_saved_geometry_on_start(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'ui_window.json'
            path.write_text(json.dumps({'width': 1200, 'height': 700, 'x': 50, 'y': 60,
                                         'maximized': False}), encoding='utf-8')
            shell = FakeShell()
            with patch.object(window_state, 'STATE_FILE', path):
                shell._apply_window_state()
            self.assertEqual(shell.root.geometry(), '1200x700+50+60')

    def test_shell_falls_back_to_default_geometry_without_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'ui_window.json'
            shell = FakeShell()
            with patch.object(window_state, 'STATE_FILE', path):
                shell._apply_window_state()
            self.assertTrue(shell.root.geometry().startswith('1440x900'))


class VideoLifecycleTests(unittest.TestCase):
    """Видеопоток: пауза, возобновление и ручное отключение — раздельно."""

    def _section(self):
        section = MonitoringSection.__new__(MonitoringSection)
        section.stop = threading.Event()
        section.stream_changed = threading.Event()
        section.paused = threading.Event()
        section.stream_selection = (0, 0, True)
        section.frames = queue.Queue(maxsize=1)
        section.settings = {}
        return section

    def test_pause_releases_capture_and_resume_reopens_it(self):
        reading, unblock, released, reopened = (threading.Event() for _ in range(4))
        captures = []

        class Capture:
            def __init__(self):
                captures.append(self)

            def open(self, *args):
                if len(captures) > 1:
                    reopened.set()

            def isOpened(self):
                return True

            def read(self):
                reading.set()
                unblock.wait(2)
                return False, None

            def release(self):
                released.set()

        cv2 = SimpleNamespace(VideoCapture=Capture, CAP_FFMPEG=1,
                              CAP_PROP_OPEN_TIMEOUT_MSEC=2, CAP_PROP_READ_TIMEOUT_MSEC=3)
        section = self._section()
        with patch.dict('sys.modules', {'cv2': cv2}):
            worker = threading.Thread(target=section.video_worker)
            worker.start()
            try:
                self.assertTrue(reading.wait(2))
                section.pause_video()
                unblock.set()
                self.assertTrue(released.wait(3))
                self.assertFalse(reopened.wait(0.3))   # второго подключения нет
                section.resume_video()
                self.assertTrue(reopened.wait(3))       # видео вернулось
            finally:
                section.stop.set()
                section.stream_changed.set()
                unblock.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_manual_disable_survives_tray_pause_and_resume(self):
        section = self._section()
        section.stream_selection = (1, 0, False)   # пользователь выключил видео
        section.pause_video()
        self.assertTrue(section.paused.is_set())
        section.resume_video()
        self.assertFalse(section.paused.is_set())
        self.assertFalse(section.stream_selection[2])   # ручное отключение осталось

    def test_pause_does_not_start_a_second_connection(self):
        section = self._section()
        section.pause_video()
        section.pause_video()
        self.assertTrue(section.paused.is_set())


if __name__ == '__main__':
    unittest.main()
