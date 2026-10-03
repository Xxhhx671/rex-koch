"""Сквозной тест API мессенджера Kofi."""
from __future__ import annotations

import base64
import json
import sys
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlencode

BASE = "http://127.0.0.1:8000"
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class Client:
    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}

    def request(self, method: str, path: str, payload=None, form=None, raw=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        elif form is not None:
            data = urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif raw is not None:
            ctype, data = raw
            headers["Content-Type"] = ctype

        req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
        if self.cookies:
            req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in self.cookies.items()))
        try:
            with urllib.request.urlopen(req) as res:
                body = res.read().decode()
                cookies = res.headers.get_all("Set-Cookie") or []
        except urllib.error.HTTPError as e:
            raise AssertionError(f"{method} {path} -> {e.code}: {e.read().decode()}") from None

        for c in cookies:
            k, _, v = c.split(";")[0].partition("=")
            if v:
                self.cookies[k] = v
            else:
                self.cookies.pop(k, None)
        return json.loads(body) if body else {}


def upload_body(field: str, filename: str, data: bytes, ctype: str, extra: dict | None = None):
    boundary = uuid.uuid4().hex
    parts = []
    for key, value in (extra or {}).items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
        )
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\n"
        f"Content-Type: {ctype}\r\n\r\n".encode()
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


PASSED = 0


def check(label: str, cond: bool, extra="") -> None:
    global PASSED
    if not cond:
        print(f"[FAIL] {label} {extra}")
        sys.exit(1)
    PASSED += 1
    print(f"[OK  ] {label}")


def expect_error(fn, label: str) -> None:
    try:
        fn()
    except AssertionError:
        check(label, True)
        return
    check(label, False, "(ошибка не выброшена)")


