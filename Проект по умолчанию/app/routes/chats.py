"""Чаты: личные, групповые диалоги, каналы, сообщения, статусы прочтения."""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import UPLOAD_DIR, get_db
from ..helpers import (
    chat_payload,
    get_membership,
    is_contact,
    load_settings,
    message_payload,
    purge_expired,
    require_member,
    require_user,
    require_viewer,
    state_flags,
    unread_count,
    user_brief,
)
from ..models import Chat, ChatMember, ChatState, Folder, Message, User, utcnow

router = APIRouter(prefix="/api", tags=["chats"])

ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
AUDIO_EXT = {".webm", ".mp4", ".m4a", ".mp3", ".ogg", ".oga", ".wav", ".aac"}
MAX_IMAGE = 8 * 1024 * 1024
MAX_AUDIO = 16 * 1024 * 1024
TYPING_TTL = 7  # секунд жизни индикатора «печатает…»

# ник канала: буква/цифра/подчёркивание, 3–32 символа
HANDLE_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_]{2,31}$")

# {chat_id: {user_id: срок действия}} — храним в памяти процесса
_TYPING: dict[int, dict[int, float]] = {}


class DirectIn(BaseModel):
    username: str


class GroupIn(BaseModel):
    title: str = Field(default="", max_length=80)
    usernames: list[str] = Field(default_factory=list, max_length=100)


class RenameIn(BaseModel):
    title: str = Field(min_length=1, max_length=80)


class MemberIn(BaseModel):
    username: str


class ReadIn(BaseModel):
    last_id: int = Field(ge=0)


class StateIn(BaseModel):
    """Блокировка/очистка чата: для себя или для обоих."""

    action: str = Field(pattern="^(block|unblock|clear)$")
    scope: str = Field(default="me", pattern="^(me|both)$")


class EditIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class ForwardIn(BaseModel):
    chat_id: int


class MuteIn(BaseModel):
    muted: bool


class PinIn(BaseModel):
    """message_id = 0 — снять закреп."""

    message_id: int = Field(default=0, ge=0)


# ----------------------------------------------------------------- списки

