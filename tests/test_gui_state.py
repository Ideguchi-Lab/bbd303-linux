import json
import stat
import tempfile
import unittest
from pathlib import Path

from bbd303.gui_state import (
    BUILTIN_DEFAULTS,
    BUILTIN_JOG_DEFAULTS,
    GuiAxisSettings,
    GuiJogSettings,
    GuiStateStore,
)


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
        self.assertEqual(store.jog_last("x"), BUILTIN_JOG_DEFAULTS)
        self.assertEqual(store.jog_defaults("y"), BUILTIN_JOG_DEFAULTS)

    def test_round_trip_keeps_last_values_and_defaults_separate(self):
        store = GuiStateStore(self.path)
        last = GuiAxisSettings(0.1, 1.0, 10.0)
        defaults = GuiAxisSettings(1.0, 10.0, 2.0)
        jog_last = GuiJogSettings("single_step", 10.0, 0.1, 1.0)
        jog_defaults = GuiJogSettings("single_step", 2.0, 1.0, 10.0)
        store.set_last("x", last)
        store.set_defaults("x", defaults)
        store.set_jog_last("x", jog_last)
        store.set_jog_defaults("x", jog_defaults)
        store.save()

        loaded = GuiStateStore(self.path)
        self.assertEqual(loaded.last("x"), last)
        self.assertEqual(loaded.defaults("x"), defaults)
        self.assertEqual(loaded.jog_last("x"), jog_last)
        self.assertEqual(loaded.jog_defaults("x"), jog_defaults)
        self.assertEqual(loaded.last("y"), BUILTIN_DEFAULTS)
        self.assertEqual(loaded.jog_last("y"), BUILTIN_JOG_DEFAULTS)
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

    def test_old_version_one_state_without_jog_remains_compatible(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({
            "version": 1,
            "axes": {"x": {"last": {
                "velocity_mm_s": 0.2,
                "acceleration_mm_s2": 2,
                "relative_step_um": 5,
            }}},
        }), encoding="utf-8")

        store = GuiStateStore(self.path)

        self.assertEqual(store.last("x"), GuiAxisSettings(0.2, 2.0, 5.0))
        self.assertEqual(store.jog_last("x"), BUILTIN_JOG_DEFAULTS)

    def test_invalid_jog_mode_is_ignored(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({
            "version": 1,
            "axes": {"x": {"jog": {"last": {
                "mode": "continuous",
                "step_size_um": 1,
                "velocity_mm_s": 1,
                "acceleration_mm_s2": 10,
            }}}},
        }), encoding="utf-8")

        store = GuiStateStore(self.path)

        self.assertEqual(store.jog_last("x"), BUILTIN_JOG_DEFAULTS)


if __name__ == "__main__":
    unittest.main()
