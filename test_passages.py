import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД
import tempfile
import unittest
from pathlib import Path
from storage import Store

class PassageTests(unittest.TestCase):
    def test_recent_passages_sorted_and_persisted_without_photos(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'events.db')
            try:
                store.add_event('one', {'iso': '2026-11-16T12:01:00+05:00', 'plate': 'OLD'})
                store.add_event('two', {'iso': '2026-11-16T12:02:00+05:00', 'plate': 'NEW', 'lane': '1'})
                self.assertEqual([row['plate'] for row in store.recent_passages()], ['NEW', 'OLD'])
                self.assertEqual(store.recent_passages(limit=1)[0]['lane'], '1')
                self.assertNotIn('image', store.recent_passages()[0])
            finally:
                store.close()

if __name__ == '__main__': unittest.main()
