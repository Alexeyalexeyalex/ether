"""Regression checks against a disposable database, never the user's instance."""
import base64
import hashlib
import hmac
import json
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
from app import rtc_config
from app import relay


@pytest.fixture(autouse=True)
def isolated_turn_settings(monkeypatch, tmp_path):
    for key in ("URLS", "SECRET", "USERNAME", "CREDENTIAL"):
        monkeypatch.delenv("EFIR_TURN_" + key, raising=False)
    monkeypatch.setattr(rtc_config, "TURN_CONFIG_FILE", tmp_path / "turn.json")
    monkeypatch.setattr(rtc_config, "load_settings", lambda: {"ice_servers": []})


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
    for table in (relay.links, relay.outboxes, relay.rates):
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


def test_private_turn_file_with_windows_bom_and_hot_reload(site, monkeypatch):
    app, connect = site
    monkeypatch.setattr(rtc_config.time, "time", lambda: 1800000000)
    config_file = Path(rtc_config.TURN_CONFIG_FILE)
    config_file.write_text(json.dumps({"urls": ["turn:relay.example.com:3478"],
                                       "secret": "private-test-secret"}), encoding="utf-8-sig")
    http, _, _ = connect("alice")
    response = http.get("/api/rtc-config")
    entry = response.json["iceServers"][-1]
    assert response.json["hasTurn"] is True
    assert entry["username"] == "1800086400:1"
    assert entry["credential"] == base64.b64encode(hmac.new(
        b"private-test-secret", b"1800086400:1", hashlib.sha1).digest()).decode()
    assert b"private-test-secret" not in response.data
    assert response.headers["Cache-Control"] == "no-store"
    assert app.test_client().get("/api/rtc-config").status_code == 302
    config_file.write_text(json.dumps({"urls": "turns:relay.example.com:5349", "username": "test-user",
                                       "credential": "test-password"}), encoding="utf-8")
    assert http.get("/api/rtc-config").json["iceServers"][-1] == {
        "urls": ["turns:relay.example.com:5349"], "username": "test-user", "credential": "test-password"}


def test_turn_environment_replaces_whole_private_config(monkeypatch):
    Path(rtc_config.TURN_CONFIG_FILE).write_text(json.dumps({"urls": ["turn:file.example.com"],
                                                           "secret": "file-secret"}), encoding="utf-8")
    monkeypatch.setenv("EFIR_TURN_URLS", "turn:env.example.com")
    # Do not accidentally use the file's secret for an unrelated environment URL.
    assert rtc_config.ice_config(1)["hasTurn"] is False
    monkeypatch.setenv("EFIR_TURN_USERNAME", "env-user")
    monkeypatch.setenv("EFIR_TURN_CREDENTIAL", "env-password")
    assert rtc_config.ice_config(1)["iceServers"][-1] == {
        "urls": ["turn:env.example.com"], "username": "env-user", "credential": "env-password"}
    monkeypatch.setenv("EFIR_TURN_URLS", "")
    assert rtc_config.ice_config(1)["hasTurn"] is False


@pytest.mark.parametrize("raw", [
    b'{"secret":"do-not-log-this",', b'[]', b'\xff', b'{"urls":7}',
    b'{"urls":[null,7,"https://bad"],"secret":"do-not-log-this"}',
    b'{"urls":"turn:relay.example.com","secret":7}',
    b'{"urls":"turn:relay.example.com","username":true,"credential":"do-not-log-this"}',
    b'{"urls":"turn:relay.example.com"}',
])
def test_broken_private_turn_file_does_not_crash_or_leak(raw, caplog):
    Path(rtc_config.TURN_CONFIG_FILE).write_bytes(raw)
    config = rtc_config.ice_config(1)
    assert config["hasTurn"] is False
    assert config["iceServers"] == rtc_config.DEFAULT_ICE_SERVERS
    assert "do-not-log-this" not in caplog.text


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


def relay_pair(connect):
    _, alice, alice_sid = connect("alice")
    _, boris, boris_sid = connect("boris")
    for ws in (alice, boris):
        assert ws.emit("voice_join", {"channel_id": 2, "relay_version": 1}, callback=True)["ok"]
        ws.get_received()
    assert alice.emit("relay_start", {"to": boris_sid, "channel_id": 2}, callback=True)["ok"]
    token = next(e["args"][0]["token"] for e in alice.get_received() if e["name"] == "relay_started")
    for ws, target in ((alice, boris_sid), (boris, alice_sid)):
        ws.emit("relay_ready", {"to": target, "channel_id": 2, "token": token})
    for ws in (alice, boris):
        assert any(e["name"] == "relay_connected" for e in ws.get_received())
    return alice, alice_sid, boris, boris_sid


def audio_packet(sid, seq=0, **changes):
    return {"channel_id": 2, "epoch": events.sid_voice[sid]["epoch"], "kind": "mic",
            "seq": seq, "data": base64.b64encode(b"\x01\x00" * 960).decode(), **changes}


def media_packets(ws):
    return [e["args"][0] for e in ws.get_received() if e["name"] == "relay_media"]


