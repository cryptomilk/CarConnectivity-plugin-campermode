"""Unit tests for the CamperMode scheduler engine."""

from __future__ import annotations

import threading
from datetime import datetime
from datetime import time as dtime
from unittest.mock import MagicMock, patch

import pytest

from carconnectivity.vehicle import GenericVehicle
from carconnectivity_plugins.campermode.models import (
    CamperSettings,
    CamperState,
    CamperTimer,
)
from carconnectivity_plugins.campermode.scheduler import CamperScheduler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_settings(**overrides) -> CamperSettings:
    defaults = dict(
        min_battery_level=20,
        minutes_between_cycles=10,
        cycle_duration_minutes=1,
        total_duration_minutes=5,
        endless=False,
        window_heating=True,
        front_zone_left=True,
        front_zone_right=True,
        rear_zone_left=False,
        rear_zone_right=False,
        target_temperature=22.0,
    )
    defaults.update(overrides)
    return CamperSettings(**defaults)


def make_vehicle(
    state: GenericVehicle.State = GenericVehicle.State.PARKED,
) -> MagicMock:
    mock_cmd = MagicMock()
    vehicle = MagicMock()
    vehicle.state.value = state
    vehicle.state.enabled = True
    vehicle.climatization.commands.commands = {"start-stop": mock_cmd}
    vehicle.climatization.settings.window_heating.is_changeable = True
    return vehicle


def make_scheduler(
    settings: CamperSettings | None = None,
    vehicle: MagicMock | None = None,
    timers: list[CamperTimer] | None = None,
    battery: int | None = None,
) -> CamperScheduler:
    s = CamperScheduler(
        settings=settings or make_settings(),
        state=CamperState(),
        timers=timers or [],
    )
    if vehicle is not None:
        s._vehicle = vehicle
    if battery is not None:
        s._battery_level = battery
    return s


def make_timer(
    timer_time: dtime = dtime(22, 30),
    days: list[int] | None = None,
    repeat: bool = True,
    enabled: bool = True,
) -> CamperTimer:
    return CamperTimer(
        id="t1",
        time=timer_time,
        days_of_week=days if days is not None else list(range(7)),
        repeat_weekly=repeat,
        enabled=enabled,
        created_at=datetime(2026, 1, 1),
    )


# ---------------------------------------------------------------------------
# Session start/stop basics
# ---------------------------------------------------------------------------


def test_start_no_vehicle_returns_false():
    sched = make_scheduler()
    assert sched.start_session() is False


def test_start_success_sets_state():
    vehicle = make_vehicle()
    sched = make_scheduler(vehicle=vehicle)
    result = sched.start_session()
    assert result is True
    assert sched._state.active is True
    assert sched._state.current_phase == "heating"
    assert sched._state.cycle_number == 1


def test_start_when_already_active_returns_false():
    vehicle = make_vehicle()
    sched = make_scheduler(vehicle=vehicle)
    sched.start_session()
    assert sched.start_session() is False


def test_start_when_driving_returns_false():
    vehicle = make_vehicle(state=GenericVehicle.State.DRIVING)
    sched = make_scheduler(vehicle=vehicle)
    assert sched.start_session() is False


def test_start_low_battery_returns_false():
    vehicle = make_vehicle()
    sched = make_scheduler(vehicle=vehicle, battery=15)
    assert sched.start_session() is False


def test_stop_when_active_sends_stop_and_sets_idle():
    from carconnectivity.command_impl import ClimatizationStartStopCommand

    vehicle = make_vehicle()
    sched = make_scheduler(vehicle=vehicle)
    with patch(
        "carconnectivity_plugins.campermode.scheduler.time_module.monotonic"
    ) as mock_mono:
        mock_mono.return_value = 0.0
        sched.start_session()
        assert sched._state.active is True
        mock_mono.return_value = 200.0  # past 180 s rate limit
        sched.stop_session(reason="manual")
    assert sched._state.active is False
    assert sched._state.current_phase == "idle"
    cmd = vehicle.climatization.commands.commands["start-stop"]
    assert cmd.value["command"] == ClimatizationStartStopCommand.Command.STOP


def test_stop_when_inactive_is_noop():
    sched = make_scheduler()
    sched.stop_session(reason="manual")  # must not raise
    assert sched._state.active is False


