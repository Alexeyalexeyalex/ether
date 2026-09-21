#!/usr/bin/env python3
"""
Эфир — голосовой и текстовый хаб (аналог Discord).

Запуск:
    python main.py

Всё настраивается в settings.json рядом с этим файлом: порт, адрес, HTTPS.
Файл создаётся при первом запуске. Флаги ниже нужны только чтобы разово
отступить от настроек:

    python main.py --port 8080     другой порт на один запуск
    python main.py --local         слушать только этот компьютер
    python main.py --http          выключить HTTPS на один запуск
    python main.py --reset-db      пересоздать базу с нуля
    python main.py --debug         автоперезагрузка при правках
"""

import argparse
import os
import socket
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# (имя пакета для pip, имя модуля для import)
REQUIRED = [
    ("flask", "flask"),
    ("flask-sqlalchemy", "flask_sqlalchemy"),
    ("flask-login", "flask_login"),
    ("flask-socketio", "flask_socketio"),
    ("simple-websocket", "simple_websocket"),
    ("cryptography", "cryptography"),
]


def ensure_dependencies() -> None:
    """Ставит недостающие пакеты, чтобы проект запускался одной командой."""
    import importlib.util

    missing = [pkg for pkg, module in REQUIRED if importlib.util.find_spec(module) is None]
    if not missing:
        return

    print("Не хватает пакетов: " + ", ".join(missing))
    print("Устанавливаю их через pip...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", *missing])
    except subprocess.CalledProcessError:
        print("\nАвтоустановка не удалась. Установите вручную:")
        print(f"    {sys.executable} -m pip install " + " ".join(missing))
        sys.exit(1)
    print("Готово.\n")


