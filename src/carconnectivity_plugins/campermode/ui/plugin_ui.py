"""Flask web application for the CamperMode plugin."""

from __future__ import annotations

import base64
import binascii
import hmac
import logging
import math
import os
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import flask
import flask_login
from flask_bootstrap import Bootstrap5
from flask_wtf import FlaskForm
from flask_wtf.csrf import CSRFProtect
from werkzeug.serving import make_server
from wtforms import BooleanField, PasswordField, StringField, SubmitField
from wtforms.validators import Length

from carconnectivity_plugins.campermode.models import CamperTimer, PhaseState

if TYPE_CHECKING:
    from werkzeug.serving import BaseWSGIServer, _TSSLContextArg
    from werkzeug.wrappers import Response as WerkzeugResponse

    from carconnectivity_plugins.campermode.plugin import Plugin

LOG: logging.Logger = logging.getLogger("carconnectivity.plugins.campermode")


class LoginForm(FlaskForm):
    """Login form."""

    username = StringField("User", validators=[Length(min=1, max=255)])
    password = PasswordField("Password")
    remember_me = BooleanField("Remember me")
    submit = SubmitField("Login")


class CamperUI:
    """Standalone Flask web server for the CamperMode plugin."""

    def __init__(
        self,
        plugin: Plugin,
        host: str,
        port: int,
        users: dict[str, str] | None = None,
        ssl_context: _TSSLContextArg | None = None,
        secret_key: str | None = None,
    ) -> None:
        self._plugin = plugin
        self._thread: threading.Thread | None = None
        self._host = host
        self._port = port
        self._ssl_context = ssl_context
        self.server: BaseWSGIServer | None = None
        self.users: dict[str, dict[str, str]] = (
            {u: {"password": p} for u, p in users.items()} if users else {}
        )

        _ui_dir = os.path.dirname(__file__)
        self.app = flask.Flask(
            "CamperMode",
            template_folder=os.path.join(_ui_dir, "templates"),
            static_folder=os.path.join(_ui_dir, "static"),
        )
        self.app.config["SECRET_KEY"] = secret_key or uuid.uuid4().hex
        self._csrf = CSRFProtect()
        self._csrf.init_app(self.app)
        Bootstrap5(self.app)

        login_manager = flask_login.LoginManager()
        login_manager.login_view = "login"  # type: ignore[assignment]
        login_manager.login_message = "Please log in to access this page."
        login_manager.login_message_category = "info"
        login_manager.init_app(self.app)

        with self.app.app_context():
            self.app.extensions["camper_plugin"] = plugin

        # ── User loading ──────────────────────────────────────────────

        @login_manager.user_loader
        def _user_loader(
            username: str,
        ) -> flask_login.UserMixin | None:
            if username not in self.users:
                return None
            user = flask_login.UserMixin()
            user.id = username  # type: ignore[assignment]
            return user

        @login_manager.request_loader
        def _load_from_request(
            request: flask.Request,
        ) -> flask_login.UserMixin | None:
            header = request.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return None
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError):
                return None
            if ":" not in decoded:
                return None
            username, password = decoded.split(":", 1)
            stored = self.users.get(username, {}).get("password", "")
            if hmac.compare_digest(stored, password):
                user = flask_login.UserMixin()
                user.id = username  # type: ignore[assignment]
                return user
            return None

        # ── Auth guard ────────────────────────────────────────────────

        _public_endpoints: frozenset[str] = frozenset(
            {"login", "static", "healthcheck"}
        )

        @self.app.before_request
        def _check_auth() -> WerkzeugResponse | None:
            if not self.users:
                return None
            endpoint = flask.request.endpoint
            if endpoint is None or endpoint in _public_endpoints:
                return None
            if flask_login.current_user.is_authenticated:
                return None
            # Return JSON 401 for API requests so JS polling can detect
            # the auth state rather than silently receiving an HTML redirect.
            if flask.request.path.startswith("/api/"):
                return flask.make_response(
                    flask.jsonify({"error": "Unauthorized"}), 401
                )
            return flask.redirect(
                flask.url_for("login", next=flask.request.path)
            )

        # ── Context processors ────────────────────────────────────────

        @self.app.context_processor
        def _inject_active_page() -> dict[str, str | None]:
            return {"active_page": flask.request.endpoint}

        # ── Routes ────────────────────────────────────────────────────

        @self.app.route("/login", methods=["GET", "POST"])
        def login() -> WerkzeugResponse | str:
            if self.users and flask_login.current_user.is_authenticated:
                return flask.redirect(flask.url_for("account"))
            if not self.users:
                return flask.redirect(flask.url_for("dashboard"))
            form = LoginForm()
            if form.validate_on_submit():
                username = form.username.data or ""
                stored_pwd = self.users.get(username, {}).get(
                    "password", ""
                )
                if stored_pwd and hmac.compare_digest(
                    stored_pwd, form.password.data or ""
                ):
                    user = flask_login.UserMixin()
                    user.id = username  # type: ignore[assignment]
                    flask_login.login_user(
                        user, remember=bool(form.remember_me.data)
                    )
                    LOG.info("User '%s' logged in", username)
                    next_url = flask.request.args.get("next") or ""
                    parsed = urllib.parse.urlsplit(next_url)
                    if parsed.scheme or parsed.netloc:
                        next_url = flask.url_for("dashboard")
                    return flask.redirect(
                        next_url or flask.url_for("dashboard")
                    )
                form.password.data = ""
                time.sleep(1)
                LOG.warning("Failed login attempt for user '%s'", username)
                flask.flash("Unknown user or wrong password.", "danger")
            return flask.render_template("campermode/login.html", form=form)

        @self.app.route("/logout")
        def logout() -> WerkzeugResponse:
            flask_login.logout_user()
            return flask.redirect(flask.url_for("login"))

        @self.app.route("/account")
        def account() -> str:
            return flask.render_template(
                "campermode/account.html",
                auth_enabled=bool(self.users),
            )

        @self.app.route("/healthcheck")
        def healthcheck() -> str:
            return "ok"

        # ── Dashboard ─────────────────────────────────────────────────

        @self.app.route("/")
        def dashboard() -> WerkzeugResponse | str:
            plugin = self._plugin
            s = plugin.settings
            state = plugin.state
            show_warning = (
                state.active
                and state.current_phase == PhaseState.HEATING
                and s.has_active_heating
            )
            return flask.render_template(
                "campermode/dashboard.html",
                settings=s,
                state=state,
                show_warning=show_warning,
            )

        @self.app.route("/start", methods=["POST"])
        def start_session() -> WerkzeugResponse:
            plugin = self._plugin
            s = plugin.settings
            try:
                min_battery_level = int(
                    flask.request.form.get(
                        "min_battery_level", s.min_battery_level
                    )
                )
                minutes_between_cycles = int(
                    flask.request.form.get(
                        "minutes_between_cycles", s.minutes_between_cycles
                    )
                )
                endless = "endless" in flask.request.form
                total_duration_minutes = s.total_duration_minutes
                if not endless:
                    total_duration_minutes = int(
                        flask.request.form.get(
                            "total_duration_minutes",
                            s.total_duration_minutes,
                        )
                    )
            except (ValueError, TypeError):
                flask.flash("Invalid form values.", "danger")
                return flask.redirect(flask.url_for("dashboard"))
            plugin.scheduler.update_settings(
                min_battery_level=min_battery_level,
                minutes_between_cycles=minutes_between_cycles,
                total_duration_minutes=total_duration_minutes,
                endless=endless,
            )
            ok = plugin.scheduler.start_session(reason="manual")
            LOG.info("Session start requested from UI (ok=%s)", ok)
            if ok:
                plugin.save_settings()
                flask.flash("Camper mode started.", "success")
            else:
                flask.flash(
                    "Could not start camper mode. Check logs for details.",
                    "danger",
                )
            return flask.redirect(flask.url_for("dashboard"))

        @self.app.route("/stop", methods=["POST"])
        def stop_session() -> WerkzeugResponse:
            LOG.info("Session stop requested from UI")
            self._plugin.scheduler.stop_session(reason="manual")
            flask.flash("Camper mode stopped.", "info")
            return flask.redirect(flask.url_for("dashboard"))

        @self.app.route("/api/status")
        def api_status() -> WerkzeugResponse:
            plugin = self._plugin
            s = plugin.settings
            state = plugin.state
            return flask.jsonify(
                {
                    "state": state.to_dict(),
                    "settings": s.to_dict(),
                    "show_warning": (
                        state.active
                        and state.current_phase == PhaseState.HEATING
                        and s.has_active_heating
                    ),
                }
            )

        _valid_intervals: frozenset[int] = frozenset({0, 30, 45, 60, 75, 90})

        @self.app.route("/api/settings", methods=["POST"])
        def api_settings() -> WerkzeugResponse:
            plugin = self._plugin
            data = flask.request.get_json(silent=True)
            if not isinstance(data, dict):
                return flask.make_response(
                    flask.jsonify({"error": "Invalid JSON"}), 400
                )
            try:
                min_battery_level = int(data["min_battery_level"])
                minutes_between_cycles = int(data["minutes_between_cycles"])
                total_duration_minutes = int(data["total_duration_minutes"])
                endless = data["endless"]
                if not isinstance(endless, bool):
                    raise TypeError("endless must be a boolean")
            except (KeyError, ValueError, TypeError):
                return flask.make_response(
                    flask.jsonify({"error": "Invalid parameters"}), 400
                )
            if not (10 <= min_battery_level <= 90):
                return flask.make_response(
                    flask.jsonify({"error": "min_battery_level out of range"}),
                    400,
                )
            if minutes_between_cycles not in _valid_intervals:
                return flask.make_response(
                    flask.jsonify({"error": "minutes_between_cycles invalid"}),
                    400,
                )
            if not (60 <= total_duration_minutes <= 720):
                return flask.make_response(
                    flask.jsonify(
                        {"error": "total_duration_minutes out of range"}
                    ),
                    400,
                )
            plugin.scheduler.update_settings(
                min_battery_level=min_battery_level,
                minutes_between_cycles=minutes_between_cycles,
                total_duration_minutes=total_duration_minutes,
                endless=endless,
            )
            plugin.save_settings()
            return flask.jsonify({"ok": True})

        @self.app.route("/settings", methods=["GET", "POST"])
        def settings() -> WerkzeugResponse | str:
            plugin = self._plugin
            s = plugin.settings
            if flask.request.method == "POST":
                try:
                    window_heating = "window_heating" in flask.request.form
                    front_zone_left = "front_zone_left" in flask.request.form
                    front_zone_right = "front_zone_right" in flask.request.form
                    rear_zone_left = "rear_zone_left" in flask.request.form
                    rear_zone_right = "rear_zone_right" in flask.request.form
                    target_temperature = float(
                        flask.request.form.get(
                            "target_temperature",
                            s.target_temperature,
                        )
                    )
                except (ValueError, TypeError):
                    flask.flash("Invalid form values.", "danger")
                    return flask.redirect(flask.url_for("settings"))
                if not math.isfinite(target_temperature) or not (
                    15.5 <= target_temperature <= 30.0
                ):
                    flask.flash(
                        "Temperature must be between 15.5 and 30.0 °C.",
                        "danger",
                    )
                    return flask.redirect(flask.url_for("settings"))
                plugin.scheduler.update_climate_settings(
                    window_heating=window_heating,
                    front_zone_left=front_zone_left,
                    front_zone_right=front_zone_right,
                    rear_zone_left=rear_zone_left,
                    rear_zone_right=rear_zone_right,
                    target_temperature=target_temperature,
                )
                plugin.save_settings()
                LOG.info("Climate settings updated from UI")
                if plugin.state.active:
                    flask.flash(
                        "Settings saved. Zone settings take effect at the"
                        " next session start; temperature applies at the"
                        " next heating cycle.",
                        "info",
                    )
                else:
                    flask.flash("Settings saved.", "success")
                return flask.redirect(flask.url_for("settings"))
            return flask.render_template(
                "campermode/settings.html",
                current_settings=s,
            )

        @self.app.route("/timers", methods=["GET", "POST"])
        def timers() -> WerkzeugResponse | str:
            plugin = self._plugin
            if flask.request.method == "POST":
                time_str = flask.request.form.get("time", "")
                try:
                    timer_time = datetime.strptime(time_str, "%H:%M").time()
                except ValueError:
                    flask.flash("Invalid time format.", "danger")
                    return flask.redirect(flask.url_for("timers"))
                days_raw = flask.request.form.getlist("days_of_week")
                try:
                    days = [int(d) for d in days_raw]
                except ValueError:
                    flask.flash("Invalid days of week.", "danger")
                    return flask.redirect(flask.url_for("timers"))
                if not days:
                    flask.flash(
                        "Select at least one day of the week.", "danger"
                    )
                    return flask.redirect(flask.url_for("timers"))
                if not all(0 <= d <= 6 for d in days):
                    flask.flash("Invalid day of week value.", "danger")
                    return flask.redirect(flask.url_for("timers"))
                repeat_weekly = "repeat_weekly" in flask.request.form
                timer = CamperTimer(
                    id=uuid.uuid4().hex,
                    time=timer_time,
                    days_of_week=days,
                    repeat_weekly=repeat_weekly,
                    enabled=True,
                    created_at=datetime.now(tz=timezone.utc),
                )
                if not plugin.scheduler.add_timer(timer):
                    flask.flash("Timer limit reached (max 50).", "danger")
                    return flask.redirect(flask.url_for("timers"))
                plugin.save_settings()
                LOG.info(
                    "Timer created: id=%s time=%s days=%s",
                    timer.id,
                    timer.time,
                    days,
                )
                flask.flash("Timer created.", "success")
                return flask.redirect(flask.url_for("timers"))
            return flask.render_template(
                "campermode/timers.html",
                timers=plugin.timers,
            )

        @self.app.route("/timers/<timer_id>/delete", methods=["POST"])
        def timers_delete(timer_id: str) -> WerkzeugResponse:
            if len(timer_id) > 64:
                flask.abort(400)
            plugin = self._plugin
            removed = plugin.scheduler.delete_timer(timer_id)
            if removed:
                plugin.save_settings()
                LOG.info("Timer %s deleted from UI", timer_id)
                flask.flash("Timer deleted.", "success")
            else:
                LOG.warning("Delete requested for unknown timer: %s", timer_id)
                flask.flash("Timer not found.", "danger")
            return flask.redirect(flask.url_for("timers"))

        @self.app.route("/timers/<timer_id>/toggle", methods=["POST"])
        def timers_toggle(timer_id: str) -> WerkzeugResponse:
            if len(timer_id) > 64:
                flask.abort(400)
            plugin = self._plugin
            found = plugin.scheduler.toggle_timer(timer_id)
            if found:
                plugin.save_settings()
                LOG.info("Timer %s toggled from UI", timer_id)
                flask.flash("Timer updated.", "success")
            else:
                LOG.warning("Toggle requested for unknown timer: %s", timer_id)
                flask.flash("Timer not found.", "danger")
            return flask.redirect(flask.url_for("timers"))

        @self.app.route("/help")
        def help_page() -> str:
            return flask.render_template("campermode/help.html")

    def start(self) -> None:
        """Start the web server in a daemon thread."""
        self.server = make_server(
            self._host,
            self._port,
            self.app,
            threaded=True,
            ssl_context=self._ssl_context,
        )
        LOG.info(
            "CamperMode web server starting on %s:%d",
            self._host,
            self._port,
        )
        self._thread = threading.Thread(target=self.server.serve_forever)
        self._thread.name = "carconnectivity.plugins.campermode-webthread"
        self._thread.daemon = True
        self._thread.start()

    def stop(self) -> None:
        """Shut down the web server if running."""
        if (
            self.server is not None
            and self._thread is not None
            and self._thread.is_alive()
        ):
            LOG.info("CamperMode web server shutting down")
            self.server.shutdown()
            self._thread.join(timeout=5)
