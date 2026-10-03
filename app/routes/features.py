"""Функции уровня Telegram: реакции, опросы, избранное, папки, черновики,
исчезающие сообщения, истории, устройства, двухэтапный вход и экспорт."""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import UPLOAD_DIR, get_db
from ..helpers import (
    _poll,
    _reactions,
    chat_payload,
    display_text,
    is_contact,
    iso,
    load_settings,
    message_payload,
    purge_expired,
    require_member,
    require_user,
    require_viewer,
    save_settings,
    user_brief,
)
from ..models import (
    Chat,
    ChatMember,
    Folder,
    Message,
    PollVote,
    Reaction,
    Story,
    StoryView,
    User,
    UserSession,
    utcnow,
)
from ..security import hash_password, verify_password

router = APIRouter(prefix="/api", tags=["features"])

STORY_TTL_HOURS = 24          # истории живут сутки
STORY_LIMIT_FREE = 1          # лимит публикаций в сутки (без Premium)
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_IMAGE = 8 * 1024 * 1024


class ReactionIn(BaseModel):
    emoji: str = Field(min_length=1, max_length=8)


class VoteIn(BaseModel):
    option: int = Field(ge=0, le=20)


class FolderIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    icon: str = Field(default="msg", max_length=24)
    chat_ids: list[int] = Field(default_factory=list, max_length=200)


class DraftIn(BaseModel):
    text: str = Field(default="", max_length=4000)


class PinChatIn(BaseModel):
    pinned: bool


class AutoDeleteIn(BaseModel):
    """Часы хранения истории: 0 — выключено."""

    hours: int = Field(default=0, ge=0, le=24 * 365)


class SettingsIn(BaseModel):
    settings: dict


class TwoFaIn(BaseModel):
    password: str = Field(min_length=4, max_length=128)
    enable: bool = True


# ------------------------------------------------------- реакции и голосование

