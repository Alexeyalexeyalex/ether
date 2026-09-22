"""Bounded media fallback over the site's existing Socket.IO connection.

PCM24 mono audio and independent JPEG frames; no files or recording. A receiver
has one unacknowledged batch plus at most 240 ms of audio / one newer image.
All routing and queue mutations use the voice room lock.
"""
import base64
import binascii
from collections import deque
import itertools
import secrets
import time

from flask import request
from flask_login import current_user
from sqlalchemy import and_, or_

from . import socketio
from . import events
from .models import Channel, Membership, Room, User, VoiceBan, db

links = {}
outboxes = {}
rates = {}
delivery_ids = itertools.count(1)
KINDS = ("mic", "screen_audio", "screen")
MAX_LINKS = 7
AUDIO_BYTES = 1920  # 40 ms, 24000 Hz, signed little-endian PCM16, mono
MAX_IMAGE_BYTES = 128 * 1024


def forget(sid):
    """Called under the room lock on leave, kick, switch and disconnect."""
    for key in list(links):
        if sid in key:
            links.pop(key, None)
    for key in list(outboxes):
        if sid in key[:2]:
            outboxes.pop(key, None)
    for key in list(rates):
        if key[0] == sid:
            rates.pop(key, None)


def stop_screen(sid):
    for key in list(outboxes):
        if key[0] == sid and key[2] in ("screen", "screen_audio"):
            outboxes.pop(key, None)


def _pair(sender, target, channel_id):
    if sender == target or not isinstance(target, str):
        return None
    first = events.sid_voice.get(sender)
    second = events.sid_voice.get(target)
    if not first or not second or first["channel_id"] != channel_id or second["channel_id"] != channel_id:
        return None
    return tuple(sorted((sender, target)))


def _authorized(sids, channel_id):
    """One query for all recipients; also honours edits made in the admin UI."""
    users = {sid: events.sid_voice[sid]["user_id"] for sid in sids if sid in events.sid_voice}
    allowed = set(row[0] for row in db.session.query(User.id)
                  .join(Channel, Channel.id == channel_id)
                  .join(Room, Room.id == Channel.room_id)
                  .outerjoin(Membership, and_(Membership.room_id == Room.id, Membership.user_id == User.id))
                  .outerjoin(VoiceBan, and_(VoiceBan.channel_id == Channel.id, VoiceBan.user_id == User.id))
                  .filter(User.id.in_(users.values()), User.is_banned.is_(False),
                          Channel.kind == "voice", VoiceBan.id.is_(None),
                          or_(Membership.id.is_not(None), Room.owner_id == User.id, User.is_admin.is_(True)))
                  .all())
    return {sid for sid, user in users.items() if user in allowed}


@socketio.on("relay_start")
def start(data):
    if not current_user.is_authenticated or current_user.is_banned or not isinstance(data, dict):
        return {"error": "Нет доступа к голосовому каналу"}
    with events.lock:
        pair = _pair(request.sid, data.get("to"), data.get("channel_id"))
        if not pair:
            return {"error": "Участник уже вышел из голосового канала"}
        if set(pair) != _authorized(pair, data["channel_id"]):
            return {"error": "Нет доступа к голосовому каналу"}
        room = events.voice_rooms[data["channel_id"]]
        if not all(room[sid].get("relay_version") == 1 for sid in pair):
            return {"error": "Для связи через сервер обоим участникам нужно обновить страницу"}
        if pair not in links:
            if any(sum(sid in key for key in links) >= MAX_LINKS for sid in pair):
                return {"error": "Резервный режим поддерживает до 8 участников в разговоре"}
            links[pair] = {"channel_id": data["channel_id"], "token": secrets.token_urlsafe(16), "ready": set()}
        link = links[pair]
        for sid, other in ((pair[0], pair[1]), (pair[1], pair[0])):
            socketio.emit("relay_started", {"from": other, "channel_id": link["channel_id"],
                                           "token": link["token"], "epoch": events.sid_voice[sid]["epoch"]}, to=sid)
        return {"ok": True}


@socketio.on("relay_ready")
def ready(data):
    if not isinstance(data, dict):
        return
    with events.lock:
        pair = _pair(request.sid, data.get("to"), data.get("channel_id"))
        link = links.get(pair)
        if not link or link["token"] != data.get("token"):
            return
        link["ready"].add(request.sid)
        if len(link["ready"]) == 2:
            for sid, other in ((pair[0], pair[1]), (pair[1], pair[0])):
                socketio.emit("relay_connected", {"from": other, "channel_id": link["channel_id"],
                                                 "token": link["token"], "epoch": events.sid_voice[sid]["epoch"]}, to=sid)


