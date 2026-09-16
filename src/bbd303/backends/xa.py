"""Small adapter around the official XA native Python binding."""

from typing import Any

from ..errors import XACommunicationError


class XABackend:
    def __init__(self, connection: str):
        try:
            from thorlabs_xa.shared.system_manager import SystemManager
            from thorlabs_xa.shared.enums import (
                TLMC_EnableState,
                TLMC_JogMode,
                TLMC_JogStopMode,
                TLMC_MoveDirection,
                TLMC_MoveMode,
                TLMC_OperatingMode,
                TLMC_StopMode,
            )
            from thorlabs_xa.shared.params import TLMC_JogParams, TLMC_VelocityParams
        except ImportError as exc:
            raise XACommunicationError(
                "thorlabs-xa is not installed; install this package in the bbd environment"
            ) from exc
        self._enums = {
            "enable": TLMC_EnableState,
            "jog": TLMC_JogMode,
            "jog_stop": TLMC_JogStopMode,
            "direction": TLMC_MoveDirection,
            "move": TLMC_MoveMode,
            "operating": TLMC_OperatingMode,
            "stop": TLMC_StopMode,
        }
        self.velocity_params_type = TLMC_VelocityParams
        self.jog_params_type = TLMC_JogParams
        self.manager = SystemManager.instance()
        self.api = self.manager.native_functions
        self.connection = connection
        self.started = False

    def _call(self, operation: str, function, *args):
        try:
            return function(*args)
        except Exception as exc:
            code = getattr(exc, "error_code", None)
            suffix = f" (XA error {code})" if code is not None else ""
            raise XACommunicationError(f"{operation} failed{suffix}: {exc}") from exc

    def start(self) -> None:
        # XA 1.4.0.28472 requires whitespace around the assignment operator.
        self._call("XA startup", self.manager.startup,
                   f"deviceDiscovery.connections = {self.connection}")
        self.started = True

    def shutdown(self) -> None:
        if self.started:
            self._call("XA shutdown", self.manager.shutdown)
            self.started = False

    def list_devices(self) -> list[Any]:
        return self._call("device discovery", self.manager.get_device_list)

    def open(self, device: str, transport: str) -> int:
        op = self._enums["operating"]
        mode = op.TLMC_OperatingMode_Expert & ~op.TLMC_OperatingMode_DoNotAutoSetConnectedProduct
        return self._call("open device", self.api.open, device, transport, mode)

    def close(self, handle: int) -> None:
        self._call("close device", self.api.close, handle)

    def hardware_info(self, handle: int, timeout_ms: int):
        return self._call("read hardware info", self.api.get_hardware_info, handle, timeout_ms)

    def stage_axis_params(self, handle: int, timeout_ms: int):
        return self._call("read stage parameters", self.api.get_stage_axis_params, handle, timeout_ms)

    def connected_product_info(self, handle: int):
        return self._call("read connected product", self.api.get_connected_product_info, handle)

    def universal_status(self, handle: int, timeout_ms: int):
        return self._call("read status", self.api.get_universal_status, handle, timeout_ms)

    def enable_state(self, handle: int, timeout_ms: int):
        return self._call("read enable state", self.api.get_enable_state, handle, timeout_ms)

    def velocity_params(self, handle: int, timeout_ms: int):
        return self._call("read velocity parameters", self.api.get_velocity_params, handle, timeout_ms)

    def limit_switch_params(self, handle: int, timeout_ms: int):
        return self._call("read limit parameters", self.api.get_limit_switch_params, handle, timeout_ms)

    def jog_params(self, handle: int, timeout_ms: int):
        return self._call("read jog parameters", self.api.get_jog_params, handle, timeout_ms)

    def set_velocity_params(self, handle: int, minimum: int, acceleration: int, maximum: int) -> None:
        params = self.velocity_params_type()
        params.min_velocity = minimum
        params.acceleration = acceleration
        params.max_velocity = maximum
        self._call("set temporary velocity parameters", self.api.set_velocity_params, handle, params)

    def set_jog_params(
        self, handle: int, mode: int, step_size: int, minimum: int,
        acceleration: int, maximum: int, stop_mode: int,
    ) -> None:
        params = self.jog_params_type()
        params.mode = self._enums["jog"](mode)
        params.step_size = step_size
        params.min_velocity = minimum
        params.acceleration = acceleration
        params.max_velocity = maximum
        params.stop_mode = self._enums["jog_stop"](stop_mode)
        self._call("set temporary jog parameters", self.api.set_jog_params, handle, params)

    def move_absolute(self, handle: int, position: int) -> None:
        mode = self._enums["move"].TLMC_MoveMode_Absolute
        self._call("move absolute", self.api.move_absolute, handle, mode, position, 0)

    def move_jog(self, handle: int, forward: bool) -> None:
        enum = self._enums["direction"]
        direction = enum.Move_Direction_Forward if forward else enum.Move_Direction_Reverse
        self._call("jog", self.api.move_jog, handle, direction, 0)

    def stop(self, handle: int, profiled: bool = False) -> None:
        enum = self._enums["stop"]
        mode = enum.TLMC_StopMode_Profiled if profiled else enum.TLMC_StopMode_Immediate
        self._call("stop", self.api.stop, handle, mode, 0)

    def set_enabled(self, handle: int, enabled: bool) -> None:
        enum = self._enums["enable"]
        state = enum.TLMC_Enabled if enabled else enum.TLMC_Disabled
        self._call("set enable state", self.api.set_enable_state, handle, state)

    def home(self, handle: int) -> None:
        self._call("home", self.api.home, handle, 0)