def test_stopped_reason_recorded():
    vehicle = make_vehicle()
    sched = make_scheduler(vehicle=vehicle)
    sched.start_session()
    sched.stop_session(reason="shutdown")
    assert sched._state.stopped_reason == "shutdown"


# ---------------------------------------------------------------------------
# Command sending & rate limiting
# ---------------------------------------------------------------------------


def test_start_command_dict_keys():
    from carconnectivity.command_impl import ClimatizationStartStopCommand
    from carconnectivity.units import Temperature

    vehicle = make_vehicle()
    sched = make_scheduler(vehicle=vehicle)
    sched.start_session()
    cmd = vehicle.climatization.commands.commands["start-stop"]
    sent = cmd.value
    assert sent["command"] == ClimatizationStartStopCommand.Command.START
    assert "target_temperature" in sent
    assert "target_temperature_unit" in sent
    assert sent["target_temperature_unit"] == Temperature.C


def test_stop_command_dict_has_only_command_key():
    from carconnectivity.command_impl import ClimatizationStartStopCommand

    vehicle = make_vehicle()
    sched = make_scheduler(vehicle=vehicle)
    with patch(
        "carconnectivity_plugins.campermode.scheduler.time_module.monotonic"
    ) as mock_mono:
        mock_mono.return_value = 0.0
        sched.start_session()
        mock_mono.return_value = 200.0
        sched.stop_session()
    cmd = vehicle.climatization.commands.commands["start-stop"]
    sent = cmd.value
    assert set(sent.keys()) == {"command"}
    assert sent["command"] == ClimatizationStartStopCommand.Command.STOP


def test_rate_limit_blocks_second_command():
    vehicle = make_vehicle()
    sched = make_scheduler(vehicle=vehicle)

    with patch(
        "carconnectivity_plugins.campermode.scheduler.time_module.monotonic"
    ) as mock_mono:
        mock_mono.return_value = 1000.0
        sched.start_session()
        # Reset state to inactive so we can try another start
        sched._state.active = False
        mock_mono.return_value = 1060.0  # only 60 s later
        result = sched._do_start_session("manual")
    assert result is False


def test_stop_bypasses_rate_limit():
    """STOP must be sent even when the rate limit window is still active."""
    from carconnectivity.command_impl import ClimatizationStartStopCommand

    vehicle = make_vehicle()
    sched = make_scheduler(vehicle=vehicle)

    with patch(
        "carconnectivity_plugins.campermode.scheduler.time_module.monotonic"
    ) as mock_mono:
        mock_mono.return_value = 1000.0
        sched.start_session()
        assert sched._state.active is True
        # Only 5 s later — well within the 180 s rate-limit window
        mock_mono.return_value = 1005.0
        sched.stop_session(reason="manual")

    assert sched._state.active is False
    cmd = vehicle.climatization.commands.commands["start-stop"]
    assert cmd.value["command"] == ClimatizationStartStopCommand.Command.STOP


def test_rate_limit_allows_command_after_interval():
    vehicle = make_vehicle()
    sched = make_scheduler(vehicle=vehicle)

    with patch(
        "carconnectivity_plugins.campermode.scheduler.time_module.monotonic"
    ) as mock_mono:
        mock_mono.return_value = 1000.0
        sched.start_session()
        sched._state.active = False
        mock_mono.return_value = 1000.0 + 181.0  # > 180 s
        result = sched._do_start_session("manual")
    assert result is True


def test_no_start_stop_command_returns_false():
    vehicle = make_vehicle()
    vehicle.climatization.commands.commands = {}  # no start-stop key
    sched = make_scheduler(vehicle=vehicle)
    assert sched.start_session() is False


# ---------------------------------------------------------------------------
# Battery monitoring
# ---------------------------------------------------------------------------


def test_update_battery_level_syncs_state():
    sched = make_scheduler()
    sched.update_battery_level(55)
    assert sched._state.current_battery_level == 55


def test_battery_drop_below_threshold_stops_active_session():
    vehicle = make_vehicle()
    settings = make_settings(min_battery_level=20)
    sched = make_scheduler(vehicle=vehicle, settings=settings)
    sched.start_session()
    assert sched._state.active is True
    sched.update_battery_level(15)  # below min_battery_level=20
    assert sched._state.active is False
    assert sched._state.stopped_reason == "battery"


def test_battery_drop_when_inactive_no_crash():
    sched = make_scheduler()
    sched.update_battery_level(10)  # must not raise
    assert sched._state.active is False


