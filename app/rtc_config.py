"""ICE settings. TURN secrets stay on the server; browsers get expiring credentials."""
import base64
import hashlib
import hmac
import os
import time

from .settings import load_settings

DEFAULT_ICE_SERVERS = [
    {"urls": ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]},
    {"urls": "stun:stun.cloudflare.com:3478"},
]


def ice_config(user_id):
    settings = load_settings()
    servers = list(settings["ice_servers"] or DEFAULT_ICE_SERVERS)
    urls = [url.strip() for url in os.environ.get("EFIR_TURN_URLS", "").split(",")
            if url.strip().startswith(("turn:", "turns:"))]
    if urls:
        secret = os.environ.get("EFIR_TURN_SECRET")
        if secret:
            username = f"{int(time.time()) + 86400}:{user_id}"
            credential = base64.b64encode(hmac.new(
                secret.encode(), username.encode(), hashlib.sha1
            ).digest()).decode()
        else:
            username = os.environ.get("EFIR_TURN_USERNAME", "")
            credential = os.environ.get("EFIR_TURN_CREDENTIAL", "")
        if username and credential:
            servers.append({"urls": urls, "username": username, "credential": credential})
    has_turn = any(
        url.startswith(("turn:", "turns:"))
        for server in servers
        for url in ([server["urls"]] if isinstance(server["urls"], str) else server["urls"])
    )
    return {"iceServers": servers, "iceCandidatePoolSize": 0, "hasTurn": has_turn}
