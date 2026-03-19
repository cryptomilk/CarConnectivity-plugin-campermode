"""Unit tests for the CamperMode scheduler engine."""

from __future__ import annotations

import threading
from datetime import datetime
from datetime import time as dtime
from unittest.mock import MagicMock, patch

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
    _tick_at(sched, 61.0)  # end of cycle → stays heating (no pause)
    assert sched._state.current_phase == "heating"
    assert sched._state.cycle_number == 2


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
