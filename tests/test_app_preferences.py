from tests import anpr_testenv  # noqa: F401  — изоляция тестов от боевой БД
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from app.core import preferences as prefs
from control_panel import Panel

class PreferencesTests(unittest.TestCase):
    def test_tray_setting_round_trip_and_default(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'ui.json'
            self.assertTrue(prefs.get_minimize_to_tray(path))
            prefs.set_minimize_to_tray(False, path)
            self.assertFalse(prefs.get_minimize_to_tray(path))
            prefs.set_minimize_to_tray(True, path)
            self.assertTrue(prefs.get_minimize_to_tray(path))

    def test_registry_write_and_remove_own_entry(self):
        key = MagicMock()
        with patch.object(prefs.winreg, 'CreateKeyEx') as create, \
             patch.object(prefs.winreg, 'QueryValueEx', side_effect=FileNotFoundError), \
             patch.object(prefs.winreg, 'SetValueEx') as write, \
             patch.object(prefs, 'startup_command', return_value='our command'):
            create.return_value.__enter__.return_value = key
            prefs.set_autostart(True)
            write.assert_called_once_with(key, prefs.RUN_NAME, 0, prefs.winreg.REG_SZ, 'our command')
        with patch.object(prefs.winreg, 'CreateKeyEx') as create, \
             patch.object(prefs.winreg, 'QueryValueEx', return_value=('our command', prefs.winreg.REG_SZ)), \
             patch.object(prefs.winreg, 'DeleteValue') as delete, \
             patch.object(prefs, 'startup_command', return_value='our command'):
            create.return_value.__enter__.return_value = key
            prefs.set_autostart(False)
            delete.assert_called_once_with(key, prefs.RUN_NAME)

    def test_close_always_hides_without_confirmation(self):
        panel = Panel.__new__(Panel)
        panel.root = MagicMock()
        panel._quit = MagicMock()
        with patch('control_panel.messagebox.askyesno') as confirm:
            panel.hide()
        panel.root.withdraw.assert_called_once()
        confirm.assert_not_called()
        panel._quit.assert_not_called()

    def test_tray_exit_cancel_does_not_stop(self):
        panel = Panel.__new__(Panel)
        panel.root = MagicMock()
        panel.busy = False
        with patch('control_panel.messagebox.askyesno', return_value=False), \
             patch('control_panel.service.stop') as stop:
            panel.confirm_stop_and_exit()
        stop.assert_not_called()

if __name__ == '__main__': unittest.main()
