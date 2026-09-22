"""Реальное время: чат, присутствие в голосовых каналах, сигналинг WebRTC.

Основной маршрут медиа — WebRTC между браузерами (mesh). Недоступные пары
используют резервную пересылку через app.relay по соединению сайта.
Этот модуль хранит каналы, присутствие, права и сигналинг SDP/ICE.
"""

import threading
import time
import secrets

from flask import current_app, request
from flask_login import current_user
from flask_socketio import emit, join_room, leave_room

from . import socketio
from .models import Channel, Membership, Room, User, VoiceBan, db

MAX_MESSAGE_LENGTH = 2000

# channel_id -> { sid: {...состояние участника...} }
voice_rooms: dict[int, dict[str, dict]] = {}
# sid -> {"channel_id": int, "room_id": int, "user_id": int}
sid_voice: dict[str, dict] = {}
# channel_id -> {"user_id": int, "name": str, "role": str} — кто включил тишину
mic_locks: dict[int, dict] = {}
# user_id -> набор соединений: вкладок может быть несколько, гасим статус
# только когда закрылась последняя
online_sids: dict[int, set[str]] = {}
# user_id -> когда гость закрыл последнюю вкладку; выдерживаем паузу,
# иначе обычная перезагрузка страницы выкидывала бы его из комнат
guest_left_at: dict[int, float] = {}
GUEST_GRACE = 60  # секунд
lock = threading.RLock()
_sweeper_started = False


def forget_guest_rooms(user_id: int) -> list[int]:
    """Убирает гостя из участников комнат. Сообщения остаются на месте —
    удаляем только участие."""
    memberships = Membership.query.filter_by(user_id=user_id).all()
    rooms = [m.room_id for m in memberships]
    for membership in memberships:
        db.session.delete(membership)
    if rooms:
        db.session.commit()
    for room_id in rooms:
        socketio.emit("member_left", {"room_id": room_id, "user_id": user_id},
                      to=f"room:{room_id}")
    return rooms


def sweep_absent_guests() -> None:
    now = time.time()
    with lock:
        due = [
            user_id for user_id, left in guest_left_at.items()
            if now - left >= GUEST_GRACE and user_id not in online_sids
        ]
        for user_id in due:
            guest_left_at.pop(user_id, None)

    for user_id in due:
        user = db.session.get(User, user_id)
        if user is not None and user.is_guest:
            forget_guest_rooms(user_id)


def guest_sweeper(app) -> None:
    while True:
        socketio.sleep(20)
        try:
            with app.app_context():
                sweep_absent_guests()
        except Exception:  # noqa: BLE001
            pass  # фоновая уборка не должна ронять сервер


def start_sweeper() -> None:
    global _sweeper_started
    if _sweeper_started:
        return
    _sweeper_started = True
    socketio.start_background_task(guest_sweeper, current_app._get_current_object())


# --------------------------------------------------------------------------- права


def role_in_room(room_id: int, user=None) -> str | None:
    """admin | owner | moderator | member | None."""
    user = user or current_user
    if getattr(user, "is_admin", False):
        return "admin"

    room = db.session.get(Room, room_id)
    if room is not None and room.owner_id == user.id:
        return "owner"

    member = Membership.query.filter_by(room_id=room_id, user_id=user.id).first()
    return member.role if member else None


def can_moderate(role: str | None) -> bool:
    return role in ("admin", "owner", "moderator")


def banned_in(channel_id: int, user_id: int) -> VoiceBan | None:
    return VoiceBan.query.filter_by(channel_id=channel_id, user_id=user_id).first()


def push_bans(room_id: int) -> None:
    bans = (
        VoiceBan.query.join(Channel, Channel.id == VoiceBan.channel_id)
        .filter(Channel.room_id == room_id)
        .all()
    )
    socketio.emit(
        "bans_updated",
        {"room_id": room_id, "bans": [b.to_dict() for b in bans]},
        to=f"room:{room_id}",
    )


def can_kick(actor: str | None, target: str | None) -> bool:
    """Владельца не исключает никто, админа сайта — тоже.
    Модератор трогает только обычных участников."""
    if actor not in ("admin", "owner", "moderator"):
        return False
    if target in ("owner", "admin"):
        return False
    if actor == "moderator" and target == "moderator":
        return False
    return True


