import unittest
from unittest.mock import patch
import anpr_telegram as bot
from setup_anpr import DIRECTIONS


class DirectionTests(unittest.TestCase):
    def test_modes(self):
        for mode, expected in [('', [True, True, True]), ('Approach', [True, False, False]), ('Leave', [False, True, False])]:
            with self.subTest(mode=mode), patch.object(bot, 'DIRECTION', mode):
                self.assertEqual([bot.direction_allowed(v) for v in ['Obverse', 'Reverse', 'Unknown']], expected)
                self.assertEqual([bot.direction_allowed(v) for v in ['0', '1', '']], expected)

    def test_caption_and_settings(self):
        self.assertEqual(set(DIRECTIONS.values()), {'', 'Approach', 'Leave'})
        self.assertIn('↔️ EXIT', bot.build_entry_caption({'plate':'TEST', 'direction':'Reverse'}))
        self.assertIn('↔️ UNKNOWN', bot.build_entry_caption({'plate':'TEST', 'direction':'Unknown'}))
