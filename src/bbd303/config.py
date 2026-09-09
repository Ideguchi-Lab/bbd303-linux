"""Private user configuration with no hardware identifiers in the repository."""

from dataclasses import dataclass, field
import ipaddress
import os
from pathlib import Path
import tomllib

from .errors import ConfigurationError
from .models import Axis


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "bbd303"


def cache_home() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "bbd303"


def default_config_path() -> Path:
    override = os.environ.get("BBD303_CONFIG")
    return Path(override).expanduser() if override else config_home() / "config.toml"


@dataclass(frozen=True)
class Config:
    serial: str
    model: str = "BBD303"
    port: int = 40303
    scan_subnets: tuple[str, ...] = ()
    expected_stages: dict[Axis, str] = field(default_factory=lambda: {
        Axis.X: "MLS203 X Axis",
        Axis.Y: "MLS203 Y Axis",
    })
    cache_path: Path = field(default_factory=lambda: cache_home() / "device.json")

    def __post_init__(self) -> None:
        if not self.serial or "REPLACE" in self.serial.upper():
            raise ConfigurationError("Set the controller serial number in the private config file")
        if not self.serial.isdigit():
            raise ConfigurationError("Controller serial must contain digits only")
        if not 1 <= self.port <= 65535:
            raise ConfigurationError("Controller port must be between 1 and 65535")
        if not self.scan_subnets:
            raise ConfigurationError("At least one discovery subnet is required")
        for subnet in self.scan_subnets:
            try:
                ipaddress.IPv4Network(subnet, strict=False)
            except ValueError as exc:
                raise ConfigurationError(f"Invalid IPv4 discovery subnet: {subnet}") from exc
        for axis in Axis:
            if axis not in self.expected_stages:
                raise ConfigurationError(f"Missing expected stage for axis {axis.value}")


def load_config(path: str | Path | None = None) -> Config:
    location = Path(path).expanduser() if path else default_config_path()
    if not location.is_file():
        raise ConfigurationError(
            f"Configuration not found: {location}. Create it from config.example.toml."
        )
    try:
        data = tomllib.loads(location.read_text(encoding="utf-8"))
        controller = data["controller"]
        axes = data["axes"]
        return Config(
            serial=str(controller["serial"]),
            model=str(controller.get("model", "BBD303")),
            port=int(controller.get("port", 40303)),
            scan_subnets=tuple(str(item) for item in controller["scan_subnets"]),
            expected_stages={
                Axis.X: str(axes["x"]["expected_stage"]),
                Axis.Y: str(axes["y"]["expected_stage"]),
            },
        )
    except (KeyError, TypeError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"Invalid configuration in {location}: {exc}") from exc


def write_config(serial: str, subnets: list[str], path: str | Path | None = None) -> Path:
    """Write a private config. Values are deliberately never written into the repository."""
    Config(serial=serial, scan_subnets=tuple(subnets))
    location = Path(path).expanduser() if path else default_config_path()
    location.parent.mkdir(parents=True, exist_ok=True)
    subnet_lines = ", ".join(f'"{item}"' for item in subnets)
    content = (
        "[controller]\n"
        f'serial = "{serial}"\n'
        'model = "BBD303"\n'
        "port = 40303\n"
        f"scan_subnets = [{subnet_lines}]\n\n"
        "[axes.x]\n"
        'expected_stage = "MLS203 X Axis"\n\n'
        "[axes.y]\n"
        'expected_stage = "MLS203 Y Axis"\n'
    )
    temporary = location.with_suffix(location.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(location)
    return location
