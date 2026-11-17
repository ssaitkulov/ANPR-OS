import tempfile
import unittest
from pathlib import Path
from photo_archive import save_photo
from storage import Store

class RulesAndArchiveTests(unittest.TestCase):
    def test_photo_file_and_path_survive_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            store = Store(path / 'data.sqlite3')
            try:
                stamp='2026-11-17T12:00:00+05:00'
                filename=save_photo(path / 'images', stamp, '01 ABC', b'jpeg bytes')
                store.set_photo_path(stamp, '01 ABC', filename)
                self.assertEqual(Path(store.get_photo_path(stamp, '01 ABC')).read_bytes(), b'jpeg bytes')
                self.assertIsNone(store.get_photo_path(stamp, 'another'))
                self.assertEqual(save_photo(path / 'images', stamp, '01 ABC', b'new'), filename)
                self.assertEqual(Path(filename).read_bytes(), b'jpeg bytes')
            finally: store.close()

if __name__ == '__main__': unittest.main()
