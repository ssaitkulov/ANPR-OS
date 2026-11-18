from tests import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД
import unittest
from unittest.mock import patch
import anpr_telegram as bot
from setup_anpr import DIRECTIONS


class DirectionTests(unittest.TestCase):
    def test_plate_display_format(self):
        self.assertEqual(bot.display_plate('010047MV'), '01 0047 MV')
        self.assertEqual(bot.display_plate('75U902GA'), '75 U 902 GA')
        self.assertEqual(bot.display_plate('AB12345'), 'AB12345')

    def test_modes(self):
        for mode, expected in [('', [True, True, True]), ('Approach', [True, False, False]), ('Leave', [False, True, False])]:
            with self.subTest(mode=mode), patch.object(bot, 'DIRECTION', mode):
                self.assertEqual([bot.direction_allowed(v) for v in ['Obverse', 'Reverse', 'Unknown']], expected)
                self.assertEqual([bot.direction_allowed(v) for v in ['0', '1', '']], expected)

    def test_caption_and_settings(self):
        self.assertEqual(set(DIRECTIONS.values()), {'', 'Approach', 'Leave'})
        self.assertIn('↔️ EXIT', bot.build_entry_caption({'plate':'TEST', 'direction':'Reverse'}))
        self.assertIn('↔️ UNKNOWN', bot.build_entry_caption({'plate':'TEST', 'direction':'Unknown'}))
