import json
import stat
import tempfile
import unittest
from pathlib import Path

from bbd303.gui_state import BUILTIN_DEFAULTS, GuiAxisSettings, GuiStateStore


class GuiStateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state" / "gui-state.json"

    def tearDown(self):
        self.temporary.cleanup()

    def test_missing_file_uses_built_in_defaults(self):
        store = GuiStateStore(self.path)
        self.assertEqual(store.last("x"), BUILTIN_DEFAULTS)
        self.assertEqual(store.defaults("y"), BUILTIN_DEFAULTS)

    def test_round_trip_keeps_last_values_and_defaults_separate(self):
        store = GuiStateStore(self.path)
        last = GuiAxisSettings(0.1, 1.0, 10.0)
        defaults = GuiAxisSettings(1.0, 10.0, 2.0)
        store.set_last("x", last)
        store.set_defaults("x", defaults)
        store.save()

        loaded = GuiStateStore(self.path)
        self.assertEqual(loaded.last("x"), last)
        self.assertEqual(loaded.defaults("x"), defaults)
        self.assertEqual(loaded.last("y"), BUILTIN_DEFAULTS)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_corrupt_file_reports_warning_and_uses_defaults(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("not json", encoding="utf-8")

        store = GuiStateStore(self.path)

        self.assertIsNotNone(store.warning)
        self.assertEqual(store.last("x"), BUILTIN_DEFAULTS)

    def test_invalid_saved_values_are_ignored(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({
            "version": 1,
            "axes": {"x": {"last": {
                "velocity_mm_s": -1,
                "acceleration_mm_s2": 1,
                "relative_step_um": 1,
            }}},
        }), encoding="utf-8")

        store = GuiStateStore(self.path)

        self.assertEqual(store.last("x"), BUILTIN_DEFAULTS)


if __name__ == "__main__":
    unittest.main()