def is_boss(role: str | None) -> bool:
    """Владелец комнаты и админ сайта — те, кого тишина не касается."""
    return role in ("admin", "owner")


def is_member(room_id: int, user_id: int) -> bool:
    if getattr(current_user, "is_admin", False):
        return True
    return Membership.query.filter_by(room_id=room_id, user_id=user_id).first() is not None


def muted_by_lock(channel_id: int, user_id: int, role: str | None) -> bool:
    """Действует ли тишина на этого участника."""
    info = mic_locks.get(channel_id)
    if not info:
        return False
    if user_id == info["user_id"]:
        return False          # тот, кто включил
    return not is_boss(role)  # владелец и админ всегда могут говорить


def lock_payload(channel_id: int):
    info = mic_locks.get(channel_id)
    if not info:
        return None
    return {"user_id": info["user_id"], "name": info["name"], "role": info["role"]}


# --------------------------------------------------------------------------- утилиты


def peers_of(channel_id: int) -> list[dict]:
    with lock:
        return [dict(state, sid=sid) for sid, state in voice_rooms.get(channel_id, {}).items()]


def broadcast_presence(room_id: int, channel_id: int) -> None:
    socketio.emit(
        "voice_presence",
        {
            "channel_id": channel_id,
            "peers": peers_of(channel_id),
            "lock": lock_payload(channel_id),
        },
        to=f"room:{room_id}",
    )


def release_lock(channel_id: int, room_id: int) -> None:
    """Снимает тишину и сообщает об этом каналу."""
    with lock:
        if channel_id not in mic_locks:
            return
        mic_locks.pop(channel_id, None)
    socketio.emit(
        "lock_state",
        {"channel_id": channel_id, "lock": None},
        to=f"room:{room_id}",
    )


def drop_from_voice(sid: str) -> None:
    with lock:
        from .relay import forget
        forget(sid)
        info = sid_voice.pop(sid, None)
        if not info:
            return
        channel_id = info["channel_id"]
        members = voice_rooms.get(channel_id, {})
        members.pop(sid, None)
        if not members:
            voice_rooms.pop(channel_id, None)
        holder = mic_locks.get(channel_id)
        # если ушёл тот, кто включил тишину, снимаем её — иначе канал залипнет
        stale_lock = bool(holder and holder["user_id"] == info["user_id"])

    socketio.server.leave_room(sid, f"voice:{channel_id}", namespace="/")
    socketio.emit("peer_left", {"sid": sid, "channel_id": channel_id}, to=f"voice:{channel_id}")
    if stale_lock:
        release_lock(channel_id, info["room_id"])
    broadcast_presence(info["room_id"], channel_id)


def sync_role(room_id: int, user_id: int, role: str) -> None:
    """Роль поменяли в обход голосового канала — обновляем присутствие."""
    touched = []
    with lock:
        for channel_id, members in voice_rooms.items():
            for state in members.values():
                if state["user_id"] == user_id:
                    state["role"] = role
                    touched.append(channel_id)
    for channel_id in set(touched):
        broadcast_presence(room_id, channel_id)


# --------------------------------------------------------------------------- соединение


@socketio.on("connect")
def on_connect():
    if not current_user.is_authenticated or current_user.is_banned:
        return False
    join_room(f"user:{current_user.id}")

    start_sweeper()

    with lock:
        sessions = online_sids.setdefault(current_user.id, set())
        first = not sessions
        sessions.add(request.sid)
        guest_left_at.pop(current_user.id, None)   # вернулся — уборку отменяем
        everyone = sorted(online_sids)

    emit("ready", {"sid": request.sid, "user": current_user.to_dict(), "online": everyone})
    if first:
        socketio.emit("online_changed", {"user_id": current_user.id, "online": True})
    return None


@socketio.on("disconnect")
def on_disconnect():
    drop_from_voice(request.sid)

    with lock:
        gone = None
        for user_id, sessions in list(online_sids.items()):
            if request.sid in sessions:
                sessions.discard(request.sid)
                if not sessions:
                    online_sids.pop(user_id, None)
                    gone = user_id
                break

    if gone is not None:
        socketio.emit("online_changed", {"user_id": gone, "online": False})
        user = db.session.get(User, gone)
        if user is not None and user.is_guest:
            with lock:
                guest_left_at[gone] = time.time()


