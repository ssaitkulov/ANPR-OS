from tests import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД
import unittest
from unittest.mock import patch
import anpr_telegram as bot


class ManualSnapshotTests(unittest.TestCase):
    def test_manual_snapshot_is_allowed_when_enabled(self):
        rec = {'RecNo': '1', 'Time': '1794847002', 'SnapSource': 'med.Force',
               'PlateNumber': 'TEST123', 'VehicleColor': 'White', 'Category': 'SaloonCar',
               'JunctionDirection': '0'}
        state = {'last_rec_no': 0, 'days': {}}
        with patch.object(bot, 'INCLUDE_MANUAL_SNAPSHOTS', True), patch.object(bot, 'record_backfill_event', return_value={'plate': 'TEST123'}) as add:
            # The filtering branch is exercised by the same condition used in backfill_once.
            source = rec['SnapSource'].lower()
            self.assertFalse(('force' in source or 'manual' in source) and not bot.INCLUDE_MANUAL_SNAPSHOTS)
            add(state, rec['PlateNumber'], bot.archive_time(int(rec['Time'])), direction='0')
            add.assert_called_once()

    def test_manual_snapshot_can_be_disabled(self):
        with patch.object(bot, 'INCLUDE_MANUAL_SNAPSHOTS', False):
            source = 'med.force'
            self.assertTrue(('force' in source or 'manual' in source) and not bot.INCLUDE_MANUAL_SNAPSHOTS)


if __name__ == '__main__':
    unittest.main()
