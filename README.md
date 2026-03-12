# CustomCTRL — Klipper Plugin

A Klipper "Extra" that reads physical buttons wired to the MCU and provides:

- **Instant multi-axis jogging** — hold a button to jog X, Y, or Z continuously at 20 Hz. Multiple axes can be held simultaneously for diagonal movement.
- **Hold-to-extrude** — hold the extrude button for continuous filament extrusion, combinable with jog buttons.
- **Macro buttons** — press once to fire any G-code macro.
- **Stop on release** — motion and extrusion halt the moment all buttons are released.

Motion is injected via `toolhead.manual_move()` + `flush_step_generation()`, bypassing the G-code queue for immediate execution.

## Requirements

- Klipper installed at `~/klipper` on a Raspberry Pi (or similar).
- Physical buttons wired to available MCU pins (e.g. SKR Mini E3 V3.0).
- Python 3.x (Klipper's environment).

## Install

```bash
cd ~/klipper-customctrl   # or wherever you cloned this repo
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
| `jog_speed` | `10` | Jog speed in mm/s |
| `jog_increment` | `0.5` | Distance per tick in mm (~20 ticks/s) |

### Extrude button (hold to extrude)

| Option | Default | Description |
|---|---|---|
| `extrude_pin` | *(none)* | MCU pin for extrude button |
| `extrude_speed` | `2` | Extrusion speed in mm/s |
| `extrude_increment` | `0.2` | Filament length per tick in mm |

### Macro buttons (press to fire)

Up to 8 macro buttons. Each requires both a pin and a G-code line:

| Option | Description |
|---|---|
| `macro_N_pin` | MCU pin for macro button N (1-8) |
| `macro_N_gcode` | G-code command to run on press |

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

## How it works

1. On `klippy:ready`, CustomCTRL registers each configured pin with Klipper's `buttons` module.
2. Button press/release events update an internal state dictionary.
3. When any jog or extrude button is pressed, a 20 Hz reactor timer starts.
4. Each timer tick reads button states, builds an XYZE delta vector, and calls `toolhead.manual_move()` followed by `flush_step_generation()` for immediate execution.
5. When all continuous buttons are released, the timer stops and a final flush halts motion.
6. Macro buttons fire their configured G-code once per press via `gcode.run_script_from_command()`.

## License

GPLv3 — same as Klipper.
