"""Integration tests for Plugin lifecycle and Flask routes."""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest

from carconnectivity_plugins.campermode.models import (
    CamperSettings,
    CamperState,
)
from carconnectivity_plugins.campermode.scheduler import CamperScheduler
from carconnectivity_plugins.campermode.ui.plugin_ui import CamperUI

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_car_connectivity(vehicles=None):
    cc = MagicMock()
    cc.garage.list_vehicles.return_value = vehicles or []
    return cc


def make_mock_plugin():
    plugin = MagicMock()
    plugin.settings = CamperSettings()
    plugin.state = CamperState()
    plugin.timers = []
    plugin.scheduler.start_session.return_value = True
    return plugin


def _stub_base_init(
    self,
    *,
    plugin_id,
    car_connectivity,
    config=None,
    log=None,
    initialization=None,
    **kwargs,
):
    self.car_connectivity = car_connectivity
    self.healthy = MagicMock()


# ---------------------------------------------------------------------------
# Part A: Plugin lifecycle tests
# ---------------------------------------------------------------------------


def test_plugin_init_creates_scheduler_and_ui(tmp_path):
    from carconnectivity_plugins.base.plugin import BasePlugin
    from carconnectivity_plugins.campermode.plugin import Plugin

    config = {"port": 4001, "data_file": str(tmp_path / "cm.json")}
    cc = make_car_connectivity()
    with patch.object(BasePlugin, "__init__", new=_stub_base_init):
        plugin = Plugin("test_id", cc, config)

    assert isinstance(plugin._scheduler, CamperScheduler)
    assert isinstance(plugin._ui, CamperUI)


def test_plugin_init_loads_defaults_when_no_data_file(tmp_path):
    from carconnectivity_plugins.base.plugin import BasePlugin
    from carconnectivity_plugins.campermode.plugin import Plugin

    config = {"port": 4001, "data_file": str(tmp_path / "nonexistent.json")}
    cc = make_car_connectivity()
    with patch.object(BasePlugin, "__init__", new=_stub_base_init):
        plugin = Plugin("test_id", cc, config)

    settings = plugin.settings
    assert isinstance(settings, CamperSettings)
    assert settings.min_battery_level == 20


def test_plugin_init_invalid_port_raises(tmp_path):
    from carconnectivity.errors import ConfigurationError
    from carconnectivity_plugins.base.plugin import BasePlugin
    from carconnectivity_plugins.campermode.plugin import Plugin

    config = {"port": 0, "data_file": str(tmp_path / "cm.json")}
    cc = make_car_connectivity()
    with (
        patch.object(BasePlugin, "__init__", new=_stub_base_init),
        pytest.raises(ConfigurationError),
    ):
        Plugin("test_id", cc, config)


def test_plugin_startup_connects_existing_vehicles(tmp_path):
    from carconnectivity_plugins.base.plugin import BasePlugin
    from carconnectivity_plugins.campermode.plugin import Plugin

    mock_vehicle = MagicMock()
    mock_vehicle.vin.value = "TEST001"
    mock_vehicle.drives.drives = {}
    cc = make_car_connectivity(vehicles=[mock_vehicle])
    config = {"port": 4001, "data_file": str(tmp_path / "cm.json")}

    with patch.object(BasePlugin, "__init__", new=_stub_base_init):
        plugin = Plugin("test_id", cc, config)

    with (
        patch.object(plugin._scheduler, "start"),
        patch.object(plugin._ui, "start"),
        patch.object(BasePlugin, "startup", return_value=None),
    ):
        plugin.startup()

    assert plugin._scheduler._vehicle is mock_vehicle


def test_plugin_shutdown_stops_scheduler_and_ui(tmp_path):
    from carconnectivity_plugins.base.plugin import BasePlugin
    from carconnectivity_plugins.campermode.plugin import Plugin

    config = {"port": 4001, "data_file": str(tmp_path / "cm.json")}
    cc = make_car_connectivity()
    with patch.object(BasePlugin, "__init__", new=_stub_base_init):
        plugin = Plugin("test_id", cc, config)

    with (
        patch.object(plugin._scheduler, "stop") as mock_sched_stop,
        patch.object(plugin._scheduler, "stop_session"),
        patch.object(plugin._ui, "stop") as mock_ui_stop,
        patch.object(BasePlugin, "shutdown", return_value=None),
    ):
        plugin.shutdown()

    mock_sched_stop.assert_called_once()
    mock_ui_stop.assert_called_once()


