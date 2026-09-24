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
        last = GuiAxisSettings(0.1, 1.0)
        defaults = GuiAxisSettings(1.0, 10.0)
        jog_last = GuiJogSettings("single_step", 10.0, 0.1, 1.0,
                                  (1.0, 5.0, 10.0, 50.0))
        jog_defaults = GuiJogSettings("single_step", 2.0, 1.0, 10.0,
                                      (0.5, 2.0, 7.0, 25.0))
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

    def test_positions_round_trip_update_and_delete(self):
        store = GuiStateStore(self.path)
        first = store.add_position("Image A", 58.23, 42.25)
        second = store.add_position("", 58.24, 42.26)
        store.save()

        loaded = GuiStateStore(self.path)
        self.assertEqual(loaded.positions, (first, second))
        self.assertIsNotNone(first.saved_at)
        self.assertIn("+", first.saved_at)
        self.assertEqual(second.label, "")

        updated = loaded.update_position(first.id, "Image B", 58.25, 42.27)
        self.assertEqual(updated.id, first.id)
        self.assertEqual(updated.label, "Image B")
        self.assertEqual((updated.x_mm, updated.y_mm), (58.25, 42.27))
        loaded.delete_position(second.id)
        loaded.save()
        self.assertEqual(GuiStateStore(self.path).positions, (updated,))

    def test_invalid_position_is_not_saved_or_loaded(self):
        store = GuiStateStore(self.path)
        with self.assertRaises(ValueError):
            store.add_position("bad", float("nan"), 1.0)
        self.assertEqual(store.positions, ())
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({
            "version": 1,
            "axes": {},
            "positions": [{"id": "old", "label": "bad", "saved_at": "invalid",
                           "x_mm": 1, "y_mm": 2}],
        }), encoding="utf-8")
        self.assertEqual(GuiStateStore(self.path).positions, ())

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

        self.assertEqual(store.last("x"), GuiAxisSettings(0.2, 2.0))
        self.assertEqual(store.jog_last("x"), BUILTIN_JOG_DEFAULTS)

    def test_saved_continuous_mode_is_migrated_to_single_step(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({
            "version": 1,
            "axes": {"x": {"jog": {"last": {
                "mode": "continuous",
                "step_size_um": 1,
                "velocity_mm_s": 0.1,
                "acceleration_mm_s2": 1,
            }}}},
        }), encoding="utf-8")

        store = GuiStateStore(self.path)

        self.assertEqual(store.jog_last("x").mode, "single_step")
        self.assertEqual(store.jog_last("x").velocity_mm_s, 0.1)
        self.assertEqual(store.jog_last("x").steps_um, (1.0, 5.0, 10.0, 50.0))

    def test_unsupported_jog_mode_is_ignored(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({
            "version": 1,
            "axes": {"x": {"jog": {"last": {
                "mode": "unsupported",
                "step_size_um": 1,
                "velocity_mm_s": 1,
                "acceleration_mm_s2": 10,
            }}}},
        }), encoding="utf-8")

        store = GuiStateStore(self.path)

        self.assertEqual(store.jog_last("x"), BUILTIN_JOG_DEFAULTS)


if __name__ == "__main__":
    unittest.main()
