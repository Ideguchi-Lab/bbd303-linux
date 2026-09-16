import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bbd303.config import Config
from bbd303.controller import BBD303Controller
from bbd303.errors import DeviceStateError
from bbd303.models import Axis, DiscoveryResult


READY = 0x80000000 | 0x00000400 | 0x00002000


class FakeBackend:
    def __init__(self, connection):
        self.connection = connection
        self.closed = []
        self.positions = {21: 1_000_000, 22: 800_000}
        self.bits = {21: READY, 22: READY}
        self.velocity = {
            21: SimpleNamespace(min_velocity=0, acceleration=10_000, max_velocity=20_000),
            22: SimpleNamespace(min_velocity=0, acceleration=10_000, max_velocity=20_000),
        }
        self.set_velocity_calls = []
        self.jog = {
            21: SimpleNamespace(mode=1, step_size=100, min_velocity=0,
                                acceleration=5000, max_velocity=10000, stop_mode=1),
            22: SimpleNamespace(mode=1, step_size=100, min_velocity=0,
                                acceleration=5000, max_velocity=10000, stop_mode=1),
        }
        self.set_jog_calls = []
        self.jog_moves = []
        self.moves = []
        self.stops = []
        self.home_calls = []

    def start(self):
        pass

    def shutdown(self):
        pass

    def list_devices(self):
        return [
            SimpleNamespace(device="123456", parent_device="", transport="mock"),
            SimpleNamespace(device="x-child", parent_device="123456", transport="mock"),
            SimpleNamespace(device="y-child", parent_device="123456", transport="mock"),
            SimpleNamespace(device="empty-child", parent_device="123456", transport="mock"),
        ]

    def open(self, device, transport):
        if device == "empty-child":
            raise RuntimeError("empty channel")
        return {"123456": 10, "x-child": 21, "y-child": 22, "empty-child": 23}[device]

    def close(self, handle):
        self.closed.append(handle)

    def hardware_info(self, handle, timeout_ms):
        return SimpleNamespace(
            serial_number=123456,
            part_number="BBD303",
            firmware_version=SimpleNamespace(
                major_version=1, interim_version=2, minor_version=3
            ),
            hardware_version=4,
            number_of_channels=3,
        )

    def stage_axis_params(self, handle, timeout_ms):
        values = {
            21: (0x10, "MLS203 X Axis", 110 * 20_000),
            22: (0x11, "MLS203 Y Axis", 75 * 20_000),
            23: (0x00, "", 0),
        }
        axis_id, part_number, maximum = values[handle]
        return SimpleNamespace(
            axis_id=axis_id,
            part_number=part_number,
            serial_number=999,
            counts_per_unit=20_000,
            min_position=0,
            max_position=maximum,
        )

    def connected_product_info(self, handle):
        name = "MLS203 X Axis" if handle == 21 else "MLS203 Y Axis"
        return SimpleNamespace(
            product_name=name,
            distance_scale_factor=20_000.0,
            velocity_scale_factor=1000.0,
            acceleration_scale_factor=100.0,
            max_velocity=100.0,
            max_acceleration=1000.0,
        )

    def universal_status(self, handle, timeout_ms):
        return SimpleNamespace(
            position=self.positions[handle], velocity=0, motor_current=0,
            status_bits=self.bits[handle]
        )

    def velocity_params(self, handle, timeout_ms):
        value = self.velocity[handle]
        return SimpleNamespace(**vars(value))

    def limit_switch_params(self, handle, timeout_ms):
        return SimpleNamespace(
            clockwise_limit_mode=1,
            counterclockwise_limit_mode=1,
            soft_limit_operating_mode=1,
        )

    def jog_params(self, handle, timeout_ms):
        return SimpleNamespace(**vars(self.jog[handle]))

    def set_jog_params(self, handle, mode, step_size, minimum,
                       acceleration, maximum, stop_mode):
        self.set_jog_calls.append(
            (handle, mode, step_size, minimum, acceleration, maximum, stop_mode)
        )
        self.jog[handle] = SimpleNamespace(
            mode=mode, step_size=step_size, min_velocity=minimum,
            acceleration=acceleration, max_velocity=maximum, stop_mode=stop_mode,
        )

    def set_velocity_params(self, handle, minimum, acceleration, maximum):
        self.set_velocity_calls.append((handle, minimum, acceleration, maximum))
        self.velocity[handle] = SimpleNamespace(
            min_velocity=minimum, acceleration=acceleration, max_velocity=maximum
        )

    def move_absolute(self, handle, position):
        self.moves.append((handle, position))
        self.positions[handle] = position

    def move_jog(self, handle, forward):
        self.jog_moves.append((handle, forward))
        step = self.jog[handle].step_size
        self.positions[handle] += step if forward else -step

    def stop(self, handle, profiled=False):
        self.stops.append((handle, profiled))

    def set_enabled(self, handle, enabled):
        if enabled:
            self.bits[handle] |= 0x80000000
        else:
            self.bits[handle] &= ~0x80000000

    def home(self, handle):
        self.home_calls.append(handle)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.config = Config(
            serial="123456",
            scan_subnets=("192.0.2.0/30",),
            cache_path=Path(self.temporary.name) / "device.json",
        )
        self.backend = None

        def factory(connection):
            self.backend = FakeBackend(connection)
            return self.backend

        def discoverer(config, **kwargs):
            return DiscoveryResult("192.0.2.1:40303", "123456", "BBD303", "test")

        self.controller = BBD303Controller(
            self.config, backend_factory=factory, discoverer=discoverer
        )
        self.controller._acquire_lock = lambda: None
        self.controller._release_lock = lambda: None
        self.controller.connect()

    def tearDown(self):
        self.controller.close()
        self.temporary.cleanup()

    def test_maps_two_axes_and_tolerates_unopenable_empty_channel(self):
        self.assertEqual(self.controller.get_stage_info("x").max_position_mm, 110.0)
        self.assertEqual(self.controller.get_stage_info("y").max_position_mm, 75.0)

    def test_relative_move_uses_explicit_device_counts(self):
        result = self.controller.move_relative("x", 0.002)
        self.assertEqual(self.backend.moves[-1], (21, 1_000_040))
        self.assertEqual(result.position_mm, 50.002)

    def test_temporary_motion_parameters_are_restored(self):
        self.controller.move_relative(
            "x", 0.001, velocity_mm_s=0.1, acceleration_mm_s2=1.0
        )
        self.assertEqual(self.backend.set_velocity_calls[0], (21, 0, 100, 100))
        self.assertEqual(self.backend.set_velocity_calls[-1], (21, 0, 10_000, 20_000))

    def test_async_move_restores_parameters_after_stable_completion(self):
        self.controller.start_move_relative(
            "x", 0.001, velocity_mm_s=0.1, acceleration_mm_s2=1.0
        )
        self.assertEqual(self.backend.set_velocity_calls, [(21, 0, 100, 100)])

        self.assertFalse(self.controller.poll_motion("x")[1])
        self.assertFalse(self.controller.poll_motion("x")[1])
        status, complete = self.controller.poll_motion("x")

        self.assertTrue(complete)
        self.assertEqual(status.position_mm, 50.001)
        self.assertEqual(self.backend.set_velocity_calls[-1], (21, 0, 10_000, 20_000))

    def test_stop_restores_async_move_parameters(self):
        self.controller.start_move_relative(
            "x", 0.001, velocity_mm_s=0.1, acceleration_mm_s2=1.0
        )
        self.controller.stop("x")

        self.assertEqual(self.backend.stops[-1], (21, False))
        self.assertEqual(self.backend.set_velocity_calls[-1], (21, 0, 10_000, 20_000))

    def test_async_fault_stops_and_restores_parameters(self):
        self.controller.start_move_relative(
            "x", 0.001, velocity_mm_s=0.1, acceleration_mm_s2=1.0
        )
        self.backend.bits[21] |= 0x40000000

        with self.assertRaisesRegex(DeviceStateError, "faulted or reached a limit"):
            self.controller.poll_motion("x")

        self.assertEqual(self.backend.stops[-1], (21, False))
        self.assertEqual(self.backend.set_velocity_calls[-1], (21, 0, 10_000, 20_000))

    def test_reads_scaled_jog_parameters(self):
        params = self.controller.get_jog_parameters("x")
        self.assertEqual(params.mode, "continuous")
        self.assertEqual(params.step_size_mm, 0.005)
        self.assertEqual(params.max_velocity_mm_s, 10.0)
        self.assertEqual(params.acceleration_mm_s2, 50.0)
        self.assertEqual(params.stop_mode, "immediate")

    def test_native_single_step_jog_and_parameter_restore(self):
        self.controller.start_jog_step(
            "x", "forward", step_size_mm=0.01,
            velocity_mm_s=0.1, acceleration_mm_s2=1.0,
        )
        self.assertEqual(self.backend.jog_moves, [(21, True)])
        self.assertEqual(self.backend.set_jog_calls[0], (21, 2, 200, 0, 100, 100, 2))

        self.assertFalse(self.controller.poll_jog_step("x")[1])
        self.assertFalse(self.controller.poll_jog_step("x")[1])
        status, complete = self.controller.poll_jog_step("x")

        self.assertTrue(complete)
        self.assertEqual(status.position_mm, 50.01)
        self.assertEqual(
            self.backend.set_jog_calls[-1], (21, 1, 100, 0, 5000, 10000, 1)
        )

    def test_reverse_jog_checks_target_and_direction(self):
        self.controller.start_jog_step(
            "y", "reverse", step_size_mm=0.01,
            velocity_mm_s=0.1, acceleration_mm_s2=1.0,
        )
        self.assertEqual(self.backend.jog_moves[-1], (22, False))

    def test_stop_restores_single_step_jog_parameters(self):
        self.controller.start_jog_step(
            "x", "forward", step_size_mm=0.01,
            velocity_mm_s=0.1, acceleration_mm_s2=1.0,
        )
        self.controller.stop("x")
        self.assertEqual(
            self.backend.set_jog_calls[-1], (21, 1, 100, 0, 5000, 10000, 1)
        )

    def test_refuses_motion_if_not_homed(self):
        self.backend.bits[21] &= ~0x00000400
        with self.assertRaisesRegex(DeviceStateError, "not homed"):
            self.controller.move_relative("x", 0.001)
        self.assertEqual(self.backend.moves, [])

    def test_target_must_be_inside_stage_range(self):
        with self.assertRaisesRegex(DeviceStateError, "strictly inside"):
            self.controller.move_absolute("x", 0.0)

    def test_home_allows_controller_managed_limit_and_intermediate_fault_states(self):
        def status(*, homed=False, homing=False, moving=False, settled=False,
                   reverse_limit=False, fault=False):
            return SimpleNamespace(
                axis=Axis.X,
                position_mm=-2.0 if reverse_limit else 50.0,
                position_counts=-40_000 if reverse_limit else 1_000_000,
                velocity_mm_s=1.0 if moving else 0.0,
                velocity_raw=1000 if moving else 0,
                motor_current_raw=0,
                status_bits=0,
                enabled=True,
                homed=homed,
                homing=homing,
                settled=settled,
                moving=moving,
                forward_hard_limit=False,
                reverse_hard_limit=reverse_limit,
                fault=fault,
            )

        states = iter([
            status(homed=True, settled=True),
            status(homing=True, moving=True),
            status(homing=True, moving=True, reverse_limit=True, fault=True),
            status(homed=True, settled=True),
            status(homed=True, settled=True),
            status(homed=True, settled=True),
        ])
        with patch.object(self.controller, "get_status", side_effect=lambda _axis: next(states)), \
             patch("bbd303.controller.time.sleep"):
            result = self.controller.home(
                "x", confirmed=True, timeout_s=10.0
            )

        self.assertTrue(result.homed)
        self.assertEqual(self.backend.home_calls, [21])
        self.assertEqual(self.backend.stops, [])

    def test_home_requires_explicit_boolean_confirmation(self):
        with self.assertRaisesRegex(DeviceStateError, "explicit confirmation"):
            self.controller.home("x")
        self.assertEqual(self.backend.home_calls, [])

    def test_async_home_ignores_intermediate_limit_and_fault_states(self):
        def status(*, homed=False, homing=False, moving=False, settled=False,
                   reverse_limit=False, fault=False):
            return SimpleNamespace(
                axis=Axis.X, position_mm=50.0, position_counts=1_000_000,
                velocity_mm_s=1.0 if moving else 0.0,
                velocity_raw=1000 if moving else 0, motor_current_raw=0,
                status_bits=0, enabled=True, homed=homed, homing=homing,
                settled=settled, moving=moving, forward_hard_limit=False,
                reverse_hard_limit=reverse_limit, fault=fault,
            )

        states = iter([
            status(homed=True, settled=True),
            status(homing=True, moving=True),
            status(homing=True, moving=True, reverse_limit=True, fault=True),
            status(homed=True, settled=True),
            status(homed=True, settled=True),
            status(homed=True, settled=True),
        ])
        with patch.object(self.controller, "get_status", side_effect=lambda _axis: next(states)):
            self.controller.start_home("x", confirmed=True, timeout_s=10.0)
            self.assertFalse(self.controller.poll_home("x")[1])
            self.assertFalse(self.controller.poll_home("x")[1])
            self.assertFalse(self.controller.poll_home("x")[1])
            self.assertFalse(self.controller.poll_home("x")[1])
            result, complete = self.controller.poll_home("x")

        self.assertTrue(complete)
        self.assertTrue(result.homed)
        self.assertEqual(self.backend.home_calls, [21])
        self.assertEqual(self.backend.stops, [])

    def test_stop_cancels_async_home(self):
        self.controller.start_home("x", confirmed=True)
        self.controller.stop("x")

        self.assertEqual(self.backend.home_calls, [21])
        self.assertEqual(self.backend.stops[-1], (21, False))
        self.assertTrue(self.controller.poll_home("x")[1])

    def test_async_home_requires_confirmation(self):
        with self.assertRaisesRegex(DeviceStateError, "explicit confirmation"):
            self.controller.start_home("x")
        self.assertEqual(self.backend.home_calls, [])


if __name__ == "__main__":
    unittest.main()
