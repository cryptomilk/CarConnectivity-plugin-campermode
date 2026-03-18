"""Module implementing the CamperMode plugin."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from carconnectivity_plugins.base.plugin import BasePlugin

if TYPE_CHECKING:
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
        LOG.info("CamperMode plugin initialised (id=%s)", plugin_id)

    def startup(self) -> None:
        LOG.info("Starting CamperMode plugin")
        self.healthy._set_value(value=True)  # pylint: disable=protected-access
        LOG.debug("Starting CamperMode plugin done")
        return super().startup()

    def shutdown(self) -> None:
        LOG.info("Shutting down CamperMode plugin")
        return super().shutdown()

    def get_version(self) -> str:
        return "0.1.0.dev0"

    def get_type(self) -> str:
        return "carconnectivity-plugin-campermode"

    def get_name(self) -> str:
        return "CamperMode Plugin"
