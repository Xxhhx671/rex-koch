"""Регистрация, вход, выход, профиль пользователя."""
from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import UPLOAD_DIR, get_db
from ..helpers import SESSION_COOKIE, current_user, load_settings, require_user, user_brief
from ..models import Chat, ChatMember, ChatState, Message, User, UserSession, utcnow
from ..security import hash_password, new_session_token, verify_password

router = APIRouter(prefix="/api", tags=["auth"])

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_\.]{3,30}$")
PHONE_RE = re.compile(r"^[+0-9][0-9\-\s()]{4,22}$")
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_AVATAR = 3 * 1024 * 1024


def _client_meta(request) -> tuple[str, str]:
    """IP и браузер входа — для раздела «Устройства»."""
    ip = request.client.host if request.client else ""
    agent = (request.headers.get("user-agent") or "")[:200]
    return ip, agent


class RegisterIn(BaseModel):
    username: str = Field(min_length=3, max_length=30)
    email: str = Field(min_length=5, max_length=255)
    password: str = Field(min_length=6, max_length=128)
    name: str = Field(default="", max_length=60)


class LoginIn(BaseModel):
    login: str
    password: str
    # код двухэтапной защиты, если он включён
    code: str = Field(default="", max_length=64)


class ProfileIn(BaseModel):
    name: str = Field(default="", max_length=60)
    status: str = Field(default="", max_length=120)
    username: str = Field(default="", max_length=30)
    phone: str = Field(default="", max_length=24)
    address: str = Field(default="", max_length=120)
    hours: str = Field(default="", max_length=120)


class DeleteMeIn(BaseModel):
    password: str


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="lax",
        path="/",
    )


@router.post("/register")
def register(
    payload: RegisterIn,
    response: Response,
    request: Request,
    db: Session = Depends(get_db),
):
    username = payload.username.strip()
    if not USERNAME_RE.match(username):
        raise HTTPException(400, "Ник: 3-30 символов, латиница, цифры, _ и .")
    if db.scalar(select(User).where(User.username == username)):
        raise HTTPException(400, "Такой ник уже занят")
    if db.scalar(select(User).where(User.email == payload.email.lower())):
        raise HTTPException(400, "Эта почта уже зарегистрирована")

    user = User(
        username=username,
        email=payload.email.lower(),
        password_hash=hash_password(payload.password),
        display_name=payload.name.strip() or username,
        last_seen=utcnow(),
    )
    db.add(user)
    db.flush()

    token = new_session_token()
    ip, agent = _client_meta(request)
    db.add(UserSession(token=token, user_id=user.id, ip=ip, agent=agent))
    db.commit()

    _set_cookie(response, token)
    return user_brief(user, db, user)


@router.post("/login")
def login(
    payload: LoginIn,
    response: Response,
    request: Request,
    db: Session = Depends(get_db),
):
    login_value = payload.login.strip().lower()
    user = db.scalar(
        select(User).where(
            (User.username == payload.login.strip()) | (User.email == login_value)
        )
    )
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(400, "Неверный ник/почта или пароль")

    # двухэтапная защита: без кода на новое устройство не пускаем
    if user.password_hash2:
        if not payload.code:
            raise HTTPException(401, "Двухэтапная защита: введите код")
        if not verify_password(payload.code, user.password_hash2):
            raise HTTPException(400, "Неверный код двухэтапной защиты")

    user.last_seen = utcnow()
    token = new_session_token()
    ip, agent = _client_meta(request)
    db.add(
        UserSession(token=token, user_id=user.id, ip=ip, agent=agent, last_seen=utcnow())
    )
    db.commit()
    _set_cookie(response, token)
    return user_brief(user, db, user)


@router.post("/logout")
def logout(response: Response, user: User = Depends(require_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(UserSession).where(UserSession.user_id == user.id)).all()
    for row in rows:
        db.delete(row)
    user.last_seen = utcnow()
    db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(
    request: Request,
    user: User | None = Depends(current_user),
    db: Session = Depends(get_db),
):
    if user is None:
        return {"user": None}
    user.last_seen = utcnow()

    # активность текущей сессии — чтобы в «Устройствах» было видно, где вы сейчас
    sid = request.cookies.get(SESSION_COOKIE) if request else None
    if sid:
        row = db.get(UserSession, sid)
        if row is not None:
            row.last_seen = utcnow()
    db.commit()
    return {
        "user": user_brief(user, db, user),
        "settings": load_settings(user),
        "two_fa": bool(user.password_hash2),
    }


@router.patch("/me")
def update_me(
    payload: ProfileIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    user.display_name = payload.name.strip() or user.username
    user.status = payload.status.strip() or "В сети"

    # телефон и бизнес-информация профиля
    phone = payload.phone.strip()
    if phone and not PHONE_RE.match(phone):
        raise HTTPException(400, "Телефон: только цифры, +, пробелы и дефисы")
    user.phone = phone
    user.address = payload.address.strip()
    user.hours = payload.hours.strip()

    # ник можно поменять — с проверкой на занятость
    new_nick = payload.username.strip()
    if new_nick and new_nick != user.username:
        if not USERNAME_RE.match(new_nick):
            raise HTTPException(400, "Ник: 3-30 символов, латиница, цифры, _ и .")
        taken = db.scalar(
            select(User).where(User.username == new_nick, User.id != user.id)
        )
        if taken:
            raise HTTPException(400, "Такой ник уже занят")
        user.username = new_nick

    user.last_seen = utcnow()
    db.commit()
    return user_brief(user, db, user)


@router.delete("/me")
def delete_me(
    payload: DeleteMeIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Удаление аккаунта: требует подтверждения паролем."""
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(400, "Неверный пароль")

    # чистим файлы профиля и свои сообщения с диска
    if user.avatar:
        f = UPLOAD_DIR / Path(user.avatar).name
        if f.exists():
            f.unlink()
    my_chat_ids = [
        m.chat_id
        for m in db.scalars(select(ChatMember).where(ChatMember.user_id == user.id))
    ]
    for m in db.scalars(select(Message).where(Message.sender_id == user.id)):
        for link in (m.image, m.audio):
            if link:
                f = UPLOAD_DIR / Path(link).name
                if f.exists():
                    f.unlink()

    # одиночные чаты, где больше никого не осталось, убираем совсем
    db.query(ChatMember).filter(ChatMember.user_id == user.id).delete()
    db.query(ChatState).filter(ChatState.user_id == user.id).delete()
    db.query(UserSession).filter(UserSession.user_id == user.id).delete()
    for cid in my_chat_ids:
        chat = db.get(Chat, cid)
        if chat is None:
            continue
        if chat.type == "direct" or not db.scalar(
            select(ChatMember.id).where(ChatMember.chat_id == cid).limit(1)
        ):
            db.delete(chat)
    db.delete(user)
    db.commit()
    return {"ok": True}


@router.post("/me/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    data = await file.read()
    if len(data) > MAX_AVATAR:
        raise HTTPException(400, "Файл больше 3 МБ")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, "Нужен файл изображения (png, jpg, gif, webp)")

    name = f"avatar_{user.id}_{uuid.uuid4().hex[:8]}{ext}"
    (UPLOAD_DIR / name).write_bytes(data)
    if user.avatar:
        old = UPLOAD_DIR / Path(user.avatar).name
        if old.exists():
            old.unlink()

    user.avatar = f"/static/uploads/{name}"
    db.commit()
    return user_brief(user, db, user)
