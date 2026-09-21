"""Страницы приложения и REST API."""

import base64
import json
import os
import secrets

from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from .models import Channel, Membership, Message, Room, User, VoiceBan, db
from .paths import AVATAR_DIR

views_bp = Blueprint("views", __name__)

MAX_MESSAGE_LENGTH = 2000
HISTORY_PAGE = 50
AVATAR_MAX_BYTES = 400 * 1024

# первые байты файла — по ним и определяем формат, расширению из браузера не верим
IMAGE_KINDS = [
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
]


# --------------------------------------------------------------------------- страницы


@views_bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("views.app_page"))
    return redirect(url_for("auth.login"))


@views_bp.get("/favicon.ico")
def favicon():
    """Браузеры и плитки Windows дёргают этот путь напрямую, минуя разметку."""
    return send_from_directory(
        os.path.join(current_app.root_path, "static"),
        "favicon.ico",
        mimetype="image/vnd.microsoft.icon",
    )


@views_bp.route("/app")
@login_required
def app_page():
    return render_template("app.html", me=current_user.to_dict())


@views_bp.get("/api/rtc-config")
@login_required
def rtc_configuration():
    from .rtc_config import ice_config

    response = jsonify(ice_config(current_user.id))
    response.headers["Cache-Control"] = "no-store"
    return response


# --------------------------------------------------------------------------- помощники


def membership_of(room_id: int, user_id: int | None = None) -> Membership | None:
    return Membership.query.filter_by(
        room_id=room_id, user_id=user_id or current_user.id
    ).first()


def role_of(room: Room, user_id: int | None = None) -> str | None:
    """admin | owner | moderator | member | None."""
    user_id = user_id or current_user.id
    if user_id == current_user.id and current_user.is_admin:
        return "admin"
    if room.owner_id == user_id:
        return "owner"
    member = membership_of(room.id, user_id)
    return member.role if member else None


def can_manage(room: Room) -> bool:
    """Создавать и удалять каналы: владелец, модератор, админ сайта."""
    return role_of(room) in ("admin", "owner", "moderator")


def is_boss(room: Room) -> bool:
    """Удалять комнату и назначать модераторов: только владелец и админ."""
    return role_of(room) in ("admin", "owner")


def name_taken(name: str, exclude_id: int | None = None) -> bool:
    """Названия комнат уникальны, регистр не учитывается.

    Сравниваем в Python: SQL-функции lower() и COLLATE NOCASE в SQLite
    складывают только латиницу, поэтому «Главная» и «главная» база считает
    разными названиями. casefold() работает с любым алфавитом.
    """
    target = name.strip().casefold()
    query = Room.query
    if exclude_id:
        query = query.filter(Room.id != exclude_id)
    return any(room.name.strip().casefold() == target for room in query.all())


def require_member(room_id: int):
    """Возвращает (room, error_response)."""
    room = db.session.get(Room, room_id)
    if room is None:
        return None, (jsonify(error="Комната не найдена"), 404)
    if not membership_of(room_id) and not current_user.is_admin:
        return None, (jsonify(error="Вы не состоите в этой комнате"), 403)
    return room, None


# --------------------------------------------------------------------------- API


@views_bp.get("/api/me")
@login_required
def api_me():
    return jsonify(current_user.to_dict())


@views_bp.get("/api/rooms")
@login_required
def api_rooms():
    counts = dict(
        db.session.query(Membership.room_id, func.count(Membership.id))
        .group_by(Membership.room_id)
        .all()
    )
    joined = {m.room_id: m.role for m in Membership.query.filter_by(user_id=current_user.id)}

    rooms = []
    for room in Room.query.order_by(Room.created_at.asc()).all():
        payload = room.to_dict(member_count=counts.get(room.id, 0))
        payload["joined"] = room.id in joined
        payload["role"] = joined.get(room.id)
        rooms.append(payload)
    return jsonify(rooms=rooms)


