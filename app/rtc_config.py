"""ICE settings. TURN secrets stay on the server; browsers get expiring credentials."""
import base64
import hashlib
import hmac
import json
import logging
import os
import time

from .paths import INSTANCE_DIR
from .settings import load_settings

TURN_CONFIG_FILE = os.path.join(INSTANCE_DIR, "turn.json")
logger = logging.getLogger(__name__)

DEFAULT_ICE_SERVERS = [
    {"urls": ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]},
    {"urls": "stun:stun.cloudflare.com:3478"},
]


def _turn_settings():
    """Use one complete source so credentials from different servers never mix."""
    if "EFIR_TURN_URLS" in os.environ:
        return {
            "urls": os.environ["EFIR_TURN_URLS"],
            "secret": os.environ.get("EFIR_TURN_SECRET", ""),
            "username": os.environ.get("EFIR_TURN_USERNAME", ""),
            "credential": os.environ.get("EFIR_TURN_CREDENTIAL", ""),
        }
    try:
        # Windows editors may save UTF-8 with a BOM.
        with open(TURN_CONFIG_FILE, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("Expected an object")
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        # Do not include exception text: it can contain part of a secret file.
        logger.warning("Cannot read instance/turn.json; check its JSON format and file permissions.")
        return {}


def _turn_server(user_id):
    turn = _turn_settings()
    urls = turn.get("urls", [])
    if isinstance(urls, str):
        urls = urls.split(",")
    if not isinstance(urls, list):
        return None
    urls = [url.strip() for url in urls
            if isinstance(url, str) and url.strip().startswith(("turn:", "turns:"))]
    if not urls:
        return None
    secret = turn.get("secret")
    if secret is not None and not isinstance(secret, str):
        logger.warning("TURN secret must be a string; TURN entry ignored.")
        return None
    if secret:
        username = f"{int(time.time()) + 86400}:{user_id}"
        credential = base64.b64encode(hmac.new(
            secret.encode(), username.encode(), hashlib.sha1
        ).digest()).decode()
    else:
        username = turn.get("username", "")
        credential = turn.get("credential", "")
    if isinstance(username, str) and username and isinstance(credential, str) and credential:
        return {"urls": urls, "username": username, "credential": credential}
    logger.warning("TURN URLs are set but credentials are missing; TURN entry ignored.")
    return None


def ice_config(user_id):
    settings = load_settings()
    servers = list(settings["ice_servers"] or DEFAULT_ICE_SERVERS)
    turn = _turn_server(user_id)
    if turn:
        servers.append(turn)
    has_turn = any(
        url.startswith(("turn:", "turns:"))
        for server in servers
        for url in ([server["urls"]] if isinstance(server["urls"], str) else server["urls"])
    )
    return {"iceServers": servers, "iceCandidatePoolSize": 0, "hasTurn": has_turn, "serverRelay": True}
