"""Module implementing the CamperMode plugin."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from carconnectivity.attributes import LevelAttribute
from carconnectivity.drive import ElectricDrive
from carconnectivity.errors import ConfigurationError
from carconnectivity.observable import Observable
from carconnectivity.vehicle import GenericVehicle
from carconnectivity_plugins.base.plugin import BasePlugin
from carconnectivity_plugins.campermode.models import (
    CamperSettings,
    CamperState,
    CamperTimer,
    load_data,
    save_data,
)
from carconnectivity_plugins.campermode.scheduler import CamperScheduler
from carconnectivity_plugins.campermode.ui.plugin_ui import CamperUI

if TYPE_CHECKING:
    from werkzeug.serving import _TSSLContextArg

    from carconnectivity.carconnectivity import CarConnectivity

LOG: logging.Logger = logging.getLogger("carconnectivity.plugins.campermode")


class Plugin(BasePlugin):
    """CarConnectivity plugin for overnight climatisation cycling."""

    def __init__(
        self,
        plugin_id: str,
        car_connectivity: CarConnectivity,
        config: dict,
        *args,
        initialization: dict | None = None,
        **kwargs,
    ) -> None:
        BasePlugin.__init__(
            self,
            plugin_id=plugin_id,
            car_connectivity=car_connectivity,
            config=config,
            log=LOG,
            initialization=initialization,
            **kwargs,
        )
        self._data_file: str = str(config.get("data_file", "campermode.json"))
        self._vin: str | None = (
            str(config["vin"])
            if "vin" in config and config["vin"] is not None
            else None
        )
        self._settings, self._timers = load_data(self._data_file)
        self._state = CamperState()
        self._scheduler = CamperScheduler(
            self._settings, self._state, self._timers
        )
        self._observed_drives: list[ElectricDrive] = []

        # ── Web server config ─────────────────────────────────────────
        host: str = str(config.get("host", "0.0.0.0"))  # nosec
        port: int = int(config.get("port", 4001))
        if port < 1 or port > 65535:
            raise ConfigurationError(
                'Invalid port specified in config ("port" out of range,'
                " must be 1-65535)"
            )

        users: dict[str, str] = {}
        if (
            "username" in config
            and config["username"] is not None
            and "password" in config
            and config["password"] is not None
        ):
            users[config["username"]] = config["password"]
        if "users" in config and config["users"] is not None:
            for user in config["users"]:
                if "username" in user and "password" in user:
                    users[user["username"]] = user["password"]

        ssl_context: _TSSLContextArg | None = None
        if config.get("https"):
            if (
                "ssl_certificate_file" in config
                and "ssl_certificate_key_file" in config
            ):
                ssl_context = (
                    config["ssl_certificate_file"],
                    config["ssl_certificate_key_file"],
                )
            else:
                ssl_context = "adhoc"

        secret_key: str | None = (
            str(config["secret_key"])
            if "secret_key" in config and config["secret_key"] is not None
            else None
        )

        self._ui = CamperUI(
            plugin=self,
            host=host,
            port=port,
            users=users or None,
            ssl_context=ssl_context,
            secret_key=secret_key,
        )

        LOG.info("CamperMode plugin initialised (id=%s)", plugin_id)

    def startup(self) -> None:
        LOG.info("Starting CamperMode plugin")
        self.car_connectivity.garage.add_observer(
            self._on_vehicle_added,
            flag=Observable.ObserverEvent.ENABLED,
            on_transaction_end=True,
        )
        for vehicle in self.car_connectivity.garage.list_vehicles():
            self._try_connect_vehicle(vehicle)
        self._scheduler.start()
        self._ui.start()
        self.healthy._set_value(value=True)  # pylint: disable=protected-access
        LOG.debug("Starting CamperMode plugin done")
        return super().startup()

    def _try_connect_vehicle(self, vehicle: GenericVehicle) -> None:
        if self._vin is not None and vehicle.vin.value != self._vin:
            return
        self._scheduler.set_vehicle(vehicle)
        for drive in vehicle.drives.drives.values():
            if isinstance(drive, ElectricDrive):
                drive.level.add_observer(
                    self._on_battery_changed,
                    flag=Observable.ObserverEvent.VALUE_CHANGED,
                )
                self._observed_drives.append(drive)
                if drive.level.value is not None:
                    self._scheduler.update_battery_level(
                        int(drive.level.value)
                    )

    def _on_vehicle_added(
        self, element: object, flags: Observable.ObserverEvent
    ) -> None:
        del flags
        if isinstance(element, GenericVehicle):
            self._try_connect_vehicle(element)

    def _on_battery_changed(
        self, element: object, flags: Observable.ObserverEvent
    ) -> None:
        del flags
        if isinstance(element, LevelAttribute) and element.value is not None:
            self._scheduler.update_battery_level(int(element.value))

    def shutdown(self) -> None:
        LOG.info("Shutting down CamperMode plugin")
        self._ui.stop()
        self._scheduler.stop_session(reason="shutdown")
        self.car_connectivity.garage.remove_observer(self._on_vehicle_added)
        for drive in self._observed_drives:
            drive.level.remove_observer(self._on_battery_changed)
        self._scheduler.stop()
        save_data(self._data_file, self._settings, self._timers)
        return super().shutdown()

    @property
    def scheduler(self) -> CamperScheduler:
        return self._scheduler

    @property
    def settings(self) -> CamperSettings:
        return self._scheduler.settings_snapshot()

    @property
    def state(self) -> CamperState:
        return self._state

    @property
    def timers(self) -> list[CamperTimer]:
        # Return a shallow copy so callers cannot structurally modify the
        # internal list that the scheduler iterates under its lock.
        return list(self._timers)

    def save_settings(self) -> None:
        """Persist current settings and timers to disk (thread-safe)."""
        self._scheduler.save(self._data_file)

    def get_version(self) -> str:
        return "0.1.0.dev0"

    def get_type(self) -> str:
        return "carconnectivity-plugin-campermode"

    def get_name(self) -> str:
        return "CamperMode Plugin"