@views_bp.post("/api/rooms")
@login_required
def api_create_room():
    if current_user.is_guest:
        return jsonify(error="Гости не создают комнаты — зарегистрируйтесь"), 403

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    topic = (data.get("topic") or "").strip()[:160]
    password = (data.get("password") or "").strip()

    if not 2 <= len(name) <= 64:
        return jsonify(error="Название комнаты: от 2 до 64 символов"), 400
    if name_taken(name):
        return jsonify(error=f"Комната «{name}» уже есть — выберите другое название"), 409
    if password and len(password) < 4:
        return jsonify(error="Пароль комнаты короче 4 символов"), 400

    room = Room(
        name=name,
        topic=topic,
        owner_id=current_user.id,
        guests_allowed=data.get("guests_allowed") is not False,
    )
    room.set_password(password or None)
    db.session.add(room)
    db.session.flush()

    db.session.add_all(
        [
            Channel(room_id=room.id, name="общий", kind="text", position=0),
            Channel(room_id=room.id, name="Голосовой", kind="voice", position=1),
        ]
    )
    db.session.add(Membership(user_id=current_user.id, room_id=room.id, role="owner"))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(error=f"Комната «{name}» уже есть — выберите другое название"), 409

    payload = room.to_dict(member_count=1)
    payload["joined"] = True
    payload["role"] = "owner"
    return jsonify(room=payload), 201


@views_bp.post("/api/rooms/<int:room_id>/join")
@login_required
def api_join_room(room_id: int):
    room = db.session.get(Room, room_id)
    if room is None:
        return jsonify(error="Комната не найдена"), 404
    if membership_of(room_id):
        return jsonify(ok=True, already=True)

    if current_user.is_guest and not room.allows_guests:
        return jsonify(error="В эту комнату гостей не пускают"), 403

    data = request.get_json(silent=True) or {}
    if room.locked and not room.check_password(data.get("password") or ""):
        return jsonify(error="Пароль не подходит"), 403

    db.session.add(Membership(user_id=current_user.id, room_id=room_id, role="member"))
    db.session.commit()
    return jsonify(ok=True)


@views_bp.post("/api/rooms/<int:room_id>/leave")
@login_required
def api_leave_room(room_id: int):
    member = membership_of(room_id)
    if member is None:
        return jsonify(ok=True)
    room = db.session.get(Room, room_id)
    if room and room.owner_id == current_user.id:
        return jsonify(error="Владелец не может выйти. Удалите комнату в админке."), 400
    db.session.delete(member)
    db.session.commit()
    return jsonify(ok=True)


@views_bp.patch("/api/rooms/<int:room_id>")
@login_required
def api_update_room(room_id: int):
    """Настройки комнаты. Меняет только владелец или админ сайта."""
    room = db.session.get(Room, room_id)
    if room is None:
        return jsonify(error="Комната не найдена"), 404
    if not is_boss(room):
        return jsonify(error="Настройки меняет владелец комнаты"), 403

    data = request.get_json(silent=True) or {}

    if "name" in data:
        name = (data.get("name") or "").strip()
        if not 2 <= len(name) <= 64:
            return jsonify(error="Название комнаты: от 2 до 64 символов"), 400
        if name_taken(name, exclude_id=room_id):
            return jsonify(error=f"Комната «{name}» уже есть — выберите другое название"), 409
        room.name = name

    if "topic" in data:
        room.topic = (data.get("topic") or "").strip()[:160]

    if "guests_allowed" in data:
        room.guests_allowed = bool(data.get("guests_allowed"))

    if "locked" in data:
        password = (data.get("password") or "").strip()
        if not data.get("locked"):
            room.set_password(None)
        elif password:
            if len(password) < 4:
                return jsonify(error="Пароль комнаты короче 4 символов"), 400
            room.set_password(password)
        elif not room.locked:
            return jsonify(error="Укажите пароль или снимите галочку"), 400

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(error="Такое название уже занято"), 409

    from . import socketio

    payload = room.to_dict()
    socketio.emit("room_updated", {"room": payload}, to=f"room:{room_id}")
    return jsonify(room=payload)


@views_bp.delete("/api/rooms/<int:room_id>")
@login_required
def api_delete_room(room_id: int):
    room = db.session.get(Room, room_id)
    if room is None:
        return jsonify(error="Комната не найдена"), 404
    if not is_boss(room):
        return jsonify(error="Удалить комнату может только владелец или админ сайта"), 403
    db.session.delete(room)
    db.session.commit()
    return jsonify(ok=True)