@router.post("/messages/{message_id}/reactions")
def toggle_reaction(
    message_id: int,
    payload: ReactionIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Поставить или снять реакцию (как в Telegram: повторный клик убирает)."""
    msg = db.get(Message, message_id)
    if msg is None:
        raise HTTPException(404, "Сообщение не найдено")
    require_viewer(db, msg.chat_id, user)

    emoji = payload.emoji.strip()
    if not emoji:
        raise HTTPException(400, "Пустая реакция")
    row = db.scalar(
        select(Reaction).where(
            Reaction.message_id == msg.id,
            Reaction.user_id == user.id,
            Reaction.emoji == emoji,
        )
    )
    if row is not None:
        db.delete(row)
    else:
        # больше трёх своих реакций на одно сообщение не держим
        mine = db.scalars(
            select(Reaction).where(
                Reaction.message_id == msg.id, Reaction.user_id == user.id
            )
        ).all()
        if len(mine) >= 3:
            db.delete(mine[0])
        db.add(Reaction(message_id=msg.id, user_id=user.id, emoji=emoji))
    db.commit()
    return {"message_id": msg.id, "reactions": _reactions(db, msg, user)}


@router.post("/messages/{message_id}/vote")
def vote_poll(
    message_id: int,
    payload: VoteIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Голос в опросе; повторное нажатие по своему варианту снимает голос."""
    msg = db.get(Message, message_id)
    if msg is None:
        raise HTTPException(404, "Сообщение не найдено")
    require_viewer(db, msg.chat_id, user)
    if not msg.poll:
        raise HTTPException(400, "Это не опрос")
    try:
        options = json.loads(msg.poll).get("options") or []
    except (ValueError, TypeError):
        raise HTTPException(400, "Опрос повреждён")
    if payload.option >= len(options):
        raise HTTPException(400, "Такого варианта нет")

    row = db.scalar(
        select(PollVote).where(
            PollVote.message_id == msg.id, PollVote.user_id == user.id
        )
    )
    if row is None:
        db.add(PollVote(message_id=msg.id, user_id=user.id, option=payload.option))
    elif row.option == payload.option:
        db.delete(row)  # снять голос
    else:
        row.option = payload.option
    db.commit()
    return {"message_id": msg.id, "poll": _poll(db, msg, user)}


# --------------------------------------------------------------- избранное

@router.get("/saved")
def saved_chat(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """«Избранное» — личный блокнот: создаётся при первом открытии."""
    chat = db.scalar(
        select(Chat)
        .join(ChatMember, ChatMember.chat_id == Chat.id)
        .where(Chat.type == "saved", ChatMember.user_id == user.id)
    )
    if chat is None:
        chat = Chat(type="saved", title="Избранное", creator_id=user.id)
        db.add(chat)
        db.flush()
        db.add(ChatMember(chat_id=chat.id, user_id=user.id, role="owner"))
        db.commit()
        db.refresh(chat)
    return chat_payload(db, chat, user)


# ------------------------------------------------------------------- папки

def _folder_payload(db: Session, f: Folder, user: User) -> dict:
    try:
        ids = [int(x) for x in json.loads(f.chat_ids or "[]")]
    except (ValueError, TypeError):
        ids = []
    mine = {
        m.chat_id
        for m in db.scalars(select(ChatMember).where(ChatMember.user_id == user.id))
    }
    ids = [i for i in ids if i in mine]
    return {"id": f.id, "name": f.name, "icon": f.icon, "chat_ids": ids}


@router.get("/folders")
def list_folders(user: User = Depends(require_user), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(Folder)
        .where(Folder.user_id == user.id)
        .order_by(Folder.position, Folder.id)
    ).all()
    return {"folders": [_folder_payload(db, f, user) for f in rows]}


@router.post("/folders")
def create_folder(
    payload: FolderIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    total = len(
        db.scalars(select(Folder).where(Folder.user_id == user.id)).all()
    )
    if total >= 10:
        raise HTTPException(400, "Можно держать не больше 10 папок")
    folder = Folder(
        user_id=user.id,
        name=payload.name.strip() or "Папка",
        icon=payload.icon.strip() or "msg",
        chat_ids=json.dumps(payload.chat_ids),
        position=total + 1,
    )
    db.add(folder)
    db.commit()
    db.refresh(folder)
    return _folder_payload(db, folder, user)


@router.patch("/folders/{folder_id}")
def update_folder(
    folder_id: int,
    payload: FolderIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    folder = db.get(Folder, folder_id)
    if folder is None or folder.user_id != user.id:
        raise HTTPException(404, "Папка не найдена")
    folder.name = payload.name.strip() or folder.name
    folder.icon = payload.icon.strip() or folder.icon
    folder.chat_ids = json.dumps(payload.chat_ids)
    db.commit()
    db.refresh(folder)
    return _folder_payload(db, folder, user)


@router.delete("/folders/{folder_id}")
def delete_folder(
    folder_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    folder = db.get(Folder, folder_id)
    if folder is None or folder.user_id != user.id:
        raise HTTPException(404, "Папка не найдена")
    db.delete(folder)
    db.commit()
    return {"ok": True}


# ------------------------------------- черновик, закреп чата, исчезающие

@router.post("/chats/{chat_id}/draft")
def save_draft(
    chat_id: int,
    payload: DraftIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    _, member = require_member(db, chat_id, user)
    member.draft = payload.text.strip()
    db.commit()
    return {"chat_id": chat_id, "draft": member.draft}


@router.post("/chats/{chat_id}/pin-chat")
def pin_chat(
    chat_id: int,
    payload: PinChatIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Важный чат: всегда наверху списка, как в Telegram."""
    _, member = require_member(db, chat_id, user)
    member.pinned = bool(payload.pinned)
    db.commit()
    return {"chat_id": chat_id, "pinned": member.pinned}


@router.post("/chats/{chat_id}/auto-delete")
def set_auto_delete(
    chat_id: int,
    payload: AutoDeleteIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Исчезающие сообщения: история чистится автоматически."""
    chat, member = require_member(db, chat_id, user)
    if chat.type == "channel" and member.role != "owner":
        raise HTTPException(403, "Настройку канала меняет только создатель")
    chat.auto_delete = int(payload.hours)
    purged = purge_expired(db, chat) if payload.hours else 0
    db.commit()
    data = chat_payload(db, chat, user)
    data["purged"] = purged
    return data


# ------------------------------------------------------- настройки и вход

@router.get("/settings")
def get_settings(user: User = Depends(require_user), db: Session = Depends(get_db)):
    return {
        "settings": load_settings(user),
        "two_fa": bool(user.password_hash2),
    }


@router.put("/settings")
def put_settings(
    payload: SettingsIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Записывает настройки приватности/оформления/уведомлений целиком."""
    data = payload.settings
    save_settings(user, data)
    db.commit()
    return {"settings": load_settings(user)}


@router.post("/me/2fa")
def set_two_fa(
    payload: TwoFaIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Код двухэтапной защиты: спрашивается при входе на новом устройстве."""
    if payload.enable:
        user.password_hash2 = hash_password(payload.password)
    else:
        if not user.password_hash2 or not verify_password(
            payload.password, user.password_hash2
        ):
            raise HTTPException(400, "Неверный код")
        user.password_hash2 = ""
    db.commit()
    return {"enabled": bool(user.password_hash2)}


# -------------------------------------------------------------- устройства

@router.get("/sessions")
def list_sessions(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Активные сессии: чем вошли, откуда и когда."""
    rows = db.scalars(
        select(UserSession)
        .where(UserSession.user_id == user.id)
        .order_by(UserSession.last_seen.desc())
    ).all()
    current = request.cookies.get("sid")
    return {
        "sessions": [
            {
                "token": r.token,
                "ip": r.ip or "—",
                "agent": r.agent or "Неизвестное устройство",
                "created_at": iso(r.created_at),
                "last_seen": iso(r.last_seen),
                "current": r.token == current,
            }
            for r in rows
        ]
    }


@router.delete("/sessions/{token}")
def revoke_session(
    token: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Завершить чужую сессию (удалённый выход с устройства)."""
    row = db.get(UserSession, token)
    if row is None or row.user_id != user.id:
        raise HTTPException(404, "Сессия не найдена")
    db.delete(row)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ экспорт

@router.get("/export")
def export_data(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Выгрузка своих данных: профиль, чаты и вся переписка в JSON."""
    chat_ids = [
        m.chat_id
        for m in db.scalars(select(ChatMember).where(ChatMember.user_id == user.id))
    ]
    chats = []
    for cid in chat_ids:
        chat = db.get(Chat, cid)
        if chat is None:
            continue
        msgs = db.scalars(
            select(Message).where(Message.chat_id == cid).order_by(Message.id)
        ).all()
        chats.append(
            {
                "id": chat.id,
                "type": chat.type,
                "title": chat.title,
                "username": chat.username,
                "messages": [
                    {
                        "id": m.id,
                        "from": (db.get(User, m.sender_id) or user).username,
                        "text": display_text(m, user.id),
                        "image": m.image,
                        "audio": m.audio,
                        "poll": m.poll,
                        "created_at": iso(m.created_at),
                    }
                    for m in msgs
                ],
            }
        )
    data = {
        "app": "Kofi",
        "exported_at": iso(utcnow()),
        "profile": {
            "username": user.username,
            "name": user.display_name,
            "email": user.email,
            "phone": user.phone,
            "status": user.status,
            "created_at": iso(user.created_at),
        },
        "settings": load_settings(user),
        "chats": chats,
    }
    return JSONResponse(
        content=data,
        headers={"Content-Disposition": 'attachment; filename="kofi_export.json"'},
    )


# ------------------------------------------------------------------ истории

def _story_visible(db: Session, story: Story, viewer: User) -> bool:
    """Кому показана история: все / контакты / близкие друзья."""
    if story.user_id == viewer.id:
        return True
    owner = db.get(User, story.user_id)
    if owner is None:
        return False
    if story.privacy == "all":
        return True
    if story.privacy == "contacts":
        return is_contact(db, owner.id, viewer.id)
    close = [str(x).lower() for x in (load_settings(owner).get("close_friends") or [])]
    return viewer.username.lower() in close


def _story_payload(db: Session, s: Story, viewer: User, viewed: set[int]) -> dict:
    author = db.get(User, s.user_id)
    return {
        "id": s.id,
        "text": s.text,
        "image": s.image,
        "privacy": s.privacy,
        "created_at": iso(s.created_at),
        "expires_at": iso(s.expires_at),
        "mine": s.user_id == viewer.id,
        "viewed": s.id in viewed,
        "author": user_brief(author, db, viewer) if author else None,
    }


@router.get("/stories")
def list_stories(user: User = Depends(require_user), db: Session = Depends(get_db)):
    now = utcnow()
    rows = db.scalars(
        select(Story).where(Story.expires_at > now).order_by(Story.id.desc())
    ).all()
    viewed = {
        v.story_id
        for v in db.scalars(select(StoryView).where(StoryView.user_id == user.id))
    }
    groups: dict[int, dict] = {}
    for s in rows:
        if not _story_visible(db, s, user):
            continue
        g = groups.setdefault(s.user_id, {"author": None, "items": []})
        if g["author"] is None:
            author = db.get(User, s.user_id)
            g["author"] = user_brief(author, db, user) if author else None
        g["items"].insert(0, _story_payload(db, s, user, viewed))

    out = []
    for g in groups.values():
        items = g["items"]
        out.append(
            {
                **g,
                "viewed": all(i["viewed"] for i in items),
            }
        )
    # своя история первыми, дальше — непросмотренные
    out.sort(
        key=lambda g: (
            0 if (g["author"] and g["author"]["is_me"]) else 1,
            1 if g["viewed"] else 0,
        )
    )
    return {"stories": out}


@router.post("/stories")
async def create_story(
    text: str = Form(default="", max_length=500),
    privacy: str = Form(default="all"),
    file: UploadFile | None = File(default=None),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Опубликовать историю: фото или текст, живёт 24 часа."""
    if privacy not in ("all", "contacts", "close"):
        raise HTTPException(400, "Неверная настройка приватности")

    day_ago = utcnow() - timedelta(hours=24)
    today = db.scalars(
        select(Story).where(Story.user_id == user.id, Story.created_at > day_ago)
    ).all()
    if len(today) >= STORY_LIMIT_FREE:
        raise HTTPException(400, "Лимит историй: 1 в сутки")

    image_name = ""
    if file is not None and file.filename:
        data = await file.read()
        if len(data) > MAX_IMAGE:
            raise HTTPException(400, "Изображение больше 8 МБ")
        ext = Path(file.filename).suffix.lower()
        if ext not in IMAGE_EXT:
            raise HTTPException(400, "Нужен файл изображения (png, jpg, gif, webp)")
        image_name = f"story_{uuid.uuid4().hex[:12]}{ext}"
        (UPLOAD_DIR / image_name).write_bytes(data)

    if not text.strip() and not image_name:
        raise HTTPException(400, "Пустая история")

    story = Story(
        user_id=user.id,
        text=text.strip(),
        image=f"/static/uploads/{image_name}" if image_name else "",
        privacy=privacy,
        expires_at=utcnow() + timedelta(hours=STORY_TTL_HOURS),
    )
    db.add(story)
    db.commit()
    db.refresh(story)
    return _story_payload(db, story, user, set())


@router.post("/stories/{story_id}/view")
def view_story(
    story_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Отметить историю просмотренной (для автора — попадает в список зрителей)."""
    story = db.get(Story, story_id)
    if story is None:
        raise HTTPException(404, "История не найдена")
    if not _story_visible(db, story, user):
        raise HTTPException(403, "История недоступна")
    seen = db.scalar(
        select(StoryView).where(
            StoryView.story_id == story_id, StoryView.user_id == user.id
        )
    )
    if seen is None:
        db.add(StoryView(story_id=story_id, user_id=user.id))
        db.commit()
    return {"ok": True}


@router.get("/stories/{story_id}/views")
def story_views(
    story_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Кто посмотрел историю — только для её автора."""
    story = db.get(Story, story_id)
    if story is None:
        raise HTTPException(404, "История не найдена")
    if story.user_id != user.id:
        raise HTTPException(403, "Список зрителей видит только автор")
    rows = db.scalars(
        select(StoryView).where(StoryView.story_id == story_id).order_by(
            StoryView.viewed_at.desc()
        )
    ).all()
    viewers = []
    for v in rows:
        u = db.get(User, v.user_id)
        if u is not None:
            viewers.append({**user_brief(u, db, user), "at": iso(v.viewed_at)})
    return {"story_id": story_id, "viewers": viewers}


@router.delete("/stories/{story_id}")
def delete_story(
    story_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    story = db.get(Story, story_id)
    if story is None:
        raise HTTPException(404, "История не найдена")
    if story.user_id != user.id:
        raise HTTPException(403, "Удалить можно только свою историю")
    if story.image:
        f = UPLOAD_DIR / Path(story.image).name
        if f.exists():
            f.unlink()
    db.delete(story)
    db.commit()
    return {"ok": True}