def test_plugin_properties_return_expected_types(tmp_path):
    from carconnectivity_plugins.base.plugin import BasePlugin
    from carconnectivity_plugins.campermode.plugin import Plugin

    config = {"port": 4001, "data_file": str(tmp_path / "cm.json")}
    cc = make_car_connectivity()
    with patch.object(BasePlugin, "__init__", new=_stub_base_init):
        plugin = Plugin("test_id", cc, config)

    assert isinstance(plugin.settings, CamperSettings)
    assert isinstance(plugin.state, CamperState)
    assert isinstance(plugin.timers, list)


def test_plugin_save_settings_persists_to_file(tmp_path):
    from carconnectivity_plugins.base.plugin import BasePlugin
    from carconnectivity_plugins.campermode.plugin import Plugin

    data_file = tmp_path / "cm.json"
    config = {"port": 4001, "data_file": str(data_file)}
    cc = make_car_connectivity()
    with patch.object(BasePlugin, "__init__", new=_stub_base_init):
        plugin = Plugin("test_id", cc, config)

    plugin.save_settings()

    assert data_file.exists()


def test_plugin_battery_observer_updates_scheduler(tmp_path):
    from carconnectivity.attributes import LevelAttribute
    from carconnectivity.observable import Observable
    from carconnectivity_plugins.base.plugin import BasePlugin
    from carconnectivity_plugins.campermode.plugin import Plugin

    config = {"port": 4001, "data_file": str(tmp_path / "cm.json")}
    cc = make_car_connectivity()
    with patch.object(BasePlugin, "__init__", new=_stub_base_init):
        plugin = Plugin("test_id", cc, config)

    mock_level = MagicMock(spec=LevelAttribute)
    mock_level.value = 75

    with patch.object(
        plugin._scheduler, "update_battery_level"
    ) as mock_update:
        plugin._on_battery_changed(
            mock_level, Observable.ObserverEvent.VALUE_CHANGED
        )

    mock_update.assert_called_once_with(75)


# ---------------------------------------------------------------------------
# Part B: Flask route tests
# ---------------------------------------------------------------------------


@pytest.fixture
def flask_client():
    plugin = make_mock_plugin()
    ui = CamperUI(plugin=plugin, host="127.0.0.1", port=4001)
    ui.app.config["WTF_CSRF_ENABLED"] = False
    ui.app.config["TESTING"] = True
    with ui.app.test_client() as client:
        yield client


def test_healthcheck_returns_ok(flask_client):
    resp = flask_client.get("/healthcheck")
    assert resp.status_code == 200
    assert b"ok" in resp.data


def test_dashboard_renders(flask_client):
    resp = flask_client.get("/")
    assert resp.status_code == 200
    assert b"CamperMode" in resp.data


def test_settings_get_renders(flask_client):
    resp = flask_client.get("/settings")
    assert resp.status_code == 200
    assert b"Settings" in resp.data


def test_timers_get_renders(flask_client):
    resp = flask_client.get("/timers")
    assert resp.status_code == 200
    assert b"Timer" in resp.data


def test_help_page_renders(flask_client):
    resp = flask_client.get("/help")
    assert resp.status_code == 200
    assert b"How It Works" in resp.data