# --------------------------------------------------------------------------- комнаты и текст


@socketio.on("open_room")
def on_open_room(data):
    room_id = int(data.get("room_id") or 0)
    if not is_member(room_id, current_user.id):
        emit("error_notice", {"message": "Нет доступа к комнате"})
        return
    join_room(f"room:{room_id}")

    snapshot = {}
    locks = {}
    for channel in Channel.query.filter_by(room_id=room_id, kind="voice"):
        snapshot[str(channel.id)] = peers_of(channel.id)
        locks[str(channel.id)] = lock_payload(channel.id)
    emit("voice_snapshot", {"room_id": room_id, "channels": snapshot, "locks": locks})


@socketio.on("close_room")
def on_close_room(data):
    leave_room(f"room:{int(data.get('room_id') or 0)}")


@socketio.on("open_channel")
def on_open_channel(data):
    channel_id = int(data.get("channel_id") or 0)
    channel = db.session.get(Channel, channel_id)
    if channel is None or not is_member(channel.room_id, current_user.id):
        emit("error_notice", {"message": "Нет доступа к каналу"})
        return
    previous = data.get("previous")
    if previous:
        leave_room(f"text:{int(previous)}")
    join_room(f"text:{channel_id}")


@socketio.on("send_message")
def on_send_message(data):
    from .models import Message

    channel_id = int(data.get("channel_id") or 0)
    content = (data.get("content") or "").strip()
    if not content:
        return
    content = content[:MAX_MESSAGE_LENGTH]

    channel = db.session.get(Channel, channel_id)
    if channel is None or channel.kind != "text":
        emit("error_notice", {"message": "Канал недоступен для сообщений"})
        return
    if not is_member(channel.room_id, current_user.id):
        emit("error_notice", {"message": "Вы не состоите в этой комнате"})
        return

    message = Message(channel_id=channel_id, user_id=current_user.id, content=content)
    db.session.add(message)
    db.session.commit()

    payload = message.to_dict()
    payload["nonce"] = data.get("nonce")
    socketio.emit("new_message", payload, to=f"text:{channel_id}")


@socketio.on("typing")
def on_typing(data):
    channel_id = int(data.get("channel_id") or 0)
    emit(
        "typing",
        {
            "channel_id": channel_id,
            "user_id": current_user.id,
            "display_name": current_user.display_name or current_user.username,
        },
        to=f"text:{channel_id}",
        include_self=False,
    )


# --------------------------------------------------------------------------- голос


@socketio.on("voice_join")
def on_voice_join(data):
    if not current_user.is_authenticated or current_user.is_banned:
        return {"error": "Сначала войдите в аккаунт"}
    try:
        channel_id = int(data.get("channel_id") or 0)
    except (TypeError, ValueError, AttributeError):
        return {"error": "Это не голосовой канал"}
    channel = db.session.get(Channel, channel_id)
    if channel is None or channel.kind != "voice":
        return {"error": "Это не голосовой канал"}
    if not is_member(channel.room_id, current_user.id):
        return {"error": "Вы не состоите в этой комнате"}

    ban = banned_in(channel_id, current_user.id)
    if ban is not None:
        who = (ban.author.display_name or ban.author.username) if ban.author else "модератор"
        return {"error": f"Вход в «{channel.name}» закрыт: вас исключил {who}"}

    drop_from_voice(request.sid)  # нельзя быть в двух каналах сразу

    role = role_in_room(channel.room_id)
    forced = muted_by_lock(channel_id, current_user.id, role)

    state = {
        "user_id": current_user.id,
        "username": current_user.username,
        "display_name": current_user.display_name or current_user.username,
        "color": current_user.color,
        "initials": current_user.initials,
        "avatar_url": current_user.avatar_url,
        "role": role or "member",
        "muted": bool(data.get("muted")) or forced,
        "sharing": False,
        "hand": False,
        "quality": None,
        "relay_version": 1 if data.get("relay_version") == 1 else 0,
    }

    # Снимок участников и добавление себя — одной операцией, иначе при
    # одновременном входе двоих никто из них не начнёт согласование.
    with lock:
        existing = peers_of(channel_id)
        voice_rooms.setdefault(channel_id, {})[request.sid] = state
        sid_voice[request.sid] = {
            "channel_id": channel_id,
            "room_id": channel.room_id,
            "user_id": current_user.id,
            "epoch": secrets.token_urlsafe(16),
        }
    join_room(f"voice:{channel_id}")

    # Новичок первым отправляет offer каждому, кто уже в канале.
    emit("voice_joined", {
        "channel_id": channel_id,
        "peers": existing,
        "self_sid": request.sid,
        "epoch": sid_voice[request.sid]["epoch"],
        "role": role,
        "lock": lock_payload(channel_id),
    })
    emit(
        "peer_joined",
        {"channel_id": channel_id, "peer": dict(state, sid=request.sid)},
        to=f"voice:{channel_id}",
        include_self=False,
    )
    broadcast_presence(channel.room_id, channel_id)
    return {"ok": True, "channel_id": channel_id}


