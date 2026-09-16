"""Typed values returned by the controller API."""

from dataclasses import dataclass
from enum import Enum


class Axis(str, Enum):
    X = "x"
    Y = "y"

    @classmethod
    def parse(cls, value: "Axis | str") -> "Axis":
        if isinstance(value, cls):
            return value
        return cls(value.lower())


@dataclass(frozen=True)
class DiscoveryResult:
    connection: str
    serial: str
    model: str
    source: str


@dataclass(frozen=True)
class ControllerInfo:
    serial: str
    model: str
    connection: str
    firmware: str
    hardware_version: int
    channel_count: int


@dataclass(frozen=True)
class StageInfo:
    axis: Axis
    device_id: str
    product_name: str
    stage_serial: int
    counts_per_mm: int
    min_position_mm: float
    max_position_mm: float
    max_velocity_mm_s: float
    max_acceleration_mm_s2: float


@dataclass(frozen=True)
class AxisStatus:
    axis: Axis
    position_mm: float
    position_counts: int
    velocity_mm_s: float
    velocity_raw: int
    motor_current_raw: int
    status_bits: int
    enabled: bool
    homed: bool
    homing: bool
    settled: bool
    moving: bool
    forward_hard_limit: bool
    reverse_hard_limit: bool
    fault: bool


@dataclass(frozen=True)
class MotionParameters:
    axis: Axis
    min_velocity_mm_s: float
    max_velocity_mm_s: float
    acceleration_mm_s2: float
    min_velocity_raw: int
    max_velocity_raw: int
    acceleration_raw: int
    hard_limit_forward_mode: int
    hard_limit_reverse_mode: int
    soft_limit_mode: int


@dataclass(frozen=True)
class JogParameters:
    axis: Axis
    mode: str
    step_size_mm: float
    min_velocity_mm_s: float
    max_velocity_mm_s: float
    acceleration_mm_s2: float
    stop_mode: str
    mode_raw: int
    step_size_raw: int
    min_velocity_raw: int
    max_velocity_raw: int
    acceleration_raw: int
    stop_mode_raw: int