# ---------------------------------------------------------------------------
# Timer evaluation
# ---------------------------------------------------------------------------


def _make_datetime(weekday: int, hour: int, minute: int) -> datetime:
    """Return a datetime for the given weekday/time (2026-03-16 = Monday)."""
    # 2026-03-16 is a Monday (weekday=0), so offset by weekday days
    from datetime import timedelta

    base = datetime(2026, 3, 16, hour, minute, 0)  # Monday
    return base + timedelta(days=weekday)


def test_timer_fires_at_matching_time_and_day():
    timer = make_timer(timer_time=dtime(22, 30), days=[0])  # Monday
    sched = make_scheduler(timers=[timer])
    now = _make_datetime(weekday=0, hour=22, minute=30)
    with patch(
        "carconnectivity_plugins.campermode.scheduler.datetime"
    ) as mock_dt:
        mock_dt.now.return_value = now
        result = sched._check_timers()
    assert result is True


def test_timer_wrong_day_does_not_fire():
    timer = make_timer(timer_time=dtime(22, 30), days=[1])  # Tuesday only
    sched = make_scheduler(timers=[timer])
    now = _make_datetime(weekday=0, hour=22, minute=30)  # Monday
    with patch(
        "carconnectivity_plugins.campermode.scheduler.datetime"
    ) as mock_dt:
        mock_dt.now.return_value = now
        result = sched._check_timers()
    assert result is False


def test_disabled_timer_does_not_fire():
    timer = make_timer(timer_time=dtime(22, 30), days=[0], enabled=False)
    sched = make_scheduler(timers=[timer])
    now = _make_datetime(weekday=0, hour=22, minute=30)
    with patch(
        "carconnectivity_plugins.campermode.scheduler.datetime"
    ) as mock_dt:
        mock_dt.now.return_value = now
        result = sched._check_timers()
    assert result is False


def test_midnight_wrap_fires_correctly():
    """Timer at 23:59 on Monday (day=0) fires at Tuesday 00:00 (day=1)."""
    timer = make_timer(timer_time=dtime(23, 59), days=[0])  # Monday only
    sched = make_scheduler(timers=[timer])
    now = _make_datetime(weekday=1, hour=0, minute=0)  # Tuesday 00:00
    with patch(
        "carconnectivity_plugins.campermode.scheduler.datetime"
    ) as mock_dt:
        mock_dt.now.return_value = now
        result = sched._check_timers()
    assert result is True


def test_midnight_wrap_does_not_fire_on_wrong_day():
    """Timer at 23:59 on Wednesday (day=2) must NOT fire at Tuesday 00:00."""
    timer = make_timer(timer_time=dtime(23, 59), days=[2])  # Wednesday only
    sched = make_scheduler(timers=[timer])
    now = _make_datetime(weekday=1, hour=0, minute=0)  # Tuesday 00:00
    with patch(
        "carconnectivity_plugins.campermode.scheduler.datetime"
    ) as mock_dt:
        mock_dt.now.return_value = now
        result = sched._check_timers()
    assert result is False


def test_timer_does_not_fire_twice_within_120s():
    """last_fired_at prevents re-triggering within the 2-minute match
    window."""
    timer = make_timer(timer_time=dtime(22, 30), days=list(range(7)))
    sched = make_scheduler(timers=[timer])
    now = _make_datetime(weekday=0, hour=22, minute=30)
    with patch(
        "carconnectivity_plugins.campermode.scheduler.datetime"
    ) as mock_dt:
        mock_dt.now.return_value = now
        first = sched._check_timers()
        # same moment — last_fired_at is set, must not fire again
        second = sched._check_timers()
    assert first is True
    assert second is False


def test_timer_fires_again_after_120s_cooldown():
    """Timer is eligible once 120 s have elapsed since last_fired_at."""
    from datetime import timedelta

    timer = make_timer(
        timer_time=dtime(22, 30), days=list(range(7)), repeat=True
    )
    sched = make_scheduler(timers=[timer])
    now = _make_datetime(weekday=0, hour=22, minute=30)
    # Pre-set last_fired_at to 121 s before now so the cooldown is already over
    # while the current time is still inside the ±60 s match window.
    timer.last_fired_at = now.astimezone() - timedelta(seconds=121)
    with patch(
        "carconnectivity_plugins.campermode.scheduler.datetime"
    ) as mock_dt:
        mock_dt.now.return_value = now
        result = sched._check_timers()
    assert result is True


