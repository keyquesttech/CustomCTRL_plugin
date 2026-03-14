# CustomCTRL - Klipper plugin for instant multi-axis jogging,
# continuous extrusion, and macro execution via physical MCU buttons.
#
# Copyright (C) 2026
# This file may be distributed under the terms of the GNU GPLv3 license.

import math, logging

LOOP_INTERVAL = 0.025  # 40 Hz (shorter ticks = faster stop response)
BATCH_TICKS = 2       # Moves issued every BATCH_TICKS ticks (longer moves = reach full speed)

class CustomCTRL:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')

        # ----- config: per-axis jog speed and increment -----
        default_speed = config.getfloat('jog_speed', 10., above=0.)
        self.jog_speed = {}
        self.jog_increment = {}
        for axis in ('x', 'y', 'z'):
            self.jog_speed[axis] = config.getfloat(
                '%s_jog_speed' % axis, default_speed, above=0.)
            self.jog_increment[axis] = config.getfloat(
                '%s_jog_increment' % axis, 0., minval=0.)

        # ----- config: manual extrusion / retraction values -----
        # Distances are in mm of filament per tick; speeds are in mm/s.
        self.extrude_speed = config.getfloat('extrude_speed', 5., above=0.)
        self.retract_speed = config.getfloat(
            'retract_speed', self.extrude_speed, above=0.)
        self.extrude_increment = config.getfloat(
            'extrude_increment', 0.25, above=0.)
        self.retract_increment = config.getfloat(
            'retract_increment', self.extrude_increment, above=0.)

        # ----- config: terminal output -----
        self.verbose = config.getboolean('verbose', False)

        # ----- config: directional jog pins (positive / negative) -----
        self.jog_pins = {}
        for axis in ('x', 'y', 'z'):
            for direction in ('pos', 'neg'):
                key = '%s_%s_pin' % (axis, direction)
                pin = config.get(key, None)
                if pin is not None:
                    self.jog_pins['%s_%s' % (axis, direction)] = pin

        # ----- config: extrude / retract pins -----
        self.extrude_pin = config.get('extrude_pin', None)
        self.retract_pin = config.get('retract_pin', None)

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
        # Batching: accumulate deltas over BATCH_TICKS so each move is longer (toolhead reaches full speed)
        self._acc_dx = self._acc_dy = self._acc_dz = self._acc_de = 0.
        self._acc_ticks = 0
        self._first_tick_done = False

        # Classify which button names are "continuous" (jog / extrude)
        self._continuous_names = set(self.jog_pins.keys())
        if self.extrude_pin is not None:
            self._continuous_names.add('extrude')
        if self.retract_pin is not None:
            self._continuous_names.add('retract')

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
        axis_info = ', '.join(
            '%s=%.1fmm/s' % (a.upper(), self.jog_speed[a])
            + (' (%.2fmm/tick)' % self.jog_increment[a]
               if self.jog_increment[a] > 0. else '')
            for a in ('x', 'y', 'z'))
        self._log_info(
            "ready (%s, extrude=%.1fmm/s@%.3fmm/tick, retract=%.1fmm/s@%.3fmm/tick)"
            % (axis_info,
               self.extrude_speed, self.extrude_increment,
               self.retract_speed, self.retract_increment)
        )

    def _register_buttons(self, config):
        buttons = self.printer.load_object(config, 'buttons')

        for name, pin in self.jog_pins.items():
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

        if self.retract_pin is not None:
            self.button_states['retract'] = False
            buttons.register_buttons(
                [self.retract_pin],
                (lambda et, s: self._on_button(et, s, 'retract'))
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
            elif not pressed and prev and not self._any_continuous_held():
                # All continuous buttons released — stop and flush immediately (no wait for next tick)
                self._stop_jog_loop()

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
    # Game loop
    # ------------------------------------------------------------------
    def _ensure_jog_loop_running(self, eventtime):
        if self.jog_timer is not None:
            return
        self._log_info("jog loop started")
        self._acc_dx = self._acc_dy = self._acc_dz = self._acc_de = 0.
        self._acc_ticks = 0
        self._first_tick_done = False
        # First tick runs synchronously so motion starts immediately (no wait for timer).
        # Timer is scheduled one interval from now so we don't double-fire.
        waketime = eventtime + LOOP_INTERVAL
        self.jog_timer = self.reactor.register_timer(
            self._jog_tick, waketime)
        # Run one tick now so there's no delay from press to first move
        self._jog_tick(eventtime)

    def _axis_delta(self, axis):
        """Return signed step for an axis based on pos/neg button states."""
        pos = self.button_states.get('%s_pos' % axis, False)
        neg = self.button_states.get('%s_neg' % axis, False)
        if pos == neg:
            return 0.
        inc = self.jog_increment[axis]
        step = inc if inc > 0. else self.jog_speed[axis] * LOOP_INTERVAL
        return step if pos else -step

    def _do_move(self, dx, dy, dz, de, e_speed):
        """Queue one manual move with given deltas; flush_step_generation is called inside."""
        cur = self.toolhead.get_position()
        new_pos = [cur[0] + dx, cur[1] + dy, cur[2] + dz, cur[3] + de]
        new_pos = self._clamp_to_limits(new_pos)
        dx = new_pos[0] - cur[0]
        dy = new_pos[1] - cur[1]
        dz = new_pos[2] - cur[2]
        de = new_pos[3] - cur[3]
        if dx == 0. and dy == 0. and dz == 0. and de == 0.:
            return
        if de != 0. and e_speed <= 0.:
            e_speed = self.extrude_speed if de > 0. else self.retract_speed
        L_xyz = math.sqrt(dx*dx + dy*dy + dz*dz)
        t_max = 0.
        if dx != 0.:
            t_max = max(t_max, abs(dx) / self.jog_speed['x'])
        if dy != 0.:
            t_max = max(t_max, abs(dy) / self.jog_speed['y'])
        if dz != 0.:
            t_max = max(t_max, abs(dz) / self.jog_speed['z'])
        if de != 0.:
            t_max = max(t_max, abs(de) / e_speed)
        if L_xyz > 0. and t_max > 0.:
            speed = L_xyz / t_max
        elif de != 0.:
            speed = e_speed
        else:
            speed = max(
                self.jog_speed['x'] if dx != 0. else 0.,
                self.jog_speed['y'] if dy != 0. else 0.,
                self.jog_speed['z'] if dz != 0. else 0.,
            ) or self.jog_speed['x']
        max_vel = self.toolhead.get_max_velocity()[0]
        speed = min(speed, max_vel)
        self.toolhead.manual_move(new_pos, speed)
        self.toolhead.flush_step_generation()

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

        dx = self._axis_delta('x')
        dy = self._axis_delta('y')
        dz = self._axis_delta('z')

        # Extrude / retract (manual values)
        de = 0.
        e_speed = 0.
        e_fwd = self.button_states.get('extrude', False)
        e_rev = self.button_states.get('retract', False)
        if e_fwd != e_rev:
            if e_fwd:
                de = self.extrude_increment
                e_speed = self.extrude_speed
            else:
                de = -self.retract_increment
                e_speed = self.retract_speed

        if dx == 0. and dy == 0. and dz == 0. and de == 0.:
            self._acc_dx = self._acc_dy = self._acc_dz = self._acc_de = 0.
            self._acc_ticks = 0
            return eventtime + LOOP_INTERVAL

        try:
            # First tick after press: issue one move immediately for instant response
            if not self._first_tick_done:
                self._do_move(dx, dy, dz, de, e_speed)
                self._first_tick_done = True
                return eventtime + LOOP_INTERVAL
            # Otherwise accumulate; issue one longer move every BATCH_TICKS
            self._acc_dx += dx
            self._acc_dy += dy
            self._acc_dz += dz
            self._acc_de += de
            self._acc_ticks += 1
            if self._acc_ticks >= BATCH_TICKS:
                self._do_move(
                    self._acc_dx, self._acc_dy, self._acc_dz, self._acc_de,
                    e_speed,
                )
                self._acc_dx = self._acc_dy = self._acc_dz = self._acc_de = 0.
                self._acc_ticks = 0
        except Exception as e:
            self._log_error("jog tick failed: %s" % e)

        return eventtime + LOOP_INTERVAL

    def _stop_jog_loop(self):
        if self.jog_timer is not None:
            self.reactor.unregister_timer(self.jog_timer)
            self.jog_timer = None
        self._last_block_reason = None
        self._acc_dx = self._acc_dy = self._acc_dz = self._acc_de = 0.
        self._acc_ticks = 0
        self._first_tick_done = False
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
        unhomed = []
        for axis in ('x', 'y', 'z'):
            if axis in homed:
                continue
            if (self.button_states.get('%s_pos' % axis, False)
                    or self.button_states.get('%s_neg' % axis, False)):
                unhomed.append(axis.upper())
        if unhomed:
            return "%s not homed" % ', '.join(unhomed)
        return None


def load_config(config):
    return CustomCTRL(config)
