"""Scheduler engine for CamperMode — timer evaluation and session lifecycle."""

from __future__ import annotations

import logging
import threading
import time as time_module
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from carconnectivity.vehicle import GenericVehicle
from carconnectivity_plugins.campermode.models import PhaseState, save_data

if TYPE_CHECKING:
    from collections.abc import Callable

    from carconnectivity.command_impl import ClimatizationStartStopCommand
    from carconnectivity_plugins.campermode.models import (
        CamperSettings,
        CamperState,
        CamperTimer,
    )

LOG: logging.Logger = logging.getLogger("carconnectivity.plugins.campermode")

# Minimum seconds between any two climatisation commands (rate-limit guard)
_MIN_CMD_INTERVAL: int = 180
# Hard cap on stored timers to prevent resource exhaustion
_MAX_TIMERS: int = 50


class CamperScheduler:
    """Background thread that evaluates timers and manages session
    lifecycle."""

    def __init__(
        self,
        settings: CamperSettings,
        state: CamperState,
        timers: list[CamperTimer],
        save_callback: Callable[[], None] | None = None,
    ) -> None:
        self._settings = settings
        self._state = state
        # _timers is guarded by _lock. External callers that need a snapshot
        # should use Plugin.timers (calls timers_snapshot()). Mutations to
        # individual CamperTimer objects must also be done while holding _lock.
        self._timers = timers
        self._vehicle: GenericVehicle | None = None
        self._battery_level: int | None = None

        # Called outside the lock when a one-time timer fires and
        # must be persisted immediately.
        self._save_callback = save_callback
        # Set by _check_timers when a one-time timer is disabled; consumed
        # and reset by _tick outside the lock.
        self._needs_save = False

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        self._last_command_time: float | None = None
        self._phase_start: float | None = None
        self._session_start: float | None = None
        self._poll_interval_seconds: float = 300.0

        self._heating_start_battery: int | None = None
        self._battery_deltas: list[int] = []
        self._half_cycle: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_vehicle(self, vehicle: GenericVehicle) -> None:
        with self._lock:
            self._vehicle = vehicle

    def set_poll_interval(self, seconds: float) -> None:
        with self._lock:
            self._poll_interval_seconds = seconds

    def update_battery_level(self, level: int) -> None:
        with self._lock:
            self._battery_level = level
            self._state.current_battery_level = level
            if self._state.active and level < self._settings.min_battery_level:
                LOG.warning(
                    "Battery %d%% below minimum %d%%, stopping session",
                    level,
                    self._settings.min_battery_level,
                )
                self._do_stop_session("battery")

    def on_climatization_state_changed(self, state: object) -> None:
        from carconnectivity.climatization import Climatization

        if not isinstance(state, Climatization.ClimatizationState):
            return
        with self._lock:
            if (
                not self._state.active
                or self._state.current_phase != PhaseState.HEATING
                or state != Climatization.ClimatizationState.OFF
            ):
                return
            if self._phase_start is None:
                return
            elapsed = time_module.monotonic() - self._phase_start
            grace = 2 * self._poll_interval_seconds + 10
            if elapsed < grace:
                LOG.debug(
                    "Climatization OFF within grace period (%.0f/%.0f s)",
                    elapsed,
                    grace,
                )
                return
            LOG.warning(
                "Vehicle reports climatization OFF after %.0f s"
                " — stopping session",
                elapsed,
            )
            self._do_stop_session("vehicle_climatization_off")

    def start(self) -> None:
        """Start the scheduler background thread."""
        if self._thread is not None and self._thread.is_alive():
            LOG.warning(
                "Scheduler already running — stopping previous thread first"
            )
            self.stop()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.name = "carconnectivity.plugins.campermode-scheduler"
        self._thread.start()
        LOG.debug("CamperScheduler started")

    def stop(self) -> None:
        """Stop the scheduler background thread."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        LOG.debug("CamperScheduler stopped")

    def update_settings(
        self,
        *,
        min_battery_level: int,
        minutes_between_cycles: int,
        total_duration_minutes: int,
        endless: bool,
    ) -> None:
        """Apply dashboard settings atomically under the scheduler lock."""
        with self._lock:
            self._settings.min_battery_level = min_battery_level
            self._settings.minutes_between_cycles = minutes_between_cycles
            self._settings.total_duration_minutes = total_duration_minutes
            self._settings.endless = endless
            self._settings.clamp()

    def update_climate_settings(
        self,
        *,
        window_heating: bool,
        front_zone_left: bool,
        front_zone_right: bool,
        rear_zone_left: bool,
        rear_zone_right: bool,
        target_temperature: float,
    ) -> None:
        """Apply climate settings atomically under the scheduler lock."""
        with self._lock:
            self._settings.window_heating = window_heating
            self._settings.front_zone_left = front_zone_left
            self._settings.front_zone_right = front_zone_right
            self._settings.rear_zone_left = rear_zone_left
            self._settings.rear_zone_right = rear_zone_right
            self._settings.target_temperature = target_temperature
            self._settings.clamp()

    def add_timer(self, timer: CamperTimer) -> bool:
        """Add a new timer. Returns False if the cap of 50 is reached."""
        with self._lock:
            if len(self._timers) >= _MAX_TIMERS:
                return False
            self._timers.append(timer)
            return True

    def delete_timer(self, timer_id: str) -> bool:
        """Remove a timer by ID. Returns True if found and removed."""
        with self._lock:
            for i, t in enumerate(self._timers):
                if t.id == timer_id:
                    self._timers.pop(i)
                    return True
            return False

    def toggle_timer(self, timer_id: str) -> bool:
        """Toggle a timer's enabled state. Returns True if found."""
        with self._lock:
            for t in self._timers:
                if t.id == timer_id:
                    t.enabled = not t.enabled
                    return True
            return False

    def timers_snapshot(self) -> list[CamperTimer]:
        """Return independent copies of all timers under the lock."""
        with self._lock:
            return [
                replace(t, days_of_week=list(t.days_of_week))
                for t in self._timers
            ]

    def settings_snapshot(self) -> CamperSettings:
        """Return a consistent copy of current settings under the lock."""
        with self._lock:
            return self._settings.snapshot()

    def save(self, path: str) -> None:
        """Persist settings and timers to disk.

        Takes a snapshot under the lock, then writes to disk without holding
        it so that file I/O stalls do not block the scheduler tick.
        """
        with self._lock:
            settings_snap = self._settings.snapshot()
            timers_snap = [
                replace(t, days_of_week=list(t.days_of_week))
                for t in self._timers
            ]
        save_data(path, settings_snap, timers_snap)

    def start_session(self, reason: str = "manual") -> bool:
        """Start a new camper session. Returns True on success."""
        with self._lock:
            return self._do_start_session(reason)

    def stop_session(self, reason: str = "manual") -> None:
        """Stop the active camper session."""
        with self._lock:
            self._do_stop_session(reason)

    # ------------------------------------------------------------------
    # Internal helpers (must be called while holding self._lock)
    # ------------------------------------------------------------------

    def _do_start_session(self, reason: str) -> bool:
        from carconnectivity.command_impl import (
            ClimatizationStartStopCommand,
        )

        if self._state.active:
            LOG.warning("Session already active")
            return False
        if self._vehicle is None:
            LOG.error("No vehicle connected")
            return False

        # Refuse when driving
        vehicle = self._vehicle
        if (
            vehicle.state.enabled
            and vehicle.state.value == GenericVehicle.State.DRIVING
        ):
            LOG.warning("Vehicle is driving — cannot start climatisation")
            return False

        if (
            vehicle.state.enabled
            and vehicle.state.value == GenericVehicle.State.OFFLINE
        ):
            LOG.warning("Vehicle is offline — cannot start climatisation")
            return False

        # Battery check
        if (
            self._battery_level is not None
            and self._battery_level < self._settings.min_battery_level
        ):
            LOG.warning(
                "Battery %d%% below minimum %d%%",
                self._battery_level,
                self._settings.min_battery_level,
            )
            return False

        # Apply zone/temperature settings
        self._apply_climate_settings()

        # Send START command
        if not self._send_command(ClimatizationStartStopCommand.Command.START):
            return False

        now = time_module.monotonic()
        self._session_start = now
        self._phase_start = now
        self._state.active = True
        self._state.current_phase = PhaseState.HEATING
        self._heating_start_battery = self._battery_level
        self._half_cycle = False
        self._state.cycle_number = 1
        self._state.started_at = datetime.now(tz=timezone.utc)
        self._state.stopped_reason = None
        self._state.phase_remaining_seconds = (
            self._settings.cycle_duration_minutes * 60
        )
        if self._settings.endless:
            self._state.total_remaining_seconds = 0
        else:
            self._state.total_remaining_seconds = (
                self._settings.total_duration_minutes * 60
            )

        LOG.info(
            "Camper session started (%s) — cycle 1, phase=heating",
            reason,
        )
        return True

    def _do_stop_session(self, reason: str) -> None:
        from carconnectivity.command_impl import (
            ClimatizationStartStopCommand,
        )

        if not self._state.active:
            return
        LOG.info("Stopping camper session: %s", reason)
        if self._state.current_phase == PhaseState.HEATING:
            self._record_heating_end()
            self._send_command(ClimatizationStartStopCommand.Command.STOP)
        self._state.active = False
        self._state.current_phase = PhaseState.IDLE
        self._state.stopped_reason = reason
        self._session_start = None
        self._phase_start = None
        self._battery_deltas = []
        self._half_cycle = False
        self._state.avg_battery_consumption = None
        self._state.half_cycle_active = False

    def _apply_climate_settings(self) -> None:
        """Push zone/temperature settings to the vehicle (hold lock)."""
        if self._vehicle is None:
            return
        clima_settings = self._vehicle.climatization.settings
        try:
            if clima_settings.window_heating.is_changeable:
                clima_settings.window_heating.value = (
                    self._settings.window_heating
                )
        except Exception as exc:
            LOG.warning("Failed to set window_heating: %s", exc)

        try:
            from carconnectivity_connectors.volkswagen.climatization import (  # type: ignore[import-not-found]
                VolkswagenClimatization,
            )

            if isinstance(
                self._vehicle.climatization, VolkswagenClimatization
            ) and hasattr(clima_settings, "front_zone_left_enabled"):
                if clima_settings.front_zone_left_enabled.is_changeable:  # type: ignore[attr-defined]
                    clima_settings.front_zone_left_enabled.value = (  # type: ignore[attr-defined]
                        self._settings.front_zone_left
                    )
                if clima_settings.front_zone_right_enabled.is_changeable:  # type: ignore[attr-defined]
                    clima_settings.front_zone_right_enabled.value = (  # type: ignore[attr-defined]
                        self._settings.front_zone_right
                    )
                if clima_settings.rear_zone_left_enabled.is_changeable:  # type: ignore[attr-defined]
                    clima_settings.rear_zone_left_enabled.value = (  # type: ignore[attr-defined]
                        self._settings.rear_zone_left
                    )
                if clima_settings.rear_zone_right_enabled.is_changeable:  # type: ignore[attr-defined]
                    clima_settings.rear_zone_right_enabled.value = (  # type: ignore[attr-defined]
                        self._settings.rear_zone_right
                    )
        except ImportError:
            pass
        except Exception as exc:
            LOG.warning("Failed to set VW zone settings: %s", exc)

    def _send_command(
        self,
        command: ClimatizationStartStopCommand.Command,
    ) -> bool:
        """Send a climate start/stop command. Returns True on success.

        Must be called while holding self._lock.
        """
        from carconnectivity.command_impl import (
            ClimatizationStartStopCommand,
        )
        from carconnectivity.units import Temperature

        if self._vehicle is None:
            LOG.warning("No vehicle — cannot send command")
            return False

        # Rate-limit guard applies to START only; STOP must always be allowed
        # so a running heater can be halted even within the throttle window.
        now = time_module.monotonic()
        if (
            command == ClimatizationStartStopCommand.Command.START
            and self._last_command_time is not None
        ):
            elapsed = now - self._last_command_time
            if elapsed < _MIN_CMD_INTERVAL:
                LOG.warning(
                    "Rate limit: last command %ds ago (min %ds)",
                    int(elapsed),
                    _MIN_CMD_INTERVAL,
                )
                return False

        cmd = self._vehicle.climatization.commands.commands.get("start-stop")
        if cmd is None:
            LOG.warning("No start-stop command on vehicle")
            return False

        try:
            if command == ClimatizationStartStopCommand.Command.START:
                cmd.value = {
                    "command": ClimatizationStartStopCommand.Command.START,
                    "target_temperature": (self._settings.target_temperature),
                    "target_temperature_unit": Temperature.C,
                }
            else:
                cmd.value = {
                    "command": ClimatizationStartStopCommand.Command.STOP,
                }
            # Only track START timestamps; STOP commands must not reset the
            # clock so that the next START is not unfairly rate-limited.
            if command == ClimatizationStartStopCommand.Command.START:
                self._last_command_time = now
            LOG.info("Sent climate command: %s", command)
            return True
        except Exception as exc:
            LOG.error("Failed to send climate command %s: %s", command, exc)
            return False

    def _avg_consumption(self) -> float | None:
        """Return mean battery drop per heating cycle, or None if no data."""
        if not self._battery_deltas:
            return None
        return sum(self._battery_deltas) / len(self._battery_deltas)

    def _record_heating_end(self) -> None:
        """Record battery drop for the just-finished heating phase."""
        if (
            self._heating_start_battery is not None
            and self._battery_level is not None
        ):
            delta = self._heating_start_battery - self._battery_level
            if delta >= 0:
                self._battery_deltas.append(delta)
        self._heating_start_battery = None
        self._state.avg_battery_consumption = self._avg_consumption()

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        tick = 0
        while not self._stop_event.is_set():
            try:
                self._tick(tick)
            except Exception as exc:
                LOG.error("Scheduler tick error: %s", exc, exc_info=True)
            tick += 1
            self._stop_event.wait(10)

    def _tick(self, tick: int) -> None:
        """Called every 10 s. Manages active session or evaluates timers."""
        start_timer = False
        with self._lock:
            if self._state.active:
                self._manage_session()
            elif tick % 3 == 0:
                # Evaluate timers every ~30 s
                start_timer = self._check_timers()
            needs_save = self._needs_save
            self._needs_save = False

        if needs_save and self._save_callback is not None:
            self._save_callback()
        if start_timer:
            self.start_session(reason="timer")

    def _check_timers(self) -> bool:
        """Return True if a timer fired and a session should start."""
        # Use local timezone-aware time for explicit wall-clock scheduling.
        now = datetime.now().astimezone()
        current_time = now.time()
        current_dow = now.weekday()  # 0 = Monday
        prev_dow = (current_dow - 1) % 7

        for timer in self._timers:
            if not timer.enabled:
                continue

            # Prevent repeated firing across multiple evaluation cycles that
            # fall inside the ±60 s match window.
            if timer.last_fired_at is not None:
                elapsed_since_fire = (
                    now - timer.last_fired_at
                ).total_seconds()
                if elapsed_since_fire < 120:
                    continue

            timer_secs = timer.time.hour * 3600 + timer.time.minute * 60
            current_secs = current_time.hour * 3600 + current_time.minute * 60
            diff = abs(current_secs - timer_secs)

            if diff <= 60 and current_dow in timer.days_of_week:
                # Normal case: within ±60 s of timer time on the scheduled day
                pass
            elif diff >= 86340 and prev_dow in timer.days_of_week:
                # Midnight wrap: just past 00:00, timer was set near 23:59 on
                # the previous calendar day
                pass
            else:
                continue

            LOG.info("Timer %s fired", timer.id)
            timer.last_fired_at = now
            if not timer.repeat_weekly:
                timer.enabled = False
                # Signal _tick to persist the disabled state outside the lock
                self._needs_save = True
            return True
        return False

    def _manage_session(self) -> None:
        """Manage phase transitions (must hold lock)."""
        from carconnectivity.command_impl import (
            ClimatizationStartStopCommand,
        )

        now = time_module.monotonic()
        if self._phase_start is None or self._session_start is None:
            return

        phase_elapsed = now - self._phase_start
        session_elapsed = now - self._session_start

        # Update remaining counters
        if self._state.current_phase == PhaseState.HEATING:
            if self._half_cycle:
                phase_total = (self._settings.cycle_duration_minutes * 60) // 2
            else:
                phase_total = self._settings.cycle_duration_minutes * 60
        else:
            phase_total = self._settings.minutes_between_cycles * 60
        self._state.phase_remaining_seconds = max(
            0, int(phase_total - phase_elapsed)
        )
        if not self._settings.endless:
            total_secs = self._settings.total_duration_minutes * 60
            self._state.total_remaining_seconds = max(
                0, int(total_secs - session_elapsed)
            )
            # Session duration limit
            if session_elapsed >= total_secs:
                LOG.info("Session duration reached")
                self._do_stop_session("duration_reached")
                return

        # Stop if vehicle went offline during session
        vehicle = self._vehicle
        if (
            vehicle is not None
            and vehicle.state.enabled
            and vehicle.state.value == GenericVehicle.State.OFFLINE
        ):
            LOG.warning("Vehicle went offline during session, stopping")
            self._do_stop_session("vehicle_offline")
            return

        # Battery safety
        if (
            self._battery_level is not None
            and self._battery_level < self._settings.min_battery_level
        ):
            LOG.warning("Battery too low during session, stopping")
            self._do_stop_session("battery")
            return

        # Phase transitions
        if self._state.current_phase == PhaseState.HEATING:
            if self._half_cycle:
                cycle_secs = (self._settings.cycle_duration_minutes * 60) // 2
            else:
                cycle_secs = self._settings.cycle_duration_minutes * 60
            if phase_elapsed >= cycle_secs:
                if self._settings.minutes_between_cycles == 0:
                    # Continuous — restart cycle immediately
                    self._record_heating_end()
                    LOG.debug(
                        "Continuous mode: starting cycle %d",
                        self._state.cycle_number + 1,
                    )
                    if self._send_command(
                        ClimatizationStartStopCommand.Command.START
                    ):
                        self._state.cycle_number += 1
                        self._phase_start = now
                        self._half_cycle = False
                        self._heating_start_battery = self._battery_level
                        self._state.phase_remaining_seconds = (
                            self._settings.cycle_duration_minutes * 60
                        )
                else:
                    self._record_heating_end()
                    LOG.info(
                        "Cycle %d complete — pausing %d min",
                        self._state.cycle_number,
                        self._settings.minutes_between_cycles,
                    )
                    self._send_command(
                        ClimatizationStartStopCommand.Command.STOP
                    )
                    self._state.current_phase = PhaseState.PAUSED
                    self._phase_start = now
                    pause_secs = self._settings.minutes_between_cycles * 60
                    self._state.phase_remaining_seconds = pause_secs

        elif self._state.current_phase == PhaseState.PAUSED:
            pause_secs = self._settings.minutes_between_cycles * 60
            if phase_elapsed >= pause_secs:
                bat_ok = (
                    self._battery_level is None
                    or self._battery_level >= self._settings.min_battery_level
                )
                if bat_ok:
                    # Predictive battery check
                    predicted = self._avg_consumption()
                    use_half_cycle = False
                    if (
                        predicted is not None
                        and self._battery_level is not None
                        and self._battery_level - predicted
                        < self._settings.min_battery_level
                    ):
                        if (
                            self._battery_level - predicted / 2
                            < self._settings.min_battery_level
                        ):
                            LOG.warning(
                                "Predicted battery drop too high,"
                                " stopping (battery_predicted)"
                            )
                            self._do_stop_session("battery_predicted")
                            return
                        else:
                            use_half_cycle = True
                    LOG.info(
                        "Pause done — starting cycle %d",
                        self._state.cycle_number + 1,
                    )
                    if self._send_command(
                        ClimatizationStartStopCommand.Command.START
                    ):
                        self._state.cycle_number += 1
                        self._state.current_phase = PhaseState.HEATING
                        self._phase_start = now
                        self._half_cycle = use_half_cycle
                        self._heating_start_battery = self._battery_level
                        if use_half_cycle:
                            cycle_secs = (
                                self._settings.cycle_duration_minutes * 60
                            ) // 2
                        else:
                            cycle_secs = (
                                self._settings.cycle_duration_minutes * 60
                            )
                        self._state.phase_remaining_seconds = cycle_secs
                else:
                    LOG.warning("Battery too low after pause, stopping")
                    self._do_stop_session("battery")

        self._state.avg_battery_consumption = self._avg_consumption()
        self._state.half_cycle_active = self._half_cycle