def test_non_repeat_timer_disabled_after_firing():
    timer = make_timer(
        timer_time=dtime(22, 30), days=list(range(7)), repeat=False
    )
    sched = make_scheduler(timers=[timer])
    now = _make_datetime(weekday=0, hour=22, minute=30)
    with patch(
        "carconnectivity_plugins.campermode.scheduler.datetime"
    ) as mock_dt:
        mock_dt.now.return_value = now
        sched._check_timers()
    assert timer.enabled is False


def test_repeat_timer_stays_enabled_after_firing():
    timer = make_timer(
        timer_time=dtime(22, 30), days=list(range(7)), repeat=True
    )
    sched = make_scheduler(timers=[timer])
    now = _make_datetime(weekday=0, hour=22, minute=30)
    with patch(
        "carconnectivity_plugins.campermode.scheduler.datetime"
    ) as mock_dt:
        mock_dt.now.return_value = now
        sched._check_timers()
    assert timer.enabled is True


# ---------------------------------------------------------------------------
# Phase transitions
# ---------------------------------------------------------------------------


def _start_session_at(sched: CamperScheduler, mono_time: float) -> None:
    with (
        patch(
            "carconnectivity_plugins.campermode.scheduler.time_module.monotonic",
            return_value=mono_time,
        ),
        sched._lock,
    ):
        sched._do_start_session("test")


def _tick_at(sched: CamperScheduler, mono_time: float) -> None:
    with (
        patch(
            "carconnectivity_plugins.campermode.scheduler.time_module.monotonic",
            return_value=mono_time,
        ),
        sched._lock,
    ):
        sched._manage_session()


def test_heating_transitions_to_paused_after_cycle():
    from carconnectivity.command_impl import ClimatizationStartStopCommand

    vehicle = make_vehicle()
    settings = make_settings(
        cycle_duration_minutes=1, minutes_between_cycles=5
    )
    sched = make_scheduler(vehicle=vehicle, settings=settings)
    _start_session_at(sched, 0.0)
    assert sched._state.current_phase == "heating"
    # Tick past cycle (61 s) AND past rate limit (181 s) so STOP can be sent
    _tick_at(sched, 181.0)
    assert sched._state.current_phase == "paused"
    cmd = vehicle.climatization.commands.commands["start-stop"]
    assert cmd.value["command"] == ClimatizationStartStopCommand.Command.STOP


def test_paused_transitions_to_heating_after_pause():
    vehicle = make_vehicle()
    settings = make_settings(
        cycle_duration_minutes=1, minutes_between_cycles=2
    )
    sched = make_scheduler(vehicle=vehicle, settings=settings)
    _start_session_at(sched, 0.0)
    _tick_at(sched, 61.0)  # end of cycle → paused
    assert sched._state.current_phase == "paused"
    cycle_before = sched._state.cycle_number
    _tick_at(sched, 61.0 + 121.0)  # end of pause → heating
    assert sched._state.current_phase == "heating"
    assert sched._state.cycle_number == cycle_before + 1


def test_continuous_mode_no_pause_phase():
    vehicle = make_vehicle()
    settings = make_settings(
        cycle_duration_minutes=1, minutes_between_cycles=0
    )
    sched = make_scheduler(vehicle=vehicle, settings=settings)
    _start_session_at(sched, 0.0)
    _tick_at(sched, 181.0)  # past cycle + rate limit → stays heating
    assert sched._state.current_phase == "heating"
    assert sched._state.cycle_number == 2


def test_continuous_mode_rate_limited_does_not_advance_cycle():
    """When the START command is rate-limited in continuous mode,
    cycle_number and _phase_start must NOT be updated."""
    vehicle = make_vehicle()
    settings = make_settings(
        cycle_duration_minutes=1, minutes_between_cycles=0
    )
    sched = make_scheduler(vehicle=vehicle, settings=settings)
    _start_session_at(sched, 0.0)
    # Tick at 61 s — past cycle but within rate-limit window (180 s)
    _tick_at(sched, 61.0)
    # cycle_number should stay at 1 because START was rate-limited
    assert sched._state.cycle_number == 1
    assert sched._state.current_phase == "heating"


