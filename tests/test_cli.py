import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bbd303.cli import _request_home_confirmation, _selected_motion_value, build_parser


class CommandLineParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_move_alias_accepts_short_negative_distance(self):
        args = self.parser.parse_args(["move", "x", "-d=-1um"])
        self.assertEqual(_selected_motion_value(args), "-1um")

    def test_relative_accepts_long_distance(self):
        args = self.parser.parse_args(["move-relative", "y", "--distance=2um"])
        self.assertEqual(_selected_motion_value(args), "2um")

    def test_absolute_uses_position_option(self):
        args = self.parser.parse_args(["move-absolute", "x", "-p=1.25mm"])
        self.assertEqual(_selected_motion_value(args), "1.25mm")

    def test_old_positional_value_remains_accepted(self):
        args = self.parser.parse_args(["move-relative", "x", "2um"])
        self.assertEqual(_selected_motion_value(args), "2um")

    def test_redundant_read_commands_are_hidden_from_help(self):
        help_text = self.parser.format_help()
        self.assertNotIn("show current position", help_text)
        self.assertNotIn("show read-only motion parameters", help_text)

    @patch("sys.stdin.isatty", return_value=True)
    @patch("builtins.input", return_value="y")
    def test_home_confirmation_is_collected_without_a_controller(self, _input, _isatty):
        args = SimpleNamespace(axis="x")
        self.assertTrue(_request_home_confirmation(args))

    @patch("sys.stdin.isatty", return_value=True)
    @patch("builtins.input", return_value="")
    def test_home_confirmation_defaults_to_cancel(self, _input, _isatty):
        self.assertFalse(_request_home_confirmation(SimpleNamespace(axis="x")))

    @patch("sys.stdin.isatty", return_value=True)
    @patch("builtins.input", side_effect=["maybe", "yes"])
    def test_home_confirmation_reprompts_for_unknown_answer(self, _input, _isatty):
        self.assertTrue(_request_home_confirmation(SimpleNamespace(axis="x")))


if __name__ == "__main__":
    unittest.main()
