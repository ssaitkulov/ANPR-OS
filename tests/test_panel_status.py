from tests import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД
import unittest
from collections import deque
from control_panel import Panel

from app.ui import status as ui_status


def make_panel(telegram_configured=True):
    """Панель без окна Tk: проверяем только обновление состояния."""
    panel = Panel.__new__(Panel)
    panel.previous = None
    panel.events = deque(maxlen=100)
    panel.busy = False
    panel.operation = None
    panel.phase = None
    panel.running = False
    panel._health = None
    panel._health_error = ''
    panel._last_check = None
    panel._last_error = ''
    panel._states = {}
    panel._settings_cache = (0.0, {})
    panel._telegram_configured = lambda: telegram_configured
    return panel


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.panel = make_panel()
        self.data = {'status': 'ok', 'pid': 1, 'started_at': 1700000000,
                     'camera_message': 'подключена. Ожидаю проезд автомобиля.',
                     'telegram_message': 'ожидаю первую отправку.', 'last_camera_data': ''}

    def state(self, key):
        return self.panel._states[key]

    def test_connection_does_not_imply_data_or_delivery(self):
        self.panel.update_status(self.data)
        self.assertEqual(self.state('camera')['label'], 'Подключена')
        self.assertEqual(self.state('camera')['detail'], 'подключена. Ожидаю проезд автомобиля.')
        # Обращений к Telegram ещё не было — это «проверяем», а не «нет связи».
        self.assertEqual(self.state('telegram')['label'], 'Проверяем…')
        self.assertEqual(self.state('service')['label'], 'Работает')

    def test_no_delivery_attempt_is_not_reported_as_failure(self):
        """До первой отправки счётчики пусты — это ожидание, а не «Ошибка»."""
        self.panel.update_status({**self.data,
                                  'telegram_delivery': {'ok': False, 'delivered': 0, 'failed': 0, 'at': ''}})
        self.assertEqual(self.state('telegram')['code'], ui_status.TELEGRAM_CHECKING)
        self.assertEqual(ui_status.telegram_delivery_view(self.panel._health)['code'],
                         ui_status.TELEGRAM_DELIVERY_NONE)

    def test_failed_delivery_is_reported_as_error(self):
        self.panel.update_status({**self.data,
                                  'telegram_message': 'не удалось доставить уведомление (в очередь).',
                                  'telegram_delivery': {'ok': False, 'delivered': 0, 'failed': 1,
                                                        'at': '2026-11-18T17:05:06+05:00'}})
        self.assertEqual(self.state('telegram')['label'], 'Ошибка доставки')

    def test_confirmed_delivery_and_stop(self):
        self.panel.update_status(self.data)
        self.panel.update_status({**self.data, 'camera_message': 'подключена, получаю данные.',
                                  'last_camera_data': '17.11.2026 13:00:00',
                                  'telegram_message': 'последнее уведомление доставлено',
                                  'telegram_delivery': {'ok': True, 'at': '2026-11-17T13:04:05+05:00'},
                                  'telegram_connection': {'ok': True, 'at': '2026-11-17T13:04:06+05:00',
                                                          'failed_at': '', 'error': ''}})
        self.assertEqual(self.state('camera')['label'], 'Подключена')
        self.assertEqual(self.state('telegram')['label'], 'Работает')
        data = {'passages': [{'id': 1, 'captured_at': '2026-11-17T13:04:05+05:00',
                              'plate': '01 A 123 BC', 'direction': 'Въезд', 'lane': '1', 'speed': '20'}]}
        self.panel.merge_events(data)
        self.panel.merge_events(data)
        self.assertEqual(list(self.panel.events), ['17.11 13:04:05 · 01 A 123 BC · Въезд · Полоса 1 · 20 км/ч'])
        self.panel.update_status(None)
        self.assertEqual(self.state('camera')['label'], 'Нет связи')
        self.assertEqual(self.state('telegram')['label'], 'Нет соединения')
        self.assertEqual(self.state('service')['label'], 'Остановлен')
        self.assertFalse(any('Система остановлена' in e for e in self.panel.events))

    def test_not_configured_telegram(self):
        panel = make_panel(telegram_configured=False)
        panel.update_status(self.data)
        self.assertEqual(panel._states['telegram']['label'], 'Не настроен')
        self.assertEqual(panel._states['telegram']['code'], ui_status.TELEGRAM_NOT_CONFIGURED)


if __name__ == '__main__':
    unittest.main()