def test_duration_reached_stops_session():
    vehicle = make_vehicle()
    settings = make_settings(
        cycle_duration_minutes=1,
        minutes_between_cycles=0,
        total_duration_minutes=2,
        endless=False,
    )
    sched = make_scheduler(vehicle=vehicle, settings=settings)
    _start_session_at(sched, 0.0)
    _tick_at(sched, 121.0)  # 2 min + 1 s
    assert sched._state.active is False
    assert sched._state.stopped_reason == "duration_reached"


def test_endless_mode_no_duration_limit():
    vehicle = make_vehicle()
    settings = make_settings(
        cycle_duration_minutes=1,
        minutes_between_cycles=0,
        total_duration_minutes=2,
        endless=True,
    )
    sched = make_scheduler(vehicle=vehicle, settings=settings)
    _start_session_at(sched, 0.0)
    _tick_at(sched, 121.0)  # would trigger stop in non-endless mode
    assert sched._state.active is True


def test_phase_remaining_seconds_counts_down():
    vehicle = make_vehicle()
    settings = make_settings(
        cycle_duration_minutes=2, minutes_between_cycles=0
    )
    sched = make_scheduler(vehicle=vehicle, settings=settings)
    _start_session_at(sched, 0.0)
    _tick_at(sched, 30.0)  # 30 s into 120 s cycle
    assert sched._state.phase_remaining_seconds == 90


def test_total_remaining_seconds_counts_down():
    vehicle = make_vehicle()
    settings = make_settings(
        cycle_duration_minutes=5,
        minutes_between_cycles=0,
        total_duration_minutes=10,
        endless=False,
    )
    sched = make_scheduler(vehicle=vehicle, settings=settings)
    _start_session_at(sched, 0.0)
    _tick_at(sched, 60.0)  # 60 s into 600 s total
    assert sched._state.total_remaining_seconds == 540


def test_battery_too_low_during_manage_session_stops():
    vehicle = make_vehicle()
    settings = make_settings(min_battery_level=20, cycle_duration_minutes=5)
    # Start session with no battery set, then drop battery during session
    sched = make_scheduler(vehicle=vehicle, settings=settings)
    _start_session_at(sched, 0.0)
    assert sched._state.active is True
    sched._battery_level = 15  # drop below min during session
    _tick_at(sched, 10.0)
    assert sched._state.active is False
    assert sched._state.stopped_reason == "battery"


def test_battery_too_low_after_pause_stops_not_heat():
    vehicle = make_vehicle()
    settings = make_settings(
        min_battery_level=20,
        cycle_duration_minutes=1,
        minutes_between_cycles=2,
    )
    sched = make_scheduler(vehicle=vehicle, settings=settings, battery=50)
    _start_session_at(sched, 0.0)
    _tick_at(sched, 61.0)  # heating → paused
    assert sched._state.current_phase == "paused"
    # Battery drops during pause
    sched._battery_level = 10
    _tick_at(sched, 61.0 + 121.0)  # pause done, but battery low
    assert sched._state.active is False
    assert sched._state.stopped_reason == "battery"


# ---------------------------------------------------------------------------
# Thread lifecycle
# ---------------------------------------------------------------------------


def test_start_stop_thread_without_hanging():
    sched = make_scheduler()
    sched.start()
    assert sched._thread is not None
    assert sched._thread.is_alive()
    sched.stop()
    assert not sched._thread.is_alive()


def test_start_called_twice_does_not_leak_thread():
    """Calling start() a second time stops the first thread before spawning."""
    sched = make_scheduler()
    sched.start()
    first_thread = sched._thread
    assert first_thread is not None and first_thread.is_alive()

    sched.start()  # should stop first_thread and start a new one
    assert not first_thread.is_alive()
    assert sched._thread is not None
    assert sched._thread.is_alive()
    assert sched._thread is not first_thread

    sched.stop()


def test_loop_exception_does_not_crash():
    sched = make_scheduler()
    called_event = threading.Event()

    def bad_tick(tick):
        called_event.set()
        raise RuntimeError("boom")

    sched._tick = bad_tick
    sched.start()
    assert called_event.wait(timeout=2), "tick was never called"
    sched.stop()


# ---------------------------------------------------------------------------
# Climate settings
# ---------------------------------------------------------------------------