@socketio.on("voice_leave")
def on_voice_leave(_data=None):
    channel_id = sid_voice.get(request.sid, {}).get("channel_id")
    drop_from_voice(request.sid)
    if channel_id:
        leave_room(f"voice:{channel_id}")


@socketio.on("voice_state")
def on_voice_state(data):
    """Флаги участника: микрофон, трансляция, качество."""
    blocked = False
    with lock:
        info = sid_voice.get(request.sid)
        if not info:
            return
        channel_id = info["channel_id"]
        state = voice_rooms.get(channel_id, {}).get(request.sid)
        if state is None:
            return

        for key in ("muted", "sharing", "quality"):
            if key in data:
                state[key] = data[key]

        if not state.get("sharing"):
            from .relay import stop_screen
            stop_screen(request.sid)

        # Тишину нельзя обойти, отправив muted=false
        if muted_by_lock(channel_id, state["user_id"], state["role"]):
            if state["muted"] is False:
                blocked = True
            state["muted"] = True

        snapshot = dict(state, sid=request.sid)

    if blocked:
        holder = mic_locks.get(channel_id, {})
        emit("mic_blocked", {
            "channel_id": channel_id,
            "by": holder.get("name", "модератор"),
        })

    socketio.emit(
        "peer_update", {"channel_id": channel_id, "peer": snapshot}, to=f"voice:{channel_id}"
    )
    broadcast_presence(info["room_id"], channel_id)


@socketio.on("raise_hand")
def on_raise_hand(data):
    up = bool(data.get("up"))
    with lock:
        info = sid_voice.get(request.sid)
        if not info:
            return
        channel_id = info["channel_id"]
        state = voice_rooms.get(channel_id, {}).get(request.sid)
        if state is None or state["hand"] == up:
            return
        state["hand"] = up
        snapshot = dict(state, sid=request.sid)

    socketio.emit(
        "peer_update", {"channel_id": channel_id, "peer": snapshot}, to=f"voice:{channel_id}"
    )
    if up:
        socketio.emit(
            "hand_raised",
            {"channel_id": channel_id, "display_name": state["display_name"]},
            to=f"voice:{channel_id}",
        )
    broadcast_presence(info["room_id"], channel_id)


@socketio.on("mic_lock")
def on_mic_lock(data):
    """Тишина в канале: глушит всех, кроме нажавшего, владельца и админа."""
    info = sid_voice.get(request.sid)
    if not info:
        return
    channel_id, room_id = info["channel_id"], info["room_id"]
    role = role_in_room(room_id)
    active = bool(data.get("active"))

    if not can_moderate(role):
        emit("error_notice", {"message": "Тишину включает владелец или модератор"})
        return

    with lock:
        current = mic_locks.get(channel_id)

        if active:
            if current and current["user_id"] != current_user.id and not is_boss(role):
                emit("error_notice", {
                    "message": f"Тишину уже включил {current['name']} — снять может он или владелец"
                })
                return
            mic_locks[channel_id] = {
                "user_id": current_user.id,
                "name": current_user.display_name or current_user.username,
                "role": role,
            }
        else:
            if not current:
                return
            if current["user_id"] != current_user.id and not is_boss(role):
                emit("error_notice", {
                    "message": f"Снять тишину может {current['name']} или владелец комнаты"
                })
                return
            mic_locks.pop(channel_id, None)

        # приводим состояние микрофонов в соответствие
        for state in voice_rooms.get(channel_id, {}).values():
            if muted_by_lock(channel_id, state["user_id"], state["role"]):
                state["muted"] = True

    socketio.emit(
        "lock_state",
        {"channel_id": channel_id, "lock": lock_payload(channel_id)},
        to=f"room:{room_id}",
    )
    broadcast_presence(room_id, channel_id)