@views_bp.get("/api/rooms/<int:room_id>")
@login_required
def api_room_detail(room_id: int):
    room, error = require_member(room_id)
    if error:
        return error

    members = (
        db.session.query(User, Membership.role)
        .join(Membership, Membership.user_id == User.id)
        .filter(Membership.room_id == room_id)
        .order_by(Membership.joined_at.asc())
        .all()
    )

    my_role = role_of(room)
    listed = []
    for user, role in members:
        # владелец комнаты всегда показывается владельцем, даже если
        # в таблице участий у него по какой-то причине другая роль
        listed.append(dict(user.to_dict(), role="owner" if user.id == room.owner_id else role))

    bans = (
        VoiceBan.query.join(Channel, Channel.id == VoiceBan.channel_id)
        .filter(Channel.room_id == room_id)
        .all()
    )

    return jsonify(
        room=room.to_dict(member_count=len(members)),
        bans=[b.to_dict() for b in bans],
        my_role=my_role,
        can_manage=can_manage(room),
        is_boss=is_boss(room),
        channels=[c.to_dict() for c in room.channels],
        members=listed,
    )


@views_bp.post("/api/rooms/<int:room_id>/channels")
@login_required
def api_create_channel(room_id: int):
    room, error = require_member(room_id)
    if error:
        return error
    if not can_manage(room):
        return jsonify(error="Каналы создаёт владелец комнаты"), 403

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    kind = data.get("kind") if data.get("kind") in ("text", "voice") else "text"
    if not 1 <= len(name) <= 48:
        return jsonify(error="Название канала: от 1 до 48 символов"), 400

    position = (db.session.query(func.max(Channel.position)).filter_by(room_id=room_id).scalar() or 0) + 1
    channel = Channel(room_id=room_id, name=name, kind=kind, position=position)
    db.session.add(channel)
    db.session.commit()
    return jsonify(channel=channel.to_dict()), 201


@views_bp.delete("/api/channels/<int:channel_id>")
@login_required
def api_delete_channel(channel_id: int):
    channel = db.session.get(Channel, channel_id)
    if channel is None:
        return jsonify(error="Канал не найден"), 404
    if not can_manage(channel.room):
        return jsonify(error="Удалять каналы может владелец комнаты"), 403
    db.session.delete(channel)
    db.session.commit()
    return jsonify(ok=True)


@views_bp.post("/api/rooms/<int:room_id>/members/<int:user_id>/role")
@login_required
def api_set_role(room_id: int, user_id: int):
    room = db.session.get(Room, room_id)
    if room is None:
        return jsonify(error="Комната не найдена"), 404
    if not is_boss(room):
        return jsonify(error="Назначать модераторов может владелец комнаты"), 403

    data = request.get_json(silent=True) or {}
    role = data.get("role")
    if role not in ("moderator", "member"):
        return jsonify(error="Роль может быть «moderator» или «member»"), 400

    member = membership_of(room_id, user_id)
    if member is None:
        return jsonify(error="Этот человек не состоит в комнате"), 404
    if user_id == room.owner_id:
        return jsonify(error="Роль владельца комнаты не меняется"), 400
    if role == "moderator" and member.user and member.user.is_guest:
        return jsonify(error="Гостя нельзя сделать модератором"), 400

    member.role = role
    db.session.commit()

    from . import socketio
    from .events import sync_role

    sync_role(room_id, user_id, role)
    socketio.emit(
        "role_changed",
        {"room_id": room_id, "user_id": user_id, "role": role},
        to=f"room:{room_id}",
    )
    return jsonify(ok=True, role=role)


