"""Разбор событий камеры: текст snapManager, группировка, отбор проездов.

Слой разбора событий — на границе с железом: от него зависят и дедупликация,
 и «чужое» событие в базе. Проверяем его напрямую, без камеры.
"""
from tests import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД

import unittest
from unittest.mock import patch

import anpr_telegram as bot


def _event(**extra):
    """Событие TrafficJunction с минимальным набором полей."""
    base = {'EventBaseInfo.Code': 'TrafficJunction', 'GroupID': '1048576',
            'IndexInGroup': '1', 'TrafficCar.PlateNumber': 'RR', 'Lane': '1'}
    base.update(extra)
    return base


class ParseTextTests(unittest.TestCase):
    """Текстовая часть события: строки «ключ=значение» и группировка по индексу."""

    def test_key_value_lines_are_parsed(self):
        kv = bot.parse_kv_text('Events[0].TrafficCar.PlateNumber=ABC123\n'
                               'Events[0].Lane=1\n'
                               'Heartbeat=1234')
        self.assertEqual(kv['Events[0].TrafficCar.PlateNumber'], 'ABC123')
        self.assertEqual(kv['Events[0].Lane'], '1')
        self.assertNotIn('Heartbeat', kv)

    def test_events_are_grouped_by_index(self):
        kv = bot.parse_kv_text('Events[0].Lane=1\n'
                               'Events[1].Lane=2\n'
                               'Events[0].TrafficCar.PlateNumber=RR')
        groups = bot.extract_events(kv)
        self.assertEqual(sorted(groups), [0, 1])
        self.assertEqual(groups[0]['TrafficCar.PlateNumber'], 'RR')
        self.assertEqual(groups[1]['Lane'], '2')

    def test_missing_boundary_is_reported(self):
        with self.assertRaises(ValueError):
            bot.parse_boundary('multipart/x-mixed-replace')


class CountableTests(unittest.TestCase):
    """Отбор проездов: код события, полоса, направление."""

    def test_traffic_junction_is_countable(self):
        countable, key = bot.is_countable(_event())
        self.assertTrue(countable)
        self.assertEqual(key, '1048576:1:RR:1')

    def test_other_event_code_is_ignored(self):
        self.assertEqual(bot.is_countable(_event(**{'EventBaseInfo.Code': 'TrafficManualSnap'})), (False, ''))

    def test_lane_outside_the_allowlist_is_ignored(self):
        with patch.object(bot, 'ALLOWED_LANES', '1'):
            self.assertEqual(bot.is_countable(_event(Lane='2')), (False, ''))
            self.assertTrue(bot.is_countable(_event(Lane='1'))[0])

    def test_direction_filter_rejects_foreign_direction(self):
        with patch.object(bot, 'DIRECTION', 'Approach'):
            self.assertEqual(bot.is_countable(_event(JunctionDirection='1')), (False, ''))

    def test_direction_is_read_from_an_alternate_key(self):
        with patch.object(bot, 'DIRECTION', 'Approach'):
            countable, _ = bot.is_countable(_event(**{'TrafficCar.Direction': 'Approach'}))
            self.assertTrue(countable)

    def test_plate_and_group_id_are_read_from_the_event(self):
        event = _event(**{'TrafficCar.PlateNumber': '  RR  '})
        self.assertEqual(bot.plate_of(event), 'RR')
        self.assertEqual(bot.camera_group_id(event), '1048576')


if __name__ == '__main__':
    unittest.main()
