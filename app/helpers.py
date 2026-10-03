"""Общие зависимости FastAPI и сериализация."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Cookie, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import UPLOAD_DIR, get_db
from .models import (
    Chat,
    ChatMember,
    ChatState,
    Message,
    PollVote,
    Reaction,
    User,
    UserSession,
    utcnow,
)

SESSION_COOKIE = "sid"

# считаем пользователя «в сети», если сервер видел его активность недавно
ONLINE_WINDOW = 60
TOUCH_INTERVAL = 10  # не пишем last_seen чаще, чем раз в N секунд


def current_user(
    sid: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    db: Session = Depends(get_db),
) -> User | None:
    if not sid:
        return None
    row = db.get(UserSession, sid)
    if row is None:
        return None
    user = db.get(User, row.user_id)
    if user is None:
        return None

    # «пинг»: пока вкладка живая, статус онлайн обновляется сам
    if time_left(user.last_seen) > TOUCH_INTERVAL:
        user.last_seen = utcnow()
        db.commit()
    return user


def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="Требуется вход в аккаунт")
    return user


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def time_left(dt: datetime | None) -> int:
    """Секунд с последней активности (для статуса «был(а) недавно»)."""
    if dt is None:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int((datetime.now(timezone.utc) - dt).total_seconds())


def _naive(dt: datetime | None) -> datetime | None:
    """Убираем таймзону, чтобы даты из SQLite сравнивались корректно."""
    if dt is not None and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


# ---------------------------------------------------- настройки и приватность

def load_settings(u: User) -> dict:
    """Настройки пользователя (приватность, оформление, звуки) из JSON."""
    try:
        data = json.loads(u.settings or "{}")
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def save_settings(u: User, data: dict) -> None:
    u.settings = json.dumps(data, ensure_ascii=False)


def is_contact(db: Session, a: int, b: int) -> bool:
    """Контакты в Kofi — люди, с которыми уже был личный чат."""
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
    return (
        db.scalar(
            select(Chat.id).where(Chat.type == "direct", has_a, has_b).limit(1)
        )
        is not None
    )


def _visible_to(db: Session, owner: User, viewer: User | None, key: str, default: str) -> bool:
    """Разрешает ли owner показывать viewer свой параметр приватности."""
    if viewer is None or owner.id == viewer.id:
        return True
    mode = str(load_settings(owner).get(key, default))
    if mode == "all":
        return True
    if mode == "nobody":
        return False
    if mode == "contacts":
        return is_contact(db, owner.id, viewer.id)
    return True


def user_brief(u: User, db: Session, viewer: User | None = None) -> dict:
    online = time_left(u.last_seen) < ONLINE_WINDOW
    show_seen = _visible_to(db, u, viewer, "last_seen", "all")
    show_phone = bool(u.phone) and _visible_to(db, u, viewer, "phone", "contacts")
    out = {
        "id": u.id,
        "username": u.username,
        "name": u.name,
        "avatar": u.avatar,
        "status": u.status,
        "online": online and show_seen,
        "last_seen": iso(u.last_seen) if show_seen else None,
        "is_me": bool(viewer and viewer.id == u.id),
    }
    if show_phone:
        out["phone"] = u.phone
    # телефон, адрес и часы работы видны в профиле по настройке владельца
    if u.address and _visible_to(db, u, viewer, "profile", "all"):
        out["address"] = u.address
    if u.hours and _visible_to(db, u, viewer, "profile", "all"):
        out["hours"] = u.hours
    return out


def get_membership(db: Session, chat_id: int, user: User) -> ChatMember | None:
    return db.scalar(
        select(ChatMember).where(ChatMember.chat_id == chat_id, ChatMember.user_id == user.id)
    )


def require_member(db: Session, chat_id: int, user: User) -> tuple[Chat, ChatMember]:
    chat = db.get(Chat, chat_id)
    if chat is None:
        raise HTTPException(404, "Чат не найден")
    member = get_membership(db, chat_id, user)
    if member is None:
        raise HTTPException(403, "Вы не состоите в этом чате")
    return chat, member


def require_viewer(db: Session, chat_id: int, user: User) -> tuple[Chat, ChatMember | None]:
    """Член чата, а для канала — любой желающий (смотрящий без вступления)."""
    chat = db.get(Chat, chat_id)
    if chat is None:
        raise HTTPException(404, "Чат не найден")
    member = get_membership(db, chat_id, user)
    if member is None and chat.type != "channel":
        raise HTTPException(403, "Вы не состоите в этом чате")
    return chat, member


def state_flags(db: Session, chat_id: int, user_id: int) -> tuple[bool, datetime | None]:
    """(заблокирован ли чат, история очищена для меня после) — без записи в БД."""
    row = db.execute(
        select(ChatState.blocked, ChatState.cleared_at).where(
            ChatState.chat_id == chat_id, ChatState.user_id == user_id
        )
    ).first()
    if row is None:
        return False, None
    return bool(row[0]), row[1]


def purge_expired(db: Session, chat: Chat) -> int:
    """Исчезающие сообщения: удаляет историю старше режима чата.

    Вызывается при чтении/отправке сообщений, чтобы чат всегда отдавался
    уже без протухших сообщений.
    """
    if not chat.auto_delete:
        return 0
    cutoff = utcnow() - timedelta(hours=int(chat.auto_delete))
    rows = db.scalars(
        select(Message).where(Message.chat_id == chat.id, Message.created_at < cutoff)
    ).all()
    for m in rows:
        for link in (m.image, m.audio):
            if link:
                f = UPLOAD_DIR / Path(link).name
                if f.exists():
                    f.unlink()
        db.delete(m)
    if rows:
        db.commit()
    return len(rows)


def unread_count(db: Session, chat_id: int, member: ChatMember) -> int:
    _, cleared = state_flags(db, chat_id, member.user_id)
    conds = [
        Message.chat_id == chat_id,
        Message.id > member.last_read_id,
        Message.sender_id != member.user_id,
    ]
    if cleared is not None:
        conds.append(Message.created_at > cleared)
    return db.scalar(select(func.count()).select_from(Message).where(*conds))


def _reply_brief(db: Session, m: Message, viewer: User) -> dict | None:
    """Квота для ответа: кто ответил и на что (без вложенных квот)."""
    if not m.reply_to_id:
        return None
    src = db.get(Message, m.reply_to_id)
    if src is None or src.chat_id != m.chat_id:
        return None
    _, cleared = state_flags(db, m.chat_id, viewer.id)
    if cleared is not None and _naive(src.created_at) <= _naive(cleared):
        return None  # оригинал очищен для меня — квоты не показываем
    sender = db.get(User, src.sender_id)
    return {
        "id": src.id,
        "text": display_text(src, viewer.id)[:120],
        "image": bool(src.image),
        "audio": bool(src.audio),
        "mine": src.sender_id == viewer.id,
        "sender": sender.name if sender else "удалённый аккаунт",
    }


def _reactions(db: Session, m: Message, viewer: User) -> list[dict]:
    """Реакции сообщения: эмодзи, количество и моя ли это реакция."""
    rows = db.execute(
        select(Reaction.emoji, Reaction.user_id).where(Reaction.message_id == m.id)
    ).all()
    if not rows:
        return []
    grouped: dict[str, dict] = {}
    for emoji, uid in rows:
        g = grouped.setdefault(emoji, {"emoji": emoji, "count": 0, "mine": False})
        g["count"] += 1
        if uid == viewer.id:
            g["mine"] = True
    return sorted(grouped.values(), key=lambda r: -r["count"])


def _poll(db: Session, m: Message, viewer: User) -> dict | None:
    """Опрос сообщения: варианты, голоса в процентах и мой голос."""
    if not m.poll:
        return None
    try:
        data = json.loads(m.poll)
    except (ValueError, TypeError):
        return None
    options = [str(o)[:80] for o in (data.get("options") or [])][:10]
    if not options:
        return None
    counts = [0] * len(options)
    my_vote = None
    rows = db.execute(
        select(PollVote.option, PollVote.user_id).where(PollVote.message_id == m.id)
    ).all()
    for opt, uid in rows:
        if 0 <= opt < len(counts):
            counts[opt] += 1
        if uid == viewer.id:
            my_vote = opt
    total = sum(counts)
    return {
        "question": str(data.get("question") or "")[:200],
        "options": [
            {
                "text": text,
                "votes": counts[i],
                "pct": round(counts[i] * 100 / total) if total else 0,
            }
            for i, text in enumerate(options)
        ],
        "total": total,
        "my_vote": my_vote,
    }


def call_info(m: Message) -> dict | None:
    """Итог звонка из служебного сообщения: разобранный JSON."""
    if m.system != "call":
        return None
    try:
        info = json.loads(m.text or "{}")
    except (TypeError, ValueError):
        info = {}
    return {
        "answered": bool(info.get("answered")),
        "seconds": max(0, int(info.get("seconds") or 0)),
        "kind": "video" if info.get("kind") == "video" else "audio",
    }


def display_text(m: Message, viewer_id: int) -> str:
    """Текст сообщения для показа: у служебного о звонке — человеческая подпись."""
    if m.system != "call":
        return m.text
    info = call_info(m) or {"answered": False, "seconds": 0}
    if not info["answered"]:
        return "Пропущенный звонок"
    direction = "Исходящий" if m.sender_id == viewer_id else "Входящий"
    secs = info["seconds"]
    return f"{direction} звонок · {secs // 60}:{secs % 60:02d}"


def message_payload(db: Session, m: Message, viewer: User, chat: Chat | None = None) -> dict:
    """Сообщение + статус доставки/прочтения для моих сообщений."""
    status = "sent"
    if m.sender_id == viewer.id:
        others = db.scalar(
            select(func.count())
            .select_from(ChatMember)
            .where(ChatMember.chat_id == m.chat_id, ChatMember.user_id != viewer.id)
        )
        if others == 0:
            status = "read"
        else:
            read_by = db.scalar(
                select(func.count())
                .select_from(ChatMember)
                .where(
                    ChatMember.chat_id == m.chat_id,
                    ChatMember.user_id != viewer.id,
                    ChatMember.last_read_id >= m.id,
                )
            )
            if read_by == others:
                status = "read"
            elif read_by > 0:
                status = "read_some"
    return {
        "id": m.id,
        "chat_id": m.chat_id,
        "text": display_text(m, viewer.id),
        "image": m.image,
        "audio": m.audio or "",
        "system": m.system or "",
        "call": call_info(m),
        "edited": iso(m.edited_at),
        "forwarded_from": m.forwarded_from or "",
        "reply_to": m.reply_to_id or 0,
        "reply": _reply_brief(db, m, viewer),
        "reactions": _reactions(db, m, viewer),
        "poll": _poll(db, m, viewer),
        "created_at": iso(m.created_at),
        "mine": m.sender_id == viewer.id,
        "status": status,
        "sender": {
            "id": m.sender.id,
            "username": m.sender.username,
            "name": m.sender.name,
            "avatar": m.sender.avatar,
        },
    }


def chat_payload(
    db: Session, chat: Chat, viewer: User, with_last: bool = True
) -> dict:
    members = db.scalars(
        select(ChatMember).where(ChatMember.chat_id == chat.id)
    ).unique().all()
    mine = next((m for m in members if m.user_id == viewer.id), None)

    member_users = [db.get(User, m.user_id) for m in members]
    member_users = [u for u in member_users if u is not None]

    peer = None
    title = chat.title
    if chat.type == "saved":
        title = "Избранное"
    elif chat.type == "direct":
        peer = next((u for u in member_users if u.id != viewer.id), None)
        if peer is not None:
            title = peer.name
    elif not title:
        title = ", ".join(u.name for u in member_users if u.id != viewer.id)[:80] or "Группа"

    last = db.scalar(
        select(Message).where(Message.chat_id == chat.id).order_by(Message.id.desc()).limit(1)
    )

    # личное состояние участника: блокировка и очищенная история
    states = {
        s.user_id: s
        for s in db.scalars(
            select(ChatState).where(ChatState.chat_id == chat.id)
        ).unique().all()
    }
    mine_state = states.get(viewer.id)
    blocked = bool(mine_state and mine_state.blocked)
    blocked_all = bool(member_users) and all(
        (states.get(u.id) is not None and states[u.id].blocked) for u in member_users
    )
    cleared = mine_state.cleared_at if mine_state else None
    if cleared is not None and last is not None and _naive(last.created_at) <= _naive(cleared):
        last = None  # очищено для меня — не показываю и в списке чатов

    # закреплённое сообщение (не показываю, если оно попало в очищенную историю)
    pinned = db.get(Message, chat.pinned_message_id) if chat.pinned_message_id else None
    if pinned is not None and (
        pinned.chat_id != chat.id
        or (cleared is not None and _naive(pinned.created_at) <= _naive(cleared))
    ):
        pinned = None

    return {
        "id": chat.id,
        "type": chat.type,
        "title": title,
        "username": chat.username or "",
        "joined": mine is not None,
        "avatar": peer.avatar if peer else "",
        "peer": user_brief(peer, db, viewer) if peer else None,
        "blocked": blocked,
        "blocked_all": blocked_all,
        "muted": bool(mine and mine.muted),
        # чат закреплён в моём списке и его черновик
        "pin_chat": bool(mine and mine.pinned),
        "draft": (mine.draft if mine and mine.draft else ""),
        "auto_delete": int(chat.auto_delete or 0),
        "pinned": message_payload(db, pinned, viewer, chat) if pinned else None,
        "members": [
            {**user_brief(u, db, viewer), "role": next(
                (m.role for m in members if m.user_id == u.id), "member"
            )}
            for u in member_users
        ],
        "members_count": len(member_users),
        "unread": unread_count(db, chat.id, mine) if mine else 0,
        "last_read_id": mine.last_read_id if mine else 0,
        "my_role": mine.role if mine else None,
        "created_at": iso(chat.created_at),
        "last_message": message_payload(db, last, viewer, chat) if (with_last and last) else None,
    }
