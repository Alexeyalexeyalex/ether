"""Модели данных."""

from datetime import datetime, timezone

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()

# Палитра для аватарок-инициалов
AVATAR_COLORS = [
    "#FFB020", "#3FBF8F", "#5AA9E6", "#E5484D",
    "#B48EAD", "#E8A87C", "#7FC8A9", "#C9A227",
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(value) -> str | None:
    return value.isoformat() + "Z" if value else None


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(32), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(48), nullable=False, default="")
    password_hash = db.Column(db.String(256), nullable=False, default="")
    color = db.Column(db.String(9), nullable=False, default="#FFB020")
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    is_banned = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    # файл аватарки в instance/avatars; nullable — колонки добавляются
    # в уже существующие базы через ALTER TABLE, а он NOT NULL не любит
    avatar = db.Column(db.String(128), nullable=True)
    # личная раскладка списка комнат: папки и порядок, JSON, виден только владельцу
    room_layout = db.Column(db.Text, nullable=True)
    # гостевая учётка: заводится на один визит и убирается уборкой
    is_guest = db.Column(db.Boolean, nullable=True, default=False)

    memberships = db.relationship(
        "Membership", back_populates="user", cascade="all, delete-orphan"
    )
    messages = db.relationship(
        "Message", back_populates="author", cascade="all, delete-orphan"
    )

    def set_password(self, raw: str) -> None:
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return bool(self.password_hash) and check_password_hash(self.password_hash, raw)

    @property
    def initials(self) -> str:
        source = self.display_name or self.username
        return source[:2].upper()

    @property
    def avatar_url(self) -> str | None:
        return f"/avatars/{self.avatar}" if self.avatar else None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name or self.username,
            "color": self.color,
            "is_admin": self.is_admin,
            "is_guest": bool(self.is_guest),
            "initials": self.initials,
            "avatar_url": self.avatar_url,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.username}>"


class Room(db.Model):
    """Комната = сервер в терминах Discord: набор текстовых и голосовых каналов."""

    __tablename__ = "rooms"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False, unique=True)
    topic = db.Column(db.String(160), nullable=False, default="")
    password_hash = db.Column(db.String(256), nullable=True)
    # пускать ли в комнату гостей; None у старых записей считается «да»
    guests_allowed = db.Column(db.Boolean, nullable=True, default=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    owner = db.relationship("User", foreign_keys=[owner_id])
    channels = db.relationship(
        "Channel",
        back_populates="room",
        cascade="all, delete-orphan",
        order_by="Channel.position",
    )
    members = db.relationship(
        "Membership", back_populates="room", cascade="all, delete-orphan"
    )

    @property
    def locked(self) -> bool:
        return bool(self.password_hash)

    @property
    def allows_guests(self) -> bool:
        return self.guests_allowed is not False

    def set_password(self, raw: str | None) -> None:
        self.password_hash = generate_password_hash(raw) if raw else None

    def check_password(self, raw: str) -> bool:
        if not self.password_hash:
            return True
        return check_password_hash(self.password_hash, raw or "")

    def to_dict(self, member_count: int | None = None) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "topic": self.topic,
            "locked": self.locked,
            "guests_allowed": self.allows_guests,
            "owner_id": self.owner_id,
            "member_count": member_count if member_count is not None else len(self.members),
            "created_at": iso(self.created_at),
        }


class Channel(db.Model):
    __tablename__ = "channels"

    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(
        db.Integer, db.ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = db.Column(db.String(48), nullable=False)
    kind = db.Column(db.String(8), nullable=False, default="text")  # text | voice
    position = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    room = db.relationship("Room", back_populates="channels")
    messages = db.relationship(
        "Message", back_populates="channel", cascade="all, delete-orphan"
    )
    bans = db.relationship(
        "VoiceBan", foreign_keys="VoiceBan.channel_id", back_populates="channel", cascade="all, delete-orphan"
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "room_id": self.room_id,
            "name": self.name,
            "kind": self.kind,
            "position": self.position,
        }


class Membership(db.Model):
    __tablename__ = "memberships"
    __table_args__ = (db.UniqueConstraint("user_id", "room_id", name="uq_member"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    room_id = db.Column(
        db.Integer, db.ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role = db.Column(db.String(16), nullable=False, default="member")  # owner | moderator | member
    joined_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    user = db.relationship("User", back_populates="memberships")
    room = db.relationship("Room", back_populates="members")


class VoiceBan(db.Model):
    """Запрет заходить в конкретный голосовой канал. Ставится при исключении."""

    __tablename__ = "voice_bans"
    __table_args__ = (db.UniqueConstraint("channel_id", "user_id", name="uq_voice_ban"),)

    id = db.Column(db.Integer, primary_key=True)
    channel_id = db.Column(
        db.Integer, db.ForeignKey("channels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    channel = db.relationship("Channel", foreign_keys=[channel_id], back_populates="bans")
    user = db.relationship("User", foreign_keys=[user_id])
    author = db.relationship("User", foreign_keys=[by_user_id])

    def to_dict(self) -> dict:
        return {
            "channel_id": self.channel_id,
            "channel_name": self.channel.name if self.channel else "канал удалён",
            "user_id": self.user_id,
            "by_user_id": self.by_user_id,
            "by_name": (self.author.display_name or self.author.username) if self.author else "—",
            "created_at": iso(self.created_at),
        }


class Message(db.Model):
    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True)
    channel_id = db.Column(
        db.Integer, db.ForeignKey("channels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    content = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    edited_at = db.Column(db.DateTime, nullable=True)

    channel = db.relationship("Channel", back_populates="messages")
    author = db.relationship("User", back_populates="messages")

    def to_dict(self) -> dict:
        author = self.author
        return {
            "id": self.id,
            "channel_id": self.channel_id,
            "content": self.content,
            "created_at": iso(self.created_at),
            "edited_at": iso(self.edited_at),
            "author": {
                "id": author.id if author else 0,
                "username": author.username if author else "удалён",
                "display_name": (author.display_name or author.username) if author else "удалён",
                "color": author.color if author else "#7C8F98",
                "initials": author.initials if author else "??",
                "avatar_url": author.avatar_url if author else None,
            },
        }
