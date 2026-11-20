"""Ручные снимки камеры (SnapSource=med.Force) — фильтр INCLUDE_MANUAL_SNAPSHOTS.

Проверяется production-код (`_apply_archive_records`), а не переписанное
в тесте выражение: ручной снимок — это тест камеры, проездом не является.
"""
from tests import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД

import unittest
from unittest.mock import patch

import anpr_telegram as bot


def _archive_record(source='med.Force'):
    return {'RecNo': '1', 'Time': '1794847002', 'SnapSource': source,
            'PlateNumber': 'TEST123', 'VehicleColor': 'White', 'Category': 'SaloonCar',
            'JunctionDirection': '0', 'Lane': '1'}


class ManualSnapshotTests(unittest.TestCase):
    def _apply(self, source='med.Force', include=False):
        """Прогнать архивную запись через фильтр добора."""
        state = {'last_rec_no': 0, 'days': {}}
        with patch.object(bot, 'INCLUDE_MANUAL_SNAPSHOTS', include), \
                patch.object(bot, 'DIRECTION', ''), \
                patch.object(bot, 'record_backfill_event', return_value={'plate': 'TEST123'}) as add:
            missed, changed = bot._apply_archive_records(state, [_archive_record(source)], None, None, 0)
        return state, missed, changed, add

    def test_manual_snapshot_is_skipped_when_disabled(self):
        state, missed, _, add = self._apply(include=False)
        self.assertEqual(missed, [])
        self.assertFalse(add.called)
        # Номер записи всё равно отмечается прочитанным — добор не вернётся к нему.
        self.assertEqual(state['last_rec_no'], 1)

    def test_manual_snapshot_is_stored_when_enabled(self):
        state, missed, _, add = self._apply(include=True)
        self.assertEqual(missed, [{'plate': 'TEST123'}])
        add.assert_called_once()
        self.assertEqual(state['last_rec_no'], 1)

    def test_regular_archive_record_is_stored_regardless_of_the_flag(self):
        _, missed, _, add = self._apply(source='Video', include=False)
        self.assertEqual(len(missed), 1)
        add.assert_called_once()


if __name__ == '__main__':
    unittest.main()