def lan_ip():
    """Адрес компьютера в локальной сети. Пакеты никуда не уходят — ядро лишь
    выбирает интерфейс, через который пошёл бы маршрут наружу."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def local_addresses() -> list[str]:
    """Все адреса этого компьютера. Чем больше имён в сертификате, тем реже
    его придётся перевыпускать — а перевыпуск сбрасывает доверие, которое
    пользователь один раз выдал."""
    names: list[str] = []
    address = lan_ip()
    if address:
        names.append(address)
    try:
        host = socket.gethostname()
        names.append(host)
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            names.append(info[4][0])
    except OSError:
        pass
    return [n for n in dict.fromkeys(names) if n and not n.startswith("127.")]


def firewall_rule_exists(port: int):
    """True / False / None (проверить не удалось или это не Windows)."""
    if os.name != "nt":
        return None
    try:
        done = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name=Efir {port}"],
            capture_output=True,
            timeout=8,
        )
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Эфир — запуск сервера")
    parser.add_argument("--host", default=None, help="разово переопределить адрес прослушивания")
    parser.add_argument("--local", action="store_true", help="слушать только этот компьютер")
    parser.add_argument("--port", type=int, default=None, help="разово переопределить порт")
    parser.add_argument("--http", action="store_true", help="выключить HTTPS на этот запуск")
    parser.add_argument("--ssl", action="store_true", help="включить HTTPS на этот запуск")
    parser.add_argument("--debug", action="store_true", help="режим разработки")
    parser.add_argument("--reset-db", action="store_true", help="удалить и пересоздать базу")
    return parser.parse_args()


def resolve(args, settings) -> dict:
    """Настройки из файла, поверх них — флаги командной строки."""
    host = settings["host"]
    if args.host:
        host = args.host
    if args.local:
        host = "127.0.0.1"

    https = settings["https"]
    if args.http:
        https = False
    if args.ssl:
        https = True

    return {
        "host": host,
        "port": args.port or settings["port"],
        "https": https,
        "cert_hosts": settings["cert_hosts"],
        "cert_file": settings["cert_file"],
        "key_file": settings["key_file"],
        "debug": args.debug or settings["debug"],
    }


def setup_https(cfg):
    """Готовит сертификат. Возвращает (ssl_context, свой ли он) или (None, False)."""
    own_cert, own_key = cfg["cert_file"], cfg["key_file"]
    if own_cert and own_key:
        if os.path.exists(own_cert) and os.path.exists(own_key):
            return (own_cert, own_key), True
        print(f"Сертификат из настроек не найден: {own_cert}")
        print("Беру самоподписанный.\n")

    from app.certs import ensure_certificate

    hosts = list(cfg["cert_hosts"])
    if cfg["host"] not in ("0.0.0.0", "::"):
        hosts.append(cfg["host"])
    hosts.extend(local_addresses())

    try:
        return ensure_certificate(hosts), False
    except Exception as exc:  # noqa: BLE001
        print(f"Сертификат выпустить не удалось: {exc}")
        print("Запускаю без HTTPS — микрофон и экран будут работать только с localhost.\n")
        return None, False


def banner(cfg, report, db_path: str, own_cert: bool = False) -> None:
    from app.settings import SETTINGS_FILE

    scheme = "https" if cfg["https"] else "http"
    host, port = cfg["host"], cfg["port"]
    wide_open = host in ("0.0.0.0", "::")
    rule = "  " + "-" * 54

    print()
    print("  ЭФИР — голосовой и текстовый хаб")
    print(rule)
    print(f"  Слушаю        {host}:{port}" + ("   (все интерфейсы)" if wide_open else ""))
    print(f"  С этого ПК    {scheme}://localhost:{port}")

    address = lan_ip() if wide_open else None
    if address:
        print(f"  Из сети       {scheme}://{address}:{port}")
    print(f"  Админка       {scheme}://localhost:{port}/admin")
    print(f"  Вход          admin / 123Alex12")
    print(f"  База          {db_path}")
    print(f"  Настройки     {SETTINGS_FILE}")
    for line in report:
        print(f"  {line}")

    if cfg["https"] and own_cert:
        print(rule)
        print(f"  Сертификат    {cfg['cert_file']}")
        print("  Свой сертификат — предупреждений в браузере не будет.")
    elif cfg["https"]:
        from app.certs import CERT_FILE

        print(rule)
        print("  Сертификат самоподписанный, поэтому браузер ругается на издателя.")
        print("  Убрать предупреждение на этом компьютере: запустите trust-cert.bat")
        print("  Чтобы его не было нигде — нужен домен, см. README.")
        print(f"  Файл сертификата: {CERT_FILE}")
    else:
        print(rule)
        print("  HTTPS выключен. Микрофон и захват экрана браузер отдаст только")
        print("  на localhost. Включите \"https\": true в settings.json.")

    if wide_open and firewall_rule_exists(port) is False:
        print(rule)
        print("  Брандмауэр Windows не пропустит соединения снаружи: правила")
        print(f"  для порта {port} нет. Запустите от имени администратора файл")
        print("  open-port.bat в папке проекта — он добавит правило.")

    if wide_open:
        print(rule)
        print("  Порт открыт наружу. Смените пароль admin, если сервис виден")
        print("  из интернета: в админке вся база и SQL-консоль.")

    print(rule)
    print("  Остановить: Ctrl+C")
    print()


def main() -> None:
    args = parse_args()
    ensure_dependencies()

    sys.path.insert(0, BASE_DIR)
    from app import create_app, socketio
    from app.database import bootstrap, database_path, wipe_database
    from app.settings import load_settings

    cfg = resolve(args, load_settings())

    if args.reset_db:
        wipe_database()
        print("База удалена, создаю заново.")

    app = create_app(debug=cfg["debug"])
    with app.app_context():
        report = bootstrap()

    ssl_context, own_cert = None, False
    if cfg["https"]:
        ssl_context, own_cert = setup_https(cfg)
        if ssl_context is None:
            cfg["https"] = False

    banner(cfg, report, database_path(), own_cert)

    try:
        socketio.run(
            app,
            host=cfg["host"],
            port=cfg["port"],
            debug=cfg["debug"],
            use_reloader=cfg["debug"],
            allow_unsafe_werkzeug=True,
            ssl_context=ssl_context,
        )
    except PermissionError:
        print(f"\nПорт {cfg['port']} занят или закрыт правами. Укажите другой в settings.json")
    except OSError as exc:
        print(f"\nНе удалось занять {cfg['host']}:{cfg['port']} — {exc}")
    except KeyboardInterrupt:
        print("\nСервер остановлен.")


if __name__ == "__main__":
    main()