def _jpeg_dimensions(data):
    """Bound decoded image size before accepting a client-supplied JPEG."""
    if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        return None
    index = 2
    while index + 4 <= len(data):
        if data[index] != 255:
            return None
        while index < len(data) and data[index] == 255:
            index += 1
        if index >= len(data):
            return None
        marker = data[index]
        index += 1
        if marker in (0xDA, 0xD9):
            return None
        length = int.from_bytes(data[index:index + 2], "big")
        if length < 2 or index + length > len(data):
            return None
        if marker in (0xC0, 0xC1, 0xC2) and length >= 8:
            return int.from_bytes(data[index + 5:index + 7], "big"), int.from_bytes(data[index + 3:index + 5], "big")
        index += length
    return None


def _accept_packet(sid, kind, data):
    encoded = data.get("data")
    sequence = data.get("seq")
    limit = MAX_IMAGE_BYTES if kind == "screen" else AUDIO_BYTES
    if not isinstance(encoded, str) or len(encoded) > (limit + 2) // 3 * 4:
        return False
    if type(sequence) is not int or not 0 <= sequence < 2**53:
        return False
    now = time.monotonic()
    key = (sid, kind)
    rate = rates.setdefault(key, {"time": now, "tokens": 8.0, "seq": -1})
    per_second = 6 if kind == "screen" else 30
    rate["tokens"] = min(8.0, rate["tokens"] + (now - rate["time"]) * per_second)
    rate["time"] = now
    if rate["tokens"] < 1 or sequence <= rate["seq"]:
        return False
    rate["tokens"] -= 1
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return False
    if kind == "screen":
        size = _jpeg_dimensions(raw)
        if len(raw) > limit or not size or not (1 <= size[0] <= 1280 and 1 <= size[1] <= 720):
            return False
    elif len(raw) != AUDIO_BYTES:
        return False
    rate["seq"] = sequence
    return True


def _flush(key, box, authorized=None):
    source, target, kind = key
    link = links.get(tuple(sorted((source, target))))
    if not link or len(link["ready"]) != 2 or box["pending"] or not box["queue"]:
        return
    if authorized is None:
        authorized = _authorized((source, target), link["channel_id"])
    if source not in authorized or target not in authorized:
        box["queue"].clear()
        return
    # The pair is removed atomically with channel departure, so queued packets
    # cannot follow a user into a different room or a new voice session.
    delivery_id = next(delivery_ids)
    box["pending"] = delivery_id
    packets = list(box["queue"])
    box["queue"].clear()
    socketio.emit("relay_media", {"from": source, "channel_id": link["channel_id"],
                                 "token": link["token"], "kind": kind, "id": delivery_id,
                                 "packets": packets}, to=target)


@socketio.on("relay_publish")
def publish(data):
    if not current_user.is_authenticated or current_user.is_banned or not isinstance(data, dict):
        return
    kind = data.get("kind")
    if kind not in KINDS:
        return
    with events.lock:
        info = events.sid_voice.get(request.sid)
        if not info or info["channel_id"] != data.get("channel_id") or info["epoch"] != data.get("epoch"):
            return
        state = events.voice_rooms.get(info["channel_id"], {}).get(request.sid)
        if not state or (kind != "mic" and not state.get("sharing")):
            return
        if kind in ("mic", "screen_audio") and (
            (kind == "mic" and state.get("muted")) or
            events.muted_by_lock(info["channel_id"], state["user_id"], state["role"])
        ):
            return
        targets = [pair[1] if pair[0] == request.sid else pair[0]
                   for pair, link in links.items() if request.sid in pair and len(link["ready"]) == 2]
        if not targets or not _accept_packet(request.sid, kind, data):
            return
        authorized = _authorized([request.sid, *targets], info["channel_id"])
        if request.sid not in authorized:
            events.drop_from_voice(request.sid)
            return
        packet = {"seq": data["seq"], "data": data["data"]}
        for target in targets:
            if target not in authorized:
                events.drop_from_voice(target)
                continue
            key = (request.sid, target, kind)
            box = outboxes.setdefault(key, {"pending": None, "queue": deque(maxlen=1 if kind == "screen" else 6)})
            box["queue"].append(packet)
            _flush(key, box, authorized)


@socketio.on("relay_ack")
def ack(data):
    if not isinstance(data, dict) or not isinstance(data.get("from"), str) or data.get("kind") not in KINDS:
        return
    with events.lock:
        key = (data["from"], request.sid, data["kind"])
        box = outboxes.get(key)
        if box and type(data.get("id")) is int and box["pending"] == data["id"]:
            box["pending"] = None
            _flush(key, box)