def test_window_heating_applied_when_changeable():
    vehicle = make_vehicle()
    vehicle.climatization.settings.window_heating.is_changeable = True
    settings = make_settings(window_heating=True)
    sched = make_scheduler(vehicle=vehicle, settings=settings)
    sched._vehicle = vehicle
    sched._apply_climate_settings()
    assert vehicle.climatization.settings.window_heating.value is True


def test_window_heating_not_changeable_skipped():
    vehicle = make_vehicle()
    vehicle.climatization.settings.window_heating.is_changeable = False
    settings = make_settings(window_heating=True)
    sched = make_scheduler(vehicle=vehicle, settings=settings)
    sched._vehicle = vehicle
    sched._apply_climate_settings()  # must not raise
    # value should not have been set — assert no AttributeError was raised


# ---------------------------------------------------------------------------
# OFFLINE state handling
# ---------------------------------------------------------------------------


def test_start_when_offline_returns_false():
    vehicle = make_vehicle(state=GenericVehicle.State.OFFLINE)
    sched = make_scheduler(vehicle=vehicle)
    assert sched.start_session() is False


def test_vehicle_goes_offline_during_session_stops():
    vehicle = make_vehicle()
    sched = make_scheduler(vehicle=vehicle)
    _start_session_at(sched, 0.0)
    assert sched._state.active is True
    vehicle.state.value = GenericVehicle.State.OFFLINE
    _tick_at(sched, 10.0)
    assert sched._state.active is False
    assert sched._state.stopped_reason == "vehicle_offline"


def test_vehicle_goes_offline_during_paused_stops():
    vehicle = make_vehicle()
    settings = make_settings(
        cycle_duration_minutes=1, minutes_between_cycles=5
    )
    sched = make_scheduler(vehicle=vehicle, settings=settings)
    _start_session_at(sched, 0.0)
    _tick_at(sched, 181.0)  # past cycle (60s) + rate-limit (180s) → PAUSED
    assert sched._state.current_phase == "paused"
    vehicle.state.value = GenericVehicle.State.OFFLINE
    _tick_at(sched, 182.0)
    assert sched._state.active is False
    assert sched._state.stopped_reason == "vehicle_offline"


def test_start_with_offline_value_but_state_disabled_succeeds():
    """Guard must be skipped when vehicle.state.enabled is False."""
    vehicle = make_vehicle()
    vehicle.state.enabled = False
    vehicle.state.value = GenericVehicle.State.OFFLINE
    sched = make_scheduler(vehicle=vehicle)
    assert sched.start_session() is True


# ---------------------------------------------------------------------------
# Climatization state observer
# ---------------------------------------------------------------------------


def test_climatization_off_after_grace_period_stops_session():
    from carconnectivity.climatization import Climatization

    vehicle = make_vehicle()
    sched = make_scheduler(vehicle=vehicle)
    sched.set_poll_interval(10.0)  # grace = 2*10+10 = 30 s
    _start_session_at(sched, 0.0)
    assert sched._state.active is True
    with patch(
        "carconnectivity_plugins.campermode.scheduler.time_module.monotonic",
        return_value=31.0,
    ):
        sched.on_climatization_state_changed(
            Climatization.ClimatizationState.OFF
        )
    assert sched._state.active is False
    assert sched._state.stopped_reason == "vehicle_climatization_off"


def test_climatization_off_within_grace_period_ignored():
    from carconnectivity.climatization import Climatization

    vehicle = make_vehicle()
    sched = make_scheduler(vehicle=vehicle)
    sched.set_poll_interval(10.0)  # grace = 30 s
    _start_session_at(sched, 0.0)
    assert sched._state.active is True
    with patch(
        "carconnectivity_plugins.campermode.scheduler.time_module.monotonic",
        return_value=15.0,
    ):
        sched.on_climatization_state_changed(
            Climatization.ClimatizationState.OFF
        )
    assert sched._state.active is True


def test_climatization_off_when_paused_is_noop():
    from carconnectivity.climatization import Climatization

    vehicle = make_vehicle()
    settings = make_settings(
        cycle_duration_minutes=1, minutes_between_cycles=5
    )
    sched = make_scheduler(vehicle=vehicle, settings=settings)
    sched.set_poll_interval(10.0)  # grace = 30 s
    _start_session_at(sched, 0.0)
    _tick_at(sched, 181.0)  # heating → paused (past cycle + rate limit)
    assert sched._state.current_phase == "paused"
    with patch(
        "carconnectivity_plugins.campermode.scheduler.time_module.monotonic",
        return_value=200.0,
    ):
        sched.on_climatization_state_changed(
            Climatization.ClimatizationState.OFF
        )
    assert sched._state.active is True


