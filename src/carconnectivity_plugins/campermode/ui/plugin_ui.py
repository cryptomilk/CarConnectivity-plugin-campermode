"""Flask web application for the CamperMode plugin."""

from __future__ import annotations

import base64
import binascii
import logging
import os
import threading
import time
import urllib.parse
import uuid
from typing import TYPE_CHECKING

import flask
import flask_login
from flask_bootstrap import Bootstrap5
from flask_wtf import FlaskForm
from flask_wtf.csrf import CSRFProtect
from werkzeug.serving import make_server
from wtforms import BooleanField, PasswordField, StringField, SubmitField
from wtforms.validators import Length

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
            if self.users.get(username, {}).get("password") == password:
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
                stored_pwd = self.users.get(username, {}).get("password")
                if stored_pwd is not None and stored_pwd == form.password.data:
                    user = flask_login.UserMixin()
                    user.id = username  # type: ignore[assignment]
                    flask_login.login_user(
                        user, remember=bool(form.remember_me.data)
                    )
                    next_url = flask.request.args.get("next") or ""
                    parsed = urllib.parse.urlsplit(next_url)
                    if parsed.scheme or parsed.netloc:
                        next_url = flask.url_for("dashboard")
                    return flask.redirect(next_url or flask.url_for("dashboard"))
                form.password.data = ""
                time.sleep(1)
                flask.flash("Unknown user or wrong password.", "danger")
            return flask.render_template("campermode/login.html", form=form)

        @self.app.route("/logout")
        def logout() -> WerkzeugResponse:
            flask_login.logout_user()
            return flask.redirect(flask.url_for("login"))

        @self.app.route("/account")
        def account() -> str:
            return flask.render_template("campermode/account.html")

        @self.app.route("/healthcheck")
        def healthcheck() -> str:
            return "ok"

        # ── Stub routes (expanded in phases 5-8) ─────────────────────

        @self.app.route("/")
        def dashboard() -> str:
            return "Dashboard (Phase 5 pending)"

        @self.app.route("/settings")
        def settings() -> str:
            return "Settings (Phase 6 pending)"

        @self.app.route("/timers")
        def timers() -> str:
            return "Timers (Phase 7 pending)"

        @self.app.route("/help")
        def help_page() -> str:
            return "Help (Phase 8 pending)"

    def start(self) -> None:
        """Start the web server in a daemon thread."""
        self.server = make_server(
            self._host, self._port, self.app,
            threaded=True, ssl_context=self._ssl_context,
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
            self.server.shutdown()
            self._thread.join(timeout=5)