@views_bp.delete("/api/rooms/<int:room_id>/members/<int:user_id>")
@login_required
def api_remove_member(room_id: int, user_id: int):
    """Убрать человека из комнаты. Не навсегда: открытую комнату он откроет
    заново сам, в закрытую войдёт по паролю."""
    room = db.session.get(Room, room_id)
    if room is None:
        return jsonify(error="Комната не найдена"), 404

    my_role = role_of(room)
    if my_role not in ("admin", "owner", "moderator"):
        return jsonify(error="Убирать участников может владелец, модератор или админ"), 403
    if user_id == current_user.id:
        return jsonify(error="Себя убрать нельзя — есть кнопка «Выйти»"), 400

    member = membership_of(room_id, user_id)
    if member is None:
        return jsonify(error="Этот человек не состоит в комнате"), 404

    target_role = role_of(room, user_id)
    target = db.session.get(User, user_id)
    if user_id == room.owner_id or (target is not None and target.is_admin):
        return jsonify(error="Владельца комнаты и админа сайта убрать нельзя"), 403
    if my_role == "moderator" and target_role == "moderator":
        return jsonify(error="Модератора может убрать только владелец или админ"), 403

    db.session.delete(member)
    db.session.commit()

    from . import socketio

    name = current_user.display_name or current_user.username
    socketio.emit("member_left", {"room_id": room_id, "user_id": user_id}, to=f"room:{room_id}")
    socketio.emit(
        "removed_from_room",
        {"room_id": room_id, "room_name": room.name, "by": name},
        to=f"user:{user_id}",
    )
    return jsonify(ok=True)


@views_bp.get("/api/channels/<int:channel_id>/messages")
@login_required
def api_messages(channel_id: int):
    channel = db.session.get(Channel, channel_id)
    if channel is None:
        return jsonify(error="Канал не найден"), 404
    _, error = require_member(channel.room_id)
    if error:
        return error

    query = Message.query.filter_by(channel_id=channel_id)
    before = request.args.get("before", type=int)
    if before:
        query = query.filter(Message.id < before)

    rows = query.order_by(Message.id.desc()).limit(HISTORY_PAGE).all()
    rows.reverse()
    return jsonify(
        messages=[m.to_dict() for m in rows],
        has_more=len(rows) == HISTORY_PAGE,
    )


@views_bp.delete("/api/messages/<int:message_id>")
@login_required
def api_delete_message(message_id: int):
    message = db.session.get(Message, message_id)
    if message is None:
        return jsonify(ok=True)
    room = message.channel.room
    if message.user_id != current_user.id and not can_manage(room):
        return jsonify(error="Можно удалять только свои сообщения"), 403

    channel_id = message.channel_id
    db.session.delete(message)
    db.session.commit()

    from . import socketio

    socketio.emit("message_deleted", {"id": message_id, "channel_id": channel_id},
                  to=f"text:{channel_id}")
    return jsonify(ok=True)


@views_bp.patch("/api/profile")
@login_required
def api_profile():
    data = request.get_json(silent=True) or {}

    if "display_name" in data:
        display_name = (data.get("display_name") or "").strip()
        if not 2 <= len(display_name) <= 48:
            return jsonify(error="Имя: от 2 до 48 символов"), 400
        current_user.display_name = display_name

    color = (data.get("color") or "").strip()
    if color.startswith("#") and len(color) in (4, 7):
        current_user.color = color

    new_password = data.get("new_password") or ""
    if new_password:
        if current_user.is_guest:
            return jsonify(error="У гостевой учётки нет пароля — заведите аккаунт"), 403
        if not current_user.check_password(data.get("current_password") or ""):
            return jsonify(error="Текущий пароль введён неверно"), 403
        if len(new_password) < 6:
            return jsonify(error="Новый пароль короче 6 символов"), 400
        current_user.set_password(new_password)

    db.session.commit()
    broadcast_profile()
    return jsonify(user=current_user.to_dict())


def broadcast_profile() -> None:
    """Имя и аватарка видны всем, поэтому рассылаем обновление всем сессиям."""
    from . import socketio

    socketio.emit("profile_updated", {"user": current_user.to_dict()})


def drop_avatar_file(name: str | None) -> None:
    """Старый файл удаляем сразу — иначе папка растёт с каждой сменой картинки."""
    if not name:
        return
    try:
        os.remove(os.path.join(AVATAR_DIR, name))
    except OSError:
        pass