def test_climatization_off_when_idle_is_noop():
    from carconnectivity.climatization import Climatization

    sched = make_scheduler()
    # No active session — must not raise or mutate state
    sched.on_climatization_state_changed(Climatization.ClimatizationState.OFF)
    assert sched._state.active is False


# ---------------------------------------------------------------------------
# Battery delta recording
# ---------------------------------------------------------------------------


def test_battery_delta_recorded_after_heating_cycle():
    """Delta is appended when HEATING→PAUSED transition fires."""
    vehicle = make_vehicle()
    settings = make_settings(
        cycle_duration_minutes=1, minutes_between_cycles=5
    )
    sched = make_scheduler(vehicle=vehicle, settings=settings, battery=80)
    _start_session_at(sched, 0.0)
    assert sched._heating_start_battery == 80
    sched._battery_level = 75
    _tick_at(sched, 61.0)  # past 1-min cycle → PAUSED
    assert sched._state.current_phase == "paused"
    assert sched._battery_deltas == [5]


def test_battery_delta_not_recorded_when_battery_none():
    """No delta recorded when battery level is None."""
    sched = make_scheduler()
    # _battery_level stays None
    with sched._lock:
        sched._record_heating_end()
    assert sched._battery_deltas == []


def test_battery_delta_recorded_on_stop():
    """_record_heating_end captures the delta correctly (partial cycle)."""
    vehicle = make_vehicle()
    sched = make_scheduler(vehicle=vehicle, battery=80)
    with sched._lock:
        sched._do_start_session("test")
    sched._battery_level = 75
    with sched._lock:
        sched._record_heating_end()
    assert sched._battery_deltas == [5]
    assert sched._state.avg_battery_consumption == pytest.approx(5.0)


def test_battery_deltas_cleared_on_stop():
    """_battery_deltas resets to [] when the session ends."""
    vehicle = make_vehicle()
    settings = make_settings(
        cycle_duration_minutes=1, minutes_between_cycles=5
    )
    sched = make_scheduler(vehicle=vehicle, settings=settings, battery=80)
    _start_session_at(sched, 0.0)
    sched._battery_level = 75
    _tick_at(sched, 61.0)  # HEATING → PAUSED, delta=5 recorded
    assert sched._battery_deltas == [5]
    sched.stop_session()
    assert sched._battery_deltas == []


# ---------------------------------------------------------------------------
# Average consumption
# ---------------------------------------------------------------------------


def test_avg_consumption_none_with_no_data():
    sched = make_scheduler()
    assert sched._avg_consumption() is None


def test_avg_consumption_single_cycle():
    sched = make_scheduler()
    sched._battery_deltas = [10]
    assert sched._avg_consumption() == pytest.approx(10.0)


def test_avg_consumption_multiple_cycles():
    sched = make_scheduler()
    sched._battery_deltas = [10, 8, 12]
    assert sched._avg_consumption() == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# Predictive checks
# ---------------------------------------------------------------------------


def test_prediction_allows_cycle_when_no_data():
    """No historical data — proceed normally into HEATING."""
    vehicle = make_vehicle()
    settings = make_settings(
        min_battery_level=20,
        cycle_duration_minutes=1,
        minutes_between_cycles=2,
    )
    sched = make_scheduler(vehicle=vehicle, settings=settings, battery=50)
    _start_session_at(sched, 0.0)
    _tick_at(sched, 61.0)  # → PAUSED
    assert sched._state.current_phase == "paused"
    _tick_at(sched, 61.0 + 181.0)  # → HEATING (no prediction data)
    assert sched._state.current_phase == "heating"
    assert sched._state.active is True


def test_prediction_allows_cycle_when_battery_sufficient():
    """Predicted floor stays above minimum — full cycle proceeds."""
    vehicle = make_vehicle()
    settings = make_settings(
        min_battery_level=20,
        cycle_duration_minutes=1,
        minutes_between_cycles=2,
    )
    # delta=5 recorded in cycle 1; battery=75 at pause; 75-5=70 > 20 → OK
    sched = make_scheduler(vehicle=vehicle, settings=settings, battery=80)
    _start_session_at(sched, 0.0)
    sched._battery_level = 75
    _tick_at(sched, 61.0)  # → PAUSED, _battery_deltas=[5]
    _tick_at(sched, 61.0 + 181.0)  # → HEATING (75-5=70 > 20 → OK)
    assert sched._state.current_phase == "heating"
    assert sched._half_cycle is False


