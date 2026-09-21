"""Regression checks against a disposable database, never the user's instance."""
import base64
import hashlib
import hmac
import os
from pathlib import Path
import sys
import tempfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["EFIR_INSTANCE_DIR"] = tempfile.mkdtemp(prefix="efir-pytest-")

from app import create_app, socketio
from app import events
from app.models import Channel, Membership, Message, Room, User, VoiceBan, db
from app.settings import _sane


@pytest.fixture()
def site():
    app = create_app()
    app.config["TESTING"] = True
    sockets = []
    with app.app_context():
        db.create_all()
        for index, name in enumerate(["alice", "boris", "outsider"], 1):
            user = User(id=index, username=name, display_name=name)
            user.set_password("local-test-only")
            db.session.add(user)
        db.session.add(Room(id=1, name="Test", owner_id=1))
        db.session.add_all([Membership(user_id=1, room_id=1, role="owner"), Membership(user_id=2, room_id=1)])
        db.session.add_all([Channel(id=1, name="Chat", room_id=1, kind="text"),
                            Channel(id=2, name="Voice", room_id=1, kind="voice"),
                            Channel(id=3, name="Other voice", room_id=1, kind="voice")])
        db.session.commit()

    def connect(name):
        http = app.test_client()
        assert http.post("/login", data={"username": name, "password": "local-test-only"}).status_code == 302
        ws = socketio.test_client(app, flask_test_client=http)
        assert ws.is_connected()
        sid = next(e["args"][0]["sid"] for e in ws.get_received() if e["name"] == "ready")
        sockets.append(ws)
        return http, ws, sid

    yield app, connect
    for client in sockets:
        if client.is_connected():
            client.disconnect()
    with app.app_context():
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
    for table in [events.voice_rooms, events.sid_voice, events.mic_locks, events.online_sids, events.guest_left_at]:
        table.clear()


def test_voice_join_signal_and_channel_isolation(site):
    app, connect = site
    _, alice, alice_sid = connect("alice")
    _, boris, boris_sid = connect("boris")
    _, outsider, _ = connect("outsider")
    assert alice.emit("voice_join", {"channel_id": 2}, callback=True)["ok"]
    assert boris.emit("voice_join", {"channel_id": 2}, callback=True)["ok"]
    joined = next(e["args"][0] for e in boris.get_received() if e["name"] == "voice_joined")
    assert joined["peers"][0]["sid"] == alice_sid
    payload = {"type": "offer", "sdp": {"type": "offer", "sdp": "test"}}
    alice.get_received()
    boris.emit("signal", {"to": alice_sid, "payload": payload, "channel_id": 2})
    signal = next(e["args"][0] for e in alice.get_received() if e["name"] == "signal")
    assert signal == {"from": boris_sid, "payload": payload, "channel_id": 2}
    outsider.emit("signal", {"to": alice_sid, "payload": payload})
    assert not any(e["name"] == "signal" for e in alice.get_received())
    assert boris.emit("voice_join", {"channel_id": 3}, callback=True)["ok"]
    boris.get_received()
    alice.emit("voice_state", {"muted": True})
    assert not any(e["name"] == "peer_update" for e in boris.get_received())
    boris.emit("signal", {"to": alice_sid, "payload": payload})
    assert not any(e["name"] == "signal" for e in alice.get_received())


@pytest.mark.parametrize("channel_id", [1, 9999, "invalid", None])
def test_invalid_join_is_acknowledged(site, channel_id):
    _, connect = site
    _, ws, sid = connect("alice")
    assert "error" in ws.emit("voice_join", {"channel_id": channel_id}, callback=True)
    assert sid not in events.sid_voice


def test_forbidden_and_banned_join(site):
    app, connect = site
    _, ws, sid = connect("outsider")
    assert "error" in ws.emit("voice_join", {"channel_id": 2}, callback=True)
    _, member, member_sid = connect("boris")
    with app.app_context():
        db.session.add(VoiceBan(channel_id=2, user_id=2, by_user_id=1))
        db.session.commit()
    assert "error" in member.emit("voice_join", {"channel_id": 2}, callback=True)
    assert member_sid not in events.sid_voice