@socketio.on("voice_kick")
def on_voice_kick(data):
    """Выкинуть участника из голосового канала.

    ban=True дополнительно закрывает вход, ban=False — просто выставляет
    за дверь, вернуться можно сразу же.
    """
    info = sid_voice.get(request.sid)
    if not info:
        return
    channel_id, room_id = info["channel_id"], info["room_id"]
    target_user = int(data.get("user_id") or 0)

    if target_user == current_user.id:
        emit("error_notice", {"message": "Себя исключать незачем — есть кнопка выхода"})
        return

    actor_role = role_in_room(room_id)

    with lock:
        targets = [
            (sid, state)
            for sid, state in voice_rooms.get(channel_id, {}).items()
            if state["user_id"] == target_user
        ]
        if not targets:
            emit("error_notice", {"message": "Этого участника нет в канале"})
            return
        target_role = targets[0][1]["role"]
        name = targets[0][1]["display_name"]

    if not can_kick(actor_role, target_role):
        explain = {
            "owner": "Владельца комнаты исключить нельзя",
            "admin": "Админа сайта исключить нельзя",
            "moderator": "Модератора может исключить только владелец или админ",
        }.get(target_role, "Недостаточно прав")
        emit("error_notice", {"message": explain})
        return

    by = current_user.display_name or current_user.username
    with_ban = bool(data.get("ban"))

    if with_ban and banned_in(channel_id, target_user) is None:
        db.session.add(
            VoiceBan(channel_id=channel_id, user_id=target_user, by_user_id=current_user.id)
        )
        db.session.commit()

    for sid, _state in targets:
        socketio.emit(
            "voice_kicked",
            {"channel_id": channel_id, "by": by, "banned": with_ban},
            to=sid,
        )
        drop_from_voice(sid)

    verb = "исключил" if with_ban else "выгнал"
    socketio.emit(
        "system_notice",
        {"channel_id": channel_id, "message": f"{by} {verb} {name} из канала"},
        to=f"room:{room_id}",
    )
    if with_ban:
        push_bans(room_id)


@socketio.on("voice_unban")
def on_voice_unban(data):
    """Снять запрет на вход в голосовой канал."""
    channel_id = int(data.get("channel_id") or 0)
    target_user = int(data.get("user_id") or 0)

    channel = db.session.get(Channel, channel_id)
    if channel is None:
        return
    role = role_in_room(channel.room_id)
    if not can_moderate(role):
        emit("error_notice", {"message": "Снимать запрет может владелец, модератор или админ"})
        return

    ban = banned_in(channel_id, target_user)
    if ban is None:
        return

    # модератор отменяет только собственные решения, владелец и админ — любые
    if not is_boss(role) and ban.by_user_id != current_user.id:
        who = (ban.author.display_name or ban.author.username) if ban.author else "другой модератор"
        emit("error_notice", {"message": f"Запрет поставил {who} — снять может он или владелец"})
        return

    db.session.delete(ban)
    db.session.commit()

    by = current_user.display_name or current_user.username
    socketio.emit(
        "system_notice",
        {"channel_id": channel_id, "message": f"{by} снял запрет на вход в «{channel.name}»"},
        to=f"room:{channel.room_id}",
    )
    push_bans(channel.room_id)


@socketio.on("signal")
def on_signal(data):
    """Пересылка offer/answer/ICE конкретному участнику."""
    if not current_user.is_authenticated or current_user.is_banned or not isinstance(data, dict):
        return
    target = data.get("to")
    payload = data.get("payload")
    if not isinstance(target, str) or not isinstance(payload, dict):
        return
    if payload.get("type") not in ("offer", "answer", "ice", "restart"):
        return
    with lock:
        sender = sid_voice.get(request.sid)
        receiver = sid_voice.get(target)
        if not sender or not receiver or sender["channel_id"] != receiver["channel_id"]:
            return
        channel_id = sender["channel_id"]
        if data.get("channel_id", channel_id) != channel_id:
            return
        socketio.emit("signal", {
            "from": request.sid, "channel_id": channel_id, "payload": payload
        }, to=target)
