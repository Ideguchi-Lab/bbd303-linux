"""Command-line interface for BBD303 control."""

import argparse
from dataclasses import asdict, is_dataclass
from enum import Enum
import json
from pathlib import Path
import sys
import time

from . import __version__
from .config import load_config, write_config
from .controller import BBD303Controller
from .discovery import discover
from .errors import (
    BBD303Error,
    ConfigurationError,
    DeviceBusyError,
    DeviceStateError,
    DiscoveryError,
    MotionTimeoutError,
    XACommunicationError,
)
from .models import Axis
from .units import parse_acceleration_mm_s2, parse_distance_mm, parse_velocity_mm_s


def _serializable(value):
    if is_dataclass(value):
        return {key: _serializable(item) for key, item in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable(item) for item in value]
    return value


class Output:
    def __init__(self, json_mode: bool):
        self.json_mode = json_mode

    def emit(self, value, text: str | None = None) -> None:
        if self.json_mode:
            print(json.dumps(_serializable(value), ensure_ascii=False, sort_keys=True))
        else:
            print(text if text is not None else value)


def _axis_values(value: str) -> list[Axis]:
    return list(Axis) if value == "all" else [Axis.parse(value)]


def _status_line(status) -> str:
    state = "moving" if status.moving else "settled" if status.settled else "idle"
    return (
        f"{status.axis.value.upper()}: {status.position_mm:.5f} mm, {state}, "
        f"homed={status.homed}, enabled={status.enabled}, fault={status.fault}, "
        f"limits=({status.reverse_hard_limit},{status.forward_hard_limit})"
    )


def _add_move_options(
    parser: argparse.ArgumentParser, *, option: str, short_option: str, description: str
) -> None:
    parser.add_argument("axis", choices=("x", "y"))
    parser.add_argument("legacy_value", nargs="?", help=argparse.SUPPRESS)
    parser.add_argument(
        short_option, option, dest="motion_value", metavar="VALUE",
        help=f"{description} with unit; use {short_option}=-1um for negative values",
    )
    parser.add_argument("--velocity", help="temporary velocity with unit, for example 0.1mm/s")
    parser.add_argument("--acceleration", help="temporary acceleration, for example 1mm/s2")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--no-wait", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bbd303", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, help="private TOML configuration file")
    parser.add_argument("--connection", help="explicit IPv4 connection, for example 192.0.2.10:40303")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    configure = commands.add_parser("configure", help="write a private user configuration")
    configure.add_argument("--serial", required=True)
    configure.add_argument("--subnet", action="append", required=True,
                           help="IPv4 CIDR to scan; repeat for multiple subnets")

    discover_parser = commands.add_parser("discover", help="find and verify the configured controller")
    discover_parser.add_argument("--refresh", action="store_true", help="ignore the cached address")

    info = commands.add_parser("info", help="show controller, stages, and motion parameters")
    info.add_argument("axis", nargs="?", default="all", choices=("x", "y", "all"))
    status = commands.add_parser("status", help="show axis status")
    status.add_argument("axis", nargs="?", default="all", choices=("x", "y", "all"))
    status.add_argument("--watch", nargs="?", type=float, const=0.2,
                        help="refresh continuously; optional interval in seconds")
    position = commands.add_parser("position", help=argparse.SUPPRESS)
    position.add_argument("axis", nargs="?", default="all", choices=("x", "y", "all"))
    params = commands.add_parser("params", help=argparse.SUPPRESS)
    params.add_argument("axis", nargs="?", default="all", choices=("x", "y", "all"))

    absolute = commands.add_parser("move-absolute", help="move to an absolute position")
    _add_move_options(
        absolute, option="--position", short_option="-p", description="absolute position"
    )
    move = commands.add_parser("move", help="move by a relative distance")
    _add_move_options(
        move, option="--distance", short_option="-d", description="relative distance"
    )
    relative = commands.add_parser("move-relative", help="move by a relative distance")
    _add_move_options(
        relative, option="--distance", short_option="-d", description="relative distance"
    )
    stop = commands.add_parser("stop", help="stop one or both axes")
    stop.add_argument("axis", nargs="?", default="all", choices=("x", "y", "all"))
    stop.add_argument("--profiled", action="store_true")
    for name in ("enable", "disable"):
        subparser = commands.add_parser(name, help=f"{name} one or both axes")
        subparser.add_argument("axis", nargs="?", default="all", choices=("x", "y", "all"))

    home = commands.add_parser("home", help="perform a potentially full-travel home operation")
    home.add_argument("axis", choices=("x", "y"))
    home.add_argument("--allow-full-travel", action="store_true", required=True)
    home.add_argument("--timeout", type=float, default=180.0)
    # Keep old read commands working without advertising redundant top-level entries.
    commands._choices_actions = [
        action for action in commands._choices_actions
        if action.dest not in {"position", "params"}
    ]
    return parser


