"""Data models and JSON persistence for the CamperMode plugin."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum

LOG: logging.Logger = logging.getLogger("carconnectivity.plugins.campermode")


class PhaseState(str, Enum):
    """Lifecycle phase of an active camper session."""

    IDLE = "idle"
    HEATING = "heating"
    PAUSED = "paused"


@dataclass
class CamperTimer:
    """A scheduled activation of camper mode."""

    id: str
    time: time
    days_of_week: list[int]
    repeat_weekly: bool
    enabled: bool
    created_at: datetime
    # Runtime-only: tracks when this timer last fired to prevent repeated
    # triggering within the ±60 s match window. Not persisted.
    last_fired_at: datetime | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "time": self.time.isoformat(),
            "days_of_week": self.days_of_week,
            "repeat_weekly": self.repeat_weekly,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> CamperTimer:
        return cls(
            id=str(data["id"]),
            time=time.fromisoformat(str(data["time"])),
            days_of_week=[int(d) for d in data["days_of_week"]],  # type: ignore[arg-type]
            repeat_weekly=bool(data["repeat_weekly"]),
            enabled=bool(data["enabled"]),
            created_at=datetime.fromisoformat(str(data["created_at"])),
        )


@dataclass
class CamperSettings:
    """Persistent settings for climatisation behaviour."""

    min_battery_level: int = 20
    minutes_between_cycles: int = 0
    cycle_duration_minutes: int = 30
    total_duration_minutes: int = 60
    endless: bool = False
    window_heating: bool = True
    front_zone_left: bool = True
    front_zone_right: bool = True
    rear_zone_left: bool = False
    rear_zone_right: bool = False
    target_temperature: float = 22.0

    def __post_init__(self) -> None:
        self.min_battery_level = max(0, min(100, self.min_battery_level))
        self.cycle_duration_minutes = max(1, self.cycle_duration_minutes)
        self.minutes_between_cycles = max(0, self.minutes_between_cycles)
        self.total_duration_minutes = max(1, self.total_duration_minutes)
        # VW climate system accepts 15.5-30.0 °C
        self.target_temperature = max(15.5, min(30.0, self.target_temperature))

    @property
    def has_active_heating(self) -> bool:
        """True if any window or seat heating zone is enabled."""
        return (
            self.window_heating
            or self.front_zone_left
            or self.front_zone_right
            or self.rear_zone_left
            or self.rear_zone_right
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "min_battery_level": self.min_battery_level,
            "minutes_between_cycles": self.minutes_between_cycles,
            "cycle_duration_minutes": self.cycle_duration_minutes,
            "total_duration_minutes": self.total_duration_minutes,
            "endless": self.endless,
            "window_heating": self.window_heating,
            "front_zone_left": self.front_zone_left,
            "front_zone_right": self.front_zone_right,
            "rear_zone_left": self.rear_zone_left,
            "rear_zone_right": self.rear_zone_right,
            "target_temperature": self.target_temperature,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> CamperSettings:
        return cls(
            min_battery_level=int(data.get("min_battery_level", 20)),  # type: ignore[arg-type]
            minutes_between_cycles=int(
                data.get("minutes_between_cycles", 0)  # type: ignore[arg-type]
            ),
            cycle_duration_minutes=int(
                data.get("cycle_duration_minutes", 30)  # type: ignore[arg-type]
            ),
            total_duration_minutes=int(
                data.get("total_duration_minutes", 60)  # type: ignore[arg-type]
            ),
            endless=bool(data.get("endless", False)),
            window_heating=bool(data.get("window_heating", True)),
            front_zone_left=bool(data.get("front_zone_left", True)),
            front_zone_right=bool(data.get("front_zone_right", True)),
            rear_zone_left=bool(data.get("rear_zone_left", False)),
            rear_zone_right=bool(data.get("rear_zone_right", False)),
            target_temperature=float(
                data.get("target_temperature", 22.0)  # type: ignore[arg-type]
            ),
        )


@dataclass
class CamperState:
    """Runtime state (not persisted)."""

    active: bool = False
    current_phase: PhaseState = PhaseState.IDLE
    cycle_number: int = 0
    phase_remaining_seconds: int = 0
    total_remaining_seconds: int = 0
    started_at: datetime | None = None
    stopped_reason: str | None = None
    current_battery_level: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "active": self.active,
            "current_phase": self.current_phase,
            "cycle_number": self.cycle_number,
            "phase_remaining_seconds": self.phase_remaining_seconds,
            "total_remaining_seconds": self.total_remaining_seconds,
            "started_at": (
                self.started_at.isoformat()
                if self.started_at is not None
                else None
            ),
            "stopped_reason": self.stopped_reason,
            "current_battery_level": self.current_battery_level,
        }


def save_data(
    path: str,
    settings: CamperSettings,
    timers: list[CamperTimer],
) -> None:
    """Serialize settings and timers to a JSON file."""
    tmp_path = path + ".tmp"
    try:
        data: dict[str, object] = {
            "settings": settings.to_dict(),
            "timers": [t.to_dict() for t in timers],
        }
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
        LOG.debug("Saved campermode data to %s", path)
    except OSError as exc:
        LOG.error("Failed to save campermode data to %s: %s", path, exc)


def load_data(
    path: str,
) -> tuple[CamperSettings, list[CamperTimer]]:
    """Deserialize settings and timers from a JSON file."""
    try:
        with open(path, encoding="utf-8") as f:
            data: dict[str, object] = json.load(f)
        settings_raw = data.get("settings", {})
        timers_raw = data.get("timers", [])
        settings = CamperSettings.from_dict(
            settings_raw if isinstance(settings_raw, dict) else {}
        )
        timers = [
            CamperTimer.from_dict(t)
            for t in (timers_raw if isinstance(timers_raw, list) else [])
        ]
        LOG.debug("Loaded campermode data from %s", path)
        return settings, timers
    except FileNotFoundError:
        LOG.debug("No data file at %s, using defaults", path)
        return CamperSettings(), []
    except (
        OSError,
        json.JSONDecodeError,
        KeyError,
        ValueError,
        TypeError,
    ) as exc:
        LOG.error(
            "Failed to load campermode data from %s: %s, using defaults",
            path,
            exc,
        )
        return CamperSettings(), []
