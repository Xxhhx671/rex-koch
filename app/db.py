"""Подключение к базе данных и базовый класс моделей."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "social.db"
UPLOAD_DIR = BASE_DIR / "static" / "uploads"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _enable_foreign_keys(dbapi_connection, _record) -> None:
    """SQLite по умолчанию игнорирует ON DELETE CASCADE — включаем их."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    """Зависимость FastAPI: сессия БД на один запрос."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
