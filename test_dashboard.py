import tempfile
import queue
import threading
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from dashboard import MonitorWindow, aggregate_hours, archived_photo, chart_last_hour, direction, load_dashboard
from storage import Store

class DashboardTests(unittest.TestCase):
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

if __name__ == '__main__': unittest.main()
