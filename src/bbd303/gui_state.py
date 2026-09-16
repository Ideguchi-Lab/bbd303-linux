"""Persistent, device-independent settings for the GUI."""

from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import tempfile

from .models import Axis


_STATE_VERSION = 1


@dataclass(frozen=True)
class GuiAxisSettings:
    velocity_mm_s: float
    acceleration_mm_s2: float
    relative_step_um: float


@dataclass(frozen=True)
class GuiJogSettings:
    mode: str
    step_size_um: float
    velocity_mm_s: float
    acceleration_mm_s2: float


BUILTIN_DEFAULTS = GuiAxisSettings(
    velocity_mm_s=1.0,
    acceleration_mm_s2=10.0,
    relative_step_um=1.0,
)

BUILTIN_JOG_DEFAULTS = GuiJogSettings(
    mode="single_step",
    step_size_um=1.0,
    velocity_mm_s=1.0,
    acceleration_mm_s2=10.0,
)


def gui_state_path() -> Path:
    root = os.environ.get("XDG_STATE_HOME")
    base = Path(root).expanduser() if root else Path.home() / ".local" / "state"
    return base / "bbd303" / "gui-state.json"


def _settings_from_mapping(value) -> GuiAxisSettings | None:
    if not isinstance(value, dict):
        return None
    try:
        settings = GuiAxisSettings(
            velocity_mm_s=float(value["velocity_mm_s"]),
            acceleration_mm_s2=float(value["acceleration_mm_s2"]),
            relative_step_um=float(value["relative_step_um"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(item) and item > 0 for item in asdict(settings).values()):
        return None
    return settings


def _jog_settings_from_mapping(value) -> GuiJogSettings | None:
    if not isinstance(value, dict):
        return None
    try:
        settings = GuiJogSettings(
            mode=str(value["mode"]),
            step_size_um=float(value["step_size_um"]),
            velocity_mm_s=float(value["velocity_mm_s"]),
            acceleration_mm_s2=float(value["acceleration_mm_s2"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    numbers = (
        settings.step_size_um,
        settings.velocity_mm_s,
        settings.acceleration_mm_s2,
    )
    if settings.mode != "single_step" or not all(
        math.isfinite(item) and item > 0 for item in numbers
    ):
        return None
    return settings


class GuiStateStore:
    """Load and atomically save last-used and user-default GUI values."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path).expanduser() if path is not None else gui_state_path()
        self.warning: str | None = None
        self._axes: dict[str, dict[str, GuiAxisSettings]] = {}
        self._jogs: dict[str, dict[str, GuiJogSettings]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("GUI state must be a JSON object")
            if payload.get("version") != _STATE_VERSION or not isinstance(
                payload.get("axes"), dict
            ):
                raise ValueError("unsupported or missing state version")
            for axis in Axis:
                raw_axis = payload["axes"].get(axis.value, {})
                if not isinstance(raw_axis, dict):
                    continue
                groups = {}
                for name in ("last", "defaults"):
                    settings = _settings_from_mapping(raw_axis.get(name))
                    if settings is not None:
                        groups[name] = settings
                if groups:
                    self._axes[axis.value] = groups
                raw_jog = raw_axis.get("jog", {})
                if not isinstance(raw_jog, dict):
                    continue
                jog_groups = {}
                for name in ("last", "defaults"):
                    settings = _jog_settings_from_mapping(raw_jog.get(name))
                    if settings is not None:
                        jog_groups[name] = settings
                if jog_groups:
                    self._jogs[axis.value] = jog_groups
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            self.warning = f"Could not read GUI settings; using defaults: {exc}"
            self._axes = {}
            self._jogs = {}

    def defaults(self, axis: Axis | str) -> GuiAxisSettings:
        key = Axis.parse(axis).value
        return self._axes.get(key, {}).get("defaults", BUILTIN_DEFAULTS)

    def last(self, axis: Axis | str) -> GuiAxisSettings:
        key = Axis.parse(axis).value
        return self._axes.get(key, {}).get("last", self.defaults(key))

    def jog_defaults(self, axis: Axis | str) -> GuiJogSettings:
        key = Axis.parse(axis).value
        return self._jogs.get(key, {}).get("defaults", BUILTIN_JOG_DEFAULTS)

    def jog_last(self, axis: Axis | str) -> GuiJogSettings:
        key = Axis.parse(axis).value
        return self._jogs.get(key, {}).get("last", self.jog_defaults(key))

    def set_last(self, axis: Axis | str, settings: GuiAxisSettings) -> None:
        self._set(axis, "last", settings)

    def set_defaults(self, axis: Axis | str, settings: GuiAxisSettings) -> None:
        self._set(axis, "defaults", settings)

    def set_jog_last(self, axis: Axis | str, settings: GuiJogSettings) -> None:
        self._set_jog(axis, "last", settings)

    def set_jog_defaults(self, axis: Axis | str, settings: GuiJogSettings) -> None:
        self._set_jog(axis, "defaults", settings)

    def _set(self, axis: Axis | str, name: str, settings: GuiAxisSettings) -> None:
        validated = _settings_from_mapping(asdict(settings))
        if validated is None:
            raise ValueError("GUI settings must be finite positive numbers")
        key = Axis.parse(axis).value
        self._axes.setdefault(key, {})[name] = validated

    def _set_jog(self, axis: Axis | str, name: str, settings: GuiJogSettings) -> None:
        validated = _jog_settings_from_mapping(asdict(settings))
        if validated is None:
            raise ValueError("Jog settings must use Single Step and finite positive numbers")
        key = Axis.parse(axis).value
        self._jogs.setdefault(key, {})[name] = validated

    def save(self) -> None:
        axis_names = sorted(set(self._axes) | set(self._jogs))
        axes = {}
        for axis in axis_names:
            groups = {
                name: asdict(settings)
                for name, settings in self._axes.get(axis, {}).items()
            }
            jog_groups = {
                name: asdict(settings)
                for name, settings in self._jogs.get(axis, {}).items()
            }
            if jog_groups:
                groups["jog"] = jog_groups
            axes[axis] = groups
        payload = {"version": _STATE_VERSION, "axes": axes}
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary_name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary_name = stream.name
                json.dump(payload, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if temporary_name is not None:
                try:
                    Path(temporary_name).unlink()
                except FileNotFoundError:
                    pass
