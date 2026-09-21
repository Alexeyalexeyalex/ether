"""Пути проекта.

Отдельный модуль, чтобы путь до базы можно было узнать в любой момент —
и до создания приложения, и вне контекста Flask.
"""

import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTANCE_DIR = os.path.abspath(os.environ.get("EFIR_INSTANCE_DIR") or os.path.join(BASE_DIR, "instance"))
DB_FILE = os.path.join(INSTANCE_DIR, "efir.db")
DB_URI = "sqlite:///" + DB_FILE.replace("\\", "/")
SECRET_FILE = os.path.join(INSTANCE_DIR, "secret.key")
AVATAR_DIR = os.path.join(INSTANCE_DIR, "avatars")


def ensure_instance_dir() -> str:
    os.makedirs(INSTANCE_DIR, exist_ok=True)
    os.makedirs(AVATAR_DIR, exist_ok=True)
    return INSTANCE_DIR