@views_bp.post("/api/profile/avatar")
@login_required
def api_avatar_upload():
    if current_user.is_guest:
        return jsonify(error="Гостям аватарки недоступны — заведите аккаунт"), 403

    data = request.get_json(silent=True) or {}
    payload = data.get("image") or ""
    if "," in payload:
        payload = payload.split(",", 1)[1]

    try:
        raw = base64.b64decode(payload, validate=True)
    except (ValueError, TypeError):
        return jsonify(error="Картинку прочитать не удалось"), 400

    if not raw:
        return jsonify(error="Пустой файл"), 400
    if len(raw) > AVATAR_MAX_BYTES:
        return jsonify(error="Картинка тяжелее 400 КБ"), 413

    extension = next((ext for magic, ext in IMAGE_KINDS if raw.startswith(magic)), None)
    if extension is None:
        return jsonify(error="Поддерживаются JPEG, PNG и GIF"), 415

    os.makedirs(AVATAR_DIR, exist_ok=True)
    name = f"{current_user.id}_{secrets.token_hex(6)}.{extension}"
    with open(os.path.join(AVATAR_DIR, name), "wb") as fh:
        fh.write(raw)

    previous = current_user.avatar
    current_user.avatar = name
    db.session.commit()
    drop_avatar_file(previous)

    broadcast_profile()
    return jsonify(user=current_user.to_dict())


@views_bp.delete("/api/profile/avatar")
@login_required
def api_avatar_remove():
    previous = current_user.avatar
    current_user.avatar = None
    db.session.commit()
    drop_avatar_file(previous)
    broadcast_profile()
    return jsonify(user=current_user.to_dict())


@views_bp.get("/avatars/<path:filename>")
def avatar_file(filename: str):
    return send_from_directory(AVATAR_DIR, filename, max_age=86400)


# --------------------------------------------------------------------------- личная раскладка


@views_bp.get("/api/layout")
@login_required
def api_layout_get():
    try:
        layout = json.loads(current_user.room_layout or "{}")
    except json.JSONDecodeError:
        layout = {}
    return jsonify(layout=layout if isinstance(layout, dict) else {})


@views_bp.put("/api/layout")
@login_required
def api_layout_put():
    """Папки и порядок комнат. Личное дело каждого — другим не показывается."""
    data = request.get_json(silent=True) or {}
    layout = data.get("layout")
    if not isinstance(layout, dict):
        return jsonify(error="Ожидается объект раскладки"), 400

    encoded = json.dumps(layout, ensure_ascii=False)
    if len(encoded) > 64_000:
        return jsonify(error="Раскладка слишком большая"), 413

    current_user.room_layout = encoded
    db.session.commit()
    return jsonify(ok=True)


# --------------------------------------------------------------------------- каналы


@views_bp.patch("/api/channels/<int:channel_id>")
@login_required
def api_rename_channel(channel_id: int):
    channel = db.session.get(Channel, channel_id)
    if channel is None:
        return jsonify(error="Канал не найден"), 404
    if not can_manage(channel.room):
        return jsonify(error="Переименовать канал может владелец, модератор или админ"), 403

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not 1 <= len(name) <= 48:
        return jsonify(error="Название канала: от 1 до 48 символов"), 400

    channel.name = name
    db.session.commit()
    push_channels(channel.room)
    return jsonify(channel=channel.to_dict())


@views_bp.post("/api/rooms/<int:room_id>/channels/order")
@login_required
def api_reorder_channels(room_id: int):
    room = db.session.get(Room, room_id)
    if room is None:
        return jsonify(error="Комната не найдена"), 404
    if not can_manage(room):
        return jsonify(error="Менять порядок каналов может владелец, модератор или админ"), 403

    order = (request.get_json(silent=True) or {}).get("order")
    if not isinstance(order, list):
        return jsonify(error="Ожидается список каналов"), 400

    own = {c.id: c for c in room.channels}
    if {int(x) for x in order if str(x).isdigit()} - set(own):
        return jsonify(error="В списке есть чужие каналы"), 400

    for position, channel_id in enumerate(order):
        channel = own.get(int(channel_id))
        if channel is not None:
            channel.position = position
    db.session.commit()

    push_channels(room)
    return jsonify(channels=[c.to_dict() for c in room.channels])


def push_channels(room: Room) -> None:
    """Каналы общие, поэтому изменения сразу уходят всем в комнате."""
    from . import socketio

    db.session.refresh(room)
    socketio.emit(
        "channels_updated",
        {"room_id": room.id, "channels": [c.to_dict() for c in room.channels]},
        to=f"room:{room.id}",
    )
