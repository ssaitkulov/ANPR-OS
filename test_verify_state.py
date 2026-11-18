"""Сверка источников: JSON-зеркало состояния ↔ SQLite (tools/verify_state.py)."""
import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from app.storage.database import Store

SPEC = importlib.util.spec_from_file_location(
    'verify_state', Path(__file__).resolve().parent / 'tools' / 'verify_state.py')
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)

RECORD = {'plate': 'RR', 'iso': '2026-11-16T20:09:00+05:00', 'lane': '1',
          'direction': 'Approach', 'speed': '', 'color': 'White', 'type': 'SaloonCar'}


class VerifyStateTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.db_path = self.root / 'events.sqlite3'
        self.state_path = self.root / 'anpr_state.json'
        store = Store(self.db_path)
        store.add_event('key-1', dict(RECORD))
        store.close()
        self.write_state({'days': {'2026-11-16': [dict(RECORD)]}})

    def write_state(self, state):
        self.state_path.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')

    def test_matching_sources_have_no_issues(self):
        report = verify.verify(self.state_path, self.db_path)
        self.assertTrue(report['ok'], report['issues'])
        self.assertEqual(report['issues'], [])
        self.assertEqual(report['json_records'], 1)
        self.assertEqual(report['db_rows_in_state_days'], 1)

    def test_passage_only_in_database_is_reported(self):
        self.write_state({'days': {'2026-11-16': []}})
        report = verify.verify(self.state_path, self.db_path)
        self.assertFalse(report['ok'])
        kinds = {issue['kind'] for issue in report['issues']}
        self.assertEqual(kinds, {'missing_in_json'})

    def test_passage_only_in_json_is_reported(self):
        state = {'days': {'2026-11-16': [dict(RECORD), dict(RECORD, iso='2026-11-16T20:10:00+05:00')]}}
        self.write_state(state)
        report = verify.verify(self.state_path, self.db_path)
        self.assertFalse(report['ok'])
        kinds = {issue['kind'] for issue in report['issues']}
        self.assertEqual(kinds, {'missing_in_db'})

    def test_field_mismatch_is_reported(self):
        self.write_state({'days': {'2026-11-16': [dict(RECORD, lane='2')]}})
        report = verify.verify(self.state_path, self.db_path)
        self.assertFalse(report['ok'])
        kinds = {issue['kind'] for issue in report['issues']}
        self.assertEqual(kinds, {'field_mismatch'})

    def test_backfill_flag_mismatch_is_reported(self):
        self.write_state({'days': {'2026-11-16': [dict(RECORD, backfill=True)]}})
        report = verify.verify(self.state_path, self.db_path)
        self.assertFalse(report['ok'])
        kinds = {issue['kind'] for issue in report['issues']}
        self.assertEqual(kinds, {'flag_mismatch'})

    def test_tool_switches_console_to_utf8(self):
        """Кириллица в консоли Windows не должна превращаться в «кракозябры»."""
        self.assertEqual(getattr(verify.sys.stdout, 'encoding', '').lower().replace('-', ''), 'utf8')


if __name__ == '__main__':
    unittest.main()
