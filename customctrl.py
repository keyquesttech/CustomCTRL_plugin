# CustomCTRL - Klipper plugin for instant multi-axis jogging,
# continuous extrusion, and macro execution via physical MCU buttons.
#
# Copyright (C) 2026
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging

LOOP_INTERVAL = 0.050  # 20 Hz

class CustomCTRL:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')

        # ----- config: jog parameters -----
        self.jog_speed = config.getfloat('jog_speed', 10., above=0.)
        self.jog_increment = config.getfloat('jog_increment', 0.5, above=0.)
        self.extrude_speed = config.getfloat('extrude_speed', 2., above=0.)
        self.extrude_increment = config.getfloat('extrude_increment', 0.2,
                                                  above=0.)

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
    # Startup
    # ------------------------------------------------------------------
    def _handle_ready(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        try:
            self.virtual_sdcard = self.printer.lookup_object('virtual_sdcard')
        except Exception:
            self.virtual_sdcard = None

        self.is_ready = True

    def _register_buttons(self, config):
        buttons = self.printer.load_object(config, 'buttons')

        for axis, pin in self.jog_pins.items():
            name = axis  # 'x', 'y', or 'z'
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
    # Macro execution (Phase 4)
    # ------------------------------------------------------------------
    def _fire_macro(self, pin_name):
        if not self._check_safe():
            return
        gcode_line = self.macro_pins[pin_name]['gcode']
        try:
            self.gcode.run_script_from_command(gcode_line)
        except Exception:
            logging.exception("customctrl: error running macro for %s",
                              pin_name)

    # ------------------------------------------------------------------
    # Game loop (Phase 5)
    # ------------------------------------------------------------------
    def _ensure_jog_loop_running(self, eventtime):
        if self.jog_timer is not None:
            return
        waketime = self.reactor.monotonic() + LOOP_INTERVAL
        self.jog_timer = self.reactor.register_timer(
            self._jog_tick, waketime)

    def _jog_tick(self, eventtime):
        # If no continuous button is held, stop the loop
        if not self._any_continuous_held():
            self._stop_jog_loop()
            return self.reactor.NEVER

        if not self._check_safe():
            return eventtime + LOOP_INTERVAL

        # Build delta vector
        dx = dy = dz = de = 0.
        if self.button_states.get('x', False):
            dx = self.jog_increment
        if self.button_states.get('y', False):
            dy = self.jog_increment
        if self.button_states.get('z', False):
            dz = self.jog_increment
        if self.button_states.get('extrude', False):
            de = self.extrude_increment

        if dx == 0. and dy == 0. and dz == 0. and de == 0.:
            return eventtime + LOOP_INTERVAL

        try:
            cur = self.toolhead.get_position()
            new_pos = [cur[0] + dx, cur[1] + dy, cur[2] + dz, cur[3] + de]
            speed = self.jog_speed
            if dx == 0. and dy == 0. and dz == 0.:
                speed = self.extrude_speed
            self.toolhead.manual_move(new_pos, speed)
            self.toolhead.flush_step_generation()
        except Exception:
            logging.exception("customctrl: error during jog tick")

        return eventtime + LOOP_INTERVAL

    def _stop_jog_loop(self):
        if self.jog_timer is not None:
            self.reactor.unregister_timer(self.jog_timer)
            self.jog_timer = None
        try:
            if self.toolhead is not None:
                self.toolhead.flush_step_generation()
        except Exception:
            logging.exception("customctrl: error flushing on stop")

    def _any_continuous_held(self):
        for name in self._continuous_names:
            if self.button_states.get(name, False):
                return True
        return False

    # ------------------------------------------------------------------
    # Safety checks
    # ------------------------------------------------------------------
    def _check_safe(self):
        if self.toolhead is None:
            return False

        # Block during an active SD card print
        if self.virtual_sdcard is not None and self.virtual_sdcard.is_active():
            return False

        # Verify required axes are homed
        kin = self.toolhead.get_kinematics()
        eventtime = self.reactor.monotonic()
        kin_status = kin.get_status(eventtime)
        homed = kin_status.get('homed_axes', '')

        for axis in ('x', 'y', 'z'):
            if self.button_states.get(axis, False) and axis not in homed:
                return False

        return True


def load_config(config):
    return CustomCTRL(config)