def test_api_status_returns_json(flask_client):
    resp = flask_client.get("/api/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "state" in data
    assert "settings" in data


def test_start_session_post_redirects(flask_client):
    resp = flask_client.post(
        "/start",
        data={
            "min_battery_level": "20",
            "minutes_between_cycles": "10",
            "total_duration_minutes": "60",
        },
    )
    assert resp.status_code in (302, 303)


def test_stop_session_post_redirects(flask_client):
    resp = flask_client.post("/stop")
    assert resp.status_code in (302, 303)


def test_climatization_observer_forwards_to_scheduler(tmp_path):
    from carconnectivity.attributes import EnumAttribute
    from carconnectivity.climatization import Climatization
    from carconnectivity.observable import Observable
    from carconnectivity_plugins.base.plugin import BasePlugin
    from carconnectivity_plugins.campermode.plugin import Plugin

    config = {"port": 4001, "data_file": str(tmp_path / "cm.json")}
    cc = make_car_connectivity()
    with patch.object(BasePlugin, "__init__", new=_stub_base_init):
        plugin = Plugin("test_id", cc, config)

    mock_element = MagicMock(spec=EnumAttribute)
    mock_element.value = Climatization.ClimatizationState.OFF

    with patch.object(
        plugin._scheduler, "on_climatization_state_changed"
    ) as mock_handler:
        plugin._on_climatization_changed(
            mock_element, Observable.ObserverEvent.VALUE_CHANGED
        )

    mock_handler.assert_called_once_with(Climatization.ClimatizationState.OFF)


# ---------------------------------------------------------------------------
# Part C: HTTP Basic Auth tests
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_flask_client():
    """Flask test client with authentication enabled."""
    plugin = make_mock_plugin()
    ui = CamperUI(
        plugin=plugin,
        host="127.0.0.1",
        port=4001,
        users={"admin": "secret"},
    )
    ui.app.config["WTF_CSRF_ENABLED"] = False
    ui.app.config["TESTING"] = True
    with ui.app.test_client() as client:
        yield client


def _basic_auth_header(username: str, password: str) -> dict[str, str]:
    cred = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {cred}"}


def test_basic_auth_valid_credentials(auth_flask_client):
    resp = auth_flask_client.get(
        "/api/status", headers=_basic_auth_header("admin", "secret")
    )
    assert resp.status_code == 200


def test_basic_auth_wrong_password(auth_flask_client):
    resp = auth_flask_client.get(
        "/api/status", headers=_basic_auth_header("admin", "wrong")
    )
    assert resp.status_code == 401


def test_basic_auth_unknown_user(auth_flask_client):
    resp = auth_flask_client.get(
        "/api/status", headers=_basic_auth_header("nobody", "secret")
    )
    assert resp.status_code == 401


def test_basic_auth_malformed_base64(auth_flask_client):
    resp = auth_flask_client.get(
        "/api/status", headers={"Authorization": "Basic !!!invalid!!!"}
    )
    assert resp.status_code == 401


def test_basic_auth_missing_colon(auth_flask_client):
    cred = base64.b64encode(b"nocolon").decode()
    resp = auth_flask_client.get(
        "/api/status", headers={"Authorization": f"Basic {cred}"}
    )
    assert resp.status_code == 401


def test_no_auth_dashboard_redirects_to_login(auth_flask_client):
    resp = auth_flask_client.get("/")
    assert resp.status_code in (302, 303)
    assert "/login" in resp.headers.get("Location", "")


def test_no_auth_api_returns_401(auth_flask_client):
    resp = auth_flask_client.get("/api/status")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Part D: /api/settings endpoint tests
# ---------------------------------------------------------------------------


def test_api_settings_valid_post(flask_client):
    resp = flask_client.post(
        "/api/settings",
        json={
            "min_battery_level": 30,
            "minutes_between_cycles": 60,
            "total_duration_minutes": 120,
            "endless": False,
        },
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_api_settings_invalid_json(flask_client):
    resp = flask_client.post(
        "/api/settings",
        data="not json",
        content_type="application/json",
    )
    assert resp.status_code == 400


def test_api_settings_missing_key(flask_client):
    resp = flask_client.post(
        "/api/settings",
        json={"min_battery_level": 30},
    )
    assert resp.status_code == 400


def test_api_settings_battery_out_of_range(flask_client):
    resp = flask_client.post(
        "/api/settings",
        json={
            "min_battery_level": 5,
            "minutes_between_cycles": 0,
            "total_duration_minutes": 120,
            "endless": False,
        },
    )
    assert resp.status_code == 400


def test_api_settings_invalid_interval(flask_client):
    resp = flask_client.post(
        "/api/settings",
        json={
            "min_battery_level": 20,
            "minutes_between_cycles": 55,
            "total_duration_minutes": 120,
            "endless": False,
        },
    )
    assert resp.status_code == 400
