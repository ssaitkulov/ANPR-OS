from tests import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import anpr_telegram as bot


def part(kind, body):
    return (b'--test\r\nContent-Type: ' + kind + b'\r\nContent-Length: '
            + str(len(body)).encode() + b'\r\n\r\n' + body + b'\r\n')


EVENT = part(b'text/plain', b'Events[0].TrafficCar.PlateNumber=TEST123\n')
IMAGE = part(b'image/jpeg', b'\xff\xd8test\xff\xd9')


class StreamLatencyTests(unittest.TestCase):
    def test_photo_is_flushed_while_camera_silent(self):
        released = threading.Event()

        class Raw:
            first = True

            def read1(self, size, decode_content=True):
                if self.first:
                    self.first = False
                    return EVENT + IMAGE
                released.wait(3)
                return b''

        response = SimpleNamespace(raw=Raw(), headers={'Content-Type': 'multipart/x-mixed-replace; boundary=test'})
        sent = []
        def send(state, events, images, dry_run=False):
            sent.append((time.monotonic(), images))
            released.set()
        started = time.monotonic()
        try:
            with patch.object(bot, 'flush_pending_events', side_effect=send), patch.object(bot, 'maybe_send_report'):
                bot.consume_camera_stream(response, {}, image_wait=0.05)
        finally:
            released.set()
        self.assertEqual(len(sent), 1)
        self.assertLess(sent[0][0] - started, 1.5)
        self.assertEqual(len(sent[0][1]), 1)

    def test_two_images_stay_together_and_heartbeat_does_not_duplicate(self):
        response = SimpleNamespace(headers={'Content-Type': 'multipart/mixed; boundary=test'})
        wire = [EVENT, IMAGE, IMAGE, part(b'text/plain', b'Heartbeat')]
        with patch.object(bot, 'stream_chunks_with_ticks', return_value=(x for x in wire)), patch.object(bot, 'flush_pending_events') as send, patch.object(bot, 'maybe_send_report'):
            bot.consume_camera_stream(response, {})
        self.assertEqual(send.call_count, 1)
        self.assertEqual(len(send.call_args.args[2]), 2)

    def test_complete_photo_retained_on_disconnect(self):
        response = SimpleNamespace(headers={'Content-Type': 'multipart/mixed; boundary=test'})
        def wire():
            yield EVENT + IMAGE
            raise OSError('disconnected')
        with patch.object(bot, 'stream_chunks_with_ticks', return_value=wire()), patch.object(bot, 'flush_pending_events') as send, patch.object(bot, 'maybe_send_report'):
            with self.assertRaises(OSError):
                bot.consume_camera_stream(response, {})
        self.assertEqual(send.call_count, 1)
        self.assertEqual(len(send.call_args.args[2]), 1)


if __name__ == '__main__':
    unittest.main()
