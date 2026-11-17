import unittest
from passage_photos import PhotoCache
from unittest.mock import patch
import anpr_telegram as m

class PhotoTests(unittest.TestCase):
    def test_cache_bounded_and_exact_event(self):
        cache=PhotoCache(max_items=2,max_bytes=6)
        cache.put('t1','A',b'aa'); cache.put('t2','A',b'bb'); cache.put('t3','B',b'cc')
        self.assertIsNone(cache.get('t1','A'))
        self.assertEqual(cache.get('t2','A'), b'bb')
        self.assertIsNone(cache.get('t2','B'))
        cache.put('t4','C',b'xxxxxxx')
        self.assertIsNone(cache.get('t4','C'))

    def test_only_actual_live_event_photo_is_cached(self):
        cache=PhotoCache()
        record={'iso':'2026-11-17T12:00:00+05:00','plate':'A','lane':'1','speed':'0','time':'12:00:00'}
        with patch.object(m, 'PHOTOS', cache), patch.object(m, 'record_event', return_value=record), \
             patch.object(m, 'prepare_event_images', return_value=[b'camera jpeg']), \
             patch.object(m, 'send_entry_notification'), patch.object(m, 'EVENTS'), \
             patch.object(m, 'save_photo', return_value=None):
            m.flush_pending_events({}, {0:{}}, [b'camera jpeg'])
        self.assertEqual(cache.get(record['iso'], 'A'), b'camera jpeg')

if __name__ == '__main__': unittest.main()
