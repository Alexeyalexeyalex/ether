"""Вход, регистрация, выход."""

import random
import re
import secrets

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from .models import AVATAR_COLORS, User, db

auth_bp = Blueprint("auth", __name__)

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("views.app_page"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""

        user = User.query.filter_by(username=username).first()
        if user is None or not user.check_password(password):
            flash("Неверный логин или пароль.", "error")
            return render_template("login.html", username=username), 401
        if user.is_banned:
            flash("Аккаунт заблокирован администратором.", "error")
            return render_template("login.html", username=username), 403

        login_user(user, remember=True)
        return redirect(request.args.get("next") or url_for("views.app_page"))

    return render_template("login.html", username="")


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("views.app_page"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        display_name = (request.form.get("display_name") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""

        form = {"username": username, "display_name": display_name}

        if not USERNAME_RE.match(username):
            flash("Логин: 3–32 символа, латиница, цифры, «_», «.», «-».", "error")
            return render_template("register.html", **form), 400
        if len(password) < 6:
            flash("Пароль короче 6 символов.", "error")
            return render_template("register.html", **form), 400
        if password != confirm:
            flash("Пароли не совпадают.", "error")
            return render_template("register.html", **form), 400
        if User.query.filter_by(username=username).first():
            flash("Такой логин уже занят.", "error")
            return render_template("register.html", **form), 409

        user = User(
            username=username,
            display_name=display_name or username,
            color=random.choice(AVATAR_COLORS),
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        login_user(user, remember=True)
        return redirect(url_for("views.app_page"))

    return render_template("register.html", username="", display_name="")


@auth_bp.post("/guest")
def guest():
    """Разовый вход без регистрации. Заводим настоящую учётку с пометкой
    «гость»: сообщения и участие в комнатах всё равно ссылаются на пользователя."""
    if current_user.is_authenticated:
        return redirect(url_for("views.app_page"))

    display_name = (request.form.get("display_name") or "").strip()[:48]
    if not display_name:
        display_name = f"Гость {random.randint(1000, 9999)}"

    for _ in range(10):
        username = "guest_" + secrets.token_hex(4)
        if not User.query.filter_by(username=username).first():
            break
    else:
        flash("Не удалось создать гостевой вход, попробуйте ещё раз.", "error")
        return redirect(url_for("auth.login"))

    user = User(
        username=username,
        display_name=display_name,
        color=random.choice(AVATAR_COLORS),
        is_guest=True,
    )
    user.set_password(secrets.token_urlsafe(24))   # войти под ним заново нельзя
    db.session.add(user)
    db.session.commit()

    login_user(user, remember=False)
    return redirect(url_for("views.app_page"))


@auth_bp.route("/logout")
@login_required
def logout():
    # Гость уходит — снимаем его с участия в комнатах. Написанное остаётся:
    # удаляем только членство, сама учётка и сообщения не трогаются.
    if current_user.is_guest:
        from .events import forget_guest_rooms

        forget_guest_rooms(current_user.id)

    logout_user()
    return redirect(url_for("auth.login"))
