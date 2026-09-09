"""High-level, unit-aware and state-checked BBD303 controller."""

from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import time
from typing import Callable

from .backends import XABackend
from .config import Config, cache_home, load_config
from .discovery import discover
from .errors import DeviceBusyError, DeviceStateError, MotionTimeoutError
from .models import Axis, AxisStatus, ControllerInfo, DiscoveryResult, MotionParameters, StageInfo
from .units import to_device_units

_TIMEOUT_MS = 2000
_POSITION_TOLERANCE_COUNTS = 4
_FAULT_BITS = 0x40000000 | 0x0F000000 | 0x000E0000 | 0x0000C000
_LIMIT_BITS = 0x0000000F
_MOTION_BITS = 0x000002F0


@dataclass
class _AxisRuntime:
    handle: int
    stage: StageInfo
    velocity_scale: float
    acceleration_scale: float


@dataclass
class _PendingMotion:
    target_counts: int
    original_velocity: tuple[int, int, int] | None
    stable_reads: int = 0


@dataclass
class _PendingHome:
    deadline: float
    began_homing: bool = False
    stable_reads: int = 0


class BBD303Controller:
    """Connect to one configured BBD303 and its MLS203 X/Y axes."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        config_path: str | Path | None = None,
        connection: str | None = None,
        refresh: bool = False,
        backend_factory: Callable[[str], object] = XABackend,
        discoverer: Callable[..., DiscoveryResult] = discover,
    ):
        self.config = config or load_config(config_path)
        self.requested_connection = connection
        self.refresh = refresh
        self._backend_factory = backend_factory
        self._discoverer = discoverer
        self._backend = None
        self._discovery: DiscoveryResult | None = None
        self._controller_handle: int | None = None
        self._axes: dict[Axis, _AxisRuntime] = {}
        self._pending_motions: dict[Axis, _PendingMotion] = {}
        self._pending_homes: dict[Axis, _PendingHome] = {}
        self._lock_stream = None

    def __enter__(self) -> "BBD303Controller":
        return self.connect()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    @property
    def connection(self) -> str:
        if not self._discovery:
            raise DeviceStateError("Controller is not connected")
        return self._discovery.connection

    def _acquire_lock(self) -> None:
        path = cache_home() / f"{self.config.serial}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            stream.seek(0)
            owner = stream.read().strip() or "unknown process"
            stream.close()
            raise DeviceBusyError(f"Controller is in use by {owner}; lock: {path}") from exc
        stream.seek(0)
        stream.truncate()
        stream.write(f"PID {os.getpid()}\n")
        stream.flush()
        self._lock_stream = stream

    def _release_lock(self) -> None:
        if self._lock_stream:
            fcntl.flock(self._lock_stream, fcntl.LOCK_UN)
            self._lock_stream.close()
            self._lock_stream = None

    def connect(self) -> "BBD303Controller":
        if self._backend is not None:
            return self
        self._acquire_lock()
        try:
            self._discovery = self._discoverer(
                self.config, connection=self.requested_connection, refresh=self.refresh
            )
            self._backend = self._backend_factory(self._discovery.connection)
            self._backend.start()
            devices = self._backend.list_devices()
            controllers = [item for item in devices if item.device == self.config.serial]
            children = sorted(
                (item for item in devices if item.parent_device == self.config.serial),
                key=lambda item: (
                    not str(item.device).isdigit(),
                    int(item.device) if str(item.device).isdigit() else str(item.device),
                ),
            )
            if len(controllers) != 1:
                raise DeviceStateError("XA did not report exactly one configured controller")
            self._controller_handle = self._backend.open(
                controllers[0].device, controllers[0].transport
            )
            child_errors: list[str] = []
            for child in children:
                if set(self._axes) == set(Axis):
                    break
                try:
                    handle = self._backend.open(child.device, child.transport)
                except Exception as exc:
                    child_errors.append(f"{child.device}: {exc}")
                    continue
                try:
                    raw_stage = self._backend.stage_axis_params(handle, _TIMEOUT_MS)
                    axis_id = int(raw_stage.axis_id)
                    if axis_id not in (0x10, 0x11):
                        continue
                    axis = Axis.X if axis_id == 0x10 else Axis.Y
                    if axis in self._axes:
                        raise DeviceStateError(
                            f"XA reported more than one {axis.value.upper()} axis"
                        )
                    product = self._backend.connected_product_info(handle)
                    expected = self.config.expected_stages[axis]
                    if raw_stage.part_number != expected or product.product_name != expected:
                        raise DeviceStateError(
                            f"{axis.value.upper()} stage mismatch: expected {expected}, "
                            f"got {raw_stage.part_number}/{product.product_name}"
                        )
                    if int(raw_stage.counts_per_unit) != 20000 or product.distance_scale_factor != 20000.0:
                        raise DeviceStateError(f"Unexpected {axis.value.upper()} position scale")
                    stage = StageInfo(
                        axis=axis,
                        device_id=str(child.device),
                        product_name=raw_stage.part_number,
                        stage_serial=int(raw_stage.serial_number),
                        counts_per_mm=int(raw_stage.counts_per_unit),
                        min_position_mm=raw_stage.min_position / raw_stage.counts_per_unit,
                        max_position_mm=raw_stage.max_position / raw_stage.counts_per_unit,
                        max_velocity_mm_s=float(product.max_velocity),
                        max_acceleration_mm_s2=float(product.max_acceleration),
                    )
                    self._axes[axis] = _AxisRuntime(
                        handle, stage, float(product.velocity_scale_factor),
                        float(product.acceleration_scale_factor)
                    )
                    handle = None
                finally:
                    if handle is not None:
                        self._backend.close(handle)
            if set(self._axes) != set(Axis):
                missing = ", ".join(axis.value.upper() for axis in Axis if axis not in self._axes)
                detail = f"; child open errors: {'; '.join(child_errors)}" if child_errors else ""
                raise DeviceStateError(
                    f"XA did not report configured MLS203 axes: {missing}{detail}"
                )
            return self
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        backend = self._backend
        if backend is not None:
            active_axes = set(self._pending_motions) | set(self._pending_homes)
            for axis in active_axes:
                try:
                    self.stop(axis)
                except Exception:
                    pass
            self._pending_motions.clear()
            self._pending_homes.clear()
            for runtime in list(self._axes.values()):
                try:
                    backend.close(runtime.handle)
                except Exception:
                    pass
            self._axes.clear()
            if self._controller_handle is not None:
                try:
                    backend.close(self._controller_handle)
                except Exception:
                    pass
                self._controller_handle = None
            try:
                backend.shutdown()
            finally:
                self._backend = None
        self._release_lock()

    def _runtime(self, axis: Axis | str) -> _AxisRuntime:
        parsed = Axis.parse(axis)
        if parsed not in self._axes:
            raise DeviceStateError(f"Axis {parsed.value} is not connected")
        return self._axes[parsed]

    def get_device_info(self) -> ControllerInfo:
        if self._controller_handle is None or self._backend is None:
            raise DeviceStateError("Controller is not connected")
        info = self._backend.hardware_info(self._controller_handle, _TIMEOUT_MS)
        version = info.firmware_version
        firmware = f"{version.major_version}.{version.interim_version}.{version.minor_version}"
        return ControllerInfo(
            serial=str(info.serial_number), model=info.part_number,
            connection=self.connection, firmware=firmware,
            hardware_version=int(info.hardware_version), channel_count=int(info.number_of_channels)
        )

    def get_stage_info(self, axis: Axis | str) -> StageInfo:
        return self._runtime(axis).stage

    def get_status(self, axis: Axis | str) -> AxisStatus:
        parsed = Axis.parse(axis)
        runtime = self._runtime(parsed)
        raw = self._backend.universal_status(runtime.handle, _TIMEOUT_MS)
        bits = int(raw.status_bits)
        return AxisStatus(
            axis=parsed,
            position_mm=raw.position / runtime.stage.counts_per_mm,
            position_counts=int(raw.position),
            velocity_mm_s=raw.velocity / runtime.velocity_scale,
            velocity_raw=int(raw.velocity), motor_current_raw=int(raw.motor_current),
            status_bits=bits, enabled=bool(bits & 0x80000000),
            homed=bool(bits & 0x00000400), homing=bool(bits & 0x00000200),
            settled=bool(bits & 0x00002000), moving=bool(bits & _MOTION_BITS),
            forward_hard_limit=bool(bits & 0x1), reverse_hard_limit=bool(bits & 0x2),
            fault=bool(bits & _FAULT_BITS),
        )

    def get_position_mm(self, axis: Axis | str) -> float:
        return self.get_status(axis).position_mm

    def get_motion_parameters(self, axis: Axis | str) -> MotionParameters:
        parsed = Axis.parse(axis)
        runtime = self._runtime(parsed)
        velocity = self._backend.velocity_params(runtime.handle, _TIMEOUT_MS)
        limits = self._backend.limit_switch_params(runtime.handle, _TIMEOUT_MS)
        return MotionParameters(
            axis=parsed,
            min_velocity_mm_s=velocity.min_velocity / runtime.velocity_scale,
            max_velocity_mm_s=velocity.max_velocity / runtime.velocity_scale,
            acceleration_mm_s2=velocity.acceleration / runtime.acceleration_scale,
            min_velocity_raw=int(velocity.min_velocity),
            max_velocity_raw=int(velocity.max_velocity), acceleration_raw=int(velocity.acceleration),
            hard_limit_forward_mode=int(limits.clockwise_limit_mode),
            hard_limit_reverse_mode=int(limits.counterclockwise_limit_mode),
            soft_limit_mode=int(limits.soft_limit_operating_mode),
        )

    @staticmethod
    def _require_ready(status: AxisStatus) -> None:
        if not status.homed:
            raise DeviceStateError(f"Axis {status.axis.value} is not homed")
        if not status.enabled:
            raise DeviceStateError(f"Axis {status.axis.value} is disabled")
        if status.moving or not status.settled or status.velocity_raw != 0:
            raise DeviceStateError(f"Axis {status.axis.value} is not stationary and settled")
        if status.fault or status.forward_hard_limit or status.reverse_hard_limit:
            raise DeviceStateError(f"Axis {status.axis.value} reports a fault or limit")

    def _validate_target(self, runtime: _AxisRuntime, target: int) -> None:
        minimum = to_device_units(runtime.stage.min_position_mm, runtime.stage.counts_per_mm)
        maximum = to_device_units(runtime.stage.max_position_mm, runtime.stage.counts_per_mm)
        if not minimum < target < maximum:
            raise DeviceStateError(
                f"Target must be strictly inside {runtime.stage.min_position_mm:g} to "
                f"{runtime.stage.max_position_mm:g} mm"
            )

    def _set_temporary_motion_parameters(
        self,
        runtime: _AxisRuntime,
        velocity_mm_s: float | None,
        acceleration_mm_s2: float | None,
    ) -> tuple[int, int, int] | None:
        if velocity_mm_s is None and acceleration_mm_s2 is None:
            return None
        original = self._backend.velocity_params(runtime.handle, _TIMEOUT_MS)
        velocity = original.max_velocity if velocity_mm_s is None else to_device_units(
            velocity_mm_s, runtime.velocity_scale
        )
        acceleration = original.acceleration if acceleration_mm_s2 is None else to_device_units(
            acceleration_mm_s2, runtime.acceleration_scale
        )
        if velocity <= 0 or (
            velocity_mm_s is not None and velocity_mm_s > runtime.stage.max_velocity_mm_s
        ):
            raise DeviceStateError("Velocity is outside the connected stage limits")
        if acceleration <= 0 or (
            acceleration_mm_s2 is not None
            and acceleration_mm_s2 > runtime.stage.max_acceleration_mm_s2
        ):
            raise DeviceStateError("Acceleration is outside the connected stage limits")
        saved = (int(original.min_velocity), int(original.acceleration), int(original.max_velocity))
        self._backend.set_velocity_params(
            runtime.handle, saved[0], acceleration, velocity
        )
        return saved

    def _restore_motion_parameters(self, axis: Axis) -> None:
        pending = self._pending_motions.get(axis)
        if pending is None or pending.original_velocity is None:
            return
        runtime = self._runtime(axis)
        minimum, acceleration, maximum = pending.original_velocity
        self._backend.set_velocity_params(runtime.handle, minimum, acceleration, maximum)

    def start_move_absolute(
        self,
        axis: Axis | str,
        position_mm: float,
        *,
        velocity_mm_s: float | None = None,
        acceleration_mm_s2: float | None = None,
    ) -> AxisStatus:
        """Start a move for an event loop and return without waiting for it."""
        parsed = Axis.parse(axis)
        if parsed in self._pending_motions or parsed in self._pending_homes:
            raise DeviceStateError(f"Axis {parsed.value} already has an active operation")
        runtime = self._runtime(parsed)
        before = self.get_status(parsed)
        self._require_ready(before)
        target = to_device_units(position_mm, runtime.stage.counts_per_mm)
        self._validate_target(runtime, target)
        original = self._set_temporary_motion_parameters(
            runtime, velocity_mm_s, acceleration_mm_s2
        )
        self._pending_motions[parsed] = _PendingMotion(target, original)
        try:
            self._backend.move_absolute(runtime.handle, target)
        except BaseException:
            try:
                self._restore_motion_parameters(parsed)
            finally:
                self._pending_motions.pop(parsed, None)
            raise
        return before

    def start_move_relative(
        self,
        axis: Axis | str,
        distance_mm: float,
        **kwargs,
    ) -> AxisStatus:
        parsed = Axis.parse(axis)
        current = self.get_position_mm(parsed)
        return self.start_move_absolute(parsed, current + distance_mm, **kwargs)

    def poll_motion(self, axis: Axis | str) -> tuple[AxisStatus, bool]:
        """Poll an asynchronous move and restore temporary parameters at completion."""
        parsed = Axis.parse(axis)
        status = self.get_status(parsed)
        pending = self._pending_motions.get(parsed)
        if pending is None:
            return status, True
        if status.fault or status.forward_hard_limit or status.reverse_hard_limit:
            try:
                self.stop(parsed)
            except Exception:
                pass
            raise DeviceStateError(f"Axis {parsed.value} faulted or reached a limit")
        at_target = abs(status.position_counts - pending.target_counts) <= _POSITION_TOLERANCE_COUNTS
        complete = status.settled and not status.moving and status.velocity_raw == 0 and at_target
        pending.stable_reads = pending.stable_reads + 1 if complete else 0
        if pending.stable_reads < 3:
            return status, False
        try:
            self._restore_motion_parameters(parsed)
        finally:
            self._pending_motions.pop(parsed, None)
        return status, True

    def start_home(
        self,
        axis: Axis | str,
        *,
        confirmed: bool = False,
        timeout_s: float = 180.0,
    ) -> AxisStatus:
        """Start Home for an event loop and return without waiting for it."""
        parsed = Axis.parse(axis)
        if not confirmed:
            raise DeviceStateError("Home requires explicit confirmation")
        if parsed in self._pending_motions or parsed in self._pending_homes:
            raise DeviceStateError(f"Axis {parsed.value} already has an active operation")
        before = self.get_status(parsed)
        if not before.enabled or before.moving or before.fault:
            raise DeviceStateError("Axis must be enabled, stationary and fault-free before homing")
        runtime = self._runtime(parsed)
        self._pending_homes[parsed] = _PendingHome(time.monotonic() + timeout_s)
        try:
            self._backend.home(runtime.handle)
        except BaseException:
            self._pending_homes.pop(parsed, None)
            raise
        return before

    def poll_home(self, axis: Axis | str) -> tuple[AxisStatus, bool]:
        """Poll Home without treating its controller-managed limit states as errors."""
        parsed = Axis.parse(axis)
        status = self.get_status(parsed)
        pending = self._pending_homes.get(parsed)
        if pending is None:
            return status, True
        pending.began_homing = pending.began_homing or status.homing or status.moving
        complete = (
            pending.began_homing and status.homed and status.settled
            and not status.homing and not status.moving and status.velocity_raw == 0
        )
        pending.stable_reads = pending.stable_reads + 1 if complete else 0
        if pending.stable_reads >= 3:
            self._pending_homes.pop(parsed, None)
            return status, True
        if time.monotonic() >= pending.deadline:
            self.stop(parsed)
            raise MotionTimeoutError(
                f"Axis {parsed.value} did not complete homing within its timeout; "
                f"last state: homed={status.homed}, homing={status.homing}, "
                f"moving={status.moving}, settled={status.settled}, fault={status.fault}, "
                f"limits=({status.reverse_hard_limit},{status.forward_hard_limit})"
            )
        return status, False

    def wait_until_settled(self, axis: Axis | str, target_counts: int | None = None,
                           timeout_s: float = 30.0) -> AxisStatus:
        deadline = time.monotonic() + timeout_s
        stable = 0
        last = self.get_status(axis)
        while time.monotonic() < deadline:
            last = self.get_status(axis)
            at_target = target_counts is None or abs(last.position_counts - target_counts) <= _POSITION_TOLERANCE_COUNTS
            complete = last.settled and not last.moving and last.velocity_raw == 0 and at_target
            stable = stable + 1 if complete else 0
            if last.fault or last.forward_hard_limit or last.reverse_hard_limit:
                raise DeviceStateError(f"Axis {last.axis.value} faulted or reached a limit")
            if stable >= 3:
                return last
            time.sleep(0.05)
        raise MotionTimeoutError(
            f"Axis {last.axis.value} did not settle within {timeout_s:g} seconds"
        )

    def move_absolute(self, axis: Axis | str, position_mm: float, *, wait: bool = True,
                      timeout_s: float = 30.0, velocity_mm_s: float | None = None,
                      acceleration_mm_s2: float | None = None) -> AxisStatus:
        parsed = Axis.parse(axis)
        runtime = self._runtime(parsed)
        before = self.get_status(parsed)
        self._require_ready(before)
        target = to_device_units(position_mm, runtime.stage.counts_per_mm)
        self._validate_target(runtime, target)
        if not wait and (velocity_mm_s is not None or acceleration_mm_s2 is not None):
            raise DeviceStateError("Temporary velocity overrides require wait=True")
        original = self._set_temporary_motion_parameters(
            runtime, velocity_mm_s, acceleration_mm_s2
        )
        try:
            self._backend.move_absolute(runtime.handle, target)
            if not wait:
                return self.get_status(parsed)
            try:
                return self.wait_until_settled(parsed, target, timeout_s)
            except BaseException:
                self._backend.stop(runtime.handle, False)
                try:
                    self.wait_until_settled(parsed, timeout_s=3.0)
                except Exception:
                    pass
                raise
        finally:
            if original is not None:
                status = self.get_status(parsed)
                if status.moving or status.velocity_raw != 0:
                    self._backend.stop(runtime.handle, False)
                    self.wait_until_settled(parsed, timeout_s=3.0)
                self._backend.set_velocity_params(
                    runtime.handle, original[0], original[1], original[2]
                )

    def move_relative(self, axis: Axis | str, distance_mm: float, **kwargs) -> AxisStatus:
        parsed = Axis.parse(axis)
        current = self.get_position_mm(parsed)
        return self.move_absolute(parsed, current + distance_mm, **kwargs)

    def stop(self, axis: Axis | str | None = None, *, profiled: bool = False) -> None:
        axes = list(Axis) if axis is None else [Axis.parse(axis)]
        first_error = None
        for item in axes:
            try:
                runtime = self._runtime(item)
                self._backend.stop(runtime.handle, profiled)
                if item in self._pending_motions:
                    try:
                        self.wait_until_settled(item, timeout_s=3.0)
                    finally:
                        try:
                            self._restore_motion_parameters(item)
                        finally:
                            self._pending_motions.pop(item, None)
                if item in self._pending_homes:
                    self._pending_homes.pop(item, None)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def enable(self, axis: Axis | str) -> AxisStatus:
        parsed = Axis.parse(axis)
        self._backend.set_enabled(self._runtime(parsed).handle, True)
        time.sleep(0.05)
        return self.get_status(parsed)

    def disable(self, axis: Axis | str) -> AxisStatus:
        parsed = Axis.parse(axis)
        status = self.get_status(parsed)
        if status.moving or status.velocity_raw != 0:
            self.stop(parsed)
            self.wait_until_settled(parsed, timeout_s=3.0)
        self._backend.set_enabled(self._runtime(parsed).handle, False)
        time.sleep(0.05)
        return self.get_status(parsed)

    def home(self, axis: Axis | str, *, confirmed: bool = False,
             timeout_s: float = 180.0) -> AxisStatus:
        parsed = Axis.parse(axis)
        if parsed in self._pending_motions or parsed in self._pending_homes:
            raise DeviceStateError(f"Axis {parsed.value} already has an active operation")
        if not confirmed:
            raise DeviceStateError("Home requires explicit confirmation")
        before = self.get_status(parsed)
        if not before.enabled or before.moving or before.fault:
            raise DeviceStateError("Axis must be enabled, stationary and fault-free before homing")
        runtime = self._runtime(parsed)
        self._backend.home(runtime.handle)
        try:
            deadline = time.monotonic() + timeout_s
            began_homing = False
            stable = 0
            result = before
            while time.monotonic() < deadline:
                result = self.get_status(parsed)
                began_homing = began_homing or result.homing or result.moving
                complete = (
                    began_homing and result.homed and result.settled
                    and not result.homing and not result.moving
                    and result.velocity_raw == 0
                )
                stable = stable + 1 if complete else 0
                if stable >= 3:
                    return result
                time.sleep(0.05)
            raise MotionTimeoutError(
                f"Axis {parsed.value} did not complete homing within {timeout_s:g} seconds; "
                f"last state: homed={result.homed}, homing={result.homing}, "
                f"moving={result.moving}, settled={result.settled}, fault={result.fault}, "
                f"limits=({result.reverse_hard_limit},{result.forward_hard_limit})"
            )
        except BaseException:
            self._backend.stop(runtime.handle, False)
            raise
