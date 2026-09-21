"""Настройки запуска из settings.json.

Файл лежит рядом с main.py и создаётся при первом запуске. Флаги командной
строки, если их указать, перекрывают то, что записано в файле.
"""

import json
import os

from .paths import BASE_DIR

SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")

DEFAULTS = {
    "_справка": "Настройки запуска. Меняйте значения ниже — флаги в командной строке не нужны.",
    "host": "0.0.0.0",
    "_host": "0.0.0.0 — доступен по IP компьютера. 127.0.0.1 — только с этого компьютера.",
    "port": 2079,
    "https": True,
    "_https": "true нужен, чтобы работали микрофон и захват экрана при заходе не с localhost.",
    "cert_hosts": [],
    "_cert_hosts": "Домен или внешний IP для сертификата, например: [\"efir.example.com\"].",
    "cert_file": "",
    "key_file": "",
    "_cert_file": "Свой сертификат (например от Let's Encrypt) — тогда браузер не ругается.",
    "debug": False,
    "ice_servers": [],
    "_ice_servers": "Дополнительные настройки WebRTC: список объектов urls/username/credential. Пусто — публичные STUN. TURN также задаётся через EFIR_TURN_URLS и EFIR_TURN_SECRET.",
}

# ключи, которые реально влияют на запуск (остальные — пояснения)
KEYS = ("host", "port", "https", "cert_hosts", "cert_file", "key_file", "debug", "ice_servers")


def _sane(data: dict) -> dict:
    """Приводит значения к ожидаемым типам, молча чиня очевидные опечатки."""
    clean = {key: DEFAULTS[key] for key in KEYS}

    host = data.get("host")
    if isinstance(host, str) and host.strip():
        clean["host"] = host.strip()

    port = data.get("port")
    if isinstance(port, bool):
        port = None
    if isinstance(port, (int, str)):
        try:
            port = int(port)
            if 1 <= port <= 65535:
                clean["port"] = port
        except (TypeError, ValueError):
            pass

    for flag in ("https", "debug"):
        value = data.get(flag)
        if isinstance(value, bool):
            clean[flag] = value
        elif isinstance(value, str):
            clean[flag] = value.strip().lower() in ("1", "true", "да", "yes", "on")

    hosts = data.get("cert_hosts")
    if isinstance(hosts, str):
        hosts = [hosts]
    if isinstance(hosts, list):
        clean["cert_hosts"] = [str(h).strip() for h in hosts if str(h).strip()]

    for path_key in ("cert_file", "key_file"):
        value = data.get(path_key)
        if isinstance(value, str):
            clean[path_key] = value.strip()

    servers = data.get("ice_servers")
    if isinstance(servers, list):
        clean["ice_servers"] = []
        for server in servers:
            if not isinstance(server, dict):
                continue
            urls = server.get("urls", [])
            if isinstance(urls, str):
                urls = [urls]
            if not isinstance(urls, list):
                continue
            urls = [u for u in urls if isinstance(u, str) and u.startswith(("stun:", "stuns:", "turn:", "turns:"))]
            if not urls:
                continue
            entry = {"urls": urls}
            for key in ("username", "credential"):
                if isinstance(server.get(key), str):
                    entry[key] = server[key]
            if any(u.startswith(("turn:", "turns:")) for u in urls) and not all(entry.get(k) for k in ("username", "credential")):
                continue
            clean["ice_servers"].append(entry)
    return clean


def save_settings(values: dict) -> None:
    payload = dict(DEFAULTS)
    payload.update({key: values[key] for key in KEYS if key in values})
    with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def load_settings() -> dict:
    if not os.path.exists(SETTINGS_FILE):
        values = {key: DEFAULTS[key] for key in KEYS}
        save_settings(values)
        return values

    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"settings.json прочитать не вышло ({exc}). Беру значения по умолчанию.")
        return {key: DEFAULTS[key] for key in KEYS}

    if not isinstance(raw, dict):
        return {key: DEFAULTS[key] for key in KEYS}
    return _sane(raw)