def test_relay_routes_audio_only_to_ready_channel_peers_and_cleans_on_switch(site):
    _, connect = site
    alice, source, boris, target = relay_pair(connect)
    _, outsider, _ = connect("outsider")
    alice.emit("relay_publish", audio_packet(source))
    packet = media_packets(boris)[0]
    assert packet["from"] == source and packet["channel_id"] == 2
    assert packet["packets"][0]["seq"] == 0
    assert not media_packets(alice) and not media_packets(outsider)
    old_epoch = events.sid_voice[source]["epoch"]
    boris.emit("voice_join", {"channel_id": 3, "relay_version": 1}, callback=True)
    assert not relay.links and not relay.outboxes
    alice.emit("relay_publish", audio_packet(source, 1))
    assert not media_packets(boris)
    alice.emit("voice_leave")
    alice.emit("voice_join", {"channel_id": 2, "relay_version": 1}, callback=True)
    assert events.sid_voice[source]["epoch"] != old_epoch
    assert not relay.rates


def test_relay_rejects_invalid_payloads_and_stale_session(site):
    _, connect = site
    alice, source, boris, _ = relay_pair(connect)
    for bad in (None, [], {"kind": []}, audio_packet(source, epoch="old"),
                audio_packet(source, channel_id=3), audio_packet(source, seq=True),
                audio_packet(source, data="?" * 2560), audio_packet(source, data="x" * 3000),
                audio_packet(source, data=base64.b64encode(b"short").decode()),
                audio_packet(source, kind="screen", data=base64.b64encode(b"not-jpeg").decode())):
        alice.emit("relay_publish", bad)
    assert not media_packets(boris)
    alice.emit("relay_publish", audio_packet(source, 1))
    assert len(media_packets(boris)) == 1
    alice.emit("relay_publish", audio_packet(source, 1))
    assert not relay.outboxes[(source, next(iter(events.voice_rooms[2].keys() - {source})), "mic")]["queue"]


def test_relay_slow_receiver_queue_is_bounded_and_ack_is_scoped(site, monkeypatch):
    _, connect = site
    alice, source, boris, target = relay_pair(connect)
    now = [1000.0]
    monkeypatch.setattr(relay.time, "monotonic", lambda: now[0])
    alice.emit("relay_publish", audio_packet(source, 0))
    delivery = media_packets(boris)[0]
    for seq in range(1, 31):
        now[0] += 0.04
        alice.emit("relay_publish", audio_packet(source, seq))
    box = relay.outboxes[(source, target, "mic")]
    assert len(box["queue"]) == 6
    assert not media_packets(boris)  # No accumulating sends to a stalled socket.
    alice.emit("relay_ack", {"from": source, "kind": "mic", "id": delivery["id"]})
    boris.emit("relay_ack", {"from": source, "kind": "mic", "id": delivery["id"] + 1})
    assert not media_packets(boris)
    boris.emit("relay_ack", {"from": source, "kind": "mic", "id": delivery["id"]})
    batch = media_packets(boris)[0]
    assert [p["seq"] for p in batch["packets"]] == list(range(25, 31))


def test_relay_mute_and_moderation_are_enforced_on_server(site):
    _, connect = site
    alice, source, boris, target = relay_pair(connect)
    boris.emit("voice_state", {"muted": True})
    boris.emit("relay_publish", audio_packet(target))
    assert not media_packets(alice)
    boris.emit("voice_state", {"muted": False, "sharing": True})
    alice.emit("mic_lock", {"active": True})
    for kind in ("mic", "screen_audio"):
        boris.emit("relay_publish", audio_packet(target, kind=kind))
    assert not media_packets(alice)
    alice.emit("voice_kick", {"user_id": 2, "ban": True})
    assert target not in events.sid_voice and not relay.links and not relay.outboxes


@pytest.mark.parametrize("change", ["membership", "ban", "channel"])
def test_relay_rechecks_access_before_flushing_buffered_audio(site, change):
    app, connect = site
    alice, source, boris, target = relay_pair(connect)
    alice.emit("relay_publish", audio_packet(source))
    packet = media_packets(boris)[0]
    alice.emit("relay_publish", audio_packet(source, 1))
    with app.app_context():
        if change == "membership":
            db.session.delete(Membership.query.filter_by(room_id=1, user_id=2).one())
        elif change == "ban":
            db.session.get(User, 2).is_banned = True
        else:
            db.session.get(Channel, 2).kind = "text"
        db.session.commit()
    boris.emit("relay_ack", {"from": source, "kind": "mic", "id": packet["id"]})
    assert not media_packets(boris)
    alice.emit("relay_publish", audio_packet(source, 2))
    assert not media_packets(boris)


def test_relay_start_checks_permissions_capabilities_and_rate_limit(site, monkeypatch):
    _, connect = site
    _, alice, source = connect("alice")
    _, boris, target = connect("boris")
    _, outsider, _ = connect("outsider")
    for ws in (alice, boris):
        ws.emit("voice_join", {"channel_id": 2}, callback=True)
    assert "error" in outsider.emit("relay_start", {"channel_id": 2, "to": source}, callback=True)
    assert "error" in alice.emit("relay_start", {"channel_id": 2, "to": target}, callback=True)
    for ws in (alice, boris):
        ws.emit("voice_join", {"channel_id": 2, "relay_version": 1}, callback=True)
    assert alice.emit("relay_start", {"channel_id": 2, "to": target}, callback=True)["ok"]
    # An unready receiver gets no media.
    alice.emit("relay_publish", audio_packet(source))
    assert not media_packets(boris)
    monkeypatch.setattr(relay.time, "monotonic", lambda: 1000.0)
    assert sum(relay._accept_packet(source, "mic", audio_packet(source, seq)) for seq in range(100)) == 8
