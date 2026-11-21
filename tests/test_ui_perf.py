"""Опциональная инструментация: выключена по умолчанию и не меняет поведение."""
import os
import unittest
from unittest.mock import ANY, Mock, patch

from app.ui import perf


class PerfTests(unittest.TestCase):
    def test_timed_returns_original_callback_when_disabled(self):
        def callback():
            pass

        with patch.dict(os.environ, {}, clear=True):
            self.assertIs(perf.timed('label', callback), callback)

    def test_timed_keeps_result_and_exceptions_when_enabled(self):
        with patch.dict(os.environ, {'EVRIKA_PERF_LOG': '1'}):
            self.assertEqual(perf.timed('label', lambda: 42)(), 42)

            def broken():
                raise ValueError('boom')

            with self.assertRaises(ValueError):
                perf.timed('label', broken)()

    def test_heartbeat_is_not_planned_when_disabled(self):
        root = Mock()
        with patch.dict(os.environ, {}, clear=True):
            perf.heartbeat(root)
        root.after.assert_not_called()

    def test_heartbeat_is_planned_when_enabled(self):
        root = Mock()
        with patch.dict(os.environ, {'EVRIKA_PERF_LOG': '1'}):
            perf.heartbeat(root)
        root.after.assert_called_once_with(perf.HEARTBEAT_MS, ANY)


if __name__ == '__main__':
    unittest.main()
