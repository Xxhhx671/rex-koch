"""Точка входа мессенджера Kofi."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from .db import BASE_DIR, Base, engine
from .routes import auth, calls, chats, features

STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Kofi", version="2.2.0")

app.include_router(auth.router)
app.include_router(chats.router)
app.include_router(features.router)
app.include_router(calls.router)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# новые колонки в уже существующей базе (create_all не меняет старые таблицы)
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("chats", "username", "ALTER TABLE chats ADD COLUMN username VARCHAR(32) NOT NULL DEFAULT ''"),
    ("messages", "audio", "ALTER TABLE messages ADD COLUMN audio VARCHAR(255) NOT NULL DEFAULT ''"),
    # ответы, редактирование, пересылка
    ("messages", "reply_to_id", "ALTER TABLE messages ADD COLUMN reply_to_id INTEGER"),
    ("messages", "edited_at", "ALTER TABLE messages ADD COLUMN edited_at DATETIME"),
    ("messages", "forwarded_from", "ALTER TABLE messages ADD COLUMN forwarded_from VARCHAR(80) NOT NULL DEFAULT ''"),
    # закреплённое сообщение и тихий чат
    ("chats", "pinned_message_id", "ALTER TABLE chats ADD COLUMN pinned_message_id INTEGER"),
    ("chat_members", "muted", "ALTER TABLE chat_members ADD COLUMN muted BOOLEAN NOT NULL DEFAULT 0"),
    # профиль: телефон, бизнес, настройки, двухэтапный вход
    ("users", "phone", "ALTER TABLE users ADD COLUMN phone VARCHAR(24) NOT NULL DEFAULT ''"),
    ("users", "address", "ALTER TABLE users ADD COLUMN address VARCHAR(120) NOT NULL DEFAULT ''"),
    ("users", "hours", "ALTER TABLE users ADD COLUMN hours VARCHAR(120) NOT NULL DEFAULT ''"),
    ("users", "settings", "ALTER TABLE users ADD COLUMN settings TEXT NOT NULL DEFAULT '{}'"),
    ("users", "password_hash2", "ALTER TABLE users ADD COLUMN password_hash2 VARCHAR(255) NOT NULL DEFAULT ''"),
    # устройства: откуда вошли
    ("user_sessions", "ip", "ALTER TABLE user_sessions ADD COLUMN ip VARCHAR(45) NOT NULL DEFAULT ''"),
    ("user_sessions", "agent", "ALTER TABLE user_sessions ADD COLUMN agent VARCHAR(200) NOT NULL DEFAULT ''"),
    ("user_sessions", "last_seen", "ALTER TABLE user_sessions ADD COLUMN last_seen DATETIME"),
    # закреплённые чаты и черновики
    ("chat_members", "pinned", "ALTER TABLE chat_members ADD COLUMN pinned BOOLEAN NOT NULL DEFAULT 0"),
    ("chat_members", "draft", "ALTER TABLE chat_members ADD COLUMN draft TEXT NOT NULL DEFAULT ''"),
    # исчезающие сообщения и опросы
    ("chats", "auto_delete", "ALTER TABLE chats ADD COLUMN auto_delete INTEGER NOT NULL DEFAULT 0"),
    ("messages", "poll", "ALTER TABLE messages ADD COLUMN poll TEXT NOT NULL DEFAULT ''"),
    # звонки: служебное сообщение с итогом
    ("messages", "system", "ALTER TABLE messages ADD COLUMN system VARCHAR(16) NOT NULL DEFAULT ''"),
)


def migrate() -> None:
    with engine.connect() as conn:
        for table, column, ddl in MIGRATIONS:
            try:
                cols = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
                if column not in cols:
                    conn.exec_driver_sql(ddl)
                conn.commit()
            except Exception:  # noqa: BLE001 — колонка уже добавлена параллельным запуском
                conn.rollback()


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)
    migrate()


@app.middleware("http")
async def revalidate_static(request, call_next):
    """Статику всегда перепроверяем — иначе браузер держит старый CSS/JS."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "app": "Kofi"}


@app.get("/{path:path}")
def spa(path: str) -> FileResponse:
    """Одностраничное приложение: любой не-API путь отдаёт index.html."""
    candidate = STATIC_DIR / path
    if path and candidate.is_file():
        return FileResponse(candidate)
    return FileResponse(STATIC_DIR / "index.html")