def main() -> None:
    tag = uuid.uuid4().hex[:6]
    a, b, c, guest = Client(), Client(), Client(), Client()

    # ---------------------------------------------------------- аккаунты
    ua, ub, uc = f"anna{tag}", f"boris{tag}", f"clara{tag}"
    ra = a.request("POST", "/api/register",
                   {"username": ua, "email": f"a{tag}@mail.ru", "password": "secret1", "name": "Анна"})
    check("регистрация", ra["username"] == ua and ra["name"] == "Анна")
    b.request("POST", "/api/register",
              {"username": ub, "email": f"b{tag}@mail.ru", "password": "secret2", "name": "Борис"})
    c.request("POST", "/api/register",
              {"username": uc, "email": f"c{tag}@mail.ru", "password": "secret3", "name": "Клара"})

    expect_error(
        lambda: a.request("POST", "/api/register",
                          {"username": ua, "email": "x@mail.ru", "password": "123456"}),
        "дубликат ника отклонён",
    )
    expect_error(
        lambda: Client().request("POST", "/api/login", {"login": ua, "password": "nope"}),
        "неверный пароль отклонён",
    )
    guest_me = guest.request("GET", "/api/me")
    check("гость не авторизован", guest_me["user"] is None)

    # ---------------------------------------------------------- личный чат
    chat = a.request("POST", "/api/chats", {"username": ub})
    check("личный чат создан", chat["type"] == "direct" and chat["peer"]["username"] == ub)
    chat2 = a.request("POST", "/api/chats", {"username": ub})
    check("личный чат не дублируется", chat2["id"] == chat["id"])
    cid = chat["id"]

    expect_error(lambda: a.request("POST", "/api/chats", {"username": ua}), "чат с собой запрещён")

    # ---------------------------------------------------------- сообщения
    m1 = a.request("POST", f"/api/chats/{cid}/messages", form={"text": "Привет, Борис!"})
    check("сообщение отправлено", m1["text"] == "Привет, Борис!" and m1["mine"] is True)
    check("статус доставки", m1["status"] == "sent")

    m2 = a.request("POST", f"/api/chats/{cid}/messages", form={"text": "Как дела?"})
    check("второе сообщение", m2["id"] > m1["id"])

    thread = b.request("GET", f"/api/chats/{cid}/messages")
    check("история получена", [m["text"] for m in thread["messages"]] == ["Привет, Борис!", "Как дела?"])
    check("непрочитанные у получателя", thread["chat"]["unread"] == 2)
    check("моё last_read у получателя", thread["my_last_read"] == 0)

    counts = b.request("GET", "/api/unread")
    check("счётчик непрочитанных", counts["chats"] == 2)

    # ---------------------------------------------------------- прочтение
    b.request("POST", f"/api/chats/{cid}/read", {"last_id": thread["messages"][-1]["id"]})
    after = a.request("GET", f"/api/chats/{cid}/messages")
    check("статус «прочитано»", after["messages"][-1]["status"] == "read")
    check("непрочитанные обнулены", after["chat"]["unread"] == 0)
    check("unread API обнулён", a.request("GET", "/api/unread")["chats"] == 0)

    # ---------------------------------------------------------- инкремент
    fresh = b.request("POST", f"/api/chats/{cid}/messages", form={"text": "Отвечаю"})
    inc = a.request("GET", f"/api/chats/{cid}/messages?after={m2['id']}")
    check("инкрементальная загрузка",
          len(inc["messages"]) == 1 and inc["messages"][0]["id"] == fresh["id"])

    # ---------------------------------------------------------- «печатает»
    b.request("POST", f"/api/chats/{cid}/typing")
    typers = a.request("GET", f"/api/chats/{cid}/messages?after={fresh['id']}")
    check("индикатор «печатает»", [t["username"] for t in typers["typers"]] == [ub])

    # ---------------------------------------------------------- картинка
    ctype, body = upload_body("image", "pic.png", PNG_1PX, "image/png", {"text": "с картинкой"})
    m_img = a.request("POST", f"/api/chats/{cid}/messages", raw=(ctype, body))
    check("изображение в сообщении", m_img["image"].startswith("/static/uploads/"))

    # ---------------------------------------------------------- поиск
    found = b.request("GET", f"/api/search?q={ua}")
    check("поиск людей", any(u["username"] == ua for u in found["users"]))
    check("себя поиск не отдаёт",
          all(u["username"] != ub for u in b.request("GET", f"/api/search?q={ub}")["users"]))

    # ---------------------------------------------------------- список чатов
    list_a = a.request("GET", "/api/chats")
    check("список чатов", any(ch["id"] == cid for ch in list_a["chats"]))
    row = next(ch for ch in list_a["chats"] if ch["id"] == cid)
    check("последнее сообщение в списке", row["last_message"]["text"] in ("с картинкой", "Отвечаю"))

    # ---------------------------------------------------------- группа
    grp = a.request("POST", "/api/chats/group", {"title": "Тестовая", "usernames": [ub]})
    check("группа создана", grp["type"] == "group" and grp["title"] == "Тестовая")
    gid = grp["id"]

    a.request("POST", f"/api/chats/{gid}/messages", form={"text": "Всем привет"})
    check("непрочитанные в группе", b.request("GET", "/api/unread")["chats"] >= 1)

    added = a.request("POST", f"/api/chats/{gid}/members", {"username": uc})
    check("участник добавлен", uc in [m["username"] for m in added["members"]])
    expect_error(
        lambda: a.request("POST", f"/api/chats/{gid}/members", {"username": uc}),
        "повторное добавление отклонено",
    )

    renamed = a.request("PATCH", f"/api/chats/{gid}", {"title": "Новое имя"})
    check("переименование группы", renamed["title"] == "Новое имя")
    expect_error(
        lambda: a.request("PATCH", f"/api/chats/{cid}", {"title": "X"}),
        "личный чат переименовать нельзя",
    )

    c_join = c.request("GET", f"/api/chats/{gid}/messages")
    check("новый участник видит историю", len(c_join["messages"]) == 1)

    # ---------------------------------------------------------- доступ
    outsider = Client()
    outsider.request("POST", "/api/register",
                     {"username": f"zoe{tag}", "email": f"z{tag}@mail.ru", "password": "secret7"})
    expect_error(lambda: outsider.request("GET", f"/api/chats/{cid}/messages"),
                 "чужой чат недоступен")
    expect_error(lambda: outsider.request("POST", f"/api/chats/{cid}/messages",
                                          form={"text": "spam"}),
                 "посторонний не пишет в чужой чат")
    expect_error(lambda: guest.request("GET", "/api/chats"), "гость не видит чаты")

    # ---------------------------------------------------------- удаление
    expect_error(lambda: b.request("DELETE", f"/api/messages/{m1['id']}"),
                 "удалить чужое сообщение нельзя")
    b.request("DELETE", f"/api/messages/{fresh['id']}")
    check("удаление своего сообщения", True)
    expect_error(lambda: b.request("DELETE", "/api/messages/999999"),
                 "несуществующее сообщение")

    # ---------------------------------------------------------- профиль
    prof = a.request("PATCH", "/api/me", {"name": "Анна Смирнова", "status": "Работаю"})
    check("обновление профиля", prof["name"] == "Анна Смирнова" and prof["status"] == "Работаю")

    ctype, body = upload_body("file", "ava.png", PNG_1PX, "image/png")
    with_ava = a.request("POST", "/api/me/avatar", raw=(ctype, body))
    check("загрузка аватара", with_ava["avatar"].startswith("/static/uploads/"))

    # ---------------------------------------------------------- ник
    new_nick = f"new{tag}"
    a.request("PATCH", "/api/me", {"username": new_nick})
    check("смена ника", a.request("GET", "/api/me")["user"]["username"] == new_nick)
    expect_error(lambda: b.request("PATCH", "/api/me", {"username": new_nick}),
                 "занятый ник")
    a.request("PATCH", "/api/me", {"username": f"anna{tag}"})

    # ---------------------------------------------------------- онлайн-статус
    import sqlite3

    db_path = Path(__file__).resolve().parent.parent / "social.db"
    b_id = b.request("GET", "/api/me")["user"]["id"]

    def peer_status(client) -> dict | None:
        """Собеседник b глазами клиента a (личный чат)."""
        for chat in client.request("GET", "/api/chats")["chats"]:
            peer = chat.get("peer")
            if peer and peer["id"] == b_id:
                return peer
        return None

    online_now = peer_status(a)
    check("только что активен — в сети", online_now and online_now["online"] is True)

    con = sqlite3.connect(db_path)
    con.execute("UPDATE users SET last_seen = datetime('now','-300 seconds') WHERE id = ?",
                (b_id,))
    con.commit()
    con.close()
    offline = peer_status(a)
    check("давно не заходил — не в сети", offline and offline["online"] is False)

    b.request("GET", "/api/me")  # любой запрос возвращает в сеть
    back = peer_status(a)
    check("любый действие снова в сеть", back and back["online"] is True)

    # ---------------------------------------------------------- очистка чата
    st = a.request("POST", f"/api/chats/{cid}/state", {"action": "clear", "scope": "me"})
    check("очистка: флаги в ответе", st["blocked"] is False and st["blocked_all"] is False)
    check("очистка только у меня", a.request("GET", f"/api/chats/{cid}/messages")["messages"] == [])
    check("у собеседника история осталась",
          len(b.request("GET", f"/api/chats/{cid}/messages")["messages"]) > 0)

    b.request("POST", f"/api/chats/{cid}/state", {"action": "clear", "scope": "both"})
    check("очистка у двоих", b.request("GET", f"/api/chats/{cid}/messages")["messages"] == [])
    b.request("POST", f"/api/chats/{cid}/messages", form={"text": "после очистки"})
    later = a.request("GET", f"/api/chats/{cid}/messages")
    check("новые сообщения после очистки видны",
          [m["text"] for m in later["messages"]] == ["после очистки"])

    # ---------------------------------------------------------- блокировка
    blk = a.request("POST", f"/api/chats/{cid}/state", {"action": "block", "scope": "me"})
    check("блокировка только у меня", blk["blocked"] is True and blk["blocked_all"] is False)
    expect_error(lambda: a.request("POST", f"/api/chats/{cid}/messages", form={"text": "…"}),
                 "заблокированный не пишет")
    check("собеседник продолжает писать",
          b.request("POST", f"/api/chats/{cid}/messages", form={"text": "живой"})["mine"] is True)
    row = next(ch for ch in a.request("GET", "/api/chats")["chats"] if ch["id"] == cid)
    check("флаг blocked в списке чатов", row["blocked"] is True and row["blocked_all"] is False)

    both = a.request("POST", f"/api/chats/{cid}/state", {"action": "block", "scope": "both"})
    check("блокировка у двоих", both["blocked"] is True and both["blocked_all"] is True)
    check("блокировка видна собеседнику",
          b.request("GET", f"/api/chats/{cid}/messages")["chat"]["blocked"] is True)
    expect_error(lambda: b.request("POST", f"/api/chats/{cid}/messages", form={"text": "…"}),
                 "и собеседник тоже не пишет")

    un = a.request("POST", f"/api/chats/{cid}/state", {"action": "unblock", "scope": "both"})
    check("разблокировка у двоих", un["blocked"] is False and un["blocked_all"] is False)
    check("после разблокировки пишем",
          a.request("POST", f"/api/chats/{cid}/messages", form={"text": "снова"})["mine"] is True)

    # ---------------------------------------------------------- групповые правила
    expect_error(
        lambda: a.request("POST", f"/api/chats/{gid}/state", {"action": "block"}),
        "блокировка группы запрещена",
    )
    expect_error(lambda: a.request("DELETE", f"/api/chats/{cid}/members/me"),
                 "выход из личного чата запрещён")
    a.request("POST", f"/api/chats/{gid}/state", {"action": "clear", "scope": "me"})
    check("очистка группы только у меня",
          a.request("GET", f"/api/chats/{gid}/messages")["messages"] == [])

    # ---------------------------------------------------------- каналы
    handle = f"chan{tag}"
    chan = a.request("POST", "/api/channels", {"title": "Новости Kofi", "username": handle})
    check("канал создан",
          chan["type"] == "channel" and chan["username"] == handle and chan["joined"] is True)
    expect_error(lambda: a.request("POST", "/api/channels", {"title": "X", "username": handle}),
                 "ник канала занят")
    expect_error(lambda: a.request("POST", "/api/channels", {"title": "X", "username": "a"}),
                 "короткий ник канала отклонён")
    expect_error(lambda: b.request("POST", f"/api/chats/{gid}/join"),
                 "вступить можно только в канал")

    found_ch = b.request("GET", f"/api/search?q={handle}")
    ch = next((x for x in found_ch.get("channels", []) if x["username"] == handle), None)
    check("канал находится поиском", ch is not None and ch["joined"] is False)

    info = b.request("GET", f"/api/channels/{handle}")
    check("канал открыт до вступления", info["joined"] is False and info["id"] == chan["id"])

    a.request("POST", f"/api/chats/{chan['id']}/messages", form={"text": "Привет, это канал"})
    preview = b.request("GET", f"/api/chats/{chan['id']}/messages")
    check("канал читается без вступления",
          preview["can_write"] is False and preview["messages"][-1]["text"] == "Привет, это канал")
    expect_error(lambda: b.request("POST", f"/api/chats/{chan['id']}/messages", form={"text": "x"}),
                 "не участник не пишет в канал")
    expect_error(lambda: b.request("POST", f"/api/chats/{chan['id']}/typing"),
                 "и не «печатает» в канале")

    joined = b.request("POST", f"/api/chats/{chan['id']}/join")
    check("вступление в канал", joined["joined"] is True and joined["members_count"] == 2)
    expect_error(lambda: b.request("POST", f"/api/chats/{chan['id']}/messages", form={"text": "x"}),
                 "подписчик не пишет в канал")
    check("канал в списке чатов",
          any(r["id"] == chan["id"] for r in b.request("GET", "/api/chats")["chats"]))
    owner_view = a.request("GET", f"/api/chats/{chan['id']}")
    names = [m["username"] for m in owner_view["members"]]
    check("создатель видит всех участников", ua in names and ub in names)

    # ---------------------------------------------------------- голосовое
    ctype, body = upload_body("audio", "voice.webm", b"\x1a\x45\xdf\xa3" + b"0" * 64, "audio/webm")
    voice = b.request("POST", f"/api/chats/{cid}/messages", raw=(ctype, body))
    check("голосовое сообщение", voice["audio"].startswith("/static/uploads/voice_"))
    echo = a.request("GET", f"/api/chats/{cid}/messages")
    check("голосовое видно собеседнику",
          echo["messages"][-1]["audio"].startswith("/static/uploads/voice_"))

    # ---------------------------------------------------------- ответ
    # история чата очищалась выше — цитируем свежее сообщение
    base = a.request("POST", f"/api/chats/{cid}/messages", form={"text": "Базовое сообщение"})
    check("базовое сообщение отправлено", base["mine"] is True and base["reply_to"] == 0)
    reply = b.request("POST", f"/api/chats/{cid}/messages",
                      form={"text": "Ответ на базовое", "reply_to": str(base["id"])})
    check("ответ на сообщение",
          reply["reply_to"] == base["id"] and reply["reply"]["text"] == "Базовое сообщение")
    my_name = a.request("GET", "/api/me")["user"]["name"]
    check("автор цитаты известен", reply["reply"]["sender"] == my_name)
    thread2 = a.request("GET", f"/api/chats/{cid}/messages")
    check("квота доходит до собеседника",
          any(m.get("reply") and m["reply"]["id"] == base["id"] for m in thread2["messages"]))
    expect_error(
        lambda: a.request("POST", f"/api/chats/{cid}/messages",
                          form={"text": "x", "reply_to": "999999"}),
        "ответ на несуществующее сообщение отклонён",
    )
    expect_error(
        lambda: a.request("POST", f"/api/chats/{cid}/messages",
                          form={"text": "x", "reply_to": str(m1["id"])}),
        "ответ на сообщение из очищенной истории отклонён",
    )

    # ---------------------------------------------------------- правка
    edited = a.request("PATCH", f"/api/messages/{base['id']}",
                       {"text": "Базовое сообщение (именно так)"})
    check("редактирование текста",
          edited["text"].endswith("(именно так)") and edited["edited"] is not None)
    check("метка «изменено» доходит до чужого",
          any(m["id"] == base["id"] and m["edited"] for m in
              b.request("GET", f"/api/chats/{cid}/messages")["messages"]))
    expect_error(lambda: b.request("PATCH", f"/api/messages/{base['id']}", {"text": "взлом"}),
                 "чужое сообщение не редактируется")
    ctype2, body2 = upload_body("image", "pic2.png", PNG_1PX, "image/png")
    m_pic = a.request("POST", f"/api/chats/{cid}/messages", raw=(ctype2, body2))
    check("картинка без подписи", m_pic["image"].startswith("/static/uploads/") and m_pic["text"] == "")
    expect_error(lambda: a.request("PATCH", f"/api/messages/{m_pic['id']}", {"text": "нельзя"}),
                 "изображение без подписи не редактируется текстом")
    cap = a.request("PATCH", f"/api/messages/{m_img['id']}", {"text": "с картинкой (обновлено)"})
    check("подпись картинки редактируется", cap["edited"] is not None
          and cap["text"].endswith("(обновлено)"))
    expect_error(lambda: a.request("PATCH", f"/api/messages/{base['id']}", {"text": "   "}),
                 "пустая правка отклонена")

    # ---------------------------------------------------------- пересылка
    fwd = a.request("POST", f"/api/messages/{base['id']}/forward", {"chat_id": gid})
    check("пересылка в группу",
          fwd["chat_id"] == gid and fwd["text"].startswith("Базовое") and fwd["forwarded_from"])
    check("источник — автор личного сообщения", fwd["forwarded_from"] == my_name)
    fwd_back = b.request("POST", f"/api/messages/{fwd['id']}/forward", {"chat_id": cid})
    check("пересылка без указания источника не ломает",
          fwd_back["chat_id"] == cid and fwd_back["forwarded_from"])
    expect_error(
        lambda: outsider.request("POST", f"/api/messages/{base['id']}/forward", {"chat_id": gid}),
        "пересылка из чужого чата запрещена",
    )
    expect_error(
        lambda: b.request("POST", f"/api/messages/{base['id']}/forward", {"chat_id": chan["id"]}),
        "подписчик не пересылает в канал",
    )

    # ---------------------------------------------------------- тихий чат
    mute = a.request("POST", f"/api/chats/{cid}/mute", {"muted": True})
    check("тихий чат включён", mute["muted"] is True)
    check("тишина видна в чате",
          a.request("GET", f"/api/chats/{cid}/messages")["chat"]["muted"] is True)
    check("тишина видна в списке",
          next(c for c in a.request("GET", "/api/chats")["chats"] if c["id"] == cid)["muted"] is True)
    a.request("POST", f"/api/chats/{cid}/mute", {"muted": False})
    check("тихий чат выключен",
          a.request("GET", f"/api/chats/{cid}/messages")["chat"]["muted"] is False)

    # ---------------------------------------------------------- закреп
    pin = a.request("POST", f"/api/chats/{cid}/pin", {"message_id": base["id"]})
    check("закреп сообщения", pin["pinned"] is not None and pin["pinned"]["id"] == base["id"])
    check("закреп отдаётся в чате",
          a.request("GET", f"/api/chats/{cid}/messages")["chat"]["pinned"]["id"] == base["id"])
    check("закреп виден собеседнику",
          b.request("GET", f"/api/chats/{cid}/messages")["chat"]["pinned"]["id"] == base["id"])
    chan_msg_id = preview["messages"][-1]["id"]
    expect_error(
        lambda: b.request("POST", f"/api/chats/{chan['id']}/pin", {"message_id": chan_msg_id}),
        "в канале закрепляет только создатель",
    )
    a.request("POST", f"/api/chats/{chan['id']}/pin", {"message_id": chan_msg_id})
    check("канал: создатель закрепил",
          a.request("GET", f"/api/chats/{chan['id']}/messages")["chat"]["pinned"]["id"] == chan_msg_id)
    unpin = a.request("POST", f"/api/chats/{cid}/pin", {"message_id": 0})
    check("снятие закрепа", unpin["pinned"] is None)
    expect_error(
        lambda: a.request("POST", f"/api/chats/{cid}/pin", {"message_id": 777777}),
        "закреп несуществующего сообщения отклонён",
    )

    # ---------------------------------------------------------- поиск по чату
    found_msgs = a.request("GET", f"/api/chats/{cid}/search?q={quote('Базовое')}")
    check("поиск по чату", any(m["id"] == base["id"] for m in found_msgs["messages"]))
    bad = [m["text"] for m in found_msgs["messages"] if "Базовое" not in (m["text"] or "")]
    check("найдено ровно нужное", not bad, extra=f"лишние: {bad[:3]!r}")
    check("поиск без результатов",
          a.request("GET", f"/api/chats/{cid}/search?q={quote('НЕТ_ТАКОГО_СЛОВА')}")["messages"] == [])
    expect_error(lambda: a.request("GET", f"/api/chats/{cid}/search?q="),
                 "пустой запрос отклонён")
    expect_error(lambda: outsider.request("GET", f"/api/chats/{cid}/search?q=x"),
                 "поиск в чужом чате запрещён")

    # ---------------------------------------------------------- реакции
    react_msg = a.request("POST", f"/api/chats/{cid}/messages",
                          form={"text": "Поставь реакцию"})
    r1 = a.request("POST", f"/api/messages/{react_msg['id']}/reactions", {"emoji": "👍"})
    check("реакция поставлена",
          any(x["emoji"] == "👍" and x["mine"] for x in r1["reactions"]))
    a.request("POST", f"/api/messages/{react_msg['id']}/reactions", {"emoji": "❤"})
    r2 = b.request("POST", f"/api/messages/{react_msg['id']}/reactions", {"emoji": "👍"})
    check("реакция собеседника учтена",
          next(x["count"] for x in r2["reactions"] if x["emoji"] == "👍") == 2)
    # взгляд B: свои реакции помечены, чужая — нет
    check("чужая реакция не моя",
          next(x for x in r2["reactions"] if x["emoji"] == "❤")["mine"] is False
          and next(x for x in r2["reactions"] if x["emoji"] == "👍")["mine"] is True)
    r3 = a.request("POST", f"/api/messages/{react_msg['id']}/reactions", {"emoji": "👍"})
    check("повторный клик снимает свою реакцию",
          next(x for x in r3["reactions"] if x["emoji"] == "👍")["count"] == 1
          and next(x for x in r3["reactions"] if x["emoji"] == "👍")["mine"] is False)
    b.request("POST", f"/api/messages/{react_msg['id']}/reactions", {"emoji": "👍"})
    a.request("POST", f"/api/messages/{react_msg['id']}/reactions", {"emoji": "😂"})
    r4 = a.request("POST", f"/api/messages/{react_msg['id']}/reactions", {"emoji": "😮"})
    check("лимит трёх своих реакций",
          sum(1 for x in r4["reactions"] if x["mine"]) == 3)
    r5 = a.request("POST", f"/api/messages/{react_msg['id']}/reactions", {"emoji": "❓"})
    check("четвёртая реакция вытесняет первую",
          sum(1 for x in r5["reactions"] if x["mine"]) == 3
          and not any(x["emoji"] == "❤" and x["mine"] for x in r5["reactions"]))
    expect_error(
        lambda: outsider.request("POST", f"/api/messages/{react_msg['id']}/reactions",
                                 {"emoji": "👍"}),
        "реакция в чужом чате запрещена",
    )

    # ---------------------------------------------------------- опрос
    poll_json = json.dumps(
        {"question": "Что берём?", "options": ["Кофе", "Чай"]}, ensure_ascii=False
    )
    poll_msg = a.request("POST", f"/api/chats/{cid}/messages",
                         form={"text": "", "poll": poll_json})
    check("опрос отправлен", poll_msg["poll"]["question"] == "Что берём?"
          and len(poll_msg["poll"]["options"]) == 2)
    v1 = b.request("POST", f"/api/messages/{poll_msg['id']}/vote", {"option": 1})
    check("голос учтён", v1["poll"]["my_vote"] == 1 and v1["poll"]["options"][1]["votes"] == 1)
    v2 = b.request("POST", f"/api/messages/{poll_msg['id']}/vote", {"option": 1})
    check("повторный голос снимается",
          v2["poll"]["my_vote"] is None and v2["poll"]["options"][1]["votes"] == 0)
    v3 = b.request("POST", f"/api/messages/{poll_msg['id']}/vote", {"option": 0})
    check("смена голоса", v3["poll"]["my_vote"] == 0 and v3["poll"]["options"][0]["votes"] == 1)
    seen_poll = a.request("GET", f"/api/chats/{cid}/messages")
    check("опрос виден второму участнику",
          any(m["id"] == poll_msg["id"] and m["poll"]["total"] == 1
              for m in seen_poll["messages"]))
    expect_error(lambda: b.request("POST", f"/api/messages/{react_msg['id']}/vote",
                                   {"option": 0}),
                 "голос вне опроса отклонён")
    expect_error(lambda: b.request("POST", f"/api/messages/{poll_msg['id']}/vote",
                                   {"option": 9}),
                 "несуществующий вариант отклонён")
    expect_error(lambda: a.request("POST", f"/api/chats/{cid}/messages",
                                   form={"text": "", "poll": "не json"}),
                 "битый опрос отклонён")
    expect_error(
        lambda: a.request("POST", f"/api/chats/{cid}/messages",
                          form={"text": "", "poll": json.dumps({"question": "x",
                                                                "options": ["один"]})}),
        "опрос с одним вариантом отклонён",
    )

    # ---------------------------------------------------------- избранное
    saved = a.request("GET", "/api/saved")
    check("избранное создано", saved["type"] == "saved" and saved["title"] == "Избранное")
    saved2 = a.request("GET", "/api/saved")
    check("избранное не дублируется", saved2["id"] == saved["id"])
    a.request("POST", f"/api/chats/{saved['id']}/messages",
              form={"text": "Мысль на потом"})
    check("заметка в избранном",
          any(m["text"] == "Мысль на потом"
              for m in a.request("GET", f"/api/chats/{saved['id']}/messages")["messages"]))
    check("избранное в списке чатов",
          any(c["id"] == saved["id"] and c["title"] == "Избранное"
              for c in a.request("GET", "/api/chats")["chats"]))
    a.request("POST", f"/api/messages/{react_msg['id']}/forward", {"chat_id": saved["id"]})
    check("пересылка в избранное",
          any(m["text"] == "Поставь реакцию"
              for m in a.request("GET", f"/api/chats/{saved['id']}/messages")["messages"]))
    expect_error(lambda: b.request("GET", f"/api/chats/{saved['id']}/messages"),
                 "чужое избранное недоступно")

    # ---------------------------------------------------------- папки чатов
    folder = a.request("POST", "/api/folders",
                       {"name": "Работа", "icon": "users", "chat_ids": [cid]})
    check("папка создана", folder["name"] == "Работа" and folder["chat_ids"] == [cid])
    check("папки в списке",
          any(f["id"] == folder["id"] for f in a.request("GET", "/api/folders")["folders"]))
    in_folder = a.request("GET", f"/api/chats?folder_id={folder['id']}")
    check("фильтр папки", [c["id"] for c in in_folder["chats"]] == [cid])
    upd = a.request("PATCH", f"/api/folders/{folder['id']}",
                    {"name": "Дела", "icon": "chats", "chat_ids": [cid, gid]})
    check("папка изменена", upd["name"] == "Дела" and len(upd["chat_ids"]) == 2)
    expect_error(
        lambda: b.request("PATCH", f"/api/folders/{folder['id']}",
                          {"name": "чужая", "icon": "msg", "chat_ids": []}),
        "чужую папку не изменить",
    )
    a.request("DELETE", f"/api/folders/{folder['id']}")
    check("папка удалена",
          all(f["id"] != folder["id"]
              for f in a.request("GET", "/api/folders")["folders"]))

    # ---------------------------------------------------------- черновик
    a.request("POST", f"/api/chats/{cid}/draft", {"text": "Черновик привет"})
    row = next(c for c in a.request("GET", "/api/chats")["chats"] if c["id"] == cid)
    check("черновик в списке чатов", row["draft"] == "Черновик привет")
    a.request("POST", f"/api/chats/{cid}/messages", form={"text": "Отправили"})
    row = next(c for c in a.request("GET", "/api/chats")["chats"] if c["id"] == cid)
    check("черновик очищен после отправки", row["draft"] == "")

    # ---------------------------------------------------------- закреп чата
    a.request("POST", f"/api/chats/{cid}/pin-chat", {"pinned": True})
    lst = a.request("GET", "/api/chats")["chats"]
    row = next(c for c in lst if c["id"] == cid)
    check("чат закреплён", row["pin_chat"] is True)
    check("закреплённый чат наверху", lst[0]["id"] == cid)
    a.request("POST", f"/api/chats/{cid}/pin-chat", {"pinned": False})
    check("закреп чата снят",
          next(c for c in a.request("GET", "/api/chats")["chats"]
               if c["id"] == cid)["pin_chat"] is False)

    # ---------------------------------------------------------- исчезающие сообщения
    import sqlite3

    db_path = Path(__file__).resolve().parent.parent / "social.db"
    con = sqlite3.connect(db_path)
    con.execute("UPDATE messages SET created_at = datetime('now', '-3 days') WHERE id = ?",
                (react_msg["id"],))
    con.commit()
    con.close()
    ad = a.request("POST", f"/api/chats/{cid}/auto-delete", {"hours": 24})
    check("исчезающие сообщения включены", ad["auto_delete"] == 24 and ad["purged"] >= 1)
    after_purge = a.request("GET", f"/api/chats/{cid}/messages")
    check("старое сообщение исчезло",
          all(m["id"] != react_msg["id"] for m in after_purge["messages"]))
    check("свежие сообщения остались",
          any(m["text"] == "Отправили" for m in after_purge["messages"]))
    expect_error(lambda: a.request("POST", f"/api/chats/{cid}/auto-delete",
                                   {"hours": -5}),
                 "отрицательный срок отклонён")
    a.request("POST", f"/api/chats/{cid}/auto-delete", {"hours": 0})
    check("исчезающие сообщения выключены",
          a.request("GET", f"/api/chats/{cid}/messages")["chat"]["auto_delete"] == 0)

    # ---------------------------------------------------------- настройки и приватность
    st0 = a.request("GET", "/api/settings")
    check("настройки по умолчанию", isinstance(st0["settings"], dict)
          and st0["two_fa"] is False)
    a.request("PUT", "/api/settings", {"settings": {"last_seen": "nobody"}})
    brief = b.request("GET", f"/api/search?q={ua}")
    me_a = next(u for u in brief["users"] if u["username"] == ua)
    check("«последний визит» скрыт", me_a.get("last_seen") is None
          and me_a["online"] is False)
    a.request("PUT", "/api/settings", {"settings": {"last_seen": "all"}})

    prof = a.request("PATCH", "/api/me", {
        "name": "Анна", "status": "В сети", "username": ua,
        "phone": "+7 900 123-45-67", "address": "ул. Мира, 1",
        "hours": "пн-пт 10:00-19:00",
    })
    check("телефон и бизнес в профиле",
          prof["phone"] == "+7 900 123-45-67" and prof["address"] == "ул. Мира, 1"
          and prof["hours"].startswith("пн-пт"))
    by_phone = b.request("GET", f"/api/search?q={quote('900123')}")
    check("поиск по номеру телефона", any(u["username"] == ua for u in by_phone["users"]))
    a.request("PUT", "/api/settings", {"settings": {"phone_search": False}})
    by_phone = b.request("GET", f"/api/search?q={quote('900123')}")
    check("поиск по номеру запрещён", all(u["username"] != ua for u in by_phone["users"]))
    a.request("PUT", "/api/settings", {"settings": {"phone_search": True}})

    a.request("PUT", "/api/settings", {"settings": {"who_can_add": "nobody"}})
    expect_error(lambda: c.request("POST", "/api/chats", {"username": ua}),
                 "новый чат при запрете отклонён")
    expect_error(lambda: outsider.request("POST", "/api/chats", {"username": ua}),
                 "посторонний при запрете отклонён")
    a.request("PUT", "/api/settings", {"settings": {"who_can_add": "all"}})
    check("после разрешения чат создаётся",
          c.request("POST", "/api/chats", {"username": ua})["type"] == "direct")

    # ---------------------------------------------------------- двухэтапный вход
    fa = a.request("POST", "/api/me/2fa", {"password": "5678", "enable": True})
    check("двухэтапный вход включён", fa["enabled"] is True)
    check("статус 2FA виден в настройках",
          a.request("GET", "/api/settings")["two_fa"] is True)
    second = Client()
    expect_error(lambda: second.request("POST", "/api/login",
                                        {"login": ua, "password": "secret1"}),
                 "вход без кода отклонён")
    second.request("POST", "/api/login", {"login": ua, "password": "secret1", "code": "5678"})
    check("вход с кодом", second.request("GET", "/api/me")["user"]["username"] == ua)
    expect_error(lambda: second.request("POST", "/api/login",
                                        {"login": ua, "password": "secret1", "code": "0000"}),
                 "неверный код отклонён")
    a.request("POST", "/api/me/2fa", {"password": "5678", "enable": False})
    check("2FA выключена", a.request("GET", "/api/settings")["two_fa"] is False)
    expect_error(lambda: a.request("POST", "/api/me/2fa",
                                   {"password": "не тот", "enable": False}),
                 "снятие 2FA требует верный код")

    # ---------------------------------------------------------- устройства
    sess = second.request("GET", "/api/sessions")
    check("сессии видны", len(sess["sessions"]) >= 2
          and any(s["current"] for s in sess["sessions"]))
    other = next(s for s in sess["sessions"] if not s["current"])
    second.request("DELETE", f"/api/sessions/{other['token']}")
    check("чужая сессия завершена",
          len(second.request("GET", "/api/sessions")["sessions"]) == len(sess["sessions"]) - 1)
    mine_tok = next(s for s in second.request("GET", "/api/sessions")["sessions"]
                    if s["current"])["token"]
    expect_error(lambda: b.request("DELETE", f"/api/sessions/{mine_tok}"),
                 "чужую сессию не завершить")

    # «a» лишился своей сессии в проверке выше — входим заново
    a.request("POST", "/api/login", {"login": ua, "password": "secret1"})
    check("вход после завершения сессии",
          a.request("GET", "/api/me")["user"]["username"] == ua)

    # ---------------------------------------------------------- экспорт данных
    exp = a.request("GET", "/api/export")
    check("экспорт данных", exp["app"] == "Kofi"
          and any(ch["id"] == cid for ch in exp["chats"]))
    check("экспорт содержит переписку",
          any(ch["id"] == cid and ch["messages"] for ch in exp["chats"]))
    check("экспорт содержит профиль", exp["profile"]["username"] == ua)

    # ---------------------------------------------------------- истории
    story1 = a.request("POST", "/api/stories", form={"text": "Мой день", "privacy": "all"})
    check("история опубликована", story1["text"] == "Мой день" and story1["mine"] is True)
    expect_error(lambda: a.request("POST", "/api/stories",
                                   form={"text": "вторая", "privacy": "all"}),
                 "лимит 1 история в сутки")
    feed_b = b.request("GET", "/api/stories")
    check("история видна всем",
          any(i["id"] == story1["id"] for g in feed_b["stories"] for i in g["items"]))
    b.request("POST", f"/api/stories/{story1['id']}/view")
    views = a.request("GET", f"/api/stories/{story1['id']}/views")
    check("зрители истории", any(v["username"] == ub for v in views["viewers"]))
    expect_error(lambda: b.request("GET", f"/api/stories/{story1['id']}/views"),
                 "зрителей видит только автор")
    expect_error(lambda: outsider.request("DELETE", f"/api/stories/{story1['id']}"),
                 "чужую историю не удалить")

    story2 = b.request("POST", "/api/stories",
                       form={"text": "только для своих", "privacy": "contacts"})
    feed_c = c.request("GET", "/api/stories")
    check("история контактов скрыта от постороннего",
          all(i["id"] != story2["id"] for g in feed_c["stories"] for i in g["items"]))
    feed_a = a.request("GET", "/api/stories")
    check("история контактов видна контакту",
          any(i["id"] == story2["id"] for g in feed_a["stories"] for i in g["items"]))
    expect_error(lambda: c.request("POST", f"/api/stories/{story2['id']}/view"),
                 "закрытая история недоступна")
    a.request("DELETE", f"/api/stories/{story1['id']}")
    check("история удалена",
          all(i["id"] != story1["id"]
              for g in a.request("GET", "/api/stories")["stories"] for i in g["items"]))

    # ---------------------------------------------------------- удаление аккаунта
    off = Client()
    off.request("POST", "/api/register",
                {"username": f"off{tag}", "email": f"off{tag}@mail.ru",
                 "password": "secret9", "name": "Уходящий"})
    expect_error(lambda: off.request("DELETE", "/api/me", {"password": "не тот"}),
                 "удаление аккаунта без пароля отклонено")
    off.request("DELETE", "/api/me", {"password": "secret9"})
    check("аккаунт удалён", off.request("GET", "/api/me")["user"] is None)

    # ---------------------------------------------------------- выход
    a.request("POST", "/api/logout")
    check("выход", a.request("GET", "/api/me")["user"] is None)

    print(f"\nAll checks passed ({PASSED})")
    cleanup(tag)


