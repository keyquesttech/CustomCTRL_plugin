# CustomCTRL — Klipper Plugin

A Klipper "Extra" that reads physical buttons wired to the MCU and provides:

- **Instant multi-axis jogging** — hold a button to jog X, Y, or Z continuously at 20 Hz. Multiple axes can be held simultaneously for diagonal movement.
- **Hold-to-extrude** — hold the extrude button for continuous filament extrusion based on volumetric flow rate, combinable with jog buttons.
- **Macro buttons** — press once to fire any G-code macro.
- **Stop on release** — motion and extrusion halt the moment all buttons are released.
- **Smooth motion** — moves chain through Klipper's lookahead queue for proper acceleration, only flushing on stop for instant halt.

Motion is injected via `toolhead.manual_move()`, bypassing the G-code queue for immediate execution.

## Requirements

- Klipper installed at `~/klipper` on a Raspberry Pi (or similar).
- Physical buttons wired to available MCU pins (e.g. SKR Mini E3 V3.0).
- Python 3.x (Klipper's environment).

## Install

```bash
cd ~/CustomCTRL_plugin   # or wherever you cloned this repo
chmod +x install.sh
./install.sh
```

This creates a symlink from `customctrl.py` into `~/klipper/klippy/extras/` and restarts Klipper.

## Uninstall

```bash
./uninstall.sh
```

Removes the symlink and restarts Klipper.

## Configuration

Add a `[customctrl]` section to your `printer.cfg`. See [`printer_snippet.cfg`](printer_snippet.cfg) for a fully commented example.

### Jog buttons (hold to jog)

| Option | Default | Description |
|---|---|---|
| `x_jog_pin` | *(none)* | MCU pin for X-axis jog button |
| `y_jog_pin` | *(none)* | MCU pin for Y-axis jog button |
| `z_jog_pin` | *(none)* | MCU pin for Z-axis jog button |
| `jog_speed` | `10` | Jog speed in mm/s (per-tick distance is derived automatically) |

### Extrude button (hold to extrude)

| Option | Default | Description |
|---|---|---|
| `extrude_pin` | *(none)* | MCU pin for extrude button |
| `filament_diameter` | `1.75` | Filament diameter in mm |
| `volumetric_flow` | `1.0` | Target volumetric flow rate in mm³/s |

The linear extrusion rate is calculated automatically:
`E_rate = volumetric_flow / (pi * (filament_diameter / 2)²)`

### Macro buttons (press to fire)

Up to 8 macro buttons. Each requires both a pin and a G-code line:

| Option | Description |
|---|---|
| `macro_N_pin` | MCU pin for macro button N (1-8) |
| `macro_N_gcode` | G-code command to run on press |

### Terminal output

| Option | Default | Description |
|---|---|---|
| `verbose` | `False` | When `True`, button events and errors are printed to the Klipper console |

Errors are always shown in the console regardless of the `verbose` setting.

### Pin syntax

Follows standard Klipper pin notation:

- `PA8` — active high
- `^PA8` — with internal pull-up
- `!PA8` — inverted (active low)
- `^!PA8` — pull-up + inverted

## Safety

- **Homing required** — jogging an axis is blocked until that axis is homed.
- **Print protection** — all jogging and extrusion are disabled while a virtual SD card print is active.
- **Immediate halt** — releasing all jog/extrude buttons flushes the motion queue so motion stops right away.
- **Machine limits** — jog positions are clamped to the printer's configured axis min/max boundaries.
- **Speed limits** — jog speed is capped to the printer's max_velocity.

## How it works

1. On `klippy:ready`, CustomCTRL registers each configured pin with Klipper's `buttons` module.
2. Button press/release events update an internal state dictionary.
3. When any jog or extrude button is pressed, a 20 Hz reactor timer starts.
4. Each timer tick reads button states, builds an XYZE delta vector (with E derived from volumetric flow), and calls `toolhead.manual_move()`. Moves chain through the lookahead queue for smooth acceleration.
5. When all continuous buttons are released, the timer stops and `flush_step_generation()` halts motion instantly.
6. Macro buttons fire their configured G-code once per press via `gcode.run_script_from_command()`.

## License

GPLv3 — same as Klipper.
