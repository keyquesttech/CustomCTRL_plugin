# CustomCTRL - Klipper plugin for instant multi-axis jogging,
# continuous extrusion, and macro execution via physical MCU buttons.
#
# Copyright (C) 2026
# This file may be distributed under the terms of the GNU GPLv3 license.

import math, logging

LOOP_INTERVAL = 0.050  # 20 Hz

class CustomCTRL:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')

        # ----- config: jog parameters -----
        self.jog_speed = config.getfloat('jog_speed', 10., above=0.)

        # ----- config: extrusion via volumetric flow -----
        self.filament_diameter = config.getfloat(
            'filament_diameter', 1.75, above=0.)
        self.volumetric_flow = config.getfloat(
            'volumetric_flow', 1., above=0.)
        filament_area = math.pi * (self.filament_diameter / 2.) ** 2
        self.extrude_rate = self.volumetric_flow / filament_area

        # ----- config: terminal output -----
        self.verbose = config.getboolean('verbose', False)

        # ----- config: jog pins (optional per axis) -----
        self.jog_pins = {}
        for axis in ('x', 'y', 'z'):
            pin_name = '%s_jog_pin' % axis
            pin = config.get(pin_name, None)
            if pin is not None:
                self.jog_pins[axis] = pin

        # ----- config: extrude pin (hold-to-extrude) -----
        self.extrude_pin = config.get('extrude_pin', None)

        # ----- config: macro pins (press-to-fire) -----
        self.macro_pins = {}
        for i in range(1, 9):
            pin_key = 'macro_%d_pin' % i
            gcode_key = 'macro_%d_gcode' % i
            pin = config.get(pin_key, None)
            gcode_line = config.get(gcode_key, None)
            if pin is not None and gcode_line is not None:
                self.macro_pins['macro_%d' % i] = {
                    'pin': pin,
                    'gcode': gcode_line,
                }

        # ----- runtime state -----
        self.toolhead = None
        self.virtual_sdcard = None
        self.button_states = {}
        self.jog_timer = None
        self.is_ready = False
        self._last_block_reason = None

        # Classify which button names are "continuous" (jog / extrude)
        self._continuous_names = set()
        for axis in self.jog_pins:
            self._continuous_names.add(axis)
        if self.extrude_pin is not None:
            self._continuous_names.add('extrude')

        # Pin registration must happen at config time (before MCU start),
        # so register buttons now — callbacks only fire after MCU is live.
        self._register_buttons(config)

        # Resolve toolhead and virtual_sdcard once the printer is ready
        self.printer.register_event_handler('klippy:ready', self._handle_ready)

    # ------------------------------------------------------------------
    # Terminal output helpers
    # ------------------------------------------------------------------
    def _log_info(self, msg):
        logging.info("customctrl: %s", msg)
        if self.verbose:
            self.gcode.respond_info("CustomCTRL: %s" % msg)

    def _log_error(self, msg):
        logging.warning("customctrl: %s", msg)
        self.gcode.respond_info("CustomCTRL ERROR: %s" % msg)

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------
    def _handle_ready(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        try:
            self.virtual_sdcard = self.printer.lookup_object('virtual_sdcard')
        except Exception:
            self.virtual_sdcard = None
        self.is_ready = True
        self._log_info("ready (flow=%.1f mm3/s, filament=%.2f mm, "
                       "E rate=%.3f mm/s, jog=%.1f mm/s)"
                       % (self.volumetric_flow, self.filament_diameter,
                          self.extrude_rate, self.jog_speed))

    def _register_buttons(self, config):
        buttons = self.printer.load_object(config, 'buttons')

        for axis, pin in self.jog_pins.items():
            name = axis
            self.button_states[name] = False
            buttons.register_buttons(
                [pin],
                (lambda et, s, n=name: self._on_button(et, s, n))
            )

        if self.extrude_pin is not None:
            self.button_states['extrude'] = False
            buttons.register_buttons(
                [self.extrude_pin],
                (lambda et, s: self._on_button(et, s, 'extrude'))
            )

        for mname, minfo in self.macro_pins.items():
            self.button_states[mname] = False
            buttons.register_buttons(
                [minfo['pin']],
                (lambda et, s, n=mname: self._on_button(et, s, n))
            )

    # ------------------------------------------------------------------
    # Button callback (runs in reactor greenlet via register_async_callback)
    # ------------------------------------------------------------------
    def _on_button(self, eventtime, state, pin_name):
        pressed = (state == 1)
        prev = self.button_states.get(pin_name, False)
        self.button_states[pin_name] = pressed

        if pressed and not prev:
            self._log_info("%s pressed" % pin_name)
        elif not pressed and prev:
            self._log_info("%s released" % pin_name)

        if not self.is_ready:
            return

        # Macro pins: fire on press only
        if pin_name in self.macro_pins and pressed and not prev:
            self._fire_macro(pin_name)
            return

        # Continuous (jog / extrude) pins: manage game loop
        if pin_name in self._continuous_names:
            if pressed and not prev:
                self._ensure_jog_loop_running(eventtime)

    # ------------------------------------------------------------------
    # Macro execution
    # ------------------------------------------------------------------
    def _fire_macro(self, pin_name):
        reason = self._check_safe()
        if reason is not None:
            self._log_error("macro blocked: %s" % reason)
            return
        gcode_line = self.macro_pins[pin_name]['gcode']
        self._log_info("firing macro: %s" % gcode_line)
        try:
            self.gcode.run_script_from_command(gcode_line)
        except Exception as e:
            self._log_error("macro '%s' failed: %s" % (gcode_line, e))

    # ------------------------------------------------------------------
    # Game loop — smooth motion
    # ------------------------------------------------------------------
    def _ensure_jog_loop_running(self, eventtime):
        if self.jog_timer is not None:
            return
        self._log_info("jog loop started")
        waketime = self.reactor.monotonic() + LOOP_INTERVAL
        self.jog_timer = self.reactor.register_timer(
            self._jog_tick, waketime)

    def _jog_tick(self, eventtime):
        if not self._any_continuous_held():
            self._stop_jog_loop()
            return self.reactor.NEVER

        reason = self._check_safe()
        if reason is not None:
            if reason != self._last_block_reason:
                self._log_error("jog blocked: %s" % reason)
                self._last_block_reason = reason
            return eventtime + LOOP_INTERVAL
        if self._last_block_reason is not None:
            self._log_info("jog resumed — condition cleared")
            self._last_block_reason = None

        # Derive per-tick distances from speed so motion is consistent
        jog_step = self.jog_speed * LOOP_INTERVAL
        extrude_step = self.extrude_rate * LOOP_INTERVAL

        dx = dy = dz = de = 0.
        if self.button_states.get('x', False):
            dx = jog_step
        if self.button_states.get('y', False):
            dy = jog_step
        if self.button_states.get('z', False):
            dz = jog_step
        if self.button_states.get('extrude', False):
            de = extrude_step

        if dx == 0. and dy == 0. and dz == 0. and de == 0.:
            return eventtime + LOOP_INTERVAL

        try:
            cur = self.toolhead.get_position()
            new_pos = [cur[0] + dx, cur[1] + dy, cur[2] + dz, cur[3] + de]
            new_pos = self._clamp_to_limits(new_pos)
            if (new_pos[0] == cur[0] and new_pos[1] == cur[1]
                    and new_pos[2] == cur[2] and new_pos[3] == cur[3]):
                return eventtime + LOOP_INTERVAL
            speed = self.jog_speed
            if dx == 0. and dy == 0. and dz == 0.:
                speed = self.extrude_rate
            max_vel = self.toolhead.get_max_velocity()[0]
            speed = min(speed, max_vel)
            self.toolhead.manual_move(new_pos, speed)
            self.toolhead.flush_step_generation()
        except Exception as e:
            self._log_error("jog tick failed: %s" % e)

        return eventtime + LOOP_INTERVAL

    def _stop_jog_loop(self):
        if self.jog_timer is not None:
            self.reactor.unregister_timer(self.jog_timer)
            self.jog_timer = None
        self._last_block_reason = None
        try:
            if self.toolhead is not None:
                self.toolhead.flush_step_generation()
        except Exception as e:
            self._log_error("flush on stop failed: %s" % e)
        self._log_info("jog loop stopped")

    def _any_continuous_held(self):
        for name in self._continuous_names:
            if self.button_states.get(name, False):
                return True
        return False

    # ------------------------------------------------------------------
    # Machine limits
    # ------------------------------------------------------------------
    def _clamp_to_limits(self, pos):
        kin = self.toolhead.get_kinematics()
        eventtime = self.reactor.monotonic()
        kin_status = kin.get_status(eventtime)
        axes_min = kin_status.get('axes_min', (-9999., -9999., -9999.))
        axes_max = kin_status.get('axes_max', (9999., 9999., 9999.))
        clamped = list(pos)
        for i in range(3):
            lo = axes_min[i] if i < len(axes_min) else -9999.
            hi = axes_max[i] if i < len(axes_max) else 9999.
            if clamped[i] < lo:
                clamped[i] = lo
            elif clamped[i] > hi:
                clamped[i] = hi
        return clamped

    # ------------------------------------------------------------------
    # Safety checks — returns None if safe, or a reason string if blocked
    # ------------------------------------------------------------------
    def _check_safe(self):
        if self.toolhead is None:
            return "toolhead not available (printer not ready)"
        if self.virtual_sdcard is not None and self.virtual_sdcard.is_active():
            return "SD card print is active"
        kin = self.toolhead.get_kinematics()
        eventtime = self.reactor.monotonic()
        kin_status = kin.get_status(eventtime)
        homed = kin_status.get('homed_axes', '')
        unhomed = [a.upper() for a in ('x', 'y', 'z')
                   if self.button_states.get(a, False) and a not in homed]
        if unhomed:
            return "%s not homed" % ', '.join(unhomed)
        return None


def load_config(config):
    return CustomCTRL(config)
