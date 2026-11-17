import tempfile
import unittest
from pathlib import Path
from dashboard import aggregate_hours, direction, load_dashboard
from storage import Store

class DashboardTests(unittest.TestCase):
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
