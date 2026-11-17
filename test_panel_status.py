import unittest
from collections import deque
from unittest.mock import Mock
from control_panel import Panel

class Value:
    def __init__(self): self.value = None
    def set(self, value): self.value = value

class StatusTests(unittest.TestCase):
    def setUp(self):
        self.panel = Panel.__new__(Panel)
        p = self.panel
        p.previous = None
        p.events = deque(maxlen=100)
        p.event_seen = set()
        p.status, p.hint = Value(), Value()
        p.camera, p.telegram = (Value(), Value()), (Value(), Value())
        p.main_button = Mock()
        p.render_events = Mock()
        p._state_color = Mock()
        p._connection_color = Mock()
        self.data = {'status': 'ok', 'pid': 1, 'started_at': 1700000000,
                     'camera_message': 'подключена. Ожидаю проезд автомобиля.',
                     'telegram_message': 'ожидаю первую отправку.', 'last_camera_data': ''}

    def test_connection_does_not_imply_data_or_delivery(self):
        self.panel.update_status(self.data)
        self.assertEqual(self.panel.camera[0].value, 'Подключена')
        self.assertEqual(self.panel.camera[1].value, '')
        self.assertEqual(self.panel.telegram[0].value, 'Ожидание отправки')
        self.assertEqual(self.panel.telegram[1].value, '')

    def test_confirmed_delivery_and_stop(self):
        self.panel.update_status(self.data)
        self.panel.update_status({**self.data, 'camera_message': 'подключена, получаю данные.',
                                  'last_camera_data': '17.11.2026 13:00:00',
                                  'telegram_message': 'последнее уведомление доставлено',
                                  'telegram_delivery': {'ok': True, 'at': '2026-11-17T13:04:05+05:00'}})
        self.assertEqual(self.panel.camera[1].value, 'Данные поступают')
        self.assertEqual(self.panel.telegram[1].value, 'Последняя отправка: 13:04')
        data = {'passages': [{'id': 1, 'captured_at': '2026-11-17T13:04:05+05:00',
                              'plate': '01 A 123 BC', 'direction': 'Въезд', 'lane': '1', 'speed': '20'}]}
        self.panel.merge_events(data)
        self.panel.merge_events(data)
        self.assertEqual(list(self.panel.events), ['17.11 13:04:05 · 01 A 123 BC · Въезд · Полоса 1 · 20 км/ч'])
        self.panel.update_status(None)
        self.assertEqual(self.panel.camera[0].value, 'Неактивна')
        self.assertEqual(self.panel.telegram[0].value, 'Неактивен')
        self.assertFalse(any('Система остановлена' in e for e in self.panel.events))

if __name__ == '__main__': unittest.main()
