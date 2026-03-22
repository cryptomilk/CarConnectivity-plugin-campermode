"""Round-trip tests for campermode data models and JSON persistence."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, time, timezone

import pytest

from carconnectivity_plugins.campermode.models import (
    CamperSettings,
    CamperState,
    CamperTimer,
    PhaseState,
    load_data,
    save_data,
)

# ---------------------------------------------------------------------------
# CamperTimer
# ---------------------------------------------------------------------------


def make_timer(**overrides) -> CamperTimer:
    defaults = dict(
        id="t1",
        time=time(22, 30),
        days_of_week=[0, 4, 5],
        repeat_weekly=True,
        enabled=True,
        created_at=datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return CamperTimer(**defaults)


def test_timer_to_dict_round_trip():
    t = make_timer()
    d = t.to_dict()
    t2 = CamperTimer.from_dict(d)
    assert t2.id == t.id
    assert t2.time == t.time
    assert t2.days_of_week == t.days_of_week
    assert t2.repeat_weekly == t.repeat_weekly
    assert t2.enabled == t.enabled
    assert t2.created_at == t.created_at


def test_timer_to_dict_keys():
    t = make_timer()
    d = t.to_dict()
    assert set(d.keys()) == {
        "id",
        "time",
        "days_of_week",
        "repeat_weekly",
        "enabled",
        "created_at",
    }


def test_timer_time_serialized_as_iso():
    t = make_timer(time=time(8, 5))
    d = t.to_dict()
    assert d["time"] == "08:05:00"


def test_timer_from_dict_string_days_of_week():
    """days_of_week elements are cast to int even when JSON delivers
    strings."""
    raw = {
        "id": "x",
        "time": "08:00:00",
        "days_of_week": ["0", "4", "5"],
        "repeat_weekly": True,
        "enabled": True,
        "created_at": "2026-01-15T10:00:00+00:00",
    }
    t = CamperTimer.from_dict(raw)
    assert t.days_of_week == [0, 4, 5]
    assert all(isinstance(v, int) for v in t.days_of_week)


# ---------------------------------------------------------------------------
# PhaseState
# ---------------------------------------------------------------------------


def test_phase_state_values_match_legacy_strings():
    assert PhaseState.IDLE == "idle"
    assert PhaseState.HEATING == "heating"
    assert PhaseState.PAUSED == "paused"


def test_phase_state_is_str():
    assert isinstance(PhaseState.HEATING, str)


# ---------------------------------------------------------------------------
# CamperSettings
# ---------------------------------------------------------------------------


def test_settings_defaults():
    s = CamperSettings()
    assert s.min_battery_level == 20
    assert s.minutes_between_cycles == 0
    assert s.cycle_duration_minutes == 30
    assert s.total_duration_minutes == 60
    assert s.endless is False
    assert s.window_heating is False
    assert s.front_zone_left is False
    assert s.front_zone_right is False
    assert s.rear_zone_left is False
    assert s.rear_zone_right is False
    assert s.target_temperature == pytest.approx(22.0)


def test_settings_round_trip():
    s = CamperSettings(
        min_battery_level=30,
        minutes_between_cycles=60,
        cycle_duration_minutes=45,
        total_duration_minutes=360,
        endless=True,
        window_heating=False,
        front_zone_left=True,
        front_zone_right=False,
        rear_zone_left=True,
        rear_zone_right=True,
        target_temperature=20.5,
    )
    d = s.to_dict()
    s2 = CamperSettings.from_dict(d)
    assert s2.min_battery_level == 30
    assert s2.minutes_between_cycles == 60
    assert s2.cycle_duration_minutes == 45
    assert s2.total_duration_minutes == 360
    assert s2.endless is True
    assert s2.window_heating is False
    assert s2.front_zone_left is True
    assert s2.front_zone_right is False
    assert s2.rear_zone_left is True
    assert s2.rear_zone_right is True
    assert s2.target_temperature == pytest.approx(20.5)


def test_settings_from_dict_missing_keys_use_defaults():
    s = CamperSettings.from_dict({})
    assert s.min_battery_level == 20
    assert s.target_temperature == pytest.approx(22.0)


def test_settings_clamps_battery_level_above_90():
    s = CamperSettings(min_battery_level=150)
    assert s.min_battery_level == 90


def test_settings_clamps_battery_level_below_10():
    s = CamperSettings(min_battery_level=-5)
    assert s.min_battery_level == 10


def test_settings_clamps_cycle_duration_to_minimum_1():
    s = CamperSettings(cycle_duration_minutes=0)
    assert s.cycle_duration_minutes == 1


def test_settings_clamps_total_duration_to_minimum_1():
    s = CamperSettings(total_duration_minutes=0)
    assert s.total_duration_minutes == 1


def test_settings_clamps_temperature_below_min():
    s = CamperSettings(target_temperature=5.0)
    assert s.target_temperature == pytest.approx(15.5)


def test_settings_clamps_temperature_above_max():
    s = CamperSettings(target_temperature=99.0)
    assert s.target_temperature == pytest.approx(30.0)


def test_settings_from_dict_clamps_invalid_values():
    s = CamperSettings.from_dict(
        {
            "cycle_duration_minutes": 0,
            "min_battery_level": 200,
            "target_temperature": 0.0,
        }
    )
    assert s.cycle_duration_minutes == 1
    assert s.min_battery_level == 90
    assert s.target_temperature == pytest.approx(15.5)


# ---------------------------------------------------------------------------
# CamperState
# ---------------------------------------------------------------------------


def test_state_defaults():
    s = CamperState()
    assert s.active is False
    assert s.current_phase == "idle"
    assert s.cycle_number == 0
    assert s.started_at is None
    assert s.stopped_reason is None
    assert s.current_battery_level is None


def test_state_to_dict():
    now = datetime(2026, 3, 18, 1, 0, tzinfo=timezone.utc)
    s = CamperState(
        active=True,
        current_phase=PhaseState.HEATING,
        cycle_number=2,
        phase_remaining_seconds=1200,
        total_remaining_seconds=3600,
        started_at=now,
        stopped_reason=None,
        current_battery_level=55,
    )
    d = s.to_dict()
    assert d["active"] is True
    assert d["current_phase"] == "heating"
    assert d["cycle_number"] == 2
    assert d["started_at"] == now.isoformat()
    assert d["stopped_reason"] is None
    assert d["current_battery_level"] == 55


def test_state_to_dict_started_at_none():
    s = CamperState()
    d = s.to_dict()
    assert d["started_at"] is None


def test_state_to_dict_includes_avg_consumption():
    s = CamperState(avg_battery_consumption=7.5)
    d = s.to_dict()
    assert "avg_battery_consumption" in d
    assert d["avg_battery_consumption"] == pytest.approx(7.5)


def test_state_to_dict_includes_half_cycle_active():
    s = CamperState(half_cycle_active=True)
    d = s.to_dict()
    assert "half_cycle_active" in d
    assert d["half_cycle_active"] is True


# ---------------------------------------------------------------------------
# save_data / load_data
# ---------------------------------------------------------------------------


def test_save_and_load_round_trip(tmp_path):
    path = str(tmp_path / "campermode.json")
    settings = CamperSettings(min_battery_level=25, target_temperature=21.0)
    timers = [make_timer(id="a"), make_timer(id="b", enabled=False)]

    save_data(path, settings, timers)
    assert os.path.exists(path)

    s2, t2 = load_data(path)
    assert s2.min_battery_level == 25
    assert s2.target_temperature == pytest.approx(21.0)
    assert len(t2) == 2
    assert t2[0].id == "a"
    assert t2[1].id == "b"
    assert t2[1].enabled is False


def test_load_data_file_not_found(tmp_path):
    path = str(tmp_path / "nonexistent.json")
    s, t = load_data(path)
    assert isinstance(s, CamperSettings)
    assert t == []


def test_load_data_corrupt_json(tmp_path):
    path = str(tmp_path / "bad.json")
    with open(path, "w") as f:
        f.write("{not valid json}")
    s, t = load_data(path)
    assert isinstance(s, CamperSettings)
    assert t == []


def test_save_creates_valid_json(tmp_path):
    path = str(tmp_path / "out.json")
    save_data(path, CamperSettings(), [])
    with open(path) as f:
        data = json.load(f)
    assert "settings" in data
    assert "timers" in data
    assert data["timers"] == []


def test_save_data_bad_path_does_not_raise():
    # Writing to an unwritable path should log an error but not raise.
    save_data("/nonexistent_dir/campermode.json", CamperSettings(), [])


def test_load_data_null_timer_entry_does_not_crash(tmp_path):
    """A null entry in the timers list must not propagate a TypeError."""
    path = str(tmp_path / "null_timer.json")
    with open(path, "w") as f:
        json.dump({"settings": {}, "timers": [None]}, f)
    s, t = load_data(path)
    assert isinstance(s, CamperSettings)
    assert t == []


def test_load_data_skips_malformed_timer_keeps_valid(tmp_path):
    """A malformed timer entry is skipped while valid ones are kept."""
    valid_timer = {
        "id": "good",
        "time": "08:00:00",
        "days_of_week": [0, 1],
        "repeat_weekly": True,
        "enabled": True,
        "created_at": "2026-01-15T10:00:00+00:00",
    }
    path = str(tmp_path / "mixed_timers.json")
    with open(path, "w") as f:
        json.dump(
            {"settings": {}, "timers": [valid_timer, {"bad": True}, None]},
            f,
        )
    _, t = load_data(path)
    assert len(t) == 1
    assert t[0].id == "good"


def test_save_data_no_tmp_file_left(tmp_path):
    """After a successful save the .tmp scratch file must be removed."""
    path = str(tmp_path / "campermode.json")
    save_data(path, CamperSettings(), [])
    assert os.path.exists(path)
    assert not os.path.exists(path + ".tmp")


def test_load_data_empty_timers(tmp_path):
    path = str(tmp_path / "empty_timers.json")
    save_data(path, CamperSettings(), [])
    _, timers = load_data(path)
    assert timers == []


def test_save_and_load_multiple_timers(tmp_path):
    path = str(tmp_path / "multi.json")
    timers = [make_timer(id=str(i), days_of_week=[i % 7]) for i in range(5)]
    save_data(path, CamperSettings(), timers)
    _, loaded = load_data(path)
    assert len(loaded) == 5
    for i, t in enumerate(loaded):
        assert t.id == str(i)


def test_settings_to_dict_all_keys():
    s = CamperSettings()
    d = s.to_dict()
    expected_keys = {
        "min_battery_level",
        "minutes_between_cycles",
        "cycle_duration_minutes",
        "total_duration_minutes",
        "endless",
        "window_heating",
        "front_zone_left",
        "front_zone_right",
        "rear_zone_left",
        "rear_zone_right",
        "target_temperature",
    }
    assert set(d.keys()) == expected_keys


def test_load_data_with_tempfile():
    """Use tempfile.NamedTemporaryFile to verify save/load."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        path = tmp.name
    try:
        s = CamperSettings(endless=True, min_battery_level=15)
        save_data(path, s, [])
        s2, _ = load_data(path)
        assert s2.endless is True
        assert s2.min_battery_level == 15
    finally:
        os.unlink(path)
