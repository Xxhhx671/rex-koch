"""ORM-модели мессенджера Kofi."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(60), default="")
    status: Mapped[str] = mapped_column(String(120), default="В сети")
    avatar: Mapped[str] = mapped_column(String(255), default="")
    # телефон и бизнес-информация профиля
    phone: Mapped[str] = mapped_column(String(24), default="")
    address: Mapped[str] = mapped_column(String(120), default="")
    hours: Mapped[str] = mapped_column(String(120), default="")
    # настройки приватности/оформления/уведомлений (JSON)
    settings: Mapped[str] = mapped_column(Text, default="{}")
    # код двухэтапной защиты при входе (пусто — защита выключена)
    password_hash2: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def name(self) -> str:
        return self.display_name or self.username


class UserSession(Base):
    __tablename__ = "user_sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # для раздела «Устройства»: откуда и чем вошли
    ip: Mapped[str] = mapped_column(String(45), default="")
    agent: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Chat(Base):
    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(10), default="direct")  # direct|group|channel
    title: Mapped[str] = mapped_column(String(80), default="")
    # @ник канала — по нему канал ищут
    username: Mapped[str] = mapped_column(String(32), default="", index=True)
    # закреплённое сообщение (одно на чат, снимается значением 0)
    pinned_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # исчезающие сообщения: часы хранения истории (0 — выключено)
    auto_delete: Mapped[int] = mapped_column(Integer, default=0)
    creator_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    members: Mapped[list["ChatMember"]] = relationship(
        back_populates="chat", cascade="all, delete-orphan"
    )
    messages: Mapped[list["Message"]] = relationship(
        back_populates="chat", cascade="all, delete-orphan"
    )


class ChatMember(Base):
    __tablename__ = "chat_members"
    __table_args__ = (UniqueConstraint("chat_id", "user_id", name="uq_chat_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(10), default="member")  # owner | member
    last_read_id: Mapped[int] = mapped_column(Integer, default=0)
    # «тихий чат»: уведомления от этого участника выключены
    muted: Mapped[bool] = mapped_column(Boolean, default=False)
    # закреплённый чат — всегда наверху списка
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    # черновик текста, который ещё не отправлен
    draft: Mapped[str] = mapped_column(Text, default="")
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    chat: Mapped[Chat] = relationship(back_populates="members")
    user: Mapped[User] = relationship()


class ChatState(Base):
    """Личное состояние участника в чате: блокировка и очищенная история."""

    __tablename__ = "chat_states"
    __table_args__ = (UniqueConstraint("chat_id", "user_id", name="uq_chat_state"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    image: Mapped[str] = mapped_column(String(255), default="")
    audio: Mapped[str] = mapped_column(String(255), default="")
    # ответ на сообщение (квота, как в Telegram)
    reply_to_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    # редактирование: ставится момент правки
    edited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # откуда переслано: название чата или автор исходного сообщения
    forwarded_from: Mapped[str] = mapped_column(String(80), default="")
    # опрос: JSON {question, options:[...]} — голоса лежат в PollVote
    poll: Mapped[str] = mapped_column(Text, default="")
    # служебное сообщение (например, итог звонка) — пусто у обычных
    system: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    chat: Mapped[Chat] = relationship(back_populates="messages")
    sender: Mapped[User] = relationship()


class Call(Base):
    """Звонок: состояние живёт на сервере, медиа идёт мимо (WebRTC)."""

    __tablename__ = "calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    initiator_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(8), default="audio")   # audio | video
    state: Mapped[str] = mapped_column(String(12), default="ringing")  # ringing|active|ended
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    members: Mapped[list[CallMember]] = relationship(
        back_populates="call", cascade="all, delete-orphan"
    )


class CallMember(Base):
    """Участник звонка: звонили, взял, вышел; его микрофон и камера."""

    __tablename__ = "call_members"
    __table_args__ = (UniqueConstraint("call_id", "user_id", name="uq_call_member"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    call_id: Mapped[int] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    state: Mapped[str] = mapped_column(String(12), default="ringing")  # ringing|joined|left
    muted: Mapped[bool] = mapped_column(Boolean, default=False)
    video: Mapped[bool] = mapped_column(Boolean, default=False)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    left_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    call: Mapped[Call] = relationship(back_populates="members")


class Reaction(Base):
    """Реакция на сообщение: один пользователь — один эмодзи на сообщение."""

    __tablename__ = "message_reactions"
    __table_args__ = (
        UniqueConstraint("message_id", "user_id", "emoji", name="uq_reaction"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    emoji: Mapped[str] = mapped_column(String(8))


class PollVote(Base):
    """Голос в опросе: один пользователь — один голос."""

    __tablename__ = "poll_votes"
    __table_args__ = (UniqueConstraint("message_id", "user_id", name="uq_poll_vote"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    option: Mapped[int] = mapped_column(Integer, default=0)


class Folder(Base):
    """Папка чатов пользователя: группа диалогов с общим названием."""

    __tablename__ = "chat_folders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(40))
    # имя значка из index.html (i-…)
    icon: Mapped[str] = mapped_column(String(24), default="msg")
    # id чатов внутри папки, JSON-массив
    chat_ids: Mapped[str] = mapped_column(Text, default="[]")
    position: Mapped[int] = mapped_column(Integer, default=0)


class Story(Base):
    """История: фото или текст, живёт сутки, видна по настройке приватности."""

    __tablename__ = "stories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    text: Mapped[str] = mapped_column(Text, default="")
    image: Mapped[str] = mapped_column(String(255), default="")
    # all | contacts | close (близкие друзья — только я и выбранные)
    privacy: Mapped[str] = mapped_column(String(12), default="all")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class StoryView(Base):
    __tablename__ = "story_views"
    __table_args__ = (UniqueConstraint("story_id", "user_id", name="uq_story_view"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    story_id: Mapped[int] = mapped_column(
        ForeignKey("stories.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    viewed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