def test_chat_history_and_permissions(site):
    app, connect = site
    http, alice, _ = connect("alice")
    other_http, boris, _ = connect("boris")
    outsider_http, outsider, _ = connect("outsider")
    for ws in [alice, boris]:
        ws.emit("open_channel", {"channel_id": 1})
        ws.get_received()
    alice.emit("send_message", {"channel_id": 1, "content": "Local chat", "nonce": "one"})
    received = next(e["args"][0] for e in boris.get_received() if e["name"] == "new_message")
    assert received["content"] == "Local chat"
    assert http.get("/api/channels/1/messages").json["messages"][0]["content"] == "Local chat"
    assert outsider_http.get("/api/channels/1/messages").status_code == 403
    assert other_http.delete(f'/api/messages/{received["id"]}').status_code == 403
    assert http.delete(f'/api/messages/{received["id"]}').status_code == 200


def test_turn_credentials_and_no_cache(site, monkeypatch):
    app, connect = site
    http, _, _ = connect("alice")
    monkeypatch.setenv("EFIR_TURN_URLS", "turn:relay.example.com:3478,turns:relay.example.com:5349")
    monkeypatch.setenv("EFIR_TURN_SECRET", "test-shared-secret")
    response = http.get("/api/rtc-config")
    config = response.json
    assert config["hasTurn"] is True
    assert response.headers["Cache-Control"] == "no-store"
    server = config["iceServers"][-1]
    assert server["credential"] == base64.b64encode(hmac.new(b"test-shared-secret", server["username"].encode(), hashlib.sha1).digest()).decode()
    assert b"test-shared-secret" not in response.data
    assert app.test_client().get("/api/rtc-config").status_code == 302


def test_invalid_ice_settings_are_ignored():
    config = _sane({"ice_servers": [None, {"urls": 4}, {"urls": ["https://bad"]},
                                    {"urls": "turn:relay.example.com"}, {"urls": "stun:stun.example.com"}]})
    assert config["ice_servers"] == [{"urls": ["stun:stun.example.com"]}]


def test_kick_leaves_voice_subscription(site):
    _, connect = site
    _, owner, _ = connect("alice")
    _, member, sid = connect("boris")
    for ws in [owner, member]:
        assert ws.emit("voice_join", {"channel_id": 2}, callback=True)["ok"]
    owner.emit("voice_kick", {"user_id": 2, "ban": False})
    assert sid not in events.sid_voice
    assert any(e["name"] == "voice_kicked" for e in member.get_received())
    owner.emit("voice_state", {"muted": True})
    assert not any(e["name"] == "peer_update" for e in member.get_received())


def test_room_and_channel_lifecycle(site):
    _, connect = site
    owner, _, _ = connect("alice")
    member, _, _ = connect("boris")
    response = owner.post("/api/rooms", json={"name": "Private test", "password": "test-pass", "guests_allowed": False})
    assert response.status_code == 201
    room_id = response.json["room"]["id"]
    assert member.post(f"/api/rooms/{room_id}/join", json={"password": "wrong"}).status_code == 403
    assert member.post(f"/api/rooms/{room_id}/join", json={"password": "test-pass"}).status_code == 200
    assert member.post(f"/api/rooms/{room_id}/channels", json={"name": "Forbidden", "kind": "voice"}).status_code == 403
    response = owner.post(f"/api/rooms/{room_id}/channels", json={"name": "New voice", "kind": "voice"})
    assert response.status_code == 201
    channel_id = response.json["channel"]["id"]
    assert owner.patch(f"/api/channels/{channel_id}", json={"name": "Renamed"}).status_code == 200
    assert owner.delete(f"/api/channels/{channel_id}").status_code == 200
    assert owner.delete(f"/api/rooms/{room_id}").status_code == 200


def test_registration_guest_and_profile(site):
    app, _ = site
    client = app.test_client()
    assert client.post("/register", data={"username": "new-user", "display_name": "New user", "password": "test-123", "confirm": "test-123"}).status_code == 302
    response = client.patch("/api/profile", json={"display_name": "Updated user", "color": "#112233"})
    assert response.status_code == 200
    assert response.json["user"]["display_name"] == "Updated user"
    assert client.get("/logout").status_code == 302
    guest = app.test_client()
    assert guest.post("/guest", data={"display_name": "Guest test"}).status_code == 302
    assert guest.post("/api/rooms", json={"name": "Guest room"}).status_code == 403
    assert guest.post("/api/rooms/1/join", json={}).status_code == 200
    assert guest.get("/logout").status_code == 302


def test_moderator_silence_enforced(site):
    _, connect = site
    _, owner, _ = connect("alice")
    _, member, sid = connect("boris")
    for ws in [owner, member]:
        ws.emit("voice_join", {"channel_id": 2}, callback=True)
    owner.emit("mic_lock", {"active": True})
    member.emit("voice_state", {"muted": False})
    assert events.voice_rooms[2][sid]["muted"] is True
    owner.emit("voice_leave")
    assert 2 not in events.mic_locks
