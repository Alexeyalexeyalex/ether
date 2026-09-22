"""Фабрика приложения."""

import os
import secrets

from flask import Flask, jsonify, render_template, request
from flask_login import LoginManager
from flask_socketio import SocketIO

from .models import User, db
from .paths import DB_URI, INSTANCE_DIR, SECRET_FILE, ensure_instance_dir

login_manager = LoginManager()
socketio = SocketIO(async_mode="threading", async_handlers=False, ping_timeout=30)


def _secret_key() -> str:
    """Постоянный ключ сессий, чтобы вход не слетал при перезапуске."""
    ensure_instance_dir()
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, "r", encoding="utf-8") as fh:
            saved = fh.read().strip()
        if saved:
            return saved
    key = secrets.token_hex(32)
    with open(SECRET_FILE, "w", encoding="utf-8") as fh:
        fh.write(key)
    return key


def create_app(debug: bool = False) -> Flask:
    ensure_instance_dir()

    app = Flask(__name__, instance_path=INSTANCE_DIR)
    app.config.update(
        SECRET_KEY=_secret_key(),
        SQLALCHEMY_DATABASE_URI=DB_URI,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={
            "pool_pre_ping": True,
            # Обработчики Socket.IO выполняются в разных потоках, а запись
            # в SQLite сериализуется — отсюда общий пул и ожидание блокировки.
            "connect_args": {"check_same_thread": False, "timeout": 15},
        },
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        SESSION_COOKIE_SAMESITE="Lax",
        TEMPLATES_AUTO_RELOAD=debug,
    )
    app.debug = debug
    app.json.ensure_ascii = False

    db.init_app(app)

    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Сначала войдите в аккаунт."
    login_manager.login_message_category = "warning"

    @login_manager.user_loader
    def load_user(user_id: str):
        return db.session.get(User, int(user_id))

    from .admin import admin_bp
    from .auth import auth_bp
    from .views import views_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(views_bp)
    app.register_blueprint(admin_bp)

    from . import events  # noqa: F401  (регистрирует обработчики Socket.IO)
    from . import relay  # noqa: F401  (резервная передача медиа)

    socketio.init_app(app)

    @app.before_request
    def kick_banned():
        from flask_login import current_user, logout_user

        if current_user.is_authenticated and current_user.is_banned:
            logout_user()
            if request.path.startswith("/api/"):
                return jsonify(error="Аккаунт заблокирован"), 403
            return render_template("error.html", code=403, message="Аккаунт заблокирован"), 403
        return None

    @app.errorhandler(404)
    def not_found(_):
        if request.path.startswith("/api/"):
            return jsonify(error="Такого адреса нет"), 404
        return render_template("error.html", code=404, message="Страница не найдена"), 404

    @app.errorhandler(403)
    def forbidden(_):
        if request.path.startswith("/api/"):
            return jsonify(error="Нет доступа"), 403
        return render_template("error.html", code=403, message="Доступ закрыт"), 403

    return app