def _selected_motion_value(args) -> str:
    named = args.motion_value
    legacy = args.legacy_value
    if named is not None and legacy is not None:
        raise ValueError("Specify the motion value only once")
    if named is None and legacy is None:
        option = "--position" if args.command == "move-absolute" else "--distance"
        raise ValueError(f"A motion value is required; use {option}=VALUE")
    return named if named is not None else legacy


def _request_home_confirmation(args) -> bool:
    if not sys.stdin.isatty():
        raise DeviceStateError("Home is refused without an interactive terminal")
    print(
        f"WARNING: Homing {args.axis.upper()} may traverse the full mechanical range.\n"
        "Clear the entire travel path before continuing."
    )
    while True:
        entered = input(f"Proceed with Home {args.axis.upper()}? [y/N] ").strip().lower()
        if entered in {"y", "yes"}:
            return True
        if entered in {"", "n", "no"}:
            return False
        print("Please answer y or n.")


def _move_kwargs(args) -> dict:
    if args.timeout <= 0:
        raise ValueError("Timeout must be positive")
    return {
        "wait": not args.no_wait,
        "timeout_s": args.timeout,
        "velocity_mm_s": parse_velocity_mm_s(args.velocity) if args.velocity else None,
        "acceleration_mm_s2": parse_acceleration_mm_s2(args.acceleration) if args.acceleration else None,
    }


