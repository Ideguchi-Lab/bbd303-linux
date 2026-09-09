"""Explicit physical-unit parsing and XA device-unit conversion."""

from decimal import Decimal, ROUND_HALF_UP
import re

from .errors import ConfigurationError

_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"


def _match(text: str, units: str) -> tuple[Decimal, str]:
    match = re.fullmatch(rf"\s*({_NUMBER})\s*({units})\s*", text, re.IGNORECASE)
    if not match:
        raise ValueError(f"A value with an explicit unit is required: {text!r}")
    return Decimal(match.group(1)), match.group(2).lower()


def parse_distance_mm(text: str) -> float:
    value, unit = _match(text, r"mm|um|µm|μm")
    if unit != "mm":
        value /= Decimal(1000)
    return float(value)


def parse_velocity_mm_s(text: str) -> float:
    value, unit = _match(text, r"mm/s|um/s|µm/s|μm/s")
    if unit != "mm/s":
        value /= Decimal(1000)
    return float(value)


def parse_acceleration_mm_s2(text: str) -> float:
    value, unit = _match(text, r"mm/s(?:\^?2)|um/s(?:\^?2)|µm/s(?:\^?2)|μm/s(?:\^?2)")
    if unit.startswith(("um", "µm", "μm")):
        value /= Decimal(1000)
    return float(value)


def to_device_units(value: float, scale: float) -> int:
    if scale <= 0:
        raise ConfigurationError(f"Invalid device scale: {scale}")
    return int((Decimal(str(value)) * Decimal(str(scale))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

