import unittest

from bbd303.units import (
    parse_acceleration_mm_s2,
    parse_distance_mm,
    parse_velocity_mm_s,
    to_device_units,
)


class UnitConversionTests(unittest.TestCase):
    def test_distance_requires_an_explicit_unit(self):
        self.assertEqual(parse_distance_mm("2um"), 0.002)
        self.assertEqual(parse_distance_mm("58.23 mm"), 58.23)
        with self.assertRaises(ValueError):
            parse_distance_mm("2")

    def test_velocity_and_acceleration(self):
        self.assertEqual(parse_velocity_mm_s("100 um/s"), 0.1)
        self.assertEqual(parse_acceleration_mm_s2("1000um/s2"), 1.0)

    def test_device_rounding_is_decimal_half_up(self):
        self.assertEqual(to_device_units(0.000025, 20000), 1)
        self.assertEqual(to_device_units(-0.000025, 20000), -1)


if __name__ == "__main__":
    unittest.main()