@router.get("/chats")
def list_chats(
    folder_id: int = 0,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    my_chat_ids = select(ChatMember.chat_id).where(ChatMember.user_id == user.id)
    chats = db.scalars(select(Chat).where(Chat.id.in_(my_chat_ids))).unique().all()

    mine = {
        m.chat_id: m
        for m in db.scalars(select(ChatMember).where(ChatMember.user_id == user.id))
    }

    # исчезающие сообщения: подчищаем историю, пока её никто не видит
    for chat in chats:
        if chat.auto_delete:
            purge_expired(db, chat)

    # папка чатов — фильтр по её составу
    if folder_id:
        folder = db.get(Folder, folder_id)
        allowed = set()
        if folder is not None and folder.user_id == user.id:
            try:
                allowed = {int(x) for x in json.loads(folder.chat_ids or "[]")}
            except (ValueError, TypeError):
                allowed = set()
        chats = [c for c in chats if c.id in allowed]

    def sort_key(chat: Chat):
        last = db.scalar(
            select(func.max(Message.id)).where(Message.chat_id == chat.id)
        )
        member = mine.get(chat.id)
        # закреплённые чаты всегда наверху, дальше — по свежести сообщений
        return (1 if (member and member.pinned) else 0, last or 0, chat.created_at.timestamp())

    chats.sort(key=sort_key, reverse=True)
    return {"chats": [chat_payload(db, c, user) for c in chats]}


def _direct_between(db: Session, a: int, b: int) -> Chat | None:
    """Личный чат, в котором состоят оба пользователя."""
    has_a = (
        select(ChatMember.id)
        .where(ChatMember.chat_id == Chat.id, ChatMember.user_id == a)
        .exists()
    )
    has_b = (
        select(ChatMember.id)
        .where(ChatMember.chat_id == Chat.id, ChatMember.user_id == b)
        .exists()
    )
    return db.scalar(select(Chat).where(Chat.type == "direct", has_a, has_b))


@router.post("/chats")
def create_direct(
    payload: DirectIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    peer = db.scalar(select(User).where(User.username == payload.username.strip()))
    if peer is None:
        raise HTTPException(404, "Пользователь не найден")
    if peer.id == user.id:
        raise HTTPException(400, "Нельзя создать чат с самим собой")

    # Ищем уже существующий личный чат с этим человеком
    chat = _direct_between(db, user.id, peer.id)
    if chat is not None:
        return chat_payload(db, chat, user)

    # приватность: «кто может добавлять меня в чаты» — только для новых чатов
    who_can_add = str(load_settings(peer).get("who_can_add", "all"))
    if who_can_add != "all":
        allowed = (
            is_contact(db, peer.id, user.id) if who_can_add == "contacts" else False
        )
        if not allowed:
            raise HTTPException(
                403, "Этот человек ограничил добавление себя в чаты"
            )

    chat = Chat(type="direct", creator_id=user.id)
    db.add(chat)
    db.flush()
    db.add(ChatMember(chat_id=chat.id, user_id=user.id, role="owner"))
    db.add(ChatMember(chat_id=chat.id, user_id=peer.id, role="member"))
    db.commit()
    db.refresh(chat)
    return chat_payload(db, chat, user)


@router.post("/chats/group")
def create_group(
    payload: GroupIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    usernames = [u.strip() for u in payload.usernames if u.strip()]
    if len(set(usernames)) < 1:
        raise HTTPException(400, "Добавьте хотя бы одного участника")

    rows = db.scalars(select(User).where(User.username.in_(set(usernames)))).unique().all()
    found = {u.username: u for u in rows}
    missing = [u for u in usernames if u not in found]
    if missing:
        raise HTTPException(400, "Нет таких людей: " + ", ".join(missing))

    chat = Chat(type="group", title=payload.title.strip(), creator_id=user.id)
    db.add(chat)
    db.flush()
    db.add(ChatMember(chat_id=chat.id, user_id=user.id, role="owner"))
    for u in rows:
        if u.id != user.id:
            db.add(ChatMember(chat_id=chat.id, user_id=u.id, role="member"))
    db.commit()
    db.refresh(chat)
    return chat_payload(db, chat, user)


class ChannelIn(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    username: str = Field(min_length=3, max_length=32)


def _clean_handle(raw: str) -> str:
    return raw.strip().lstrip("@")


def _channel_by_handle(db: Session, handle: str) -> Chat | None:
    return db.scalar(
        select(Chat).where(Chat.type == "channel", func.lower(Chat.username) == handle.lower())
    )


@router.post("/channels")
def create_channel(
    payload: ChannelIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Создать канал: писать сможет только создатель."""
    handle = _clean_handle(payload.username)
    if not HANDLE_RE.match(handle):
        raise HTTPException(400, "Ник канала: 3–32 символа, буквы, цифры и _")
    if _channel_by_handle(db, handle) is not None:
        raise HTTPException(400, "Такой ник канала уже занят")

    chat = Chat(
        type="channel",
        title=payload.title.strip(),
        username=handle,
        creator_id=user.id,
    )
    db.add(chat)
    db.flush()
    db.add(ChatMember(chat_id=chat.id, user_id=user.id, role="owner"))
    db.commit()
    db.refresh(chat)
    return chat_payload(db, chat, user)


@router.get("/channels/{handle}")
def channel_info(
    handle: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Канал по @нику — даже до вступления."""
    chat = _channel_by_handle(db, _clean_handle(handle))
    if chat is None:
        raise HTTPException(404, "Канал не найден")
    return chat_payload(db, chat, user)


@router.post("/chats/{chat_id}/join")
def join_channel(
    chat_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    chat = db.get(Chat, chat_id)
    if chat is None:
        raise HTTPException(404, "Чат не найден")
    if chat.type != "channel":
        raise HTTPException(400, "Вступить можно только в канал")
    if get_membership(db, chat_id, user) is None:
        db.add(ChatMember(chat_id=chat_id, user_id=user.id, role="member"))
        db.commit()
    return chat_payload(db, chat, user)


@router.get("/chats/{chat_id}")
def chat_details(
    chat_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    chat, member = require_viewer(db, chat_id, user)
    data = chat_payload(db, chat, user)
    data["can_add"] = chat.type == "group"
    data["can_write"] = chat.type != "channel" or (member is not None and member.role == "owner")
    return data


@router.patch("/chats/{chat_id}")
def rename_chat(
    chat_id: int,
    payload: RenameIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    chat, member = require_member(db, chat_id, user)
    if chat.type not in ("group", "channel"):
        raise HTTPException(400, "Переименовать можно только группу или канал")
    if chat.type == "channel" and member.role != "owner":
        raise HTTPException(403, "Переименовать канал может только его создатель")
    chat.title = payload.title.strip() or chat.title
    db.commit()
    return chat_payload(db, chat, user)


@router.post("/chats/{chat_id}/members")
def add_member(
    chat_id: int,
    payload: MemberIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    chat, _ = require_member(db, chat_id, user)
    if chat.type == "channel":
        raise HTTPException(400, "Участники канала подключаются сами через поиск")
    if chat.type != "group":
        raise HTTPException(400, "В личный чат нельзя добавить людей")
    target = db.scalar(select(User).where(User.username == payload.username.strip()))
    if target is None:
        raise HTTPException(404, "Пользователь не найден")
    if get_membership(db, chat_id, target):
        raise HTTPException(400, "Этот человек уже в чате")
    db.add(ChatMember(chat_id=chat_id, user_id=target.id, role="member"))
    db.commit()
    return chat_payload(db, chat, user)


@router.delete("/chats/{chat_id}/members/me")
def leave_chat(
    chat_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    chat, member = require_member(db, chat_id, user)
    if chat.type == "direct":
        raise HTTPException(400, "Из личного чата выходить не нужно")
    if chat.type == "channel":
        if member.role == "owner":
            raise HTTPException(400, "Создатель не может покинуть свой канал")
        db.delete(member)  # подписчик отписывается, канал остаётся
        db.commit()
        return {"ok": True}
    if member.role == "owner":
        rest = db.scalars(
            select(ChatMember).where(
                ChatMember.chat_id == chat_id, ChatMember.user_id != user.id
            )
        ).all()
        if rest:
            rest[0].role = "owner"
            chat.creator_id = rest[0].user_id
    db.delete(member)
    if not db.scalar(select(ChatMember.id).where(ChatMember.chat_id == chat_id)):
        db.delete(chat)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------- блокировка и очистка

@router.post("/chats/{chat_id}/state")
def set_chat_state(
    chat_id: int,
    payload: StateIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Блокировка и очистка истории.

    ``scope="me"`` — только для меня, ``scope="both"`` — для обоих.
    """
    chat, member = require_member(db, chat_id, user)
    if payload.action in {"block", "unblock"} and chat.type != "direct":
        raise HTTPException(400, "Блокировать можно только личный чат")

    members = db.scalars(
        select(ChatMember).where(ChatMember.chat_id == chat_id)
    ).all()
    targets = members if payload.scope == "both" else [member]

    for m in targets:
        row = db.scalar(
            select(ChatState).where(
                ChatState.chat_id == chat_id, ChatState.user_id == m.user_id
            )
        )
        if row is None:
            row = ChatState(chat_id=chat_id, user_id=m.user_id)
            db.add(row)
        if payload.action == "block":
            row.blocked = True
        elif payload.action == "unblock":
            row.blocked = False
        else:  # clear
            row.cleared_at = utcnow()

    if payload.action == "clear":
        last_id = db.scalar(select(func.max(Message.id)).where(Message.chat_id == chat_id)) or 0
        for m in targets:
            m.last_read_id = max(m.last_read_id, last_id)
        _TYPING.pop(chat_id, None)

    db.commit()
    return chat_payload(db, chat, user)


# --------------------------------------------------------------- сообщения

@router.get("/chats/{chat_id}/messages")
def get_messages(
    chat_id: int,
    after: int = 0,
    before: int = 0,
    limit: int = 60,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Сообщения чата.

    ``after`` — только новые (для опроса), ``before`` — история старше
    сообщения (для кнопки «показать более ранние»).
    """
    chat, member = require_viewer(db, chat_id, user)
    limit = min(max(limit, 1), 200)

    # исчезающие сообщения: убираем протухшее до выдачи истории
    if chat.auto_delete:
        purge_expired(db, chat)

    # очищенная для меня история остаётся скрытой
    _, cleared = state_flags(db, chat_id, user.id)
    base = [Message.chat_id == chat_id]
    if cleared is not None:
        base.append(Message.created_at > cleared)

    if after > 0:
        rows = db.scalars(
            select(Message)
            .where(*base, Message.id > after)
            .order_by(Message.id.asc())
            .limit(200)
        ).all()
    elif before > 0:
        rows = db.scalars(
            select(Message)
            .where(*base, Message.id < before)
            .order_by(Message.id.desc())
            .limit(limit)
        ).all()
        rows = list(reversed(rows))
    else:
        rows = db.scalars(
            select(Message)
            .where(*base)
            .order_by(Message.id.desc())
            .limit(limit)
        ).all()
        rows = list(reversed(rows))

    # есть ли что-то старше первого показанного сообщения
    has_more = False
    if rows:
        has_more = (
            db.scalar(
                select(Message.id)
                .where(*base, Message.id < rows[0].id)
                .limit(1)
            )
            is not None
        )

    # индикатор «печатает…» живёт по TTL и очищается при отправке сообщения
    now = time.time()
    typers = [
        user_brief(db.get(User, uid), db, user)
        for uid, expiry in list(_TYPING.get(chat_id, {}).items())
        if expiry > now and uid != user.id
    ]
    typers = [t for t in typers if t]

    payload = chat_payload(db, chat, user)
    return {
        "chat": payload,
        "messages": [message_payload(db, m, user, chat) for m in rows],
        "typers": typers,
        "my_last_read": member.last_read_id if member else 0,
        "has_more": has_more,
        # в канале писать может только его создатель
        "can_write": chat.type != "channel" or (member is not None and member.role == "owner"),
    }


@router.post("/chats/{chat_id}/messages")
async def send_message(
    chat_id: int,
    text: str = Form(default="", max_length=4000),
    image: UploadFile | None = File(default=None),
    audio: UploadFile | None = File(default=None),
    reply_to: int = Form(default=0),
    poll: str = Form(default="", max_length=4000),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    chat, member = require_member(db, chat_id, user)
    if chat.type == "channel" and member.role != "owner":
        raise HTTPException(403, "Писать в канал может только его создатель")
    blocked, _ = state_flags(db, chat_id, user.id)
    if blocked:
        raise HTTPException(403, "Чат заблокирован")

    # ответ: цитируемое сообщение должно жить в этом же чате
    if reply_to:
        src = db.get(Message, reply_to)
        if src is None or src.chat_id != chat_id:
            raise HTTPException(400, "Сообщение для ответа не найдено")
        _, cleared_src = state_flags(db, chat_id, user.id)
        if cleared_src is not None and src.created_at <= cleared_src:
            raise HTTPException(400, "Сообщение удалено из истории")

    text = text.strip()
    image_name = ""
    audio_name = ""

    if image is not None and image.filename:
        data = await image.read()
        if len(data) > MAX_IMAGE:
            raise HTTPException(400, "Изображение больше 8 МБ")
        ext = Path(image.filename).suffix.lower()
        if ext not in ALLOWED_EXT:
            raise HTTPException(400, "Нужен файл изображения (png, jpg, gif, webp)")
        image_name = f"msg_{uuid.uuid4().hex[:12]}{ext}"
        (UPLOAD_DIR / image_name).write_bytes(data)

    if audio is not None and audio.filename:
        data = await audio.read()
        if len(data) > MAX_AUDIO:
            raise HTTPException(400, "Голосовое сообщение больше 16 МБ")
        ext = Path(audio.filename).suffix.lower()
        if ext not in AUDIO_EXT:
            raise HTTPException(400, "Нужен аудиофайл (webm, mp4, m4a, mp3, ogg, wav)")
        audio_name = f"voice_{uuid.uuid4().hex[:12]}{ext}"
        (UPLOAD_DIR / audio_name).write_bytes(data)

    # опрос приходит JSON-строкой
    poll_data = ""
    if poll.strip():
        try:
            parsed = json.loads(poll)
        except (ValueError, TypeError):
            raise HTTPException(400, "Опрос повреждён")
        question = str(parsed.get("question") or "").strip()
        options = [str(o).strip() for o in (parsed.get("options") or []) if str(o).strip()]
        if not question or len(options) < 2:
            raise HTTPException(400, "Опросу нужны вопрос и минимум два варианта")
        poll_data = json.dumps(
            {"question": question[:200], "options": [o[:80] for o in options][:10]},
            ensure_ascii=False,
        )

    if not text and not image_name and not audio_name and not poll_data:
        raise HTTPException(400, "Сообщение пустое")

    msg = Message(
        chat_id=chat_id,
        sender_id=user.id,
        text=text,
        image=f"/static/uploads/{image_name}" if image_name else "",
        audio=f"/static/uploads/{audio_name}" if audio_name else "",
        reply_to_id=reply_to or None,
        poll=poll_data,
    )
    db.add(msg)
    db.flush()
    # отправитель сам «прочитал» всё, что было до этого
    member = get_membership(db, chat_id, user)
    member.last_read_id = max(member.last_read_id, msg.id)
    member.draft = ""  # отправили — черновик чата больше не нужен
    _TYPING.get(chat_id, {}).pop(user.id, None)
    db.commit()
    db.refresh(msg)
    return message_payload(db, msg, user, chat)


@router.delete("/messages/{message_id}")
def delete_message(
    message_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    msg = db.get(Message, message_id)
    if msg is None:
        raise HTTPException(404, "Сообщение не найдено")
    _, member = require_member(db, msg.chat_id, user)
    if msg.sender_id != user.id and member.role != "owner":
        raise HTTPException(403, "Можно удалять только свои сообщения")
    if msg.image:
        f = UPLOAD_DIR / Path(msg.image).name
        if f.exists():
            f.unlink()
    if msg.audio:
        f = UPLOAD_DIR / Path(msg.audio).name
        if f.exists():
            f.unlink()
    db.delete(msg)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------- правка и пересылка

@router.patch("/messages/{message_id}")
def edit_message(
    message_id: int,
    payload: EditIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Редактирование своего текстового сообщения (ставится метка «изменено»)."""
    msg = db.get(Message, message_id)
    if msg is None:
        raise HTTPException(404, "Сообщение не найдено")
    _, member = require_member(db, msg.chat_id, user)
    if msg.sender_id != user.id:
        raise HTTPException(403, "Редактировать можно только свои сообщения")
    blocked, _ = state_flags(db, msg.chat_id, user.id)
    if blocked:
        raise HTTPException(403, "Чат заблокирован")
    if not msg.text:
        raise HTTPException(400, "Редактировать можно только текстовые сообщения")

    text = payload.text.strip()
    if not text:
        raise HTTPException(400, "Сообщение пустое")

    msg.text = text
    msg.edited_at = utcnow()
    db.commit()
    db.refresh(msg)
    return message_payload(db, msg, user)


@router.post("/messages/{message_id}/forward")
def forward_message(
    message_id: int,
    payload: ForwardIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Пересылка сообщения в другой чат (как в Telegram, с указанием источника)."""
    src = db.get(Message, message_id)
    if src is None:
        raise HTTPException(404, "Сообщение не найдено")
    source_chat, _ = require_viewer(db, src.chat_id, user)  # источник должен быть виден

    target, member = require_member(db, payload.chat_id, user)
    if target.type == "channel" and member.role != "owner":
        raise HTTPException(403, "Писать в канал может только его создатель")
    blocked, _ = state_flags(db, target.id, user.id)
    if blocked:
        raise HTTPException(403, "Чат заблокирован")
    if not (src.text or src.image or src.audio or src.poll):
        raise HTTPException(400, "Пустое сообщение")

    # откуда переслали: название группы/канала, в личке — автор исходника
    if source_chat.type == "direct":
        origin = (db.get(User, src.sender_id) or user).name
    else:
        origin = source_chat.title or source_chat.username or "Kofi"

    msg = Message(
        chat_id=target.id,
        sender_id=user.id,
        text=src.text or "",
        image=src.image or "",
        audio=src.audio or "",
        poll=src.poll or "",
        forwarded_from=origin[:80],
    )
    db.add(msg)
    db.flush()
    member = get_membership(db, target.id, user)
    member.last_read_id = max(member.last_read_id, msg.id)
    db.commit()
    db.refresh(msg)
    return message_payload(db, msg, user, target)


# ------------------------------------------------- тишина, закреп, поиск

@router.post("/chats/{chat_id}/mute")
def mute_chat(
    chat_id: int,
    payload: MuteIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """«Тихий чат»: уведомления этого чата выключены для меня."""
    _, member = require_member(db, chat_id, user)
    member.muted = bool(payload.muted)
    db.commit()
    return {"chat_id": chat_id, "muted": member.muted}


@router.post("/chats/{chat_id}/pin")
def pin_message(
    chat_id: int,
    payload: PinIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Закрепить сообщение в чате (в канале — только создателем)."""
    chat, member = require_member(db, chat_id, user)
    if payload.message_id:
        msg = db.get(Message, payload.message_id)
        if msg is None or msg.chat_id != chat.id:
            raise HTTPException(404, "Сообщение не найдено")
        if chat.type == "channel" and member.role != "owner":
            raise HTTPException(403, "В канале закрепляет только создатель")
        chat.pinned_message_id = msg.id
    else:
        chat.pinned_message_id = None
    db.commit()
    db.refresh(chat)
    return chat_payload(db, chat, user)


@router.get("/chats/{chat_id}/search")
def search_in_chat(
    chat_id: int,
    q: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Поиск по сообщениям внутри чата (для панели поиска в переписке)."""
    chat, _ = require_viewer(db, chat_id, user)
    q = q.strip()
    if not q:
        raise HTTPException(400, "Введите запрос")

    _, cleared = state_flags(db, chat_id, user.id)
    conds = [
        Message.chat_id == chat_id,
        Message.text.contains(q, autoescape=True),
        Message.system == "",  # служебные (звонки) не ищем
    ]
    if cleared is not None:
        conds.append(Message.created_at > cleared)

    rows = db.scalars(
        select(Message).where(*conds).order_by(Message.id.desc()).limit(50)
    ).all()
    return {
        "chat_id": chat_id,
        "query": q,
        "messages": [message_payload(db, m, user, chat) for m in rows],
    }


@router.post("/chats/{chat_id}/read")
def mark_read(
    chat_id: int,
    payload: ReadIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    _, member = require_member(db, chat_id, user)
    if payload.last_id > member.last_read_id:
        member.last_read_id = payload.last_id
        db.commit()
    return {"last_read_id": member.last_read_id}


@router.post("/chats/{chat_id}/typing")
def typing(
    chat_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    chat, member = require_member(db, chat_id, user)
    if chat.type == "channel" and member.role != "owner":
        raise HTTPException(403, "Писать в канал может только его создатель")
    blocked, _ = state_flags(db, chat_id, user.id)
    if blocked:
        raise HTTPException(403, "Чат заблокирован")
    _TYPING.setdefault(chat_id, {})[user.id] = time.time() + TYPING_TTL
    return {"ok": True}


# ------------------------------------------------------------------- поиск

@router.get("/search")
def search(
    q: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    term = q.strip()
    if not term:
        return {"users": [], "channels": []}
    like = f"%{term}%"
    rows = db.scalars(
        select(User)
        .where(
            User.id != user.id,
            or_(User.username.ilike(like), User.display_name.ilike(like)),
        )
        .order_by(User.username)
        .limit(30)
    ).all()

    # поиск по номеру телефона — если владелец не запретил его в настройках
    digits = re.sub(r"\D", "", term)
    if len(digits) >= 4:
        known = {u.id for u in rows}
        for u in db.scalars(
            select(User).where(User.id != user.id, User.phone != "")
        ).all():
            if u.id in known or digits not in re.sub(r"\D", "", u.phone):
                continue
            s = load_settings(u)
            if s.get("phone_search", True) is False:
                continue
            mode = str(s.get("phone", "contacts"))
            if mode == "nobody":
                continue
            if mode == "contacts" and not is_contact(db, u.id, user.id):
                continue
            rows.append(u)

    # каналы: по @нику (с начала) или по названию
    handle = _clean_handle(term)
    channels = db.scalars(
        select(Chat)
        .where(
            Chat.type == "channel",
            or_(Chat.username.ilike(f"{handle}%"), Chat.title.ilike(like)),
        )
        .order_by(Chat.username)
        .limit(10)
    ).all()
    channel_rows = []
    for ch in channels:
        count = db.scalar(
            select(func.count()).select_from(ChatMember).where(ChatMember.chat_id == ch.id)
        )
        channel_rows.append({
            "id": ch.id,
            "title": ch.title,
            "username": ch.username,
            "avatar": "",
            "members_count": count,
            "joined": get_membership(db, ch.id, user) is not None,
            "is_creator": ch.creator_id == user.id,
        })

    return {"users": [user_brief(u, db, user) for u in rows], "channels": channel_rows}


@router.get("/unread")
def unread(user: User = Depends(require_user), db: Session = Depends(get_db)):
    members = db.scalars(
        select(ChatMember).where(ChatMember.user_id == user.id)
    ).all()
    total = sum(unread_count(db, m.chat_id, m) for m in members)
    return {"chats": total}
