import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД
import threading
import unittest
from unittest.mock import patch

from app.core.system_events import EventLog
import anpr_telegram as module


class EventLogTests(unittest.TestCase):
    def test_cursor_and_bounded_history(self):
        log = EventLog(capacity=2)
        for i in range(3):
            log.add('test', str(i), '2026-01-01T12:00:00+05:00')
        self.assertEqual([x['id'] for x in log.snapshot(0)['events']], [2, 3])
        self.assertEqual([x['text'] for x in log.snapshot(2)['events']], ['2'])
        self.assertEqual(log.snapshot(3)['events'], [])

    def test_concurrent_writers_have_unique_ordered_ids(self):
        log = EventLog(capacity=400)
        def writer():
            for _ in range(100): log.add('test', 'event', 'now')
        threads = [threading.Thread(target=writer) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        events = log.snapshot()['events']
        self.assertEqual([e['id'] for e in events], list(range(1, 401)))

    def test_delivery_only_recorded_after_actual_attempt(self):
        log = EventLog()
        with patch.object(module, 'EVENTS', log), patch.object(module, 'SEND_EACH_ENTRY', True), \
             patch.object(module, 'build_entry_caption', return_value='caption'), \
             patch.object(module, 'telegram_send', return_value=True):
            module.send_entry_notification({}, [], dry_run=True)
            self.assertEqual(log.snapshot()['events'], [])
            module.send_entry_notification({}, [])
            self.assertEqual(log.snapshot()['events'][0]['kind'], 'telegram_ok')


if __name__ == '__main__': unittest.main()