def _run_controller(args, output: Output, config=None) -> int:
    config = config or load_config(args.config)
    with BBD303Controller(config, connection=args.connection) as controller:
        command = args.command
        if command == "info":
            axes = _axis_values(args.axis)
            value = {
                "controller": controller.get_device_info(),
                "axes": [
                    {
                        "stage": controller.get_stage_info(axis),
                        "motion": controller.get_motion_parameters(axis),
                    }
                    for axis in axes
                ],
            }
            info = value["controller"]
            lines = [
                f"Controller: {info.model}, firmware {info.firmware}, "
                f"{info.channel_count} channels",
                f"Connection: {info.connection}",
            ]
            for axis_info in value["axes"]:
                stage = axis_info["stage"]
                motion = axis_info["motion"]
                lines.append(
                    f"{stage.axis.value.upper()}: {stage.product_name}, "
                    f"travel {stage.min_position_mm:g}..{stage.max_position_mm:g} mm, "
                    f"{stage.counts_per_mm} counts/mm"
                )
                lines.append(
                    f"  max velocity {motion.max_velocity_mm_s:.6g} mm/s, "
                    f"acceleration {motion.acceleration_mm_s2:.6g} mm/s², "
                    f"hard limits=({motion.hard_limit_reverse_mode},"
                    f"{motion.hard_limit_forward_mode}), soft limit={motion.soft_limit_mode}"
                )
            output.emit(value, "\n".join(lines))
        elif command == "status":
            if args.watch is not None and args.watch <= 0:
                raise ValueError("Watch interval must be positive")
            while True:
                values = [controller.get_status(axis) for axis in _axis_values(args.axis)]
                output.emit(values, "\n".join(_status_line(value) for value in values))
                if args.watch is None:
                    break
                time.sleep(args.watch)
        elif command == "position":
            values = {axis.value: controller.get_position_mm(axis) for axis in _axis_values(args.axis)}
            text = "\n".join(f"{axis.upper()}: {value:.5f} mm" for axis, value in values.items())
            output.emit(values, text)
        elif command == "params":
            values = [controller.get_motion_parameters(axis) for axis in _axis_values(args.axis)]
            text = "\n".join(
                f"{value.axis.value.upper()}: max velocity {value.max_velocity_mm_s:.6g} mm/s, "
                f"acceleration {value.acceleration_mm_s2:.6g} mm/s², "
                f"hard limits=({value.hard_limit_reverse_mode},"
                f"{value.hard_limit_forward_mode}), soft limit={value.soft_limit_mode}"
                for value in values
            )
            output.emit(values, text)
        elif command in ("move", "move-absolute", "move-relative"):
            value = parse_distance_mm(args.motion_value)
            kwargs = _move_kwargs(args)
            method = controller.move_absolute if command == "move-absolute" else controller.move_relative
            result = method(args.axis, value, **kwargs)
            output.emit(result, _status_line(result))
        elif command == "stop":
            controller.stop(None if args.axis == "all" else args.axis, profiled=args.profiled)
            values = [controller.get_status(axis) for axis in _axis_values(args.axis)]
            output.emit(values, "\n".join(_status_line(value) for value in values))
        elif command in ("enable", "disable"):
            method = controller.enable if command == "enable" else controller.disable
            values = [method(axis) for axis in _axis_values(args.axis)]
            output.emit(values, "\n".join(_status_line(value) for value in values))
        elif command == "home":
            status = controller.get_status(args.axis)
            stage = controller.get_stage_info(args.axis)
            print(
                f"Starting Home {args.axis.upper()} from {status.position_mm:.5f} mm; "
                f"reported travel is {stage.min_position_mm:g} to "
                f"{stage.max_position_mm:g} mm."
            )
            result = controller.home(
                args.axis, confirmed=args.home_confirmed, timeout_s=args.timeout
            )
            output.emit(result, _status_line(result))
        else:
            raise RuntimeError(f"Unhandled command: {command}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    output = Output(args.json)
    try:
        if args.command in ("move", "move-absolute", "move-relative"):
            args.motion_value = _selected_motion_value(args)
            parse_distance_mm(args.motion_value)
        if args.command == "configure":
            location = write_config(args.serial, args.subnet, args.config)
            # Validate the file immediately without printing its sensitive content.
            load_config(location)
            output.emit({"config": str(location)}, f"Private configuration written to {location}")
            return 0
        if args.command == "discover":
            config = load_config(args.config)
            result = discover(config, args.connection, args.refresh)
            output.emit(result, f"{result.model} found at {result.connection} ({result.source})")
            return 0
        if args.command == "home":
            config = load_config(args.config)
            args.home_confirmed = _request_home_confirmation(args)
            if not args.home_confirmed:
                output.emit({"cancelled": True}, "Home cancelled")
                return 0
            return _run_controller(args, output, config)
        return _run_controller(args, output)
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except (ValueError, ConfigurationError) as exc:
        print(f"Configuration or input error: {exc}", file=sys.stderr)
        return 2
    except DiscoveryError as exc:
        print(f"Discovery error: {exc}", file=sys.stderr)
        return 3
    except (DeviceBusyError, DeviceStateError) as exc:
        print(f"Device state error: {exc}", file=sys.stderr)
        return 4
    except MotionTimeoutError as exc:
        print(f"Motion timeout: {exc}", file=sys.stderr)
        return 5
    except XACommunicationError as exc:
        print(f"XA communication error: {exc}", file=sys.stderr)
        return 6
    except BBD303Error as exc:
        print(f"BBD303 error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
