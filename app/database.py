"""Создание базы и первичное наполнение."""

import os

from sqlalchemy import text

from datetime import timedelta

from .models import AVATAR_COLORS, Channel, Membership, Room, User, db, utcnow
from .paths import AVATAR_DIR, DB_FILE, ensure_instance_dir

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "123Alex12"


def database_path() -> str:
    """Путь до файла базы. Работает и вне контекста приложения."""
    return DB_FILE


def wipe_database() -> None:
    """Удаляет файл базы вместе со служебными журналами SQLite."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = DB_FILE + suffix
        if os.path.exists(path):
            os.remove(path)


def ensure_column(table: str, column: str, ddl: str) -> bool:
    """Добавляет колонку, если её нет. create_all() существующие таблицы
    не меняет, поэтому базам, созданным раньше, нужен ALTER."""
    existing = {row[1] for row in db.session.execute(text(f"PRAGMA table_info({table})"))}
    if column in existing:
        return False
    db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
    db.session.commit()
    return True


def sweep_avatars() -> int:
    """Убирает файлы аватарок, на которые уже никто не ссылается."""
    if not os.path.isdir(AVATAR_DIR):
        return 0
    used = {u.avatar for u in User.query.filter(User.avatar.isnot(None))}
    removed = 0
    for name in os.listdir(AVATAR_DIR):
        if name not in used:
            try:
                os.remove(os.path.join(AVATAR_DIR, name))
                removed += 1
            except OSError:
                pass
    return removed


def sweep_guests(days: int = 7) -> int:
    """Убирает гостей старше недели. Тех, кто успел что-то написать или создать
    комнату, не трогаем — иначе вместе с ними исчезнет и переписка."""
    cutoff = utcnow() - timedelta(days=days)
    removed = 0
    for guest in User.query.filter(User.is_guest.is_(True), User.created_at < cutoff):
        if guest.messages:
            continue
        if Room.query.filter_by(owner_id=guest.id).first():
            continue
        drop_avatar(guest.avatar)
        db.session.delete(guest)
        removed += 1
    if removed:
        db.session.commit()
    return removed


def drop_avatar(name: str | None) -> None:
    if not name:
        return
    try:
        os.remove(os.path.join(AVATAR_DIR, name))
    except OSError:
        pass


def bootstrap() -> list[str]:
    """Создаёт таблицы и стартовые данные. Возвращает строки для вывода в консоль."""
    ensure_instance_dir()
    db.create_all()
    report: list[str] = []

    added = []
    if ensure_column("users", "avatar", "avatar VARCHAR(128)"):
        added.append("avatar")
    if ensure_column("users", "room_layout", "room_layout TEXT"):
        added.append("room_layout")
    if ensure_column("users", "is_guest", "is_guest BOOLEAN DEFAULT 0"):
        added.append("is_guest")
    if ensure_column("rooms", "guests_allowed", "guests_allowed BOOLEAN DEFAULT 1"):
        added.append("guests_allowed")
    if added:
        report.append("База обновлена: добавлены поля " + ", ".join(added))

    orphans = sweep_avatars()
    if orphans:
        report.append(f"Удалено неиспользуемых аватарок: {orphans}")

    stale = sweep_guests()
    if stale:
        report.append(f"Убрано старых гостевых учёток: {stale}")

    # Уникальность названий комнат. Индексом, а не только через модель:
    # базы, созданные раньше, об ограничении ещё не знают. Индекс ловит
    # точные совпадения; регистр сравнивается в коде, потому что NOCASE
    # и lower() в SQLite умеют только латиницу и кириллицу не сложат.
    try:
        db.session.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS uq_rooms_name ON rooms (name)")
        )
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        report.append("Есть комнаты с одинаковыми названиями — переименуйте их в админке")

    admin = User.query.filter_by(username=ADMIN_USERNAME).first()
    if admin is None:
        admin = User(
            username=ADMIN_USERNAME,
            display_name="Администратор",
            color=AVATAR_COLORS[0],
            is_admin=True,
        )
        admin.set_password(ADMIN_PASSWORD)
        db.session.add(admin)
        db.session.commit()
        report.append("Создан администратор admin")
    elif not admin.is_admin:
        admin.is_admin = True
        db.session.commit()

    if Room.query.count() == 0:
        room = Room(name="Главная", topic="Общая комната для всех", owner_id=admin.id)
        db.session.add(room)
        db.session.flush()
        db.session.add_all(
            [
                Channel(room_id=room.id, name="общий", kind="text", position=0),
                Channel(room_id=room.id, name="флудилка", kind="text", position=1),
                Channel(room_id=room.id, name="Общий эфир", kind="voice", position=2),
                Channel(room_id=room.id, name="Переговорка", kind="voice", position=3),
            ]
        )
        db.session.add(Membership(user_id=admin.id, room_id=room.id, role="owner"))
        db.session.commit()
        report.append("Создана стартовая комната «Главная»")

    return report