def test_prediction_triggers_half_cycle():
    """Full cycle would breach threshold; half cycle is safe."""
    vehicle = make_vehicle()
    settings = make_settings(
        min_battery_level=20,
        cycle_duration_minutes=1,
        minutes_between_cycles=2,
    )
    # Cycle 1: battery drops 45 → 30, so delta = 15, avg = 15.
    # At PAUSED→HEATING: battery=30, predicted=15.
    #   full check: 30 - 15 = 15 < 20 → full cycle too risky.
    #   half check: 30 - 7.5 = 22.5 ≥ 20 → half cycle is safe.
    sched = make_scheduler(vehicle=vehicle, settings=settings, battery=45)
    _start_session_at(sched, 0.0)
    sched._battery_level = 30
    _tick_at(sched, 61.0)  # → PAUSED, _battery_deltas=[15]
    assert sched._state.current_phase == "paused"
    _tick_at(sched, 61.0 + 181.0)  # → HEATING with half cycle
    assert sched._state.current_phase == "heating"
    assert sched._half_cycle is True
    assert sched._state.half_cycle_active is True


def test_prediction_stops_when_even_half_too_risky():
    """Both full and half cycle would breach threshold — stop."""
    vehicle = make_vehicle()
    settings = make_settings(
        min_battery_level=20,
        cycle_duration_minutes=1,
        minutes_between_cycles=2,
    )
    # delta=12 (37→25); 25-12=13 < 20 → risky; 25-6=19 < 20 → stop
    sched = make_scheduler(vehicle=vehicle, settings=settings, battery=37)
    _start_session_at(sched, 0.0)
    sched._battery_level = 25
    _tick_at(sched, 61.0)  # → PAUSED, _battery_deltas=[12]
    assert sched._state.current_phase == "paused"
    _tick_at(sched, 61.0 + 181.0)  # → battery_predicted stop
    assert sched._state.active is False
    assert sched._state.stopped_reason == "battery_predicted"


def test_prediction_skipped_when_battery_none():
    """Battery None → conservative: skip predictive check, proceed."""
    vehicle = make_vehicle()
    settings = make_settings(
        min_battery_level=20,
        cycle_duration_minutes=1,
        minutes_between_cycles=2,
    )
    sched = make_scheduler(vehicle=vehicle, settings=settings)
    # battery stays None; pre-load deltas to confirm they're ignored
    sched._battery_deltas = [15]
    _start_session_at(sched, 0.0)
    _tick_at(sched, 61.0)  # → PAUSED (no delta recorded, battery None)
    _tick_at(sched, 61.0 + 181.0)  # → HEATING (predictive check skipped)
    assert sched._state.current_phase == "heating"
    assert sched._state.active is True


# ---------------------------------------------------------------------------
# Half-cycle duration
# ---------------------------------------------------------------------------


def test_half_cycle_uses_halved_duration():
    """When half_cycle is active, HEATING ends at half the full duration."""
    vehicle = make_vehicle()
    settings = make_settings(
        cycle_duration_minutes=2, minutes_between_cycles=5
    )
    sched = make_scheduler(vehicle=vehicle, settings=settings)
    _start_session_at(sched, 0.0)
    with sched._lock:
        sched._half_cycle = True
    # Half of 120 s is 60 s; tick at 61 s → PAUSED
    _tick_at(sched, 61.0)
    assert sched._state.current_phase == "paused"


def test_half_cycle_records_delta():
    """Delta is recorded when a half cycle completes."""
    vehicle = make_vehicle()
    settings = make_settings(
        cycle_duration_minutes=2, minutes_between_cycles=5
    )
    sched = make_scheduler(vehicle=vehicle, settings=settings, battery=80)
    _start_session_at(sched, 0.0)
    with sched._lock:
        sched._half_cycle = True
    sched._battery_level = 75
    _tick_at(sched, 61.0)  # half cycle done → PAUSED
    assert sched._state.current_phase == "paused"
    assert sched._battery_deltas == [5]