def cleanup(tag: str) -> None:
    """Убирает тестовые данные, чтобы база осталась чистой."""
    import sqlite3

    db_path = Path(__file__).resolve().parent.parent / "social.db"
    if not db_path.exists():
        return
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    # убираем не только текущий прогон, но и любые прежние тестовые аккаунты,
    # оставшиеся после упавших запусков
    import re

    pat = re.compile(r"^(anna|boris|clara|zoe|new|off|vis)[0-9a-f]{6}$|^(sq1|sq2)$")
    ids = [
        uid
        for uid, uname in cur.execute("SELECT id, username FROM users")
        if pat.match(uname or "")
    ]
    if ids:
        marks = ",".join("?" * len(ids))
        chat_ids = [r[0] for r in cur.execute(f"SELECT id FROM chats WHERE id IN (SELECT chat_id FROM chat_members WHERE user_id IN ({marks}))", ids)]
        for cid in chat_ids:
            cur.execute("DELETE FROM chat_members WHERE chat_id = ?", (cid,))
            cur.execute("DELETE FROM messages WHERE chat_id = ?", (cid,))
            cur.execute("DELETE FROM chat_states WHERE chat_id = ?", (cid,))
            cur.execute("DELETE FROM chats WHERE id = ?", (cid,))
        cur.execute(f"DELETE FROM chat_members WHERE user_id IN ({marks})", ids)
        cur.execute(f"DELETE FROM messages WHERE sender_id IN ({marks})", ids)
        cur.execute(f"DELETE FROM user_sessions WHERE user_id IN ({marks})", ids)
        cur.execute(f"DELETE FROM chat_states WHERE user_id IN ({marks})", ids)
        cur.execute(f"DELETE FROM chat_folders WHERE user_id IN ({marks})", ids)
        cur.execute(f"DELETE FROM stories WHERE user_id IN ({marks})", ids)
        cur.execute(f"DELETE FROM users WHERE id IN ({marks})", ids)
    # сироты от новых таблиц (каскад по прямому SQL не срабатывает)
    cur.execute("DELETE FROM message_reactions WHERE message_id NOT IN (SELECT id FROM messages)")
    cur.execute("DELETE FROM poll_votes WHERE message_id NOT IN (SELECT id FROM messages)")
    cur.execute("DELETE FROM story_views WHERE story_id NOT IN (SELECT id FROM stories)")
    con.commit()

    # удаляем только файлы, на которые больше никто не ссылается:
    # аватарки и голосовые реальных пользователей остаются на месте
    cur.execute("SELECT avatar FROM users WHERE avatar <> ''")
    keep = {Path(r[0]).name for r in cur.fetchall()}
    cur.execute("SELECT image FROM messages WHERE image <> ''")
    keep |= {Path(r[0]).name for r in cur.fetchall()}
    cur.execute("SELECT audio FROM messages WHERE audio <> ''")
    keep |= {Path(r[0]).name for r in cur.fetchall()}
    con.close()

    uploads = Path(__file__).resolve().parent.parent / "static" / "uploads"
    if uploads.exists():
        for f in uploads.iterdir():
            if f.is_file() and f.name not in keep:
                f.unlink()


if __name__ == "__main__":
    main()
