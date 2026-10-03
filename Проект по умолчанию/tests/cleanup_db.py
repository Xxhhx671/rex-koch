"""Очистка social.db от тестовых данных прошлых прогонов.

Удаляет аккаунты анны/бориса/клары/зои (создаёт smoke-тест), а также
sq1/sq2 (ручная проверка интерфейса), их чаты, сообщения, папки, истории
и сессии. Аккаунты Nick/nick и их чаты не трогаются.

Запуск:  py tests/cleanup_db.py
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "social.db"
UPLOADS = ROOT / "static" / "uploads"

TEST_USER = re.compile(r"^(anna|boris|clara|zoe|new|off|vis)[0-9a-f]{6}$|^(sq1|sq2)$")


def main() -> None:
    con = sqlite3.connect(DB)
    cur = con.cursor()

    users = [r for r in cur.execute("SELECT id, username FROM users")]
    test_ids = [uid for uid, name in users if TEST_USER.match(name or "")]
    real_ids = [uid for uid, name in users if uid not in test_ids]
    if not test_ids:
        print("Тестовых аккаунтов нет.")
    marks = ",".join("?" * len(test_ids)) or "0"

    # ---- чаты: прямые и «Избранное» с тестовым участником удаляем целиком,
    #      каналы/группы оставляем (это может быть проект пользователя),
    #      просто выкидываем из них тестовых участников
    chat_owner: dict[int, int] = {}
    chat_kind: dict[int, str] = {}
    for cid, kind, creator in cur.execute("SELECT id, type, creator_id FROM chats"):
        chat_owner[cid] = creator
        chat_kind[cid] = kind

    members: dict[int, list[int]] = {}
    for cid, uid in cur.execute("SELECT chat_id, user_id FROM chat_members"):
        members.setdefault(cid, []).append(uid)

    drop: list[int] = []
    keep: list[int] = []
    for cid, mlist in members.items():
        tested = [m for m in mlist if m in test_ids]
        if not tested:
            continue
        others = [m for m in mlist if m in real_ids]
        if not others or chat_kind.get(cid) in ("direct", "saved"):
            drop.append(cid)
        else:
            keep.append(cid)

    # одиночные чаты, у которых остались только тестовые участники
    for cid, mlist in members.items():
        if cid not in drop and cid not in keep and any(m in test_ids for m in mlist):
            drop.append(cid)

    for cid in drop:
        cur.execute("DELETE FROM chat_members WHERE chat_id = ?", (cid,))
        cur.execute("DELETE FROM messages WHERE chat_id = ?", (cid,))
        cur.execute("DELETE FROM chat_states WHERE chat_id = ?", (cid,))
        cur.execute("DELETE FROM chats WHERE id = ?", (cid,))

    for cid in keep:
        cur.execute("DELETE FROM chat_members WHERE chat_id = ? AND user_id IN (%s)" % marks,
                    (cid, *test_ids))

    # ---- данные тестовых пользователей
    cur.execute(f"DELETE FROM chat_members WHERE user_id IN ({marks})", test_ids)
    cur.execute(f"DELETE FROM messages WHERE sender_id IN ({marks})", test_ids)
    cur.execute(f"DELETE FROM user_sessions WHERE user_id IN ({marks})", test_ids)
    cur.execute(f"DELETE FROM chat_states WHERE user_id IN ({marks})", test_ids)
    cur.execute(f"DELETE FROM chat_folders WHERE user_id IN ({marks})", test_ids)
    cur.execute(f"DELETE FROM stories WHERE user_id IN ({marks})", test_ids)
    cur.execute(f"DELETE FROM message_reactions WHERE user_id IN ({marks})", test_ids)
    cur.execute(f"DELETE FROM poll_votes WHERE user_id IN ({marks})", test_ids)

    # ---- сироты: ссылки на удалённые строки
    cur.execute("DELETE FROM message_reactions WHERE message_id NOT IN (SELECT id FROM messages)")
    cur.execute("DELETE FROM poll_votes WHERE message_id NOT IN (SELECT id FROM messages)")
    cur.execute("DELETE FROM story_views WHERE story_id NOT IN (SELECT id FROM stories)")
    cur.execute("DELETE FROM story_views WHERE user_id IN (%s)" % marks, test_ids)
    cur.execute(f"DELETE FROM users WHERE id IN ({marks})", test_ids)

    # ---- файлы, на которые больше никто не ссылается
    keep_files: set[str] = set()
    for col, table in (("avatar", "users"), ("image", "messages"), ("audio", "messages")):
        cur.execute(f"SELECT {col} FROM {table} WHERE {col} <> ''")
        keep_files |= {Path(r[0]).name for r in cur.fetchall()}

    con.commit()
    counts = {
        "users": cur.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        "chats": cur.execute("SELECT COUNT(*) FROM chats").fetchone()[0],
        "messages": cur.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        "folders": cur.execute("SELECT COUNT(*) FROM chat_folders").fetchone()[0],
        "stories": cur.execute("SELECT COUNT(*) FROM stories").fetchone()[0],
    }
    con.close()

    removed = 0
    if UPLOADS.exists():
        for f in UPLOADS.iterdir():
            if f.is_file() and f.name not in keep_files:
                f.unlink()
                removed += 1

    print("Удалено тестовых аккаунтов:", len(test_ids))
    print("Удалено чатов:", len(drop), "| очищено от тестовых участников:", len(keep))
    print("Удалено файлов:", removed)
    print("Осталось:", ", ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
