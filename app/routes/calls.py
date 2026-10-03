"""Звонки: сигналинг по WebSocket и учёт вызовов.

Звук и видео идут напрямую между браузерами (WebRTC), серверу отводится
роль почтальона: кто кому звонит, кто принял и SDP/ICE-обмен.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..helpers import SESSION_COOKIE, iso, require_user
from ..models import Call, CallMember, Chat, ChatMember, Message, User, UserSession, utcnow

router = APIRouter(prefix="/api", tags=["calls"])

RING_TIMEOUT = 45          # секунд звонка без ответа — «не ответили»
MAX_MEMBERS = 5            # участников в одном звонке (mesh-соединения)
JOIN_GRACE = 2             # секунд на переподключение того же человека

# живые подключения: user_id -> сокеты (у одного человека может быть 2 вкладки)
ONLINE: dict[int, set[WebSocket]] = {}
# отложенные задачи «никто не ответил»
_RING_TASKS: dict[int, asyncio.Task] = {}


# --------------------------------------------------------------- рассылка

async def _push(user_id: int, payload: dict) -> None:
    """Отправить всем открытым вкладкам пользователя."""
    for ws in list(ONLINE.get(user_id, ())):
        try:
            await ws.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception:  # noqa: BLE001 — сокет уже умер
            pass


async def _push_many(user_ids: set[int], payload: dict) -> None:
    await asyncio.gather(*[_push(uid, payload) for uid in user_ids])


def _members_of(db: Session, call_id: int) -> list[CallMember]:
    return list(
        db.scalars(select(CallMember).where(CallMember.call_id == call_id)).all()
    )


def _chat_title(db: Session, chat: Chat, viewer_id: int) -> str:
    if chat.type == "saved":
        return "Избранное"
    if chat.type == "direct":
        rows = db.scalars(
            select(ChatMember).where(ChatMember.chat_id == chat.id)
        ).all()
        for m in rows:
            if m.user_id != viewer_id:
                u = db.get(User, m.user_id)
                if u:
                    return u.name
        return "Диалог"
    return chat.title or "Группа"


def call_payload(db: Session, call: Call, viewer_id: int) -> dict:
    """Состояние звонка для рассылки участникам."""
    members = []
    for m in _members_of(db, call.id):
        u = db.get(User, m.user_id)
        if u is None:
            continue
        members.append(
            {
                "id": u.id,
                "name": u.name,
                "username": u.username,
                "avatar": u.avatar,
                "state": m.state,
                "muted": bool(m.muted),
                "video": bool(m.video),
                "is_me": u.id == viewer_id,
            }
        )
    chat = db.get(Chat, call.chat_id)
    initiator = db.get(User, call.initiator_id)
    return {
        "id": call.id,
        "chat_id": call.chat_id,
        "chat_title": _chat_title(db, chat, viewer_id) if chat else "",
        "kind": call.kind,
        "state": call.state,
        "initiator": call.initiator_id,
        "initiator_name": initiator.name if initiator else "",
        "created_at": iso(call.created_at),
        "answered_at": iso(call.answered_at),
        "members": members,
    }


async def _broadcast_state(db: Session, call: Call) -> None:
    """Отправить актуальное состояние всем участникам звонка."""
    members = _members_of(db, call.id)
    for m in members:
        await _push(m.user_id, {"type": "call.state", "call": call_payload(db, call, m.user_id)})


async def _end_call(db: Session, call: Call, reason: str) -> None:
    """Завершает звонок, пишет служебное сообщение и рассылает итог."""
    if call.state == "ended":
        return
    call.state = "ended"
    call.ended_at = utcnow()
    answered = call.answered_at is not None
    seconds = 0
    if answered:
        seconds = max(0, int((call.ended_at - call.answered_at).total_seconds()))
    db.flush()

    chat = db.get(Chat, call.chat_id)
    if chat is not None:
        info = {"answered": answered, "seconds": seconds, "kind": call.kind}
        db.add(
            Message(
                chat_id=chat.id,
                sender_id=call.initiator_id,
                system="call",
                text=json.dumps(info, ensure_ascii=False),
            )
        )
        db.commit()
    else:
        db.commit()

    task = _RING_TASKS.pop(call.id, None)
    if task is not None and task is not asyncio.current_task():
        task.cancel()

    for m in _members_of(db, call.id):
        await _push(
            m.user_id,
            {"type": "call.ended", "call_id": call.id, "reason": reason,
             "duration": seconds, "answered": answered},
        )


async def _ring_timeout(call_id: int) -> None:
    """N секунд без ответа — звонок завершается как «не ответили»."""
    try:
        await asyncio.sleep(RING_TIMEOUT)
    except asyncio.CancelledError:
        return
    _RING_TASKS.pop(call_id, None)
    db = SessionLocal()
    try:
        call = db.get(Call, call_id)
        if call is not None and call.state == "ringing":
            await _end_call(db, call, "no-answer")
    finally:
        db.close()


# ------------------------------------------------------------------ действия

async def _start_call(db: Session, uid: int, chat_id: int, kind: str) -> None:
    chat = db.get(Chat, chat_id)
    if chat is None:
        return await _push(uid, {"type": "error", "detail": "Чат не найден"})
    if chat.type in ("channel", "saved"):
        return await _push(uid, {"type": "error", "detail": "В этом чате звонки недоступны"})
    if db.scalar(
        select(ChatMember.id).where(ChatMember.chat_id == chat_id, ChatMember.user_id == uid)
    ) is None:
        return await _push(uid, {"type": "error", "detail": "Вы не состоите в этом чате"})

    # звонок в этот чат уже идёт — присоединяемся к нему
    existing = db.scalar(
        select(Call)
        .where(Call.chat_id == chat_id, Call.state.in_(("ringing", "active")))
        .order_by(Call.id.desc())
    )
    if existing is not None:
        mine = db.scalar(
            select(CallMember).where(
                CallMember.call_id == existing.id, CallMember.user_id == uid
            )
        )
        if mine is None:
            if len(_members_of(db, existing.id)) >= MAX_MEMBERS:
                return await _push(uid, {"type": "error", "detail": "В звонке уже 5 участников"})
            mine = CallMember(call_id=existing.id, user_id=uid, state="ringing")
            db.add(mine)
            db.commit()
        # раз звонок уже идёт — отвечаем сразу, без второго «входящего»
        if existing.state == "active":
            mine.state = "joined"
            mine.joined_at = utcnow()
            if existing.answered_at is None:
                existing.answered_at = utcnow()
            db.commit()
            task = _RING_TASKS.pop(existing.id, None)
            if task is not None:
                task.cancel()
            await _broadcast_state(db, existing)
        else:
            await _push(uid, {"type": "call.state", "call": call_payload(db, existing, uid)})
            if mine.state == "ringing":
                for m in _members_of(db, existing.id):
                    if m.user_id != uid:
                        await _push(
                            m.user_id,
                            {"type": "call.invited", "call": call_payload(db, existing, m.user_id)},
                        )
        return

    rows = db.scalars(select(ChatMember).where(ChatMember.chat_id == chat_id)).all()
    people = [m.user_id for m in rows]
    if uid not in people:
        people = people[: MAX_MEMBERS - 1] + [uid]
    else:
        people = people[:MAX_MEMBERS]

    call = Call(chat_id=chat_id, initiator_id=uid, kind=kind, state="ringing")
    db.add(call)
    db.flush()
    for pid in people:
        db.add(
            CallMember(
                call_id=call.id,
                user_id=pid,
                state="joined" if pid == uid else "ringing",
            )
        )
    db.commit()
    db.refresh(call)

    await _push(uid, {"type": "call.state", "call": call_payload(db, call, uid)})
    for pid in people:
        if pid != uid:
            await _push(pid, {"type": "call.incoming", "call": call_payload(db, call, pid)})

    _RING_TASKS[call.id] = asyncio.create_task(_ring_timeout(call.id))


async def _join_call(db: Session, uid: int, call_id: int) -> None:
    call = db.get(Call, call_id)
    if call is None or call.state == "ended":
        return await _push(uid, {"type": "error", "detail": "Звонок уже завершён"})
    member = db.scalar(
        select(CallMember).where(CallMember.call_id == call_id, CallMember.user_id == uid)
    )
    if member is None:
        # приглашён позже — заводим запись
        if len(_members_of(db, call_id)) >= MAX_MEMBERS:
            return await _push(uid, {"type": "error", "detail": "В звонке уже 5 участников"})
        member = CallMember(call_id=call_id, user_id=uid, state="ringing")
        db.add(member)
    member.state = "joined"
    member.joined_at = utcnow()
    if call.answered_at is None:
        call.answered_at = utcnow()
    call.state = "active"
    db.commit()

    task = _RING_TASKS.pop(call_id, None)
    if task is not None:
        task.cancel()
    await _broadcast_state(db, call)


async def _leave_call(db: Session, uid: int, call_id: int, reason: str) -> None:
    call = db.get(Call, call_id)
    if call is None or call.state == "ended":
        return
    member = db.scalar(
        select(CallMember).where(CallMember.call_id == call_id, CallMember.user_id == uid)
    )
    if member is None:
        return
    member.state = "left"
    member.left_at = utcnow()
    db.commit()

    members = _members_of(db, call_id)
    others = [m for m in members if m.state in ("ringing", "joined") and m.user_id != uid]

    if not others:
        # в звонке никого не осталось
        await _end_call(db, call, "finished" if call.answered_at else "declined")
        return

    if call.answered_at is None:
        # звонок ещё гудит: вызывающий передумал — снимаем вызов
        if uid == call.initiator_id:
            await _end_call(db, call, "cancelled")
            return
        # или все вызываемые отклонили
        callees = [
            m for m in members
            if m.state in ("ringing", "joined") and m.user_id != call.initiator_id
        ]
        if not callees:
            await _end_call(db, call, "declined")
            return

    await _broadcast_state(db, call)


async def _invite(db: Session, uid: int, call_id: int, user_id: int) -> None:
    call = db.get(Call, call_id)
    if call is None or call.state == "ended":
        return await _push(uid, {"type": "error", "detail": "Звонок уже завершён"})
    mine = db.scalar(
        select(CallMember).where(CallMember.call_id == call_id, CallMember.user_id == uid)
    )
    if mine is None or mine.state != "joined":
        return await _push(uid, {"type": "error", "detail": "Присоединитесь к звонку сначала"})
    if db.scalar(
        select(ChatMember.id).where(
            ChatMember.chat_id == call.chat_id, ChatMember.user_id == user_id
        )
    ) is None:
        return await _push(uid, {"type": "error", "detail": "Этого человека нет в чате"})
    if db.scalar(
        select(CallMember.id).where(CallMember.call_id == call_id, CallMember.user_id == user_id)
    ):
        return
    if len(_members_of(db, call_id)) >= MAX_MEMBERS:
        return await _push(uid, {"type": "error", "detail": "В звонке уже 5 участников"})

    db.add(CallMember(call_id=call_id, user_id=user_id, state="ringing"))
    db.commit()
    await _push(user_id, {"type": "call.incoming", "call": call_payload(db, call, user_id)})
    await _broadcast_state(db, call)


async def _set_media(db: Session, uid: int, call_id: int, data: dict) -> None:
    member = db.scalar(
        select(CallMember).where(CallMember.call_id == call_id, CallMember.user_id == uid)
    )
    call = db.get(Call, call_id)
    if member is None or call is None:
        return
    if "muted" in data:
        member.muted = bool(data["muted"])
    if "video" in data:
        member.video = bool(data["video"])
    db.commit()
    await _broadcast_state(db, call)


async def _relay(uid: int, data: dict) -> None:
    """Перекинуть служебное сообщение WebRTC участнику звонка."""
    to = data.get("to")
    if to is None:
        return
    to = int(to)
    call_id = int(data.get("call_id") or 0)
    db = SessionLocal()
    try:
        call = db.get(Call, call_id)
        if call is None:
            return
        pair = {
            m.user_id
            for m in _members_of(db, call_id)
            if m.state in ("joined", "ringing")
        }
        if uid not in pair or to not in pair:
            return
    finally:
        db.close()
    await _push(
        to,
        {
            "type": "rtc",
            "from": uid,
            "call_id": call_id,
            "kind": data.get("kind"),
            "payload": data.get("payload"),
        },
    )


async def _handle(uid: int, data: dict[str, Any]) -> None:
    kind = data.get("type")
    db = SessionLocal()
    try:
        if kind == "ping":
            await _push(uid, {"type": "pong"})
        elif kind == "call.start":
            await _start_call(db, uid, int(data.get("chat_id") or 0),
                              "video" if data.get("kind") == "video" else "audio")
        elif kind == "call.join":
            await _join_call(db, uid, int(data.get("call_id") or 0))
        elif kind in ("call.decline", "call.leave"):
            await _leave_call(
                db, uid, int(data.get("call_id") or 0),
                "declined" if kind == "call.decline" else "left",
            )
        elif kind == "call.invite":
            await _invite(db, uid, int(data.get("call_id") or 0), int(data.get("user_id") or 0))
        elif kind == "call.media":
            await _set_media(db, uid, int(data.get("call_id") or 0), data)
        elif kind == "rtc":
            await _relay(uid, data)
    except Exception:  # noqa: BLE001 — не роняем сокет из-за ошибки обработки
        try:
            await _push(uid, {"type": "error", "detail": "Не удалось выполнить команду"})
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()


# ------------------------------------------------------------------ endpoints


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    """Сигналинг: приглашения в звонок и обмен SDP/ICE."""
    sid = websocket.cookies.get(SESSION_COOKIE)
    db = SessionLocal()
    try:
        row = db.get(UserSession, sid) if sid else None
        user = db.get(User, row.user_id) if row else None
    finally:
        db.close()
    if user is None:
        await websocket.close(code=4401)
        return

    uid = user.id
    await websocket.accept()
    ONLINE.setdefault(uid, set()).add(websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if isinstance(data, dict):
                await _handle(uid, data)
    except WebSocketDisconnect:
        pass
    finally:
        socks = ONLINE.get(uid)
        if socks is not None:
            socks.discard(websocket)
            if not socks:
                ONLINE.pop(uid, None)
        # вкладку закрыли — выходим из звонков, где мы одни
        if not ONLINE.get(uid):
            await _on_disconnect(uid)


async def _on_disconnect(uid: int) -> None:
    db = SessionLocal()
    try:
        rows = db.scalars(
            select(CallMember).where(
                CallMember.user_id == uid,
                CallMember.state.in_(("ringing", "joined")),
            )
        ).all()
        call_ids = sorted({r.call_id for r in rows})
        for call_id in call_ids:
            call = db.get(Call, call_id)
            if call is None:
                continue
            # даём секунду на переподключение (перезагрузка страницы)
            await asyncio.sleep(JOIN_GRACE)
            if not ONLINE.get(uid):
                await _leave_call(db, uid, call_id, "left")
    finally:
        db.close()


# --------------------------------------------------------------- REST-часть

class ActiveOut(BaseModel):
    calls: list[dict]


@router.get("/calls/active", response_model=ActiveOut)
def active_calls(user: User = Depends(require_user)) -> ActiveOut:
    """Звонки, которые идут прямо сейчас (для восстановления экрана после F5)."""
    db = SessionLocal()
    try:
        rows = db.scalars(
            select(CallMember)
            .where(CallMember.user_id == user.id, CallMember.state != "left")
            .order_by(CallMember.id.desc())
        ).all()
        out = []
        seen = set()
        for m in rows:
            if m.call_id in seen:
                continue
            seen.add(m.call_id)
            call = db.get(Call, m.call_id)
            if call is None or call.state == "ended":
                continue
            out.append(call_payload(db, call, user.id))
        return ActiveOut(calls=out)
    finally:
        db.close()
