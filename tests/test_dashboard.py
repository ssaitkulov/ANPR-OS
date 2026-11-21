from tests import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД
import tempfile
import queue
import sqlite3
import threading
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from app.ui import dashboard
from app.ui.dashboard import (_iso_hour, MonitorWindow, MonitoringSection,
                              aggregate_hours, archived_photo, chart_last_hour,
                              direction, load_dashboard)
from app.storage.database import Store

class DashboardTests(unittest.TestCase):
    def test_plate_search_spans_all_period_rows_without_changing_metrics(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'events.sqlite3'
            store = Store(path)
            try:
                for i in range(35):
                    store.add_event(f'recent{i}', {'iso': f'2026-11-17T12:{i:02}:00+05:00',
                                                   'plate': f'OTHER{i}', 'direction': 'entry'})
                store.add_event('older', {'iso': '2026-11-16T23:00:00+05:00',
                                          'plate': '01 D 555 AA', 'direction': 'exit'})
                store.add_event('outside', {'iso': '2026-11-15T23:00:00+05:00',
                                            'plate': '01D555AA', 'direction': 'exit'})
                result = load_dashboard(path, '2026-11-16', '2026-11-17', 'd-555')
                self.assertEqual(result['total'], 36)
                self.assertEqual(result['directions'], {'Въезд': 35, 'Выезд': 1, 'Неизвестно': 0})
                self.assertEqual([row[2] for row in result['recent']], ['01 D 555 AA'])
                missing = load_dashboard(path, '2026-11-17', '2026-11-17', 'd555')
                self.assertEqual(missing['total'], 35)
                self.assertEqual(missing['recent'], [])
                self.assertEqual(len(load_dashboard(path, '2026-11-17', plate_query='  --  ')['recent']), 35)
            finally:
                store.close()

    def test_period_includes_both_boundaries_and_aggregates_hours_and_plates(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'events.sqlite3'
            store = Store(path)
            try:
                events = [
                    ('before', '2026-10-30T20:00:00+05:00', 'X', 'entry'),
                    ('first', '2026-10-31T20:00:00+05:00', 'A', 'entry'),
                    ('last', '2026-11-01T20:30:00+05:00', 'A', 'exit'),
                    ('after', '2026-11-02T20:00:00+05:00', 'Y', 'entry'),
                ]
                for key, stamp, plate, direct in events:
                    store.add_event(key, {'iso': stamp, 'plate': plate, 'direction': direct})
                result = load_dashboard(path, '2026-10-31', '2026-11-01')
                self.assertEqual((result['total'], result['unique'], result['hours'][20]), (2, 1, 2))
                self.assertEqual((result['directions']['Въезд'], result['directions']['Выезд']), (1, 1))
                self.assertEqual(len(result['recent']), 2)
                with self.assertRaises(ValueError):
                    load_dashboard(path, '2026-11-01', '2026-10-31')
                now = datetime(2026, 11, 1, 9, 0)
                self.assertEqual(chart_last_hour(date(2026, 10, 31), now, date(2026, 11, 1)), 23)
            finally:
                store.close()

    def test_historical_day_includes_late_events_and_archived_photo(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'events.sqlite3'
            photo = Path(folder) / 'image.jpg'
            photo.write_bytes(b'archived image')
            store = Store(path)
            try:
                for i in range(35):
                    stamp = f'2026-11-16T23:{i:02}:00+05:00'
                    store.add_event(str(i), {'iso': stamp, 'plate': 'A', 'direction': 'entry'})
                store.add_event('today', {'iso': '2026-11-17T01:00:00+05:00', 'plate': 'B'})
                store.set_photo_path('2026-11-16T23:34:00+05:00', 'A', str(photo))
                history = load_dashboard(path, '2026-11-16')
                self.assertEqual((history['total'], history['hours'][23], len(history['recent'])), (35, 35, 35))
                self.assertEqual(archived_photo(path, history['recent'][0][0]), b'archived image')
                self.assertEqual(load_dashboard(path, '2026-11-17')['total'], 1)
                now = datetime(2026, 11, 17, 9, 0)
                self.assertEqual(chart_last_hour(date(2026, 11, 16), now), 23)
                self.assertEqual(chart_last_hour(date(2026, 11, 17), now), 9)
            finally:
                store.close()

    def test_video_worker_releases_capture_while_disabled_and_reopens(self):
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
        window = MonitorWindow.__new__(MonitorWindow)
        window.stop = threading.Event()
        window.stream_changed = threading.Event()
        window.stream_selection = (0, 0, True)
        window.frames = queue.Queue(maxsize=1)
        window.settings = {}
        with patch.dict('sys.modules', {'cv2': cv2}):
            worker = threading.Thread(target=window.video_worker)
            worker.start()
            try:
                self.assertTrue(reading.wait(2))
                window.stream_selection = (1, 0, False)
                window.stream_changed.set()
                unblock.set()
                self.assertTrue(released.wait(2))
                self.assertFalse(reopened.wait(0.2))
                window.stream_selection = (2, 0, True)
                window.stream_changed.set()
                self.assertTrue(reopened.wait(2))
            finally:
                window.stop.set()
                window.stream_changed.set()
                unblock.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_intervals_group_counts_and_stop_at_current_bin(self):
        hours = [0] * 24
        hours[0], hours[1], hours[3], hours[7] = 1, 2, 4, 8
        self.assertEqual(aggregate_hours(hours, 3, 7), [(0, 3), (3, 4), (6, 8)])
        self.assertEqual(aggregate_hours(hours, 6, 7), [(0, 7), (6, 8)])
        self.assertEqual(len(aggregate_hours(hours, 1, 7)), 8)
        with self.assertRaises(ValueError): aggregate_hours(hours, 2, 7)

    def test_stats_use_full_day_and_distinguish_directions(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'events.sqlite3'
            store = Store(path)
            try:
                store.add_event('a', {'iso': '2026-11-17T13:04:00+05:00', 'plate': 'A', 'direction': 'entry'})
                store.add_event('b', {'iso': '2026-11-17T13:05:00+05:00', 'plate': 'A', 'direction': 'exit'})
                store.add_event('c', {'iso': '2026-11-16T08:00:00+05:00', 'plate': 'B'})
                result = load_dashboard(path, '2026-11-17')
                self.assertEqual((result['total'], result['unique'], result['hours'][13]), (2, 1, 2))
                self.assertEqual(result['directions'], {'Въезд': 1, 'Выезд': 1, 'Неизвестно': 0})
                self.assertEqual(len(result['recent']), 2)
                self.assertEqual(direction('unrecognized'), 'Неизвестно')
            finally:
                store.close()

    def test_closed_period_aggregates_are_cached(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'events.sqlite3'
            store = Store(path)
            self.addCleanup(dashboard._AGG_CACHE.clear)
            dashboard._AGG_CACHE.clear()
            try:
                store.add_event('a', {'iso': '2026-11-17T12:00:00+05:00',
                                      'plate': 'A 123', 'direction': 'entry'})
                with patch.object(dashboard, '_aggregate_stats',
                                  wraps=dashboard._aggregate_stats) as stats:
                    first = load_dashboard(path, '2026-11-17')
                    second = load_dashboard(path, '2026-11-17', plate_query='A123')
                self.assertEqual(stats.call_count, 1)
                self.assertEqual(first['total'], second['total'])
                self.assertEqual(first['hours'], second['hours'])
                self.assertEqual(first['directions'], second['directions'])
            finally:
                store.close()

    def test_new_rows_invalidate_the_closed_period_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'events.sqlite3'
            store = Store(path)
            self.addCleanup(dashboard._AGG_CACHE.clear)
            dashboard._AGG_CACHE.clear()
            try:
                store.add_event('a', {'iso': '2026-11-17T12:00:00+05:00',
                                      'plate': 'A', 'direction': 'entry'})
                self.assertEqual(load_dashboard(path, '2026-11-17')['total'], 1)
                store.add_event('b', {'iso': '2026-11-17T13:00:00+05:00',
                                      'plate': 'B', 'direction': 'exit'})
                self.assertEqual(load_dashboard(path, '2026-11-17')['total'], 2)
            finally:
                store.close()

    def test_today_is_not_cached(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'events.sqlite3'
            store = Store(path)
            self.addCleanup(dashboard._AGG_CACHE.clear)
            dashboard._AGG_CACHE.clear()
            try:
                stamp = date.today().isoformat() + 'T12:00:00+05:00'
                store.add_event('a', {'iso': stamp, 'plate': 'A', 'direction': 'entry'})
                with patch.object(dashboard, '_aggregate_stats',
                                  wraps=dashboard._aggregate_stats) as stats:
                    load_dashboard(path, date.today().isoformat())
                    load_dashboard(path, date.today().isoformat())
                self.assertEqual(stats.call_count, 2)
            finally:
                store.close()

    def test_recent_limit_does_not_change_the_aggregates(self):

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'events.sqlite3'
            store = Store(path)
            try:
                for index in range(10):
                    store.add_event(f'row{index}',
                                    {'iso': f'2026-11-17T12:{index:02}:00+05:00',
                                     'plate': f'P{index}', 'direction': 'entry'})
                all_rows = load_dashboard(path, '2026-11-17', recent_limit=50)
                limited = load_dashboard(path, '2026-11-17', recent_limit=3)
                for key in ('total', 'unique', 'hours', 'directions'):
                    self.assertEqual(limited[key], all_rows[key], key)
                self.assertEqual(len(limited['recent']), 3)
                self.assertEqual([row[2] for row in limited['recent']],
                                 ['P9', 'P8', 'P7'])
            finally:
                store.close()

    def test_search_is_applied_before_the_recent_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'events.sqlite3'
            store = Store(path)
            try:
                for index in range(10):
                    store.add_event(f'row{index}',
                                    {'iso': f'2026-11-17T12:{index:02}:00+05:00',
                                     'plate': f'OTHER{index}', 'direction': 'entry'})
                store.add_event('match', {'iso': '2026-11-17T01:00:00+05:00',
                                          'plate': '01 D 555 AA', 'direction': 'exit'})
                result = load_dashboard(path, '2026-11-17', plate_query='d-555', recent_limit=2)
                self.assertEqual([row[2] for row in result['recent']], ['01 D 555 AA'])
                self.assertEqual(result['total'], 11)
            finally:
                store.close()

    def test_invalid_timestamps_are_counted_but_not_shown_on_the_chart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'events.sqlite3'
            store = Store(path)
            try:
                store.add_event('valid', {'iso': '2026-11-17T12:00:00+05:00',
                                          'plate': 'A', 'direction': 'entry'})
                conn = sqlite3.connect(path)
                try:
                    conn.execute('INSERT INTO events (event_key, day, captured_at, plate, direction) '
                                 "VALUES ('broken', '2026-11-17', 'T25:00:00', 'B', 'exit')")
                    conn.commit()
                finally:
                    conn.close()
                result = load_dashboard(path, '2026-11-17')
                self.assertEqual(result['total'], 2)
                self.assertEqual(result['unique'], 2)
                self.assertEqual(result['hours'][12], 1)
                self.assertEqual(result['directions']['Выезд'], 1)
                self.assertIsNone(_iso_hour('T25:00:00'))
                self.assertEqual(_iso_hour('2026-11-17T12:00:00+05:00'), 12)
            finally:
                store.close()


class _FakeWidget:
    """Заглушка виджета CustomTkinter: запоминает, что на нём нарисовали."""

    created = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        _FakeWidget.created.append(self)

    def winfo_children(self):
        return []

    def destroy(self):
        pass

    def grid(self, **kwargs):
        pass

    def grid_forget(self):
        pass

    def pack(self, **kwargs):
        pass

    def pack_forget(self):
        pass

    def configure(self, **kwargs):
        self.kwargs.update(kwargs)

    def cget(self, option):
        return self.kwargs.get(option)


class _FakeCanvas:
    """Холст графика: считает нарисованные линии, тексты и области."""

    def __init__(self, width=640, height=260):
        self.size = (width, height)
        self.lines = []
        self.texts = []
        self.polygons = []
        self.ovals = []

    def delete(self, _what):
        self.lines.clear()
        self.texts.clear()
        self.polygons.clear()
        self.ovals.clear()

    def winfo_width(self):
        return self.size[0]

    def winfo_height(self):
        return self.size[1]

    def create_line(self, *args, **kwargs):
        self.lines.append((args, kwargs))

    def create_text(self, *args, **kwargs):
        self.texts.append((args, kwargs))

    def create_polygon(self, *args, **kwargs):
        self.polygons.append((args, kwargs))

    def create_oval(self, *args, **kwargs):
        self.ovals.append((args, kwargs))


class _FakeVar:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _FakeFont:
    def measure(self, text):
        return 7 * len(text)


class MonitoringWidgetsTests(unittest.TestCase):
    """График и лента «Мониторинга»: что именно на них попадает."""

    def setUp(self):
        _FakeWidget.created = []
        # Настоящие окна не нужны: лента и график проверяются без Tk.
        for name in ('CTkLabel', 'CTkFrame', 'CTkButton'):
            patcher = patch.object(dashboard.ctk, name, _FakeWidget)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.section = MonitoringSection.__new__(MonitoringSection)
        self.section.visible_count = 30
        self.section.photo_bytes = {}
        self.section.photo_images = {}
        self.section.photo_buttons = {}
        self.section._cards = []
        self.section.search_selection = (0, '')
        self.section.feed = _FakeWidget()
        self.section.graph = _FakeCanvas()
        self.section.chart_interval = _FakeVar('1 ч')
        self.section.chart_axis_font = _FakeFont()
        self.section.hours = [0] * 24
        # Исторический период: график строит все 24 часа независимо от
        # текущего времени (иначе после полуночи остаётся одна точка).
        self.section.display_period = (date(2026, 11, 1), date(2026, 11, 30))
        self.section.tz = ZoneInfo('Europe/Moscow')

    def _texts(self):
        return [widget.kwargs.get('text') for widget in _FakeWidget.created]

    def test_feed_shows_the_plate_and_the_time_of_a_passage(self):
        stamp = '2026-11-17T12:34:00+05:00'
        self.section.render_passages([(7, stamp, '01 D 555 AA', 'Въезд', False)])
        texts = self._texts()
        self.assertIn('№  01 D 555 AA', texts)
        self.assertIn('◷  17.11.2026  12:34:00', texts)
        self.assertIn('Въезд', texts)

    def test_feed_reports_an_empty_period(self):
        self.section.render_passages([])
        texts = self._texts()
        self.assertTrue(any(text and text.startswith('Проездов за выбранный период нет')
                            for text in texts))

    def test_feed_reports_a_failed_load(self):
        self.section.render_passages(None, error=True)
        self.assertIn('Не удалось загрузить данные', self._texts())

    def test_feed_cuts_the_list_and_offers_more(self):
        rows = [(index, '2026-11-17T12:00:00+05:00', 'A', 'Въезд', False)
                for index in range(35)]
        self.section.render_passages(rows)
        self.assertIn('Показать ещё', self._texts())
        cards = [widget for widget in _FakeWidget.created
                 if widget.kwargs.get('fg_color') == '#202d40']
        self.assertEqual(len(cards), 30, 'лента показала больше карточек, чем видно')

    def test_chart_draws_the_curve_and_the_axis(self):
        self.section.hours[13] = 4
        self.section.draw_chart()
        # Сетка по вертикали, подписи оси времени и область под кривой.
        self.assertGreaterEqual(len(self.section.graph.lines), 5)
        self.assertGreaterEqual(len(self.section.graph.texts), 9)
        self.assertTrue(self.section.graph.polygons)
        self.assertTrue(self.section.graph.ovals)

    def test_chart_draws_nothing_in_a_too_small_area(self):
        self.section.graph = _FakeCanvas(width=40, height=20)
        self.section.draw_chart()
        self.assertEqual(self.section.graph.lines, [])
        self.assertEqual(self.section.graph.texts, [])

    def test_chart_of_an_empty_day_shows_a_message(self):
        self.section.draw_chart()
        labels = [kwargs.get('text') for _, kwargs in self.section.graph.texts]
        self.assertIn('Нет проездов за выбранный период', labels)
        self.assertFalse(self.section.graph.polygons)
        self.assertFalse(self.section.graph.ovals)

    def test_chart_does_not_repeat_zero_on_both_axes(self):
        self.section.draw_chart()
        labels = [kwargs.get('text') for _, kwargs in self.section.graph.texts]
        self.assertNotIn('00', labels)
        self.assertEqual(labels.count('0'), 1)

    def test_today_chart_marks_the_current_hour(self):
        today = date.today()
        self.section.display_period = (today, today)
        self.section.draw_chart()
        labels = [kwargs.get('text') for _, kwargs in self.section.graph.texts]
        self.assertIn('сейчас', labels)

    def test_keyboard_cycles_the_chart_interval(self):
        self.section.chart_interval = _FakeVar('1 ч')
        self.section.draw_chart = Mock()
        MonitoringSection._cycle_interval(self.section)
        self.assertEqual(self.section.chart_interval.get(), '3 ч')
        self.section.draw_chart.assert_called_once()

    def test_feed_scrollbar_appears_only_when_needed(self):
        class FakeScrollbar:
            state = None

            def grid(self, **kwargs):
                self.state = 'shown'

            def grid_forget(self):
                self.state = 'hidden'

        bar = FakeScrollbar()
        self.section.feed._scrollbar = bar
        self.section._set_feed_scrollbar(list(range(3)))
        self.assertEqual(bar.state, 'hidden')
        self.section._set_feed_scrollbar(list(range(20)))
        self.assertEqual(bar.state, 'shown')

    def _poll_with_hours(self, hours):
        """Один цикл poll() с готовым ответом данных, без Tk и планировщика."""
        section = self.section
        section.period_selection = (0, None)
        section.search_selection = (0, '')
        section.stream_selection = (0, 0, True)
        section.today = lambda: date(2026, 11, 20)
        section.video_label = SimpleNamespace(winfo_width=lambda: 0, winfo_height=lambda: 0)
        section.frames = queue.Queue()
        section.results = queue.Queue()
        section.photo_results = queue.Queue()
        section.total = _FakeWidget()
        section.unique = _FakeWidget()
        section.entry_metric = _FakeWidget()
        section.exit_metric = _FakeWidget()
        section._feed_content = None
        section.reset_feed_scroll = False
        section.schedule = lambda *args: setattr(section, '_scheduled', args)
        section.draw_chart = Mock()
        section.render_passages = Mock()
        section.set_video_status = Mock()
        section.show_video_message = Mock()
        data = {'total': 1, 'unique': 1, 'hours': hours,
                'directions': {'Въезд': 1, 'Выезд': 0, 'Неизвестно': 0},
                'recent': [(1, '2026-11-20T10:00:00+05:00', 'A', 'entry', 0)]}
        day = date(2026, 11, 20)
        section.results.put_nowait((0, 0, (day, day), 'data', data))
        section.poll()
        return section

    def test_chart_is_not_redrawn_when_hours_are_unchanged(self):
        section = self._poll_with_hours([0] * 24)
        section.draw_chart.assert_not_called()

    def test_chart_is_redrawn_when_hours_change(self):
        hours = [0] * 24
        hours[10] = 3
        section = self._poll_with_hours(hours)
        section.draw_chart.assert_called_once_with()
        self.assertEqual(section.hours, hours)

    def test_poll_runs_at_the_frame_rate(self):
        section = self._poll_with_hours([0] * 24)
        self.assertEqual(section._scheduled[0], 50)

    def test_graph_redraw_is_debounced_on_resize(self):
        section = MonitoringSection.__new__(MonitoringSection)
        section._chart_redraw = None
        calls = []
        section.cancel_after = lambda ident: calls.append(('cancel', ident))
        section.schedule_idle = lambda cb: (calls.append(('schedule', cb)), 'id')[1]
        section._on_graph_configure()
        section._on_graph_configure()
        self.assertEqual([call[0] for call in calls],
                         ['cancel', 'schedule', 'cancel', 'schedule'])
        self.assertEqual([call[1] for call in calls if call[0] == 'cancel'],
                         [None, 'id'])

    def test_already_fitted_video_is_not_resized_again(self):
        image = SimpleNamespace(size=(320, 180))
        with patch.object(dashboard.ImageOps, 'contain') as contain:
            self.assertIs(self.section._fit_video(image, 320, 180), image)
        contain.assert_not_called()

    def test_video_is_fitted_only_when_the_size_changed(self):
        image = SimpleNamespace(size=(640, 360))
        fitted = SimpleNamespace(size=(320, 180))
        with patch.object(dashboard.ImageOps, 'contain', return_value=fitted) as contain:
            result = self.section._fit_video(image, 320, 180)
        self.assertIs(result, fitted)
        contain.assert_called_once_with(image, (320, 180), dashboard.Image.Resampling.LANCZOS)

    def test_video_size_is_cached_from_configure(self):
        self.section.video_label = SimpleNamespace(winfo_width=lambda: 320,
                                                   winfo_height=lambda: 180)
        self.section.fullscreen_label = None
        self.section._on_video_configure()
        self.assertEqual(self.section._video_label_size, (320, 180))
        self.assertEqual(self.section.video_size, (320, 180))

        self.section.fullscreen_label = SimpleNamespace(winfo_width=lambda: 1280,
                                                        winfo_height=lambda: 720)
        self.section._on_video_configure()
        self.assertEqual(self.section._fullscreen_size, (1280, 720))
        self.assertEqual(self.section.video_size, (1280, 720))

    def test_photo_is_decoded_only_once_per_event(self):
        button = _FakeWidget()
        self.section.photo_buttons = {7: button}
        preview = SimpleNamespace(size=(86, 72))
        with patch.object(dashboard.Image, 'open'), \
             patch.object(dashboard.ImageOps, 'contain', return_value=preview) as contain, \
             patch.object(dashboard.ctk, 'CTkImage',
                          side_effect=lambda **kwargs: SimpleNamespace(**kwargs)):
            self.section.set_photo(7, b'jpeg')
            first = button.photo
            self.section.set_photo(7, b'jpeg')
        self.assertIs(button.photo, first)
        contain.assert_called_once()

    def test_feed_reuses_cards_and_images_on_rerender(self):
        rows = [(1, '2026-11-17T12:00:00+05:00', 'A', 'Въезд', False)]
        self.section.photo_bytes = {1: b'jpeg'}
        preview = SimpleNamespace(size=(86, 72))
        with patch.object(dashboard.Image, 'open'), \
             patch.object(dashboard.ImageOps, 'contain', return_value=preview) as contain, \
             patch.object(dashboard.ctk, 'CTkImage',
                          side_effect=lambda **kwargs: SimpleNamespace(**kwargs)):
            self.section.render_passages(rows)
            created = len(_FakeWidget.created)
            self.assertEqual(contain.call_count, 1)
            self.section.render_passages(rows)
        self.assertEqual(contain.call_count, 1)
        self.assertEqual(len(_FakeWidget.created), created)

    def test_card_resets_the_photo_when_another_passage_takes_its_place(self):
        preview = SimpleNamespace(size=(86, 72))
        with patch.object(dashboard.Image, 'open'), \
             patch.object(dashboard.ImageOps, 'contain', return_value=preview), \
             patch.object(dashboard.ctk, 'CTkImage',
                          side_effect=lambda **kwargs: SimpleNamespace(**kwargs)):
            self.section.photo_bytes = {1: b'jpeg'}
            self.section.render_passages([(1, '2026-11-17T12:00:00+05:00', 'A', 'Въезд', False)])
            button = self.section.photo_buttons[1]
            self.assertEqual(str(button.kwargs.get('state')), 'normal')
            self.section.photo_bytes = {}
            self.section.render_passages([(2, '2026-11-17T13:00:00+05:00', 'B', 'Выезд', False)])
        self.assertIs(self.section.photo_buttons[2], button)
        self.assertEqual(str(button.kwargs.get('state')), 'disabled')
        self.assertEqual(button.kwargs.get('text'), 'Нет\nфото')


if __name__ == '__main__': unittest.main()
