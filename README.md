# BBD303 Control for Linux

This package controls a Thorlabs BBD303 with MLS203 X/Y stages directly from
Linux over Ethernet. It uses the official Thorlabs XA Python package and does
not require a Windows virtual machine or a server process.

The implementation currently covers the everyday functions from the XA GUI:

- Ethernet discovery by controller serial number, with a cached address for DHCP
- controller and connected-stage identity checks
- position, status, homed/enabled/fault/limit state, and motion-parameter reads
- absolute and relative moves
- immediate or profiled stop and enable/disable
- a small PySide6 GUI with live X/Y positions and hardware-state indicators,
  absolute moves, relative micro-steps, speed/acceleration controls,
  enable/disable, and an always-responsive Stop button
- guarded Home commands in the CLI and GUI

Path movement, raster scans, synchronization, triggers, persistent parameter
editing, and firmware updates are outside the first release.

## Install

Use the existing `bbd` conda environment:

```bash
conda activate bbd
cd /home/liukdiihmieu/Lab/BBD303
python -m pip install -e .
```

Install the GUI extra when PySide6 is wanted:

```bash
python -m pip install -e '.[gui]'
```

The package is pinned to `thorlabs-xa==1.4.0.28472`, the XA build against
which this controller was validated.

## Private configuration

Hardware serial numbers and local network addresses do not belong in the Git
repository. The default config is `~/.config/bbd303/config.toml`; the command
below creates it with mode `0600`:

```bash
bbd303 configure --serial YOUR_CONTROLLER_SERIAL --subnet 192.0.2.0/24
```

Repeat `--subnet` to scan more than one network. A checked-in template is
available as [`config.example.toml`](config.example.toml). The environment
variable `BBD303_CONFIG` or the global CLI option `--config` can select another
private file. Discovery first tries its last verified address and then scans
the configured subnet, so a DHCP address change does not require editing the
config.

## Read-only commands

```bash
bbd303 discover
bbd303 info
bbd303 info x
bbd303 status
bbd303 status x --watch
bbd303 --json status
```

Global options such as `--json`, `--config`, and `--connection` go before the
subcommand. A direct address can be used for one invocation without putting it
in a file:

```bash
bbd303 --connection 192.0.2.10:40303 status
```

## Motion commands

Distances require an explicit `mm` or `um` unit. Velocity and acceleration also
require units, which prevents a bare number from being interpreted at the wrong
scale.

```bash
bbd303 move x -d=2um
bbd303 move x -d=-2um --velocity 0.1mm/s --acceleration 1mm/s2
bbd303 move-relative y --distance=2um
bbd303 move-absolute x -p=58.232mm
bbd303 stop
bbd303 disable x
bbd303 enable x
```

Before a move, the library requires the selected axis to report Homed, Enabled,
stationary, Settled, and free of faults and hard-limit signals. It checks that
the target is strictly inside the connected stage's reported travel range.
Temporary velocity and acceleration values are restored after a completed or
failed synchronous move. `Ctrl+C` during a synchronous CLI move triggers the
controller cleanup path; use a separate `bbd303 stop` only when no other local
process owns the device lock.

Home can travel across much of the stage. The CLI requires both a dedicated
flag and an interactive `y/N` confirmation:

```bash
bbd303 home x --allow-full-travel
```

Only run Home when the full mechanical path is clear.

`move` is the short alias for `move-relative`. Use an equals sign when a value
is negative (`-d=-1um` or `--distance=-1um`), because command-line parsers can
otherwise interpret the value as another option. The older `position` and
`params` commands remain available for compatibility; their information is now
provided by `status` and `info`, respectively.

## GUI

```bash
bbd303-gui
```

The GUI polls both axes every 200 ms. Motion calls are issued asynchronously,
so position updates and Stop remain available during a move. The velocity and
acceleration fields apply temporarily to each move; the controller restores its
previous values at completion, after Stop, on error, and during shutdown.
Commands that differ from the last read position by more than 1 mm show a
confirmation dialog. GUI Home also runs asynchronously, keeps live position
updates and Stop available, and requires a separate full-travel confirmation.
A disabled Jog tab reserves space for a future implementation of the
controller's native XA Jog.

The GUI remembers each axis's maximum velocity, acceleration, and relative
step. Changes are saved automatically to
`~/.local/state/bbd303/gui-state.json` (or `$XDG_STATE_HOME/bbd303/gui-state.json`)
and restored on the next connection. `Save as Default` records a separate
per-axis preset, while `Restore Default` reloads it. Until a preset is saved,
the built-in defaults are 1 mm/s, 10 mm/s², and a 1 µm relative step. The state
file contains no controller serial number or network address.

## Python API

```python
from bbd303 import BBD303Controller

with BBD303Controller() as stage:
    x = stage.get_position_mm("x")
    stage.move_relative("x", 0.002)  # values in the API are millimetres
    stage.move_absolute("x", x)
```

The context manager owns an inter-process lock and closes all XA handles on
exit. Only one process on this Linux host can use a configured controller at a
time.

## Tests

The automated suite uses a fake XA backend and never connects to or moves real
hardware:

```bash
python -m unittest discover -s tests -v
```

Earlier exploratory scripts, official PDFs, logs, and machine-specific notes
are retained locally under `.private_research/`. That directory is ignored by
Git because those records contain hardware and network identifiers.

## License

MIT. See [`LICENSE`](LICENSE).
