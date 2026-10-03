/* Kofi — клиентская часть мессенджера */
"use strict";

/* ============================================================ утилиты */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );

const icon = (name) => `<svg><use href="#i-${name}"/></svg>`;

/**
 * Оформление текста сообщения: ссылки, @упоминания, **жирный**,
 * *курсив* и `код` — по образцу Telegram.
 */
function formatText(raw) {
  let s = esc(raw);
  s = s.replace(/(https?:\/\/[^\s<]+)/g, (m, url) =>
    `<a href="${url}" target="_blank" rel="noopener">${url}</a>`
  );
  s = s.replace(/(^|[\s(])@([A-Za-z0-9_]{3,30})/g, '$1<span class="mention">@$2</span>');
  s = s.replace(/\*\*([^*\n]{1,200})\*\*/g, "<b>$1</b>");
  s = s.replace(/(^|[^*\w])\*([^*\n]{1,200})\*/g, "$1<i>$2</i>");
  s = s.replace(/`([^`\n]{1,200})`/g, "<code>$1</code>");
  return s;
}

function toast(message, isError = false) {
  const t = $("#toast");
  t.innerHTML = message;
  t.className = "toast" + (isError ? " err" : "");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), 3200);
}

async function api(path, { method = "GET", body, form } = {}) {
  const opts = { method, headers: {} };
  if (form) opts.body = form;
  else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (res.status === 401 && state.me) sessionExpired();
    const detail = data.detail;
    const msg = Array.isArray(detail)
      ? detail.map((d) => d.msg).join(", ")
      : detail || "Что-то пошло не так";
    throw new Error(msg);
  }
  return data;
}

/** Сессия протухла — возвращаем экран входа, не ломая интерфейс. */
function sessionExpired() {
  stopPolling();
  clearInterval(state.listTimer);
  stopCallSession();
  state.me = null;
  state.chats = [];
  state.loaded = false;
  closeProfile();
  document.body.classList.remove("drawer-open", "chat-open");
  $("#main-view").classList.add("hidden");
  $("#auth-view").classList.remove("hidden");
  setTimeout(() => toast("Сессия истекла — войдите заново", true), 100);
}

const pad = (n) => String(n).padStart(2, "0");

function isToday(d) {
  const n = new Date();
  return d.getDate() === n.getDate() && d.getMonth() === n.getMonth() && d.getFullYear() === n.getFullYear();
}

function shortTime(iso) {
  const d = new Date(iso);
  if (isToday(d)) return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 604800) return ["вс", "пн", "вт", "ср", "чт", "пт", "сб"][d.getDay()];
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}`;
}

function dayLabel(iso) {
  const d = new Date(iso);
  const yest = new Date(Date.now() - 86400000);
  if (isToday(d)) return "Сегодня";
  if (d.toDateString() === yest.toDateString()) return "Вчера";
  return d.toLocaleDateString("ru-RU", { day: "numeric", month: "long" });
}

const clockTime = (iso) => {
  const d = new Date(iso);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return few;
  return many;
}

function avatarHTML(u, cls = "") {
  if (!u) return `<div class="avatar ${cls}">?</div>`;
  const letter = esc((u.name || u.username || "?").trim().charAt(0).toUpperCase() || "?");
  if (u.avatar) {
    return `<img class="avatar ${cls}" src="${esc(u.avatar)}" alt="" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'avatar ${cls}',textContent:'${letter}'}))">`;
  }
  return `<div class="avatar ${cls}">${letter}</div>`;
}

const onlineDot = (u, cls = "") =>
  `<span class="avatar-wrap">${avatarHTML(u, cls)}${u && u.online ? '<span class="on-dot"></span>' : ""}</span>`;

/* ============================================================ состояние */

const state = {
  me: null,
  mode: "chats",          // chats | group | channel | settings
  chats: [],
  loaded: false,
  activeId: null,
  chat: null,
  messages: [],
  lastId: 0,
  hasMore: false,
  lastDay: "",
  lastSender: null,
  pollTimer: null,
  listTimer: null,
  typingSent: 0,
  pendingFile: null,
  query: "",
  searchSeq: 0,
  groupTitle: "",
  groupPicked: [],
  channelTitle: "",
  channelHandle: "",
  opening: false,   // идёт загрузка чата — список ещё не обновлять
  replyTo: null,    // сообщение, на которое отвечаем
  editing: null,    // id редактируемого сообщения
  folderId: 0,      // открытая папка чатов
  folders: null,    // папки пользователя (грузятся один раз)
  stories: [],      // лента историй
  sound: true,      // звук входящих
};

/* запись голосового сообщения (общий для чата объект) */
const voice = {
  rec: null,     // MediaRecorder или null
  chunks: [],
  mime: "",
  start: 0,
  timer: null,
  send: true,    // флаг: завершить отправкой или отменой
  stream: null,  // поток микрофона
};

/* ============================================================ вход */

$$(".tab").forEach((btn) =>
  btn.addEventListener("click", () => {
    $$(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    $("#login-form").classList.toggle("hidden", btn.dataset.tab !== "login");
    $("#register-form").classList.toggle("hidden", btn.dataset.tab !== "register");
    $("#auth-error").classList.add("hidden");
  })
);

const authFail = (err) => {
  const box = $("#auth-error");
  box.textContent = err.message;
  box.classList.remove("hidden");
};

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  const payload = { login: f.get("login"), password: f.get("password") };
  try {
    await enterApp(await api("/api/login", { method: "POST", body: payload }));
  } catch (err) {
    // двухэтапная защита — спрашиваем код и входим повторно
    if (String(err.message).includes("Двухэтапная")) {
      const code = await promptModal("Двухэтапная защита", "Код подтверждения", "");
      if (!code) return;
      try {
        await enterApp(await api("/api/login", { method: "POST", body: { ...payload, code } }));
      } catch (err2) {
        authFail(err2);
      }
      return;
    }
    authFail(err);
  }
});

$("#register-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    await enterApp(await api("/api/register", {
      method: "POST",
      body: {
        username: f.get("username"),
        name: f.get("name"),
        email: f.get("email"),
        password: f.get("password"),
      },
    }));
    toast("Аккаунт создан");
  } catch (err) {
    authFail(err);
  }
});

async function logout() {
  try {
    await api("/api/logout", { method: "POST" });
  } catch {}
  stopPolling();
  clearInterval(state.listTimer);
  stopCallSession();
  state.me = null;
  state.chats = [];
  state.loaded = false;
  document.body.classList.remove("drawer-open", "chat-open");
  location.hash = "";
  $("#main-view").classList.add("hidden");
  $("#auth-view").classList.remove("hidden");
}

async function enterApp(user) {
  state.me = user;
  $("#auth-view").classList.add("hidden");
  $("#main-view").classList.remove("hidden");
  renderDrawerUser();
  await refreshChats();
  loadFolders();
  loadStories();
  if ("Notification" in window && Notification.permission === "default") {
    Notification.requestPermission().catch(() => {});
  }
  location.hash = "#/chats";
  route();
  clearInterval(state.listTimer);
  state.listTimer = setInterval(() => refreshChats(), 4000);
  setInterval(() => loadStories(), 60000);
  connectCallWS();
}

async function boot() {
  try {
    const { user } = await api("/api/me");
    if (!user) {
      $("#auth-view").classList.remove("hidden");
      return;
    }
    await enterApp(user);
  } catch {
    $("#auth-view").classList.remove("hidden");
  }
}

function renderDrawerUser() {
  const u = state.me;
  $("#drawer-user").innerHTML = `
    ${avatarHTML(u, "md")}
    <span><b>${esc(u.name)}</b><small>@${esc(u.username)}</small></span>`;
}

/* ============================================================ меню (drawer) */

$("#menu-btn").addEventListener("click", () => document.body.classList.toggle("drawer-open"));
$("#scrim").addEventListener("click", () => document.body.classList.remove("drawer-open"));

$$(".drawer [data-mode]").forEach((btn) =>
  btn.addEventListener("click", async () => {
    const mode = btn.dataset.mode;
    document.body.classList.remove("drawer-open");
    if (mode === "logout") return logout();
    if (mode === "newchat") {
      state.query = "";
      $("#search-input").value = "";
      setMode("chats");
      setTimeout(() => $("#search-input").focus(), 60);
      return;
    }
    if (mode === "saved") return openSaved();
    if (mode === "settings") {
      location.hash = "#/settings";
      return;
    }
    setMode(mode);
  })
);

$("#new-btn").addEventListener("click", () => {
  if (state.mode !== "chats") setMode("chats");
  state.query = "";
  $("#search-input").value = "";
  renderBody();
  $("#search-input").focus();
});

/* ============================================================ боковая панель */

const TITLES = { chats: "Чаты", group: "Новая группа", channel: "Новый канал" };

function setMode(mode, push = true) {
  if (push && location.hash !== `#/${mode}`) {
    location.hash = `#/${mode}`;
    return;
  }
  if (state.mode === mode && $("#list-body").dataset.view === mode) return;
  state.mode = mode;
  renderSidebar();
}

function renderSidebar() {
  const unread = state.chats.reduce((s, c) => s + c.unread, 0);
  $("#sb-title").innerHTML =
    TITLES[state.mode] +
    (state.mode === "chats" && unread
      ? ` <span class="title-badge">${unread}</span>`
      : "");

  const search = $("#sb-search");
  search.classList.toggle("hidden", state.mode === "settings");
  $("#search-input").placeholder =
    state.mode === "group"
      ? "Найти людей…"
      : state.mode === "channel"
        ? "Найти канал или человека…"
        : "Поиск";

  $$(".drawer [data-mode]").forEach((b) =>
    b.classList.toggle("active", b.dataset.mode === state.mode)
  );

  renderBody();
}

function renderBody() {
  renderFolders();
  renderStories();
  if (state.mode === "group") return renderGroup();
  if (state.mode === "channel") return renderChannelPanel();
  if (state.query) return renderSearchResults();
  renderChatList();
}

/* ---------- список чатов (обновляется на месте, без мигания) ---------- */

function renderChatList() {
  const body = $("#list-body");
  if (body.dataset.view !== "chats") {
    body.dataset.view = "chats";
    body.innerHTML = "";
  }
  if (!state.chats.length) {
    body.innerHTML = `<div class="empty">${icon("chats")}<br>Чатов пока нет.<br>Нажмите <b>Новый чат</b>, чтобы начать общение.</div>`;
    return;
  }
  syncChatRows(state.chats);
}

function syncChatRows(items) {
  const body = $("#list-body");
  if (body.dataset.view !== "chats") {
    body.dataset.view = "chats";
    body.innerHTML = "";
  }

  const existing = new Map(
    [...body.querySelectorAll(".chat-row")].map((el) => [+el.dataset.id, el])
  );
  const nodes = items.map((c) => {
    const html = chatRow(c);
    let el = existing.get(c.id);
    if (el && el._html === html) return el;
    const tmp = document.createElement("div");
    tmp.innerHTML = html;
    const fresh = tmp.firstElementChild;
    fresh._html = html;
    if (el) {
      el.replaceWith(fresh);
    } else {
      fresh.classList.add("enter"); // новый чат появляется с анимацией
    }
    return fresh;
  });

  nodes.forEach((el, i) => {
    const at = body.children[i];
    if (at !== el) body.insertBefore(el, at || null);
  });
  while (body.children.length > nodes.length) body.lastElementChild.remove();
}

function chatRow(c) {
  const last = c.last_message;
  const mine = last && last.mine;
  const typing = (state.typersIn || {})[c.id];
  const isChannel = c.type === "channel";
  let preview;
  if (c.blocked) {
    preview = `<span class="blocked-mark">${icon("lock")}Заблокирован чат</span>`;
  } else if (typing) {
    preview = `<span class="typing">печатает…</span>`;
  } else if (c.draft) {
    preview = `<span class="draft-line">Черновик: ${esc(c.draft)}</span>`;
  } else if (last) {
    const who = mine ? "Вы: " : c.type === "group" ? `${last.sender.name}: ` : "";
    const body =
      last.text ||
      (last.audio
        ? "Голосовое сообщение"
        : last.poll
          ? "Опрос"
          : last.image
            ? "изображение"
            : "сообщение");
    preview = `<span class="who">${esc(who)}</span>${esc(body)}`;
  } else if (isChannel) {
    preview = `Канал · ${c.members_count} ${plural(c.members_count, "подписчик", "подписчика", "подписчиков")}`;
  } else {
    preview = c.type === "group" ? `${c.members_count} ${plural(c.members_count, "участник", "участника", "участников")}` : "Нет сообщений";
  }

  const avatar =
    c.type === "group" || isChannel
      ? avatarHTML({ name: c.title, avatar: c.avatar }, "md")
      : onlineDot(c.peer, "md");

  return `
    <div class="chat-row ${c.id === state.activeId ? "active" : ""}" data-id="${c.id}" data-act="open-chat">
      ${avatar}
      <span class="meta">
        <span class="line1"><b>${esc(c.title)}</b>${isChannel ? `<span class="type-chip">${icon("channel")}канал</span>` : ""}${c.pin_chat ? `<span class="pin-ic" title="Закреплён">${icon("pin")}</span>` : ""}${c.muted ? `<span class="mute-ic" title="Тихий чат">${icon("bell-off")}</span>` : ""}${last ? `<span class="time">${shortTime(last.created_at)}</span>` : ""}</span>
        <span class="line2"><span class="preview">${preview}</span>
          ${c.unread ? `<span class="unread ${c.type === "group" ? "muted" : ""}">${c.unread}</span>` : ""}
        </span>
      </span>
    </div>`;
}

/* ---------- поиск ---------- */

$("#search-input").addEventListener("input", (e) => {
  state.query = e.target.value.trim();
  $("#clear-search").classList.toggle("hidden", !state.query);
  if (state.mode !== "chats" && state.mode !== "group") {
    state.mode = "chats";
  }
  clearTimeout(state._searchTimer);
  state._searchTimer = setTimeout(renderBody, 180);
});

$("#clear-search").addEventListener("click", () => {
  state.query = "";
  $("#search-input").value = "";
  $("#clear-search").classList.add("hidden");
  renderBody();
  $("#search-input").focus();
});

async function renderSearchResults() {
  const body = $("#list-body");
  const q = state.query;
  const seq = ++state.searchSeq;
  body.dataset.view = "search";

  const matched = state.chats.filter((c) => {
    const hay = `${c.title} ${c.peer ? "@" + c.peer.username : ""} ${c.last_message ? c.last_message.text : ""}`.toLowerCase();
    return hay.includes(q.toLowerCase());
  });

  body.innerHTML =
    (matched.length
      ? `<div class="section-title">Чаты</div>` + matched.map(chatRow).join("")
      : "") +
    `<div class="section-title">Люди</div>
     <div id="people-results"><div class="skeleton"></div><div class="skeleton"></div></div>
     <div class="section-title hidden" id="channels-sec">Каналы</div>
     <div id="channel-results"></div>`;

  try {
    const { users, channels = [] } = await api(`/api/search?q=${encodeURIComponent(q)}`);
    if (seq !== state.searchSeq || state.query !== q) return;
    $("#people-results").innerHTML = users.length
      ? users.map(personRow).join("")
      : `<div class="empty">Никого не нашлось</div>`;
    if (channels.length) {
      $("#channels-sec").classList.remove("hidden");
      $("#channel-results").innerHTML = channels.map(channelRow).join("");
    }
  } catch (err) {
    if (seq !== state.searchSeq) return;
    $("#people-results").innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
}

function channelRow(ch) {
  return `
    <div class="person channel" data-act="open-chat" data-id="${ch.id}">
      <span class="avatar md ch-ava">${icon("channel")}</span>
      <span class="meta">
        <b>${esc(ch.title)}</b>
        <small>@${esc(ch.username)}</small>
        <span class="status">${ch.members_count} ${plural(ch.members_count, "подписчик", "подписчика", "подписчиков")}</span>
      </span>
      <span class="tail ${ch.joined ? "ok" : ""}">${icon(ch.joined ? "check" : "plus")}</span>
    </div>`;
}

function personRow(u) {
  const chosen = state.groupPicked.some((x) => x.username === u.username);
  const inGroup = state.mode === "group";
  return `
    <div class="person ${inGroup && chosen ? "selected" : ""}" data-act="pick-user" data-u="${esc(u.username)}">
      ${onlineDot(u, "md")}
      <span class="meta">
        <b>${esc(u.name)}</b>
        <small>@${esc(u.username)}</small>
        <span class="status ${u.online ? "on" : ""}">${u.online ? "в сети" : "был(а) недавно"}</span>
      </span>
      ${inGroup ? `<span class="tail">${icon(chosen ? "x" : "plus")}</span>` : ""}
    </div>`;
}

/* ---------- новая группа ---------- */

function renderGroup() {
  const body = $("#list-body");
  body.dataset.view = "group";
  const panel = `
    <div class="panel">
      <div class="field">
        <label>Название группы</label>
        <input id="group-title" maxlength="80" placeholder="Например, Команда" value="${esc(state.groupTitle)}">
      </div>
      ${state.groupPicked.length
        ? `<div class="chips">${state.groupPicked
            .map(
              (u) =>
                `<span class="chip">${esc(u.name)}<button data-act="remove-chip" data-u="${esc(u.username)}" title="Убрать">${icon("x")}</button></span>`
            )
            .join("")}</div>
           <button class="btn primary" data-act="create-group">Создать группу</button>`
        : `<div class="hint">Найдите людей в поиске сверху — они появятся здесь.</div>`}
    </div>`;

  if (state.query) {
    body.innerHTML = panel + `<div id="people-results"><div class="skeleton"></div></div>`;
    const seq = ++state.searchSeq;
    api(`/api/search?q=${encodeURIComponent(state.query)}`)
      .then(({ users }) => {
        if (seq !== state.searchSeq) return;
        $("#people-results").innerHTML =
          `<div class="section-title">Люди</div>` +
          (users.length ? users.map(personRow).join("") : `<div class="empty">Никого не нашлось</div>`);
      })
      .catch(() => {});
  } else {
    body.innerHTML = panel;
  }

  const titleInput = $("#group-title");
  if (titleInput) titleInput.addEventListener("input", (e) => (state.groupTitle = e.target.value));
}

async function createGroup() {
  if (!state.groupPicked.length) return toast("Добавьте участников", true);
  try {
    const chat = await api("/api/chats/group", {
      method: "POST",
      body: { title: state.groupTitle.trim(), usernames: state.groupPicked.map((u) => u.username) },
    });
    state.groupTitle = "";
    state.groupPicked = [];
    state.query = "";
    $("#search-input").value = "";
    $("#clear-search").classList.add("hidden");
    await refreshChats();
    location.hash = `#/chat/${chat.id}`;
    toast("Группа создана");
  } catch (err) {
    toast(err.message, true);
  }
}

/* ---------- новый канал ---------- */

function renderChannelPanel() {
  const body = $("#list-body");
  body.dataset.view = "channel";
  body.innerHTML = `
    <div class="panel">
      <div class="ch-head">${icon("channel")}<span>Новый канал</span></div>
      <div class="field">
        <label>Название</label>
        <input id="ch-title" maxlength="80" placeholder="Например, Новости Kofi" value="${esc(state.channelTitle)}">
      </div>
      <div class="field">
        <label>Ник канала</label>
        <div class="nick-row">
          <span class="nick-at">@</span>
          <input id="ch-handle" maxlength="32" placeholder="kofi_news" spellcheck="false" value="${esc(state.channelHandle)}">
        </div>
        <span class="hint">По этому нику канал ищут: 3–32 символа, латиница, цифры и _</span>
      </div>
      <button class="btn primary" data-act="create-channel">Создать канал</button>
      <div class="hint">Писать в канал сможете только вы — подписчики будут его читать.</div>
    </div>`;

  $("#ch-title").addEventListener("input", (e) => (state.channelTitle = e.target.value));
  $("#ch-handle").addEventListener("input", (e) => {
    state.channelHandle = e.target.value.replace(/^@+/, "");
  });
}

async function createChannel() {
  const title = state.channelTitle.trim();
  const handle = state.channelHandle.trim().replace(/^@+/, "");
  if (!title) return toast("Введите название канала", true);
  if (handle.length < 3) return toast("Ник канала: минимум 3 символа", true);
  try {
    const chat = await api("/api/channels", {
      method: "POST",
      body: { title, username: handle },
    });
    state.channelTitle = "";
    state.channelHandle = "";
    state.query = "";
    $("#search-input").value = "";
    $("#clear-search").classList.add("hidden");
    await refreshChats();
    location.hash = `#/chat/${chat.id}`;
    toast("Канал создан");
  } catch (err) {
    toast(err.message, true);
  }
}

/* ---------- профиль на весь экран ---------- */

function openProfile() {
  renderProfile();
  $("#profile-view").classList.remove("hidden");
}

function closeProfile() {
  $("#profile-view").classList.add("hidden");
}

function renderProfile() {
  const u = state.me;
  if (!u) return;
  const body = $("#pv-body");
  body.innerHTML = `
    <div class="panel">
      <div class="profile-top">
        ${avatarHTML(u, "lg")}
        <h3>${esc(u.name)}</h3>
        <small>@${esc(u.username)}</small>
        <label class="btn ghost small" style="cursor:pointer">
          Сменить аватар
          <input type="file" id="avatar-input" accept="image/*" hidden>
        </label>
        <span class="hint">png, jpg, gif, webp до 3 МБ</span>
      </div>

      <div class="section-title">Профиль</div>
      <div class="field">
        <label>Имя</label>
        <input id="set-name" maxlength="60" value="${esc(u.name)}" placeholder="Как вас видят в чатах">
      </div>
      <div class="field">
        <label>Ник</label>
        <div class="nick-row">
          <span class="nick-at">@</span>
          <input id="set-nick" maxlength="30" value="${esc(u.username)}" placeholder="username" spellcheck="false">
        </div>
        <span class="hint">Латиница, цифры, _ и . — от 3 до 30 символов</span>
      </div>
      <div class="field">
        <label>Статус</label>
        <input id="set-status" maxlength="120" value="${esc(u.status || "")}" placeholder="В сети">
      </div>

      <div class="section-title">Контакты и бизнес</div>
      <div class="field">
        <label>Телефон</label>
        <input id="set-phone" maxlength="24" value="${esc(u.phone || "")}" placeholder="+7 900 000-00-00">
        <span class="hint">Виден по настройке приватности — его же ищут в поиске</span>
      </div>
      <div class="field">
        <label>Адрес</label>
        <input id="set-address" maxlength="120" value="${esc(u.address || "")}" placeholder="Город, улица">
      </div>
      <div class="field">
        <label>Часы работы</label>
        <input id="set-hours" maxlength="120" value="${esc(u.hours || "")}" placeholder="пн-пт 10:00–19:00">
      </div>
      <div class="field">
        <label>Микрофон для голосовых</label>
        <select id="set-mic" class="select">
          <option value="">Микрофон по умолчанию</option>
        </select>
        <span class="hint">С этого устройства записываются голосовые сообщения</span>
      </div>
      <button class="btn primary" data-act="save-profile">Сохранить</button>

      <div class="section-title">Приватность</div>
      <div class="field">
        <label>Кто видит ваш «последний визит»</label>
        <div class="seg" id="seg-last_seen"></div>
      </div>
      <div class="field">
        <label>Кто видит ваш телефон</label>
        <div class="seg" id="seg-phone"></div>
      </div>
      <div class="field">
        <label>Кто может добавлять вас в чаты</label>
        <div class="seg" id="seg-who_can_add"></div>
      </div>
      <div class="switch-row">
        <span class="sr-text"><b>Поиск по номеру телефона</b><small>Вас сможет найти тот, кто знает номер</small></span>
        <button class="switch" type="button" data-act="toggle" data-key="phone_search"></button>
      </div>

      <div class="section-title">Оформление и звук</div>
      <div class="switch-row">
        <span class="sr-text"><b>Звуки уведомлений</b><small>Сигнал на новые сообщения в других чатах</small></span>
        <button class="switch" type="button" data-act="toggle" data-key="sound"></button>
      </div>
      <div class="switch-row">
        <span class="sr-text"><b>Уведомления браузера</b><small id="notif-state">Проверка…</small></span>
        <button class="btn ghost small" type="button" data-act="notif">Включить</button>
      </div>

      <div class="section-title">Безопасность</div>
      <div class="switch-row">
        <span class="sr-text"><b>Двухэтапный вход</b><small>Код при входе на новое устройство</small></span>
        <button class="switch" type="button" data-act="two-fa"></button>
      </div>
      <div class="field">
        <label>Устройства и сессии</label>
        <div id="sessions" class="sess-list"></div>
      </div>

      <div class="section-title">Данные</div>
      <div class="switch-row">
        <span class="sr-text"><b>Экспорт данных</b><small>Профиль, чаты и вся переписка одним файлом JSON</small></span>
        <button class="btn ghost small" type="button" data-act="export">${icon("download")}Выгрузить</button>
      </div>
      <div class="switch-row danger-zone">
        <span class="sr-text"><b>Удаление аккаунта</b><small>Профиль и все сообщения исчезнут навсегда</small></span>
        <button class="btn danger small" type="button" data-act="delete-account">Удалить</button>
      </div>

      <div class="section-title">Аккаунт</div>
      <div class="stats"><span>ник <b>@${esc(u.username)}</b></span></div>
      <button class="btn danger" data-act="logout">Выйти из аккаунта</button>
    </div>`;

  $("#avatar-input").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    try {
      state.me = await api("/api/me/avatar", { method: "POST", form: fd });
      renderDrawerUser();
      renderProfile();
      toast("Аватар обновлён");
    } catch (err) {
      toast(err.message, true);
    }
  });

  const micSel = $("#set-mic");
  micSel.addEventListener("change", () => {
    localStorage.setItem("kofi.mic", micSel.value);
    toast(micSel.value ? "Микрофон сохранён" : "Микрофон по умолчанию");
  });
  fillMicSelect();
  loadSettingsUI();
}

/* ---------- настройки приватности, звука, безопасности, устройств ---------- */

const PRIVACY_OPTS = [["all", "Все"], ["contacts", "Контакты"], ["nobody", "Никто"]];

function renderSeg(sel, key, value, options) {
  const box = $(sel);
  if (!box) return;
  box.innerHTML = options
    .map(
      ([v, l]) =>
        `<button type="button" class="${v === value ? "on" : ""}" data-act="set" data-key="${key}" data-val="${v}">${l}</button>`
    )
    .join("");
}

function renderSettingsUI() {
  const s = state.settings || {};
  renderSeg("#seg-last_seen", "last_seen", s.last_seen || "all", PRIVACY_OPTS);
  renderSeg("#seg-phone", "phone", s.phone || "contacts", PRIVACY_OPTS);
  renderSeg("#seg-who_can_add", "who_can_add", s.who_can_add || "all", PRIVACY_OPTS);
  $('#pv-body [data-key="phone_search"]')?.classList.toggle("on", s.phone_search !== false);
  $('#pv-body [data-key="sound"]')?.classList.toggle("on", s.sound !== false);
  $("#sw-2fa, [data-act='two-fa']")?.classList.toggle("on", !!state.twoFa);
  const ns = $("#notif-state");
  if (ns) {
    const granted = "Notification" in window && Notification.permission === "granted";
    ns.textContent = granted
      ? "Разрешены — придёт всплывающее уведомление"
      : "Заблокированы браузером — разрешите их в адресной строке";
  }
}

async function saveSetting(key, value) {
  state.settings = { ...(state.settings || {}), [key]: value };
  renderSettingsUI();
  try {
    const data = await api("/api/settings", {
      method: "PUT",
      body: { settings: state.settings },
    });
    state.settings = data.settings;
  } catch (err) {
    toast(err.message, true);
  }
}

async function loadSettingsUI() {
  try {
    const data = await api("/api/settings");
    state.settings = data.settings || {};
    state.twoFa = !!data.two_fa;
  } catch {
    state.settings = state.settings || {};
  }
  renderSettingsUI();
  loadSessions();
}

const agentIcon = (a) =>
  /mobile|android|iphone|ipad/i.test(a || "") ? "phone" : "monitor";

/** Читаемое имя устройства вместо сырой строки User-Agent. */
function agentName(a) {
  const ua = a || "";
  const os = /Windows NT 10/.test(ua)
    ? "Windows 10/11"
    : /Windows/.test(ua)
      ? "Windows"
      : /Android/.test(ua)
        ? "Android"
        : /(iPhone|iPad|iPod)/.test(ua)
          ? "iOS"
          : /Mac OS X/.test(ua)
            ? "macOS"
            : /Linux/.test(ua)
              ? "Linux"
              : "";
  const br = /Edg\//.test(ua)
    ? "Edge"
    : /OPR\/|Opera/.test(ua)
      ? "Opera"
      : /YaBrowser/.test(ua)
        ? "Яндекс.Браузер"
        : /Chrome\//.test(ua)
          ? "Chrome"
          : /Firefox\//.test(ua)
            ? "Firefox"
            : /Safari\//.test(ua)
              ? "Safari"
              : /python|curl|urllib|requests/i.test(ua)
                ? "Программный доступ"
                : "Устройство";
  return br + (os ? ` · ${os}` : "");
}

function whenText(isoStr) {
  if (!isoStr) return "—";
  const d = new Date(isoStr);
  if (Number.isNaN(d.getTime())) return "—";
  const sameDay = d.toDateString() === new Date().toDateString();
  const time = d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  return sameDay ? `сегодня в ${time}` : d.toLocaleDateString("ru-RU", { day: "numeric", month: "short" }) + ` в ${time}`;
}

async function loadSessions() {
  const box = $("#sessions");
  if (!box) return;
  box.innerHTML = `<span class="hint">Загрузка…</span>`;
  try {
    const { sessions } = await api("/api/sessions");
    box.innerHTML =
      sessions
        .map(
          (s) => `
      <div class="sess-row">
        <span class="ses-ic">${icon(agentIcon(s.agent))}</span>
        <span class="ses-text">
          <b>${esc(agentName(s.agent))}</b>
          <small title="${esc(s.agent || "")}">${esc(s.ip)} · вход ${esc(whenText(s.last_seen))}</small>
        </span>
        ${
          s.current
            ? `<span class="ses-now">Это устройство</span>`
            : `<button class="btn ghost small" type="button" data-act="revoke" data-token="${esc(s.token)}">Завершить</button>`
        }
      </div>`
        )
        .join("") || `<span class="hint">Активных сессий нет</span>`;
  } catch (err) {
    box.innerHTML = `<span class="hint">${esc(err.message)}</span>`;
  }
}

async function twoFaDialog() {
  if (state.twoFa) {
    const code = await promptModal("Выключить двухэтапный вход?", "Введите текущий код", "");
    if (!code) return;
    try {
      const r = await api("/api/me/2fa", { method: "POST", body: { password: code, enable: false } });
      state.twoFa = r.enabled;
      renderSettingsUI();
      toast("Двухэтапный вход выключен");
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  const code = await promptModal(
    "Двухэтапный вход",
    "Придумайте код (4 символа и больше) — он спросится при входе",
    ""
  );
  if (!code || code.length < 4) return toast("Код слишком короткий", true);
  try {
    const r = await api("/api/me/2fa", { method: "POST", body: { password: code, enable: true } });
    state.twoFa = r.enabled;
    renderSettingsUI();
    toast("Двухэтапный вход включён");
  } catch (err) {
    toast(err.message, true);
  }
}

async function deleteAccountDialog() {
  const ok = await confirmModal(
    "Удалить аккаунт?",
    "Профиль, чаты и все сообщения будут удалены безвозвратно."
  );
  if (!ok) return;
  const password = await promptModal("Подтверждение", "Введите пароль от аккаунта", "");
  if (!password) return;
  try {
    await api("/api/me", { method: "DELETE", body: { password } });
    toast("Аккаунт удалён");
    clearInterval(state.listTimer);
    stopPolling();
    location.hash = "#/chats";
    setTimeout(() => location.reload(), 800);
  } catch (err) {
    toast(err.message, true);
  }
}

function notifStateText() {
  const granted = "Notification" in window && Notification.permission === "granted";
  return granted
    ? "Разрешены — придёт всплывающее уведомление"
    : "Заблокированы браузером — разрешите их в адресной строке";
}

async function saveProfile() {
  try {
    state.me = await api("/api/me", {
      method: "PATCH",
      body: {
        name: $("#set-name").value,
        status: $("#set-status").value,
        username: $("#set-nick").value.trim(),
        phone: $("#set-phone").value.trim(),
        address: $("#set-address").value.trim(),
        hours: $("#set-hours").value.trim(),
      },
    });
    renderDrawerUser();
    renderProfile();
    toast("Профиль сохранён");
  } catch (err) {
    toast(err.message, true);
  }
}

$("#pv-back").addEventListener("click", () => {
  closeProfile();
  if (location.hash === "#/settings") location.hash = "#/chats";
});

$("#pv-body").addEventListener("click", async (e) => {
  const target = e.target.closest("[data-act]");
  if (!target) return;
  const act = target.dataset.act;

  if (act === "save-profile") return saveProfile();
  if (act === "logout") return logout();

  if (act === "set") return saveSetting(target.dataset.key, target.dataset.val);

  if (act === "toggle") {
    const key = target.dataset.key;
    const now = key === "sound" ? state.settings?.sound !== false : state.settings?.phone_search !== false;
    return saveSetting(key, !now);
  }

  if (act === "two-fa") return twoFaDialog();

  if (act === "notif") {
    if (!("Notification" in window)) return toast("Браузер не поддерживает уведомления", true);
    const p = await Notification.requestPermission();
    renderSettingsUI();
    toast(p === "granted" ? "Уведомления включены" : "Уведомления не разрешены", p !== "granted");
    return;
  }

  if (act === "revoke") {
    const token = target.dataset.token;
    if (!await confirmModal("Завершить сессию?", "С этого устройства выйдут из аккаунта.")) return;
    try {
      await api(`/api/sessions/${encodeURIComponent(token)}`, { method: "DELETE" });
      await loadSessions();
      toast("Сессия завершена");
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }

  if (act === "export") {
    window.open("/api/export", "_blank");
    return;
  }

  if (act === "delete-account") return deleteAccountDialog();
});

/* ============================================================ клики по списку */

$("#list-body").addEventListener("click", async (e) => {
  const target = e.target.closest("[data-act]");
  if (!target) return;
  const act = target.dataset.act;

  if (act === "open-chat") {
    location.hash = `#/chat/${target.dataset.id}`;
    return;
  }

  if (act === "pick-user") {
    const username = target.dataset.u;
    if (state.mode === "group") {
      const picked = state.groupPicked.some((x) => x.username === username);
      if (picked) state.groupPicked = state.groupPicked.filter((x) => x.username !== username);
      else {
        const row = target.querySelector(".meta");
        state.groupPicked.push({
          username,
          name: row ? row.querySelector("b").textContent : username,
        });
      }
      renderGroup();
    } else {
      try {
        const chat = await api("/api/chats", { method: "POST", body: { username } });
        state.query = "";
        $("#search-input").value = "";
        $("#clear-search").classList.add("hidden");
        await refreshChats();
        location.hash = `#/chat/${chat.id}`;
      } catch (err) {
        toast(err.message, true);
      }
    }
    return;
  }

  if (act === "remove-chip") {
    state.groupPicked = state.groupPicked.filter((x) => x.username !== target.dataset.u);
    renderGroup();
    return;
  }

  if (act === "create-group") return createGroup();
  if (act === "create-channel") return createChannel();
  if (act === "save-profile") return saveProfile();
  if (act === "logout") return logout();
  if (act === "load-older") return loadOlder();
});

/* ============================================================ чаты */

async function refreshChats() {
  if (!state.me) return;
  try {
    const { chats } = await api(
      "/api/chats" + (state.folderId ? `?folder_id=${state.folderId}` : "")
    );
    const prevUnread = new Map(state.chats.map((c) => [c.id, c.unread]));
    const hadData = state.loaded;
    state.chats = chats;
    state.loaded = true;

    const total = chats.reduce((s, c) => s + c.unread, 0);
    $("#sb-title").querySelectorAll(".title-badge").forEach((b) => b.remove());
    if (state.mode === "chats" && total) {
      $("#sb-title").insertAdjacentHTML(
        "beforeend",
        ` <span class="title-badge badge-pop">${total}</span>`
      );
    }
    document.title = total ? `(${total}) Kofi — мессенджер` : "Kofi — мессенджер";

    // уведомления о новых сообщениях в других чатах
    if (hadData) {
      chats.forEach((c) => {
        const before = prevUnread.get(c.id) ?? 0;
        if (c.unread > before && c.id !== state.activeId) {
          const lastMsg = c.last_message;
          const text = lastMsg
            ? lastMsg.text ||
              (lastMsg.poll
                ? "Опрос"
                : lastMsg.audio
                  ? "Голосовое сообщение"
                  : lastMsg.image
                    ? "изображение"
                    : "сообщение")
            : "";
          toast(`<b>${esc(c.title)}</b> · ${esc(text.slice(0, 40))}`);
          if (!c.muted) {
            beep();
            notifyMe(c.title, text);
          }
        }
      });
    }

    // активный чат пропал (вышли или удалили) — но просмотр канала без
    // вступления остаётся, его просто нет в списке
    const watchingChannel = !!(state.chat && state.chat.type === "channel" && state.chat.joined === false);
    if (
      state.activeId &&
      !state.opening &&          // чат ещё грузится — не закрываем на середине
      !watchingChannel &&
      !chats.some((c) => c.id === state.activeId)
    ) {
      stopPolling();
      state.activeId = null;
      state.chat = null;
      document.body.classList.remove("chat-open");
      $("#conv-open").classList.add("hidden");
      $("#conv-empty").classList.remove("hidden");
      toast("Чат больше недоступен");
    }

    if (state.mode === "chats" && !state.query) {
      if (!chats.length) renderChatList();
      else syncChatRows(chats);
    }
    if (state.activeId) {
      const cur = chats.find((c) => c.id === state.activeId);
      if (cur) {
        updateActiveRow(cur);
        syncHeaderStatus();
      }
    }
  } catch {}
}

/** Статус «в сети» и блокировка обновляются в шапке чата на лету. */
function syncHeaderStatus() {
  const cur = state.chats.find((c) => c.id === state.activeId);
  if (!cur || !state.chat) return;
  const changed =
    (cur.peer && state.chat.peer && cur.peer.online !== state.chat.peer.online) ||
    (!!cur.blocked !== !!state.chat.blocked);
  if (changed) {
    if (cur.peer) state.chat.peer = { ...state.chat.peer, ...cur.peer };
    state.chat.blocked = !!cur.blocked;
    state.chat.blocked_all = !!cur.blocked_all;
    renderChatHeader(state.chat);
  }
}

function updateActiveRow(c) {
  const el = $(`.chat-row[data-id="${c.id}"]`);
  if (!el) return;
  const html = chatRow(c);
  const tmp = document.createElement("div");
  tmp.innerHTML = html;
  const fresh = tmp.firstElementChild;
  fresh._html = html;
  el.replaceWith(fresh);
}

function stopPolling() {
  clearInterval(state.pollTimer);
  state.pollTimer = null;
}

/* ============================================================ переписка */

async function openChat(id) {
  if (voice.rec) stopRecording(false);
  stopAllVoice();
  cancelComposer();
  closeMsgSearch();
  const known = state.chats.find((c) => c.id === id);
  state.activeId = id;
  state.opening = true;
  state.chat = known || { id, title: "…", type: "direct" };
  document.body.classList.add("chat-open");
  $("#conv-empty").classList.add("hidden");
  $("#conv-open").classList.remove("hidden");
  renderChatHeader(state.chat);
  renderMessages([]);
  renderPinnedBar(state.chat);

  try {
    const data = await api(`/api/chats/${id}/messages`);
    state.chat = data.chat;
    state.messages = data.messages;
    state.hasMore = data.has_more;
    state.lastId = data.messages.length ? data.messages[data.messages.length - 1].id : 0;
    renderChatHeader(data.chat);
    renderMessages(data.messages, { top: data.has_more });
    renderTypers(data.typers);
    renderPinnedBar(data.chat);
    applyDraft(data.chat.draft || "");
    if (data.chat.unread > 0 && state.lastId && data.chat.joined !== false) {
      await markRead(state.lastId);
    }
    await refreshChats();
    state.opening = false;
  } catch (err) {
    state.opening = false;
    stopPolling();
    state.activeId = null;
    state.chat = null;
    state.messages = [];
    document.body.classList.remove("chat-open");
    $("#conv-open").classList.add("hidden");
    $("#conv-empty").classList.remove("hidden");
    $("#pinned-bar").classList.add("hidden");
    closeMsgSearch();
    toast(err.message, true);
    if (location.hash !== "#/chats") location.hash = "#/chats";
    else route();
    return;
  }

  $$(".chat-row").forEach((r) => r.classList.toggle("active", +r.dataset.id === id));
  stopPolling();
  state.pollTimer = setInterval(pollChat, 2500);
  if (!$("#conv-form").classList.contains("hidden")) $("#msg-input").focus();
}

/** Перечитывает переписку с нуля (после очистки истории). */
async function reloadMessages() {
  if (!state.activeId) return;
  const data = await api(`/api/chats/${state.activeId}/messages`);
  state.chat = data.chat;
  state.messages = data.messages;
  state.hasMore = data.has_more;
  state.lastId = data.messages.length ? data.messages[data.messages.length - 1].id : 0;
  renderChatHeader(data.chat);
  renderMessages(data.messages);
  renderTypers(data.typers);
  await refreshChats();
}

function closeChat() {
  stopPolling();
  if (voice.rec) stopRecording(false);
  stopAllVoice();
  cancelComposer();
  closeMsgSearch();
  $("#pinned-bar").classList.add("hidden");
  state.activeId = null;
  state.chat = null;
  state.messages = [];
  document.body.classList.remove("chat-open");
  $("#conv-open").classList.add("hidden");
  $("#conv-empty").classList.remove("hidden");
}

function renderChatHeader(chat) {
  const isGroup = chat.type === "group";
  const isChannel = chat.type === "channel";
  const blocked = !!chat.blocked;
  const online = !!(chat.peer && chat.peer.online);
  const creator = chat.my_role === "owner";
  const subs = chat.members_count || 0;

  const sub = blocked
    ? "Заблокирован чат"
    : isChannel
      ? `@${chat.username || "channel"} · ${subs} ${plural(subs, "подписчик", "подписчика", "подписчиков")}`
      : isGroup
        ? `${subs} ${plural(subs, "участник", "участника", "участников")}`
        : chat.peer
          ? online
            ? "в сети"
            : "был(а) недавно"
          : "";

  const headAvatar =
    isGroup || isChannel
      ? `<button class="head-ava" data-act="group-info" title="${isChannel ? "Участники канала" : "Информация о группе"}">${avatarHTML(
          { name: chat.title, avatar: chat.avatar }, "md")}</button>`
      : `<button class="head-ava" data-act="peer-profile" title="Профиль собеседника">${onlineDot(
          chat.peer, "md")}</button>`;

  let menu = "";
  if (isGroup) {
    menu = `
      <button data-act="rename">${icon("edit")}<span>Переименовать группу</span></button>
      <button data-act="add-member">${icon("user-plus")}<span>Добавить участника</span></button>
      <button data-act="clear">${icon("trash")}<span>Очистить чат</span></button>
      <button class="danger" data-act="leave">${icon("logout")}<span>Покинуть чат</span></button>`;
  } else if (isChannel) {
    menu = `<button data-act="group-info">${icon("users")}<span>Участники канала</span></button>`;
    if (creator) {
      menu += `
        <button data-act="rename">${icon("edit")}<span>Переименовать канал</span></button>
        <button data-act="clear">${icon("trash")}<span>Очистить историю</span></button>`;
    } else if (chat.joined) {
      menu += `<button class="danger" data-act="leave">${icon("logout")}<span>Покинуть канал</span></button>`;
    }
  } else {
    menu = `<button data-act="clear">${icon("trash")}<span>Очистить чат</span></button>` + (
      blocked
        ? `<button data-act="unblock">${icon("lock")}<span>Разблокировать</span></button>`
        : `<button class="danger" data-act="block">${icon("ban")}<span>Заблокировать</span></button>`
    );
  }

  const extra = `
      <button data-act="search-in-chat">${icon("search")}<span>Поиск по сообщениям</span></button>
      <button data-act="mute">${icon(chat.muted ? "bell" : "bell-off")}<span>${chat.muted ? "Включить звук" : "Тихий чат"}</span></button>
      <button data-act="pin-chat">${icon("pin")}<span>${chat.pin_chat ? "Открепить чат" : "Закрепить чат"}</span></button>
      <button data-act="auto-delete">${icon("clock")}<span>Исчезающие сообщения${chat.auto_delete ? ` · ${autoDeleteLabel(chat.auto_delete)}` : ""}</span></button>`;

  $("#conv-head").innerHTML = `
    <button class="icon-btn back-btn" data-act="back" title="Назад">${icon("back")}</button>
    ${headAvatar}
    <span class="who">
      <b>${esc(chat.title || "…")}${isChannel ? `<span class="type-chip">${icon("channel")}канал</span>` : ""}</b>
      <small class="${blocked ? "blocked" : !isGroup && !isChannel && online ? "on" : ""}">${esc(sub)}</small>
    </span>
    <span class="actions">
      ${
        !isChannel && chat.type !== "saved"
          ? `<button class="icon-btn" data-act="call-audio" title="Аудиозвонок">${icon("call")}</button>
             <button class="icon-btn" data-act="call-video" title="Видеозвонок">${icon("video")}</button>`
          : ""
      }
      ${isGroup ? `<button class="icon-btn" data-act="add-member" title="Добавить участника">${icon("user-plus")}</button>` : ""}
      ${isGroup || isChannel ? `<button class="icon-btn" data-act="group-info" title="Участники">${icon("users")}</button>` : ""}
      <button class="icon-btn" data-act="menu" title="Ещё">${icon("dots")}</button>
    </span>
    <div class="menu hidden" id="conv-menu">${menu}${extra}</div>`;

  renderComposerState();
}

/**
 * Нижняя зона чата: блокировка, канал без вступления, подписка без права
 * писать и запись голосового.
 */
function renderComposerState() {
  const chat = state.chat;
  const blocked = !!(chat && chat.blocked);
  const isChannel = !!(chat && chat.type === "channel");
  const notJoined = isChannel && chat.joined === false;
  const readOnly = isChannel && !notJoined && chat.my_role !== "owner";
  const recording = !!voice.rec;
  const showForm = !blocked && !notJoined && !readOnly;

  $("#conv-form").classList.toggle("hidden", recording || !showForm);
  $("#rec-bar").classList.toggle("hidden", !recording);
  $("#blocked-bar").classList.toggle("hidden", !blocked);
  $("#chat-bar").classList.toggle("hidden", blocked || recording || (!notJoined && !readOnly));

  if (blocked) {
    state.pendingFile = null;
    $("#attach-input").value = "";
    $("#attach-bar").classList.add("hidden");
  }

  if (!blocked && !recording && (notJoined || readOnly)) {
    const action = $("#cb-action");
    if (notJoined) {
      $("#cb-title").textContent = "Канал не в ваших чатах";
      $("#cb-sub").textContent =
        `@${chat.username} · ${subsText(chat)}`;
      action.classList.remove("hidden");
      action.textContent = "Присоединиться";
      action.dataset.act = "join";
    } else {
      $("#cb-title").textContent = "Писать в канал может только создатель";
      $("#cb-sub").textContent = `@${chat.username} · вы подписаны`;
      action.classList.add("hidden");
      action.dataset.act = "";
      action.textContent = "";
    }
  }
}

function subsText(chat) {
  const n = chat.members_count || 0;
  return `${n} ${plural(n, "подписчик", "подписчика", "подписчиков")}`;
}

$("#conv-head").addEventListener("click", async (e) => {
  const target = e.target.closest("[data-act]");
  if (!target) return;
  const act = target.dataset.act;
  const chat = state.chat;

  if (act === "back") return (location.hash = "#/chats");

  if (act === "menu") {
    const menu = $("#conv-menu");
    menu.classList.toggle("hidden");
    e.stopPropagation();
    return;
  }

  $("#conv-menu")?.classList.add("hidden");

  if (act === "peer-profile") return profileModal(chat.peer);
  if (act === "group-info") return groupInfoModal(chat);
  if (act === "search-in-chat") return openMsgSearch();
  if (act === "mute") return toggleMute(chat);
  if (act === "pin-chat") return togglePinChat(chat);
  if (act === "auto-delete") return autoDeleteDialog(chat);

  if (act === "call-audio") return startCall("audio");
  if (act === "call-video") return startCall("video");

  if (act === "clear") {
    const isChannel = chat.type === "channel";
    const scope = await choiceModal({
      title: isChannel ? "Очистить историю канала?" : "Очистить чат?",
      hint: isChannel
        ? "Сообщения исчезнут из переписки. Для кого именно?"
        : "Сообщения исчезнут из переписки. Для кого именно?",
      options: [
        {
          label: "Только у меня",
          desc: isChannel ? "Подписчики продолжат видеть историю" : "Собеседник продолжит видеть историю",
          value: "me",
        },
        {
          label: isChannel ? "У всех подписчиков" : "У двоих",
          desc: isChannel ? "История исчезнет у всех, кто подписан" : "История исчезнет у собеседника тоже",
          value: "both",
          danger: true,
        },
      ],
    });
    if (!scope) return;
    try {
      state.chat = await api(`/api/chats/${chat.id}/state`, {
        method: "POST",
        body: { action: "clear", scope },
      });
      await reloadMessages();
      toast(scope === "both" ? (isChannel ? "История очищена у всех" : "Чат очищен у двоих") : "Чат очищен");
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }

  if (act === "block") {
    const scope = await choiceModal({
      title: "Заблокировать чат?",
      hint: "В переписке появится отметка «Заблокирован чат», писать будет нельзя.",
      options: [
        { label: "Только у меня", desc: "Собеседник об этом не узнает", value: "me" },
        { label: "У двоих", desc: "Писать не сможете ни вы, ни собеседник", value: "both", danger: true },
      ],
    });
    if (!scope) return;
    try {
      state.chat = await api(`/api/chats/${chat.id}/state`, {
        method: "POST",
        body: { action: "block", scope },
      });
      renderChatHeader(state.chat);
      await refreshChats(true);
      toast("Чат заблокирован");
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }

  if (act === "unblock") {
    try {
      const scope = chat.blocked_all ? "both" : "me";
      state.chat = await api(`/api/chats/${chat.id}/state`, {
        method: "POST",
        body: { action: "unblock", scope },
      });
      renderChatHeader(state.chat);
      await refreshChats(true);
      toast("Чат разблокирован");
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }

  if (act === "rename") {
    const value = await promptModal(
      chat.type === "channel" ? "Переименовать канал" : "Переименовать группу",
      "Название",
      chat.title
    );
    if (!value) return;
    try {
      state.chat = await api(`/api/chats/${chat.id}`, { method: "PATCH", body: { title: value } });
      renderChatHeader(state.chat);
      await refreshChats(true);
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }

  if (act === "add-member") {
    const username = await promptModal("Добавить участника", "Ник человека", "");
    if (!username) return;
    try {
      await api(`/api/chats/${chat.id}/members`, { method: "POST", body: { username } });
      state.chat = await api(`/api/chats/${chat.id}`);
      renderChatHeader(state.chat);
      await refreshChats(true);
      toast("Участник добавлен");
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }

  if (act === "leave") {
    const isChannel = chat.type === "channel";
    const ok = await confirmModal(
      isChannel ? "Покинуть канал?" : chat.type === "group" ? "Покинуть группу?" : "Покинуть чат?",
      isChannel
        ? "Вы перестанете быть подписчиком, но канал можно найти поиском снова."
        : "Сообщения останутся у остальных участников, у вас — исчезнут."
    );
    if (!ok) return;
    try {
      await api(`/api/chats/${chat.id}/members/me`, { method: "DELETE" });
      await refreshChats();
      location.hash = "#/chats";
      toast(isChannel ? "Вы отписались от канала" : "Вы покинули чат");
    } catch (err) {
      toast(err.message, true);
    }
  }
});

document.addEventListener("click", (e) => {
  const menu = $("#conv-menu");
  if (menu && !menu.classList.contains("hidden") && !e.target.closest("#conv-menu") && !e.target.closest('[data-act="menu"]')) {
    menu.classList.add("hidden");
  }
});

/* ---------- голосовое: свой плеер вместо нативного <audio controls> ---------- */

function voicePlayerHTML(src) {
  return `
    <div class="voice-player" data-src="${esc(src)}">
      <button class="vp-play" type="button" title="Слушать">
        <svg class="ic-play"><use href="#i-play"/></svg>
        <svg class="ic-pause"><use href="#i-pause"/></svg>
      </button>
      <span class="vp-track" title="Перемотать"><span class="vp-fill"></span></span>
      <span class="vp-time">0:00</span>
      <audio preload="metadata" src="${esc(src)}"></audio>
    </div>`;
}

const fmtDuration = (s) => {
  if (!isFinite(s) || s < 0) s = 0;
  return `${Math.floor(s / 60)}:${pad(Math.floor(s % 60))}`;
};

/** Привязывает управление к новым плеерам (вызывается после вставки сообщений). */
function wireVoicePlayers(root) {
  (root || document).querySelectorAll(".voice-player").forEach((box) => {
    if (box.dataset.wired) return;
    box.dataset.wired = "1";
    const audio = box.querySelector("audio");
    const fill = box.querySelector(".vp-fill");
    const time = box.querySelector(".vp-time");
    const track = box.querySelector(".vp-track");
    if (!audio) return;

    const paint = () => {
      const d = isFinite(audio.duration) ? audio.duration : 0;
      fill.style.width = d ? `${(audio.currentTime / d) * 100}%` : "0%";
      time.textContent = fmtDuration(audio.currentTime || d);
    };

    // файл может отсутствовать (например, удалён) — показываем это спокойно,
    // а не битый плеер с пустой длительностью
    const fail = () => {
      box.classList.add("broken");
      box.title = "Аудиофайл недоступен";
      time.textContent = "нет файла";
      fill.style.width = "0%";
      const btn = box.querySelector(".vp-play");
      if (btn) btn.disabled = true;
    };
    audio.addEventListener("error", fail);
    if (audio.error) fail();

    audio.addEventListener("loadedmetadata", paint);
    audio.addEventListener("durationchange", paint);
    audio.addEventListener("timeupdate", paint);
    audio.addEventListener("play", () => box.classList.add("playing"));
    audio.addEventListener("pause", () => {
      box.classList.remove("playing");
      paint();
    });
    audio.addEventListener("ended", () => {
      box.classList.remove("playing");
      audio.currentTime = 0;
      fill.style.width = "0%";
      time.textContent = fmtDuration(isFinite(audio.duration) ? audio.duration : 0);
    });

    track.addEventListener("click", (ev) => {
      const r = track.getBoundingClientRect();
      const p = Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width));
      const seek = () => {
        if (isFinite(audio.duration)) audio.currentTime = p * audio.duration;
      };
      if (isFinite(audio.duration)) seek();
      else audio.addEventListener("loadedmetadata", seek, { once: true });
    });
  });
}

function toggleVoice(box) {
  const audio = box.querySelector("audio");
  if (!audio) return;
  if (box.classList.contains("broken")) {
    toast("Аудиофайл недоступен", true);
    return;
  }
  if (!audio.paused) {
    audio.pause();
    return;
  }
  document.querySelectorAll("#conv-body audio").forEach((a) => {
    if (a !== audio) a.pause();
  });
  audio.play().catch(() => toast("Не удалось воспроизвести запись", true));
}

function stopAllVoice() {
  document.querySelectorAll("#conv-body audio").forEach((a) => a.pause());
}

/* ---------- отрисовка сообщений ---------- */

function pollHTML(m) {
  const p = m.poll;
  const voted = p.my_vote !== null && p.my_vote !== undefined;
  const opts = p.options
    .map(
      (o, i) => `
      <button class="poll-opt${p.my_vote === i ? " mine" : ""}" data-act="vote" data-i="${i}" type="button">
        <span class="bar" style="width:${p.total ? o.pct : 0}%"></span>
        <span>${esc(o.text)}</span>
        <span class="pct">${p.total ? o.pct + "%" : ""}</span>
      </button>`
    )
    .join("");
  return `<div class="poll">
      <div class="poll-q">${esc(p.question)}</div>
      ${opts}
      <div class="poll-meta">${p.total} ${plural(p.total, "голос", "голоса", "голосов")}${voted ? " · голос учтён" : " · выберите вариант"}</div>
    </div>`;
}

/** Служебное сообщение с итогом звонка: пилюля по центру переписки. */
function sysCallHTML(m, opts = {}) {
  let html = "";
  if (opts.showDay) html += `<div class="day-sep">${esc(opts.showDay)}</div>`;
  html += `<div class="msg sys${opts.enter ? " enter" : ""}" data-id="${m.id}">
      <div class="bubble">${icon("call")}<span class="sys-text">${esc(m.text)}</span><span class="sys-time">${clockTime(m.created_at)}</span></div>
    </div>`;
  return html;
}

function messageHTML(m, opts = {}) {
  m._opts = opts;
  if (m.system === "call") return sysCallHTML(m, opts);
  const showDay = opts.showDay;
  const showName = opts.showName;
  const inGroup = !!(state.chat && state.chat.type === "group");
  const rx = m.reactions || [];
  let html = "";
  if (showDay) html += `<div class="day-sep">${esc(showDay)}</div>`;
  html += `<div class="msg ${m.mine ? "mine" : ""}${opts.enter ? " enter" : ""}" data-id="${m.id}">`;
  // в личном чате аватарки рядом с сообщениями не нужны
  if (!m.mine && inGroup) {
    html += `<span class="ava-btn" data-act="sender-profile" data-u="${esc(m.sender.username)}" title="Профиль">${avatarHTML(m.sender, "sm")}</span>`;
  }
  html += `<div class="bubble has-more">`;
  if (m.forwarded_from) {
    html += `<span class="fwd">${icon("forward")}${esc(m.forwarded_from)}</span>`;
  }
  if (showName) html += `<span class="sender">${esc(m.sender.name)}</span>`;
  if (m.reply) {
    const body =
      m.reply.text ||
      (m.reply.image ? "изображение" : m.reply.audio ? "голосовое сообщение" : "сообщение");
    html += `<div class="quote" data-act="jump" data-id="${m.reply.id}">
      <b>${esc(m.reply.sender)}</b><small>${esc(body)}</small></div>`;
  }
  if (m.text) {
    html += `<span class="text">${formatText(m.text)}${m.edited ? `<span class="edited">изменено</span>` : ""}</span>`;
  }
  if (m.image) html += `<img class="attach" src="${esc(m.image)}" alt="">`;
  if (m.audio) html += voicePlayerHTML(m.audio);
  if (m.poll) html += pollHTML(m);
  html += m.mine
    ? `<span class="stamp ${esc(m.status)}">${m.status === "read" ? icon("checks") : icon("check")}${clockTime(m.created_at)}</span>`
    : `<span class="stamp">${clockTime(m.created_at)}</span>`;
  if (m.mine) html += `<button class="del" data-act="delete-msg" data-id="${m.id}" title="Удалить">${icon("trash")}</button>`;
  if (rx.length) {
    html += `<div class="rx-row">${rx
      .map(
        (r) =>
          `<button class="rx-chip${r.mine ? " mine" : ""}" data-act="rx" data-id="${m.id}" data-emoji="${esc(r.emoji)}" type="button">${esc(r.emoji)}<b>${r.count}</b></button>`
      )
      .join("")}</div>`;
  }
  html += `<button class="more-btn" data-act="msg-menu" data-id="${m.id}" type="button" title="Действия">${icon("more")}</button>`;
  html += `</div></div>`;
  return html;
}

function renderMessages(list, opts = {}) {
  const box = $("#conv-body");
  if (!list) list = state.messages;
  const isGroup = !!(state.chat && state.chat.type === "group");

  let html = state.hasMore ? `<button class="load-older" data-act="load-older">Показать более ранние</button>` : "";
  let day = "";
  let prev = null;

  list.forEach((m) => {
    const d = dayLabel(m.created_at);
    const showDay = d !== day ? d : "";
    if (showDay) {
      day = d;
      prev = null;
    }
    const sameSender = prev && prev.sender.id === m.sender.id && prev.mine === m.mine;
    html += messageHTML(m, {
      showDay,
      showName: !m.mine && isGroup && !sameSender,
      enter: opts.enter,
    });
    prev = m;
  });

  box.innerHTML = html || `<div class="empty">${icon("msg")}<br>Пока пусто. Напишите первое сообщение.</div>`;
  wireVoicePlayers(box);
  state.lastDay = day;
  state.lastSender = prev;
  box.scrollTop = box.scrollHeight;
}

function appendMessages(list) {
  const box = $("#conv-body");
  const empty = box.querySelector(".empty");
  if (empty) box.innerHTML = "";

  const isGroup = !!(state.chat && state.chat.type === "group");
  let html = "";
  let day = state.lastDay;
  let prev = state.lastSender;

  list.forEach((m) => {
    const d = dayLabel(m.created_at);
    const showDay = d !== day ? d : "";
    if (showDay) {
      day = d;
      prev = null;
    }
    const sameSender = prev && prev.sender.id === m.sender.id && prev.mine === m.mine;
    html += messageHTML(m, {
      showDay,
      showName: !m.mine && isGroup && !sameSender,
      enter: true,
    });
    prev = m;
  });

  box.insertAdjacentHTML("beforeend", html);
  wireVoicePlayers(box);
  state.lastDay = day;
  state.lastSender = prev;
  box.scrollTop = box.scrollHeight;
}

async function loadOlder() {
  const first = state.messages[0];
  if (!first) return;
  const box = $("#conv-body");
  const keep = box.scrollHeight - box.scrollTop;
  try {
    const data = await api(`/api/chats/${state.activeId}/messages?before=${first.id}`);
    if (!data.messages.length) {
      state.hasMore = false;
      renderMessages(state.messages);
      return;
    }
    state.messages = [...data.messages, ...state.messages];
    state.hasMore = data.has_more;
    renderMessages(state.messages);
    box.scrollTop = box.scrollHeight - keep;
  } catch (err) {
    toast(err.message, true);
  }
}

function renderTypers(typers) {
  const box = $("#conv-body");
  $("#typing-line")?.remove();
  state.typersIn = state.typersIn || {};
  if (state.activeId) state.typersIn[state.activeId] = typers.length > 0;

  if (!typers.length) return;
  const names = typers.map((t) => t.name).join(", ");
  box.insertAdjacentHTML(
    "beforeend",
    `<div class="msg" id="typing-line"><div class="bubble"><span class="typing"><i></i><i></i><i></i></span></div>
     <span style="font-size:12.5px;color:var(--muted)">${esc(names)} печатает…</span></div>`
  );
  box.scrollTop = box.scrollHeight;
}

async function pollChat() {
  if (!state.activeId) return stopPolling();
  try {
    const data = await api(`/api/chats/${state.activeId}/messages?after=${state.lastId}`);
    if (data.messages.length) {
      state.messages.push(...data.messages);
      state.lastId = data.messages[data.messages.length - 1].id;
      appendMessages(data.messages);
      $("#typing-line")?.remove();
      if (state.chat && state.chat.joined !== false) await markRead(state.lastId, false);
      await refreshChats();
    }
    renderTypers(data.typers);
    syncHeaderStatus();
  } catch (err) {
    stopPolling();
    toast(err.message, true);
    if (location.hash !== "#/chats") location.hash = "#/chats";
  }
}

async function markRead(lastId, notify = true) {
  try {
    await api(`/api/chats/${state.activeId}/read`, { method: "POST", body: { last_id: lastId } });
    if (notify) await refreshChats();
    else {
      const cur = state.chats.find((c) => c.id === state.activeId);
      if (cur) {
        cur.unread = 0;
        updateActiveRow(cur);
      }
    }
  } catch {}
}

/* ============================================================ отправка */

const input = $("#msg-input");

function autoGrow() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 132) + "px";
}

input.addEventListener("input", () => {
  autoGrow();
  scheduleDraft();
  const now = Date.now();
  if (state.activeId && now - state.typingSent > 3000 && input.value.trim()) {
    state.typingSent = now;
    api(`/api/chats/${state.activeId}/typing`, { method: "POST" }).catch(() => {});
  }
});

input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("#conv-form").requestSubmit();
  }
});

$("#attach-input").addEventListener("change", (e) => {
  const file = e.target.files[0];
  state.pendingFile = file || null;
  $("#attach-bar").classList.toggle("hidden", !file);
  $("#attach-name").textContent = file ? file.name : "";
});

$("#attach-cancel").addEventListener("click", () => {
  state.pendingFile = null;
  $("#attach-input").value = "";
  $("#attach-bar").classList.add("hidden");
});

$("#blocked-bar").addEventListener("click", async (e) => {
  const btn = e.target.closest('[data-act="unblock"]');
  if (!btn || !state.chat) return;
  try {
    state.chat = await api(`/api/chats/${state.chat.id}/state`, {
      method: "POST",
      body: { action: "unblock", scope: state.chat.blocked_all ? "both" : "me" },
    });
    renderChatHeader(state.chat);
    await refreshChats(true);
    toast("Чат разблокирован");
    $("#msg-input").focus();
  } catch (err) {
    toast(err.message, true);
  }
});

/* ---------- голосовые сообщения ---------- */

function micDevice() {
  try {
    return localStorage.getItem("kofi.mic") || "";
  } catch {
    return "";
  }
}

/** Список микрофонов в настройках: подписи видны после доступа к мику. */
async function fillMicSelect() {
  const sel = $("#set-mic");
  if (!sel || !navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
  const saved = micDevice();
  const inputs = async () =>
    (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "audioinput");

  const paint = (list) => {
    if (!document.body.contains(sel)) return;
    sel.innerHTML =
      `<option value="">Микрофон по умолчанию</option>` +
      list
        .map(
          (d, i) =>
            `<option value="${esc(d.deviceId)}"${d.deviceId === saved ? " selected" : ""}>` +
            `${esc(d.label || `Микрофон ${i + 1}`)}</option>`
        )
        .join("");
    if (saved && ![...sel.options].some((o) => o.value === saved)) sel.value = "";
  };

  let list = [];
  try {
    list = await inputs();
  } catch {
    return;
  }
  paint(list); // пока есть что показать — показываем сразу (без разрешения будут номера)

  if (list.length && list.every((d) => !d.label)) {
    // названия скрыты — одно разрешение на микрофон, и список обновится с именами
    try {
      const s = await navigator.mediaDevices.getUserMedia({ audio: true });
      s.getTracks().forEach((t) => t.stop());
      paint(await inputs());
    } catch {
      /* отказались — остаёмся на номерах */
    }
  }
}

function cleanupVoice() {
  clearInterval(voice.timer);
  voice.timer = null;
  voice.rec = null;
  voice.chunks = [];
  if (voice.stream) {
    voice.stream.getTracks().forEach((t) => t.stop());
    voice.stream = null;
  }
}

function recTimeTick() {
  const s = Math.floor((Date.now() - voice.start) / 1000);
  const el = $("#rec-time");
  if (el) el.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

async function startRecording() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || typeof MediaRecorder === "undefined") {
    toast("Этот браузер не умеет записывать голос", true);
    return;
  }
  try {
    const want = micDevice();
    voice.stream = await navigator.mediaDevices.getUserMedia({
      audio: want ? { deviceId: { exact: want } } : true,
    });
  } catch {
    toast("Нет доступа к микрофону", true);
    return;
  }
  const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"];
  voice.mime = candidates.find((t) => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t)) || "";
  const rec = new MediaRecorder(voice.stream, voice.mime ? { mimeType: voice.mime } : undefined);
  voice.chunks = [];
  voice.send = true;
  voice.start = Date.now();
  rec.ondataavailable = (ev) => {
    if (ev.data && ev.data.size) voice.chunks.push(ev.data);
  };
  rec.onstop = sendRecorded;
  try {
    rec.start();
  } catch {
    cleanupVoice();
    renderComposerState();
    toast("Не удалось начать запись", true);
    return;
  }
  voice.rec = rec;
  recTimeTick();
  voice.timer = setInterval(recTimeTick, 300);
  renderComposerState();
}

/** Останавливает запись: send=true — отправить, false — отменить. */
function stopRecording(send) {
  if (!voice.rec) return;
  voice.send = send;
  const rec = voice.rec;
  voice.rec = null;
  clearInterval(voice.timer);
  voice.timer = null;
  if (rec.state !== "inactive") rec.stop();
  else sendRecorded();
}

async function sendRecorded() {
  const duration = Date.now() - voice.start;
  const chunks = voice.chunks;
  const mime = voice.mime || "audio/webm";
  const send = voice.send;
  const chatId = state.activeId;
  if (voice.stream) {
    voice.stream.getTracks().forEach((t) => t.stop());
    voice.stream = null;
  }
  voice.chunks = [];
  if (state.activeId) renderComposerState();
  if (!send || !chatId) return;

  const type = mime.split(";")[0];
  const blob = new Blob(chunks, { type });
  if (!blob.size) return toast("Не удалось записать звук", true);
  if (duration < 700) return toast("Запись слишком короткая", true);

  const ext = mime.includes("mp4") ? "m4a" : mime.includes("ogg") ? "ogg" : "webm";
  const fd = new FormData();
  fd.append("text", "");
  fd.append("audio", new File([blob], `voice.${ext}`, { type }));
  try {
    const msg = await api(`/api/chats/${chatId}/messages`, { method: "POST", form: fd });
    if (state.activeId !== chatId) return;
    state.messages.push(msg);
    state.lastId = msg.id;
    appendMessages([msg]);
    await refreshChats();
  } catch (err) {
    toast(err.message, true);
  }
}

$("#mic-btn").addEventListener("click", () => startRecording());
$("#rec-send").addEventListener("click", () => stopRecording(true));
$("#rec-cancel").addEventListener("click", () => stopRecording(false));

/* ---------- канал без вступления: кнопка «Присоединиться» ---------- */

$("#chat-bar").addEventListener("click", async (e) => {
  const btn = e.target.closest("#cb-action");
  if (!btn || btn.dataset.act !== "join" || !state.chat) return;
  btn.disabled = true;
  try {
    state.chat = await api(`/api/chats/${state.chat.id}/join`, { method: "POST" });
    renderChatHeader(state.chat);
    await refreshChats(true);
    toast("Вы подписаны на канал");
    $("#msg-input")?.focus();
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
  }
});

$("#conv-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (state.chat && state.chat.blocked) {
    toast("Чат заблокирован", true);
    return;
  }
  if (state.chat && state.chat.type === "channel" && state.chat.my_role !== "owner") {
    toast("Писать в канал может только его создатель", true);
    return;
  }
  const text = input.value.trim();

  // правка своего сообщения
  if (state.editing) {
    if (!text) return;
    try {
      const upd = await api(`/api/messages/${state.editing}`, {
        method: "PATCH",
        body: { text },
      });
      const i = state.messages.findIndex((m) => m.id === upd.id);
      if (i > -1) state.messages[i] = { ...state.messages[i], ...upd };
      cancelComposer();
      renderMessages(state.messages);
      toast("Сообщение изменено");
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }

  if (!text && !state.pendingFile) return;
  const fd = new FormData();
  fd.append("text", text);
  if (state.pendingFile) fd.append("image", state.pendingFile);
  if (state.replyTo) fd.append("reply_to", state.replyTo.id);

  try {
    const msg = await api(`/api/chats/${state.activeId}/messages`, { method: "POST", form: fd });
    state.messages.push(msg);
    state.lastId = msg.id;
    input.value = "";
    autoGrow();
    state.pendingFile = null;
    $("#attach-input").value = "";
    $("#attach-bar").classList.add("hidden");
    cancelComposer();
    saveDraft("");
    $("#typing-line")?.remove();
    appendMessages([msg]);
    await refreshChats();
  } catch (err) {
    toast(err.message, true);
  }
});

$("#conv-body").addEventListener("click", async (e) => {
  const img = e.target.closest(".msg .attach");
  if (img) return window.open(img.src, "_blank");

  const vp = e.target.closest(".voice-player");
  if (vp) {
    if (e.target.closest(".vp-track")) return; // перемотка — своим обработчиком
    return toggleVoice(vp);
  }

  const quote = e.target.closest('[data-act="jump"]');
  if (quote) return jumpToMessage(+quote.dataset.id);

  const chip = e.target.closest('[data-act="rx"]');
  if (chip) return toggleReaction(+chip.dataset.id, chip.dataset.emoji);

  const vote = e.target.closest('[data-act="vote"]');
  if (vote) return votePoll(+vote.closest(".msg").dataset.id, +vote.dataset.i);

  const menu = e.target.closest('[data-act="msg-menu"]');
  if (menu) {
    const r = menu.getBoundingClientRect();
    return openMsgMenu(+menu.dataset.id, r.right, r.bottom + 8);
  }

  const ava = e.target.closest('[data-act="sender-profile"]');
  if (ava) {
    const username = ava.dataset.u;
    const found = (state.chat && state.chat.members
      ? state.chat.members.find((u) => u.username === username)
      : null) || null;
    if (found) return profileModal(found);
    return;
  }

  const del = e.target.closest('[data-act="delete-msg"]');
  if (!del) return;
  const id = +del.dataset.id;
  if (!await confirmModal("Удалить сообщение?", "Действие нельзя отменить.")) return;
  try {
    await api(`/api/messages/${id}`, { method: "DELETE" });
    state.messages = state.messages.filter((m) => m.id !== id);
    $(`.msg[data-id="${id}"]`)?.remove();
    await refreshChats();
  } catch (err) {
    toast(err.message, true);
  }
});

/* ============================================================ модалки */

function modal({ title, hint = "", value = "", placeholder = "", okText = "OK", danger = false }) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.innerHTML = `
      <div class="modal glass">
        <h3>${esc(title)}</h3>
        ${hint ? `<p>${esc(hint)}</p>` : ""}
        ${placeholder === null ? "" : `<input id="modal-input" placeholder="${esc(placeholder)}" value="${esc(value)}" maxlength="120">`}
        <div class="modal-actions">
          <button class="btn ghost" data-act="cancel">Отмена</button>
          <button class="btn ${danger ? "danger" : "primary"}" data-act="ok">${esc(okText)}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const field = $("#modal-input", overlay);
    if (field) {
      field.focus();
      field.select();
      field.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") overlay.querySelector('[data-act="ok"]').click();
      });
    }
    const done = (val) => {
      overlay.style.animation = "fadeIn .15s reverse";
      setTimeout(() => overlay.remove(), 120);
      resolve(val);
    };
    overlay.querySelector('[data-act="cancel"]').addEventListener("click", () => done(null));
    overlay.querySelector('[data-act="ok"]').addEventListener("click", () =>
      done(field ? field.value.trim() || null : true)
    );
    overlay.addEventListener("click", (ev) => {
      if (ev.target === overlay) done(null);
    });
  });
}

const promptModal = (title, placeholder, value) =>
  modal({ title, placeholder, value, okText: "Готово" });

const confirmModal = (title, hint) =>
  modal({ title, hint, placeholder: null, okText: "Да", danger: true });

/** Выбор из вариантов: возвращает value выбранной кнопки или null. */
function choiceModal({ title, hint = "", options = [] }) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.innerHTML = `
      <div class="modal glass">
        <h3>${esc(title)}</h3>
        ${hint ? `<p>${esc(hint)}</p>` : ""}
        <div class="choice-list">
          ${options.map((o, i) => `
            <button class="choice ${o.danger ? "danger" : ""}" data-i="${i}">
              <b>${esc(o.label)}</b>${o.desc ? `<small>${esc(o.desc)}</small>` : ""}
            </button>`).join("")}
        </div>
        <div class="modal-actions">
          <button class="btn ghost" data-act="cancel">Отмена</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);

    const done = (val) => {
      overlay.style.animation = "fadeIn .15s reverse";
      setTimeout(() => overlay.remove(), 120);
      resolve(val);
    };
    overlay.querySelectorAll(".choice").forEach((b) =>
      b.addEventListener("click", () => done(options[+b.dataset.i].value))
    );
    overlay.querySelector('[data-act="cancel"]').addEventListener("click", () => done(null));
    overlay.addEventListener("click", (ev) => {
      if (ev.target === overlay) done(null);
    });
  });
}

function _closeOverlay(overlay) {
  overlay.style.animation = "fadeIn .15s reverse";
  setTimeout(() => overlay.remove(), 120);
}

/** Карточка профиля: открывается кликом по аватарке. */
/** Профиль собеседника — как в Telegram: обложка, большое аватар-имя, карточка сведений. */
function profileModal(u) {
  if (!u) return;
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  const online = !!u.online;
  const seen = u.last_seen ? shortTime(u.last_seen) : "";

  overlay.innerHTML = `
    <div class="tg-profile glass">
      <div class="tp-cover">
        <button class="icon-btn ghost tp-close" data-act="cancel" title="Закрыть">${icon("x")}</button>
        <div class="tp-ava">${avatarHTML(u, "xl")}</div>
      </div>

      <div class="tp-body">
        <h3>${esc(u.name || u.username || "?")}</h3>
        <div class="tp-status ${online ? "on" : ""}">${
          online ? "в сети" : seen ? `был(а) ${seen}` : "был(а) недавно"
        }</div>

        <div class="tp-card">
          <div class="tp-row">
            <span>Ник</span>
            <b class="tp-nick">@${esc(u.username || "")}</b>
          </div>
          <div class="tp-row">
            <span>Имя</span>
            <b>${esc(u.name || "—")}</b>
          </div>
          <div class="tp-row">
            <span>О себе</span>
            <b>${esc(u.status && u.status !== "В сети" ? u.status : "—")}</b>
          </div>
          <div class="tp-row">
            <span>Активность</span>
            <b>${online ? "сейчас в сети" : seen ? `был(а) ${seen}` : "давно"}</b>
          </div>
        </div>

        <div class="tp-actions">
          ${u.is_me ? "" : `<button class="btn primary" data-act="write">${icon("msg")}<span>Написать сообщение</span></button>`}
          <button class="btn ghost" data-act="cancel">Закрыть</button>
        </div>
      </div>
    </div>`;

  document.body.appendChild(overlay);
  overlay.querySelectorAll('[data-act="cancel"]').forEach((b) =>
    b.addEventListener("click", () => _closeOverlay(overlay))
  );
  overlay.addEventListener("click", (ev) => {
    if (ev.target === overlay) _closeOverlay(overlay);
  });

  const write = overlay.querySelector('[data-act="write"]');
  if (write) {
    write.addEventListener("click", async () => {
      if (!u.username) return;
      write.disabled = true;
      _closeOverlay(overlay);
      try {
        const chat = await api("/api/chats", { method: "POST", body: { username: u.username } });
        await refreshChats();
        location.hash = `#/chat/${chat.id}`;
      } catch (err) {
        toast(err.message, true);
      }
    });
  }
}

/** Участники группы или канала: по клику — профиль. */
function groupInfoModal(chat) {
  const members = chat.members || [];
  const isChannel = chat.type === "channel";
  const count = isChannel
    ? `${members.length} ${plural(members.length, "подписчик", "подписчика", "подписчиков")}`
    : `${members.length} ${plural(members.length, "участник", "участника", "участников")}`;
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal glass group-card">
      <button class="icon-btn ghost pc-close" data-act="cancel" title="Закрыть">${icon("x")}</button>
      <div class="pc-ava">${avatarHTML({ name: chat.title, avatar: chat.avatar }, "lg")}</div>
      <h3>${esc(chat.title || (isChannel ? "Канал" : "Группа"))}</h3>
      <div class="pc-nick">${isChannel && chat.username ? `@${esc(chat.username)} · ` : ""}${count}</div>
      <div class="member-list">
        ${members.map((m) => `
          <button class="member" data-u="${esc(m.username)}">
            ${onlineDot(m, "sm")}
            <span class="ml-meta"><b>${esc(m.name)}</b><small>@${esc(m.username)}</small></span>
            <span class="ml-status ${m.online ? "on" : ""}">${
              m.role === "owner"
                ? `<span class="role-chip">создатель</span>`
                : m.is_me
                  ? "вы"
                  : m.online ? "в сети" : "был(а) недавно"
            }</span>
          </button>`).join("")}
      </div>
      <div class="modal-actions">
        <button class="btn primary" data-act="cancel">Закрыть</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  overlay.querySelectorAll('[data-act="cancel"]').forEach((b) =>
    b.addEventListener("click", () => _closeOverlay(overlay))
  );
  overlay.addEventListener("click", (ev) => {
    if (ev.target === overlay) _closeOverlay(overlay);
  });
  overlay.querySelectorAll(".member").forEach((b) =>
    b.addEventListener("click", () => {
      const u = members.find((x) => x.username === b.dataset.u);
      _closeOverlay(overlay);
      if (u) setTimeout(() => profileModal(u), 60);
    })
  );
}

/* ============================================================ маршруты */

function route() {
  if (!state.me) return;
  const parts = (location.hash || "#/chats").slice(2).split("/").filter(Boolean);

  if (parts[0] === "chat" && parts[1]) {
    const id = +parts[1];
    if (state.mode !== "chats") setMode("chats", false);
    if (state.activeId !== id) openChat(id);
    else document.body.classList.add("chat-open");
    return;
  }

  if (parts[0] === "settings") {
    if (state.activeId) closeChat();
    openProfile();
    return;
  }
  closeProfile();

  if (state.activeId) closeChat();
  const mode = ["chats", "group"].includes(parts[0]) ? parts[0] : "chats";
  setMode(mode, false);
}

/* ============================================================ Telegram-функции */

/* ------------------------- композер: ответ и правка ------------------------- */

function renderReplyStrip() {
  const strip = $("#reply-strip");
  const r = state.replyTo;
  const ed = state.editing;
  if (!r && !ed) {
    strip.classList.add("hidden");
    strip.classList.remove("editing");
    strip.innerHTML = "";
    return;
  }
  strip.classList.remove("hidden");
  if (ed) {
    const m = state.messages.find((x) => x.id === ed);
    strip.classList.add("editing");
    strip.innerHTML = `
      <span class="rs-bar"></span>
      <span class="rs-text"><b>Редактирование сообщения</b><small>${esc(m ? m.text || "изображение" : "")}</small></span>
      <button class="icon-btn ghost rs-close" type="button" data-act="cancel-composer" title="Отменить">${icon("x")}</button>`;
    input.placeholder = "Изменить текст";
    return;
  }
  strip.classList.remove("editing");
  strip.innerHTML = `
    <span class="rs-bar"></span>
    <span class="rs-text"><b>Ответ · ${esc(r.sender)}</b><small>${esc(r.text || "сообщение")}</small></span>
    <button class="icon-btn ghost rs-close" type="button" data-act="cancel-composer" title="Отменить">${icon("x")}</button>`;
  input.placeholder = "Ответ";
}

/** Сбрасывает ответ/правку — вызывается после отправки и при смене чата. */
function cancelComposer() {
  const had = state.replyTo || state.editing;
  state.replyTo = null;
  state.editing = null;
  if (had) {
    renderReplyStrip();
    input.placeholder = "Сообщение";
  }
}

function startReply(m) {
  state.editing = null;
  state.replyTo = {
    id: m.id,
    sender: m.mine ? "Вы" : m.sender.name,
    text: m.text || (m.image ? "изображение" : m.audio ? "голосовое сообщение" : m.poll ? "опрос" : "сообщение"),
  };
  renderReplyStrip();
  input.focus();
}

function startEdit(m) {
  if (!m.mine) return toast("Редактировать можно только свои сообщения", true);
  if (!m.text) return toast("Редактировать можно только текст", true);
  state.replyTo = null;
  state.editing = m.id;
  input.value = m.text;
  autoGrow();
  renderReplyStrip();
  input.focus();
}

$("#reply-strip").addEventListener("click", (e) => {
  if (e.target.closest('[data-act="cancel-composer"]')) cancelComposer();
});

/* ------------------------------- черновики -------------------------------- */

function scheduleDraft() {
  clearTimeout(state._draftTimer);
  state._draftTimer = setTimeout(() => saveDraft(), 700);
}

/** Черновик текста: хранится на сервере и виден в списке чатов. */
function saveDraft(text) {
  if (!state.activeId) return;
  const t = text !== undefined ? String(text) : input.value.trim();
  if (state.chat && (state.chat.draft || "") === t) return;
  if (state.chat) state.chat.draft = t;
  api(`/api/chats/${state.activeId}/draft`, { method: "POST", body: { text: t } }).catch(() => {});
}

function applyDraft(text) {
  if (state.replyTo || state.editing) return;
  input.value = text || "";
  autoGrow();
  if (state.chat) state.chat.draft = text || "";
}

/* --------------------- переход к сообщению и поиск ------------------------ */

async function jumpToMessage(id) {
  if (!state.activeId) return;
  let el = $(`.msg[data-id="${id}"]`);
  if (!el) {
    try {
      const data = await api(`/api/chats/${state.activeId}/messages?before=${id + 1}`);
      if (!data.messages.length) return toast("Сообщение не найдено", true);
      state.messages = data.messages;
      state.hasMore = data.has_more;
      renderMessages(data.messages, { top: data.has_more });
    } catch (err) {
      return toast(err.message, true);
    }
    el = $(`.msg[data-id="${id}"]`);
  }
  if (!el) return toast("Сообщение не найдено", true);
  el.scrollIntoView({ block: "center", behavior: "smooth" });
  el.classList.remove("flash");
  void el.offsetWidth;
  el.classList.add("flash");
  setTimeout(() => el.classList.remove("flash"), 1700);
}

function openMsgSearch() {
  if (!state.activeId) return;
  const box = $("#msg-search");
  box.classList.remove("hidden");
  $("#ms-q").value = "";
  $("#ms-results").innerHTML = `<div class="empty">Введите слово из сообщения</div>`;
  $("#ms-q").focus();
}

function closeMsgSearch() {
  const box = $("#msg-search");
  if (box) box.classList.add("hidden");
}

$("#ms-close").addEventListener("click", closeMsgSearch);

let _msTimer = null;
$("#ms-q").addEventListener("input", (e) => {
  clearTimeout(_msTimer);
  const q = e.target.value.trim();
  if (!q) {
    $("#ms-results").innerHTML = `<div class="empty">Введите слово из сообщения</div>`;
    return;
  }
  _msTimer = setTimeout(() => runMsgSearch(q), 260);
});

async function runMsgSearch(q) {
  const box = $("#ms-results");
  box.innerHTML = `<div class="empty">Ищем…</div>`;
  try {
    const data = await api(`/api/chats/${state.activeId}/search?q=${encodeURIComponent(q)}`);
    if (!data.messages.length) {
      box.innerHTML = `<div class="empty">Ничего не найдено</div>`;
      return;
    }
    const safe = q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const re = new RegExp(`(${safe})`, "ig");
    box.innerHTML = data.messages
      .map((m) => {
        const body = esc(m.text || (m.image ? "изображение" : m.audio ? "голосовое сообщение" : "сообщение"));
        const who = m.mine ? "Вы" : m.sender.name;
        return `<div class="ms-hit" data-id="${m.id}">
          <b>${esc(who)} · ${clockTime(m.created_at)}</b>
          <small>${body.replace(re, "<mark>$1</mark>")}</small></div>`;
      })
      .join("");
  } catch (err) {
    box.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
}

$("#ms-results").addEventListener("click", (e) => {
  const hit = e.target.closest(".ms-hit");
  if (!hit) return;
  closeMsgSearch();
  jumpToMessage(+hit.dataset.id);
});

/* --------------------------- реакции и опросы ----------------------------- */

const findMsg = (id) => state.messages.find((m) => m.id === id);

function rerenderMsg(id) {
  const m = findMsg(id);
  const el = $(`.msg[data-id="${id}"]`);
  if (!m || !el) return;
  const tmp = document.createElement("div");
  tmp.innerHTML = messageHTML(m, { ...(m._opts || {}), enter: false });
  el.replaceWith(tmp.firstElementChild);
}

async function toggleReaction(id, emoji) {
  try {
    const data = await api(`/api/messages/${id}/reactions`, { method: "POST", body: { emoji } });
    const m = findMsg(id);
    if (m) m.reactions = data.reactions;
    rerenderMsg(id);
  } catch (err) {
    toast(err.message, true);
  }
}

async function votePoll(id, option) {
  try {
    const data = await api(`/api/messages/${id}/vote`, { method: "POST", body: { option } });
    const m = findMsg(id);
    if (m) m.poll = data.poll;
    rerenderMsg(id);
  } catch (err) {
    toast(err.message, true);
  }
}

/** Новый опрос: вопрос и варианты одним окном, как в Telegram. */
function pollDialog() {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.innerHTML = `
      <div class="modal glass">
        <h3>Новый опрос</h3>
        <div class="field">
          <label>Вопрос</label>
          <input id="poll-q" maxlength="200" placeholder="Куда едем в выходные?">
        </div>
        <div class="field">
          <label>Варианты ответа — каждый с новой строки</label>
          <textarea id="poll-opts" rows="4" maxlength="900" placeholder="Море&#10;Горы&#10;Дома"></textarea>
          <span class="hint">От 2 до 10 вариантов</span>
        </div>
        <div class="modal-actions">
          <button class="btn ghost" data-act="cancel">Отмена</button>
          <button class="btn primary" data-act="ok">Отправить</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const q = $("#poll-q", overlay);
    const o = $("#poll-opts", overlay);
    q.focus();
    const done = (val) => {
      overlay.style.animation = "fadeIn .15s reverse";
      setTimeout(() => overlay.remove(), 120);
      resolve(val);
    };
    overlay.querySelector('[data-act="cancel"]').addEventListener("click", () => done(null));
    overlay.querySelector('[data-act="ok"]').addEventListener("click", () => {
      const question = q.value.trim();
      const options = o.value.split("\n").map((s) => s.trim()).filter(Boolean).slice(0, 10);
      if (!question) return toast("Введите вопрос", true);
      if (options.length < 2) return toast("Нужно минимум два варианта", true);
      done({ question, options });
    });
    overlay.addEventListener("click", (ev) => {
      if (ev.target === overlay) done(null);
    });
  });
}

async function sendPoll(payload) {
  const fd = new FormData();
  fd.append("text", "");
  fd.append("poll", JSON.stringify(payload));
  if (state.replyTo) fd.append("reply_to", state.replyTo.id);
  try {
    const msg = await api(`/api/chats/${state.activeId}/messages`, { method: "POST", form: fd });
    state.messages.push(msg);
    state.lastId = msg.id;
    cancelComposer();
    saveDraft("");
    appendMessages([msg]);
    await refreshChats();
  } catch (err) {
    toast(err.message, true);
  }
}

$("#poll-btn").addEventListener("click", async () => {
  if (!state.activeId) return;
  if (state.chat && state.chat.type === "channel" && state.chat.my_role !== "owner") {
    return toast("Писать в канал может только его создатель", true);
  }
  const payload = await pollDialog();
  if (payload) sendPoll(payload);
});

/* ------------------------- контекстное меню чата -------------------------- */

const QUICK_RX = ["👍", "❤", "😂", "😮", "😢", "🔥"];

function closeCtxMenu() {
  const box = $("#ctx-menu");
  if (!box) return;
  box.classList.add("hidden");
  box.innerHTML = "";
}

function openMsgMenu(id, x, y) {
  const m = findMsg(id);
  if (!m) return;
  const canEdit = !!m.mine && !!m.text;
  const pinned = state.chat && state.chat.pinned;
  const mineRx = new Set((m.reactions || []).filter((r) => r.mine).map((r) => r.emoji));
  const box = $("#ctx-menu");
  box.dataset.id = id;
  box.innerHTML = `
    <div class="ctx-react">${QUICK_RX
      .map(
        (e) =>
          `<button type="button" class="${mineRx.has(e) ? "mine" : ""}" data-a="rx" data-emoji="${e}">${e}</button>`
      )
      .join("")}</div>
    <button class="ctx-item" data-a="reply" type="button">${icon("reply")}Ответить</button>
    <button class="ctx-item" data-a="copy" type="button">${icon("copy")}Копировать</button>
    <button class="ctx-item" data-a="forward" type="button">${icon("forward")}Пересылать</button>
    <button class="ctx-item" data-a="pin" type="button">${icon("pin")}${pinned && pinned.id === m.id ? "Открепить" : "Закрепить"}</button>
    ${canEdit ? `<button class="ctx-item" data-a="edit" type="button">${icon("edit")}Редактировать</button>` : ""}
    <button class="ctx-item danger" data-a="delete" type="button">${icon("trash")}Удалить</button>`;
  box.classList.remove("hidden");
  const w = box.offsetWidth;
  const h = box.offsetHeight;
  box.style.left = Math.max(8, Math.min(x, window.innerWidth - w - 8)) + "px";
  box.style.top = Math.max(8, Math.min(y, window.innerHeight - h - 8)) + "px";
}

$("#ctx-menu").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-a]");
  if (!btn) return;
  const id = +$("#ctx-menu").dataset.id;
  const m = findMsg(id);
  const act = btn.dataset.a;
  closeCtxMenu();
  if (!m) return;

  if (act === "rx") return toggleReaction(id, btn.dataset.emoji);
  if (act === "reply") return startReply(m);
  if (act === "edit") return startEdit(m);
  if (act === "forward") return forwardDialog(m);
  if (act === "pin") return pinMessage(m);

  if (act === "copy") {
    const text = m.text || (m.image ? m.image : m.audio ? m.audio : "");
    try {
      await navigator.clipboard.writeText(text);
      toast("Скопировано");
    } catch {
      toast("Не удалось скопировать", true);
    }
    return;
  }

  if (act === "delete") {
    if (!await confirmModal("Удалить сообщение?", "Действие нельзя отменить.")) return;
    try {
      await api(`/api/messages/${id}`, { method: "DELETE" });
      state.messages = state.messages.filter((x) => x.id !== id);
      $(`.msg[data-id="${id}"]`)?.remove();
      if (state.chat && state.chat.pinned && state.chat.pinned.id === id) {
        state.chat.pinned = null;
        renderPinnedBar(state.chat);
      }
      await refreshChats();
    } catch (err) {
      toast(err.message, true);
    }
  }
});

/** Пересылка: выбираем чат-получатель из списка. */
async function forwardDialog(m) {
  const targets = state.chats.filter((c) => c.id !== state.activeId);
  if (!targets.length) return toast("Некуда пересылать", true);
  const picked = await choiceModal({
    title: "Переслать в",
    hint: "Выберите чат-получатель",
    options: targets.map((c) => ({
      label: c.title,
      desc: c.type === "group" ? "группа" : c.type === "channel" ? "канал" : "личный чат",
      value: c.id,
    })),
  });
  if (!picked) return;
  try {
    await api(`/api/messages/${m.id}/forward`, { method: "POST", body: { chat_id: picked } });
    toast("Сообщение переслано");
    await refreshChats();
  } catch (err) {
    toast(err.message, true);
  }
}

async function pinMessage(m) {
  const pinned = state.chat && state.chat.pinned;
  const same = pinned && pinned.id === m.id;
  try {
    state.chat = await api(`/api/chats/${state.activeId}/pin`, {
      method: "POST",
      body: { message_id: same ? 0 : m.id },
    });
    renderPinnedBar(state.chat);
    toast(same ? "Закреп снят" : "Сообщение закреплено");
  } catch (err) {
    toast(err.message, true);
  }
}

/* --------------------- правый клик, долгое нажатие ------------------------- */

$("#conv-body").addEventListener("contextmenu", (e) => {
  const msg = e.target.closest(".msg");
  if (!msg || !msg.dataset.id) return;
  e.preventDefault();
  openMsgMenu(+msg.dataset.id, e.clientX, e.clientY);
});

let _pressTimer = null;
$("#conv-body").addEventListener("pointerdown", (e) => {
  if (e.pointerType === "mouse") return;
  const msg = e.target.closest(".msg");
  if (!msg || !msg.dataset.id) return;
  const id = +msg.dataset.id;
  clearTimeout(_pressTimer);
  _pressTimer = setTimeout(() => {
    _pressTimer = null;
    const r = msg.getBoundingClientRect();
    openMsgMenu(id, r.left + r.width / 2 - 60, r.top + 12);
  }, 450);
});
["pointerup", "pointercancel", "pointerleave"].forEach((ev) =>
  $("#conv-body").addEventListener(ev, () => clearTimeout(_pressTimer))
);

document.addEventListener("click", (e) => {
  if (e.target.closest("#ctx-menu")) return;
  if (e.target.closest('[data-act="msg-menu"]')) return;
  closeCtxMenu();
});
window.addEventListener("scroll", closeCtxMenu, true);

/* --------------------------- закреплённое сообщение ----------------------- */

function renderPinnedBar(chat) {
  const bar = $("#pinned-bar");
  const p = chat && chat.pinned;
  if (!p) {
    bar.classList.add("hidden");
    bar.innerHTML = "";
    return;
  }
  const body =
    p.text || (p.image ? "изображение" : p.audio ? "голосовое сообщение" : p.poll ? "опрос" : "сообщение");
  const who = p.mine ? "вы" : p.sender ? p.sender.name : "собеседник";
  bar.classList.remove("hidden");
  bar.innerHTML = `
    <span class="pin-ic">${icon("pin")}</span>
    <span class="pin-text"><b>Закреплённое сообщение · ${esc(who)}</b><small>${esc(body)}</small></span>
    <button class="icon-btn ghost pin-close" type="button" data-act="unpin" title="Открепить">${icon("x")}</button>`;
}

$("#pinned-bar").addEventListener("click", async (e) => {
  const p = state.chat && state.chat.pinned;
  if (!p) return;
  if (e.target.closest('[data-act="unpin"]')) {
    try {
      state.chat = await api(`/api/chats/${state.activeId}/pin`, {
        method: "POST",
        body: { message_id: 0 },
      });
      renderPinnedBar(state.chat);
      toast("Закреп снят");
    } catch (err) {
      toast(err.message, true);
    }
    return;
  }
  jumpToMessage(p.id);
});

/* ------------------- тишина, закреп чата, исчезающие ----------------------- */

async function toggleMute(chat) {
  try {
    const r = await api(`/api/chats/${chat.id}/mute`, {
      method: "POST",
      body: { muted: !chat.muted },
    });
    chat.muted = r.muted;
    if (state.chat && state.chat.id === chat.id) state.chat.muted = r.muted;
    renderChatHeader(state.chat);
    const row = state.chats.find((c) => c.id === chat.id);
    if (row) {
      row.muted = r.muted;
      updateActiveRow(row);
    }
    toast(r.muted ? "Уведомления этого чата выключены" : "Звук чата включён");
  } catch (err) {
    toast(err.message, true);
  }
}

async function togglePinChat(chat) {
  try {
    const r = await api(`/api/chats/${chat.id}/pin-chat`, {
      method: "POST",
      body: { pinned: !chat.pin_chat },
    });
    if (state.chat && state.chat.id === chat.id) state.chat.pin_chat = r.pinned;
    const row = state.chats.find((c) => c.id === chat.id);
    if (row) row.pin_chat = r.pinned;
    renderChatHeader(state.chat);
    await refreshChats();
    toast(r.pinned ? "Чат закреплён сверху" : "Чат откреплён");
  } catch (err) {
    toast(err.message, true);
  }
}

const AUTO_OPTS = [
  [0, "Не выключать"],
  [1, "1 час"],
  [24, "1 день"],
  [168, "1 неделя"],
  [720, "30 дней"],
];

function autoDeleteLabel(hours) {
  const f = AUTO_OPTS.find(([v]) => v === hours);
  return f ? f[1] : `${hours} ч`;
}

async function autoDeleteDialog(chat) {
  const hours = await choiceModal({
    title: "Исчезающие сообщения",
    hint: "История чистится автоматически: старые сообщения удаляются у всех участников.",
    options: AUTO_OPTS.map(([h, label]) => ({
      label,
      desc: h ? `Хранить сообщения ${label.toLowerCase()}` : "Хранить историю бессрочно",
      value: h,
      danger: h > 0 && chat.auto_delete === h,
    })),
  });
  if (hours === null) return;
  try {
    state.chat = await api(`/api/chats/${chat.id}/auto-delete`, {
      method: "POST",
      body: { hours },
    });
    renderChatHeader(state.chat);
    await reloadMessages();
    toast(hours ? `Сообщения старше ${autoDeleteLabel(hours)} будут исчезать` : "История больше не чистится");
  } catch (err) {
    toast(err.message, true);
  }
}

/* ------------------------------- Избранное -------------------------------- */

async function openSaved() {
  document.body.classList.remove("drawer-open");
  try {
    const chat = await api("/api/saved");
    await refreshChats();
    location.hash = `#/chat/${chat.id}`;
  } catch (err) {
    toast(err.message, true);
  }
}

/* --------------------------------- папки ---------------------------------- */

async function loadFolders() {
  try {
    const { folders } = await api("/api/folders");
    state.folders = folders;
  } catch {
    state.folders = state.folders || [];
  }
  renderFolders();
}

function renderFolders() {
  const bar = $("#folder-tabs");
  if (!bar) return;
  const list = state.folders || [];
  const show = state.mode === "chats" && !state.query;
  bar.classList.toggle("hidden", !show);
  if (!show) return;
  bar.innerHTML =
    `<button class="ft ${state.folderId ? "" : "active"}" type="button" data-f="0">${icon("chats")}Все</button>` +
    list
      .map(
        (f) =>
          `<button class="ft ${state.folderId === f.id ? "active" : ""}" type="button" data-f="${f.id}">${icon(f.icon || "folder")}${esc(f.name)}</button>`
      )
      .join("") +
    `<button class="ft add" type="button" data-f="add" title="Новая папка">${icon("plus")}</button>`;
}

$("#folder-tabs").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-f]");
  if (!b) return;
  if (b.dataset.f === "add") return folderDialog(null);
  const id = +b.dataset.f;
  if (state.folderId === id) return;
  state.folderId = id;
  renderFolders();
  await refreshChats();
});

/** Окно папки: название и состав чатов. */
function folderDialog(existing) {
  return new Promise((resolve) => {
    const chats = state.chats.filter((c) => c.type !== "saved");
    const picked = new Set(existing ? existing.chat_ids : []);
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.innerHTML = `
      <div class="modal glass group-card">
        <h3>${existing ? "Изменить папку" : "Новая папка"}</h3>
        <div class="field">
          <label>Название</label>
          <input id="fd-name" maxlength="40" placeholder="Работа" value="${esc(existing ? existing.name : "")}">
        </div>
        <div class="field">
          <label>Чаты в папке</label>
          <div class="member-list" id="fd-chats">
            ${
              chats.length
                ? chats
                    .map(
                      (c) => `
              <button class="person ${picked.has(c.id) ? "selected" : ""}" type="button" data-id="${c.id}">
                <span class="meta"><b>${esc(c.title)}</b></span>
                ${picked.has(c.id) ? icon("check") : ""}
              </button>`
                    )
                    .join("")
                : `<span class="hint">Чатов пока нет</span>`
            }
          </div>
        </div>
        <div class="modal-actions">
          ${existing ? `<button class="btn danger" data-act="del">Удалить</button>` : ""}
          <button class="btn ghost" data-act="cancel">Отмена</button>
          <button class="btn primary" data-act="ok">Сохранить</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);

    const done = (val) => {
      overlay.style.animation = "fadeIn .15s reverse";
      setTimeout(() => overlay.remove(), 120);
      resolve(val);
    };

    $("#fd-chats", overlay)?.addEventListener("click", (ev) => {
      const person = ev.target.closest(".person");
      if (!person) return;
      const id = +person.dataset.id;
      if (picked.has(id)) picked.delete(id);
      else picked.add(id);
      person.classList.toggle("selected", picked.has(id));
      person.innerHTML = `<span class="meta"><b>${esc(
        (state.chats.find((c) => c.id === id) || {}).title || ""
      )}</b></span>${picked.has(id) ? icon("check") : ""}`;
    });

    overlay.querySelector('[data-act="cancel"]').addEventListener("click", () => done(null));
    overlay.querySelector('[data-act="del"]')?.addEventListener("click", async () => {
      try {
        await api(`/api/folders/${existing.id}`, { method: "DELETE" });
        if (state.folderId === existing.id) state.folderId = 0;
        await loadFolders();
        await refreshChats();
        toast("Папка удалена");
      } catch (err) {
        toast(err.message, true);
      }
      done(null);
    });
    overlay.querySelector('[data-act="ok"]').addEventListener("click", async () => {
      const name = $("#fd-name", overlay).value.trim();
      if (!name) return toast("Введите название папки", true);
      const payload = { name, icon: existing ? existing.icon : "folder", chat_ids: [...picked] };
      try {
        if (existing) await api(`/api/folders/${existing.id}`, { method: "PATCH", body: payload });
        else await api("/api/folders", { method: "POST", body: payload });
        await loadFolders();
        await refreshChats();
        toast(existing ? "Папка изменена" : "Папка создана");
      } catch (err) {
        toast(err.message, true);
      }
      done(null);
    });
    overlay.addEventListener("click", (ev) => {
      if (ev.target === overlay) done(null);
    });
    $("#fd-name", overlay).focus();
  });
}

/* -------------------------------- истории --------------------------------- */

async function loadStories() {
  if (!state.me) return;
  try {
    const { stories } = await api("/api/stories");
    state.stories = stories || [];
  } catch {
    return;
  }
  renderStories();
}

function storyAva(g, cls = "") {
  const a = g && g.author;
  if (a && a.avatar) return avatarHTML(a, cls);
  const letter = esc(((a && a.name) || "?").trim().slice(0, 1) || "?");
  return `<div class="avatar ${cls}">${letter}</div>`;
}

function renderStories() {
  const bar = $("#stories-bar");
  if (!bar) return;
  const show = state.mode === "chats" && !state.query;
  bar.classList.toggle("hidden", !show);
  if (!show) return;

  const all = state.stories || [];
  const mine = all.find((g) => g.author && g.author.is_me);
  state._storyGroups = all;

  bar.innerHTML =
    `<button class="story add" type="button" data-add="1">
       <span class="st-ring"><span class="st-ava">${icon("plus")}</span></span>
       <small>Добавить</small>
     </button>` +
    all
      .map(
        (g, i) => `
    <button class="story ${g.viewed || (g.author && g.author.is_me) ? "seen" : ""}" type="button" data-story="${i}">
      <span class="st-ring"><span class="st-ava">${storyAva(g)}</span></span>
      <small>${esc((g.author && g.author.name) || "?")}</small>
    </button>`
      )
      .join("");

  watchStoryMedia(bar);
}

/** Пока картинка истории грузится — вокруг крутится орбита. */
function watchStoryMedia(container) {
  container.querySelectorAll("img").forEach((img) => {
    const host = img.closest(".st-ava") || img.closest(".st-media");
    if (!host || img.complete) return;
    host.classList.add("loading");
    if (host.classList.contains("st-ava")) host.classList.add("orbit");
    const done = () => host.classList.remove("loading");
    img.addEventListener("load", done, { once: true });
    img.addEventListener("error", done, { once: true });
  });
}

$("#stories-bar").addEventListener("click", (e) => {
  if (e.target.closest("[data-add]")) return storyDialog();
  const b = e.target.closest("[data-story]");
  if (!b) return;
  openStoryViewer(+b.dataset.story);
});

const STORY_MS = 5000;
let _storyState = null;
let _storyTimer = null;

function closeStory() {
  clearTimeout(_storyTimer);
  _storyTimer = null;
  _storyState = null;
  $(".story-view")?.remove();
}

function paintStory() {
  const st = _storyState;
  if (!st) return;
  const group = state._storyGroups[st.gi];
  if (!group) return closeStory();
  const item = group.items[st.ii];
  if (!item) {
    // закончились истории этой пары — идём к следующей группе
    if (st.gi + 1 < state._storyGroups.length) {
      st.gi++;
      st.ii = 0;
      return paintStory();
    }
    return closeStory();
  }

  const overlay = $(".story-view");
  if (!overlay) return closeStory();
  const bars = state._storyGroups[st.gi].items
    .map((_, i) => `<i class="${i < st.ii ? "done" : ""}"><b style="width:${i < st.ii ? 100 : 0}%"></b></i>`)
    .join("");
  const author = group.author || {};
  const when = item.created_at ? shortTime(item.created_at) : "";

  overlay.innerHTML = `
    <div class="story-card glass">
      <div class="st-bars">${bars}</div>
      <div class="st-top">
        <span class="st-ava orbit">${storyAva(group)}</span>
        <span><b>${esc(author.name || "?")}</b><small>${esc(when)}</small></span>
        <button class="icon-btn ghost st-close" type="button" data-s="close" title="Закрыть">${icon("x")}</button>
      </div>
      <div class="st-media" data-s="next">
        ${item.image ? `<img src="${esc(item.image)}" alt="">` : `<p>${esc(item.text || "")}</p>`}
      </div>
      <div class="st-foot">
        ${
          item.mine
            ? `<span class="st-views" data-s="views">${icon("eye")}<span>—</span></span>
               <button class="btn danger small" type="button" data-s="del">Удалить</button>`
            : `<span class="st-views">${icon("clock")}${esc(when)}</span>`
        }
        <button class="btn ghost small" type="button" data-s="close">Закрыть</button>
      </div>
    </div>`;

  if (item.mine) loadStoryViews(item.id);
  watchStoryMedia(overlay);

  // отметили просмотренную
  if (!item.viewed) {
    item.viewed = true;
    group.viewed = group.items.every((x) => x.viewed);
    api(`/api/stories/${item.id}/view`, { method: "POST" }).catch(() => {});
    renderStories();
  }

  clearTimeout(_storyTimer);
  _storyTimer = setTimeout(() => {
    const it = _storyState;
    if (!it) return;
    if (it.ii + 1 < state._storyGroups[it.gi].items.length) it.ii++;
    else if (it.gi + 1 < state._storyGroups.length) {
      it.gi++;
      it.ii = 0;
    } else return closeStory();
    paintStory();
  }, STORY_MS);
}

async function loadStoryViews(storyId) {
  try {
    const { viewers } = await api(`/api/stories/${storyId}/views`);
    const box = $(".story-view [data-s='views'] span");
    if (box) box.textContent = `${viewers.length} ${plural(viewers.length, "просмотр", "просмотра", "просмотров")}`;
  } catch {}
}

function openStoryViewer(index) {
  if (!state._storyGroups || !state._storyGroups[index]) return;
  closeStory();
  const overlay = document.createElement("div");
  overlay.className = "story-view";
  document.body.appendChild(overlay);
  _storyState = { gi: index, ii: 0 };
  paintStory();

  overlay.addEventListener("click", async (e) => {
    const st = _storyState;
    if (!st) return;
    const btn = e.target.closest("[data-s]");
    const act = btn && btn.dataset.s;
    if (act === "close") return closeStory();
    if (act === "del") {
      const group = state._storyGroups[st.gi];
      const item = group.items[st.ii];
      if (!await confirmModal("Удалить историю?", "Её больше не увидит никто.")) return;
      try {
        await api(`/api/stories/${item.id}`, { method: "DELETE" });
        closeStory();
        await loadStories();
        toast("История удалена");
      } catch (err) {
        toast(err.message, true);
      }
      return;
    }
    if (act === "views") return;
    // клик по медиа — следующая история, по левому краю — предыдущая
    const r = overlay.getBoundingClientRect();
    if (e.clientX - r.left < r.width * 0.28) {
      if (st.ii > 0) st.ii--;
      else if (st.gi > 0) {
        st.gi--;
        st.ii = state._storyGroups[st.gi].items.length - 1;
      } else return paintStory();
    } else if (st.ii + 1 < state._storyGroups[st.gi].items.length) st.ii++;
    else if (st.gi + 1 < state._storyGroups.length) {
      st.gi++;
      st.ii = 0;
    } else return closeStory();
    paintStory();
  });
}

/** Публикация истории: текст, картинка и кому она показывается. */
function storyDialog() {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.innerHTML = `
      <div class="modal glass">
        <h3>Новая история</h3>
        <p class="hint">История живёт 24 часа. Текст или картинка — что-то одно.</p>
        <div class="field">
          <label>Текст</label>
          <textarea id="st-text" rows="3" maxlength="500" placeholder="Чем занялись сегодня?"></textarea>
        </div>
        <div class="field">
          <label>Изображение</label>
          <input type="file" id="st-file" accept="image/png,image/jpeg,image/gif,image/webp">
        </div>
        <div class="field">
          <label>Кому видно</label>
          <div class="seg" id="st-priv">
            <button type="button" class="on" data-v="all">Все</button>
            <button type="button" data-v="contacts">Контакты</button>
            <button type="button" data-v="close">Близкие друзья</button>
          </div>
        </div>
        <div class="modal-actions">
          <button class="btn ghost" data-act="cancel">Отмена</button>
          <button class="btn primary" data-act="ok">Опубликовать</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    let privacy = "all";
    $("#st-priv", overlay).addEventListener("click", (e) => {
      const b = e.target.closest("[data-v]");
      if (!b) return;
      privacy = b.dataset.v;
      $$("#st-priv button", overlay).forEach((x) => x.classList.toggle("on", x === b));
    });
    const done = (val) => {
      overlay.style.animation = "fadeIn .15s reverse";
      setTimeout(() => overlay.remove(), 120);
      resolve(val);
    };
    overlay.querySelector('[data-act="cancel"]').addEventListener("click", () => done(null));
    overlay.querySelector('[data-act="ok"]').addEventListener("click", async () => {
      const text = $("#st-text", overlay).value.trim();
      const file = $("#st-file", overlay).files[0];
      if (!text && !file) return toast("Пустая история", true);
      const fd = new FormData();
      fd.append("text", text);
      fd.append("privacy", privacy);
      if (file) fd.append("file", file);
      const addTile = $("#stories-bar .story.add");
      addTile?.classList.add("loading");
      try {
        await api("/api/stories", { method: "POST", form: fd });
        done(true);
        await loadStories();
        toast("История опубликована");
      } catch (err) {
        toast(err.message, true);
      } finally {
        addTile?.classList.remove("loading");
      }
    });
    overlay.addEventListener("click", (ev) => {
      if (ev.target === overlay) done(null);
    });
    $("#st-text", overlay).focus();
  });
}

/* --------------------------- звук и уведомления --------------------------- */

let _audioCtx = null;

/** Короткий сигнал на входящее (по настройке «Звуки уведомлений»). */
function beep() {
  if (state.settings && state.settings.sound === false) return;
  try {
    _audioCtx = _audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const t = _audioCtx.currentTime;
    const osc = _audioCtx.createOscillator();
    const gain = _audioCtx.createGain();
    osc.type = "sine";
    osc.frequency.setValueAtTime(920, t);
    osc.frequency.setValueAtTime(1240, t + 0.09);
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(0.14, t + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.32);
    osc.connect(gain).connect(_audioCtx.destination);
    osc.start(t);
    osc.stop(t + 0.34);
  } catch {}
}

function notifyMe(title, body) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  try {
    new Notification("Kofi", { body: `${title}: ${body}`.slice(0, 140) });
  } catch {}
}

/* ------------------------------ горячие клавиши --------------------------- */

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    const cv = $("#call-view");
    if (call.active && cv && !cv.classList.contains("hidden") && !call.minimized) {
      return setMini(true);
    }
    const ctx = $("#ctx-menu");
    if (ctx && !ctx.classList.contains("hidden")) return closeCtxMenu();
    if ($(".story-view")) return closeStory();
    if ($("#modal-input")) return; // модалка закроется своим обработчиком
    const ms = $("#msg-search");
    if (ms && !ms.classList.contains("hidden")) return closeMsgSearch();
    if (state.replyTo || state.editing) return cancelComposer();
    if (document.body.classList.contains("drawer-open")) {
      document.body.classList.remove("drawer-open");
      return;
    }
    if (!$("#profile-view").classList.contains("hidden")) return closeProfile();
    return;
  }
  const key = (e.key || "").toLowerCase();
  if ((e.ctrlKey || e.metaKey) && key === "f" && state.activeId) {
    e.preventDefault();
    openMsgSearch();
  }
  if ((e.ctrlKey || e.metaKey) && key === "k") {
    e.preventDefault();
    $("#new-btn")?.click();
  }
});

/* ================================ звонки ================================
   Звук и видео идут напрямую между браузерами (WebRTC). Серверу отводится
   роль почтальона: он держит WebSocket /api/ws, раздаёт приглашения и
   перекидывает SDP/ICE между участниками звонка. */

const call = {
  ws: null,
  wsRetry: 0,
  wsTimer: 0,
  active: null,     // состояние звонка, присланное сервером
  phase: "idle",    // idle | outgoing | incoming | active
  kind: "audio",
  local: null,      // локальный MediaStream
  hasMic: false,
  muted: false,
  camOff: false,
  minimized: false,
  peers: new Map(),   // userId -> RTCPeerConnection
  remotes: new Map(), // userId -> MediaStream
  iceWait: new Map(), // userId -> ICE, пришедшие до remoteDescription
  ringTimer: null,
  tickTimer: null,
  started: 0,
};

const RTC_CFG = {
  iceServers: [
    { urls: ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"] },
  ],
};

const myId = () => (state.me ? state.me.id : 0);
const fmtDur = (sec) => `${Math.floor(Math.max(0, sec) / 60)}:${pad(Math.floor(Math.max(0, sec)) % 60)}`;

function wsSend(payload) {
  const ws = call.ws;
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(payload));
    return true;
  }
  return false;
}

function connectCallWS() {
  if (!state.me) return;
  const old = call.ws;
  if (old && (old.readyState === WebSocket.OPEN || old.readyState === WebSocket.CONNECTING)) return;
  let sock;
  try {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    sock = new WebSocket(`${proto}//${location.host}/api/ws`);
  } catch {
    return;
  }
  call.ws = sock;
  sock.addEventListener("open", () => {
    call.wsRetry = 0;
    restoreActiveCall();
  });
  sock.addEventListener("message", (ev) => {
    let data = null;
    try {
      data = JSON.parse(ev.data);
    } catch {
      return;
    }
    handleCallEvent(data);
  });
  sock.addEventListener("close", () => {
    if (call.ws === sock) call.ws = null;
    if (!state.me) return;
    clearTimeout(call.wsTimer);
    call.wsRetry += 1;
    call.wsTimer = setTimeout(connectCallWS, Math.min(15000, 800 * call.wsRetry));
  });
  sock.addEventListener("error", () => {});
}

function closeCallWS() {
  clearTimeout(call.wsTimer);
  call.wsTimer = 0;
  call.wsRetry = 0;
  const ws = call.ws;
  call.ws = null;
  if (ws) {
    try {
      ws.close();
    } catch {}
  }
}

async function handleCallEvent(data) {
  switch (data.type) {
    case "call.state":
      return applyCallState(data.call);
    case "call.incoming":
      return incomingCall(data.call);
    case "call.invited":
      return incomingCall(data.call, true);
    case "call.ended":
      return callEnded(data);
    case "rtc":
      return rtcMessage(data);
    case "error":
      return toast(data.detail || "Не удалось выполнить команду", true);
    default:
      return;
  }
}

/* --------------------------------------------------- состояния звонка */

function applyCallState(p) {
  if (!p) return;
  const mine = (p.members || []).find((m) => m.is_me);
  if (!mine) return;
  call.active = p;
  call.kind = p.kind || call.kind;

  if (mine.state === "left") {
    teardownCall();
    call.active = null;
    call.phase = "idle";
    call.started = 0;
    renderCall();
    return;
  }
  if (mine.state === "joined") {
    if (call.phase !== "active") {
      stopRing();
      call.phase = "active";
      if (!call.started) call.started = Date.now();
    }
    syncPeers();
  }
  renderCall();
}

function incomingCall(p) {
  if (!p) return;
  if (call.active && call.active.id !== p.id) {
    wsSend({ type: "call.decline", call_id: p.id });
    return;
  }
  if (call.active && call.active.id === p.id) return;
  call.active = p;
  call.kind = p.kind || "audio";
  call.phase = "incoming";
  call.muted = false;
  call.camOff = false;
  call.minimized = false;
  startRing("in");
  beep();
  renderCall();
  const who = p.initiator_name || "Вам звонят";
  notifyMe("Входящий звонок", `${who} · ${p.kind === "video" ? "видеозвонок" : "голосовой звонок"}`);
}

async function callEnded(data) {
  const wasActive = !!call.active && call.active.id === data.call_id;
  const inChat = wasActive && call.active && state.activeId === call.active.chat_id;
  if (wasActive) {
    teardownCall();
    call.active = null;
    call.phase = "idle";
    call.started = 0;
    call.minimized = false;
    renderCall();
    const sec = Number(data.duration) || 0;
    toast(
      data.answered
        ? `Звонок завершён · ${fmtDur(sec)}`
        : data.reason === "no-answer"
          ? "Не ответили"
          : "Звонок завершён"
    );
    if (inChat) {
      try {
        await reloadMessages();
      } catch {}
    }
    refreshChats();
  }
}

/* ------------------------------------------------------- исходящий звонок */

async function startCall(kind) {
  const chat = state.chat;
  if (!chat) return;
  if (chat.type === "channel" || chat.type === "saved") {
    return toast("В этом чате звонки недоступны", true);
  }
  if (call.active) return toast("У вас уже идёт звонок", true);

  call.kind = kind;
  call.muted = false;
  call.camOff = false;
  call.minimized = false;
  call.started = 0;

  await openMedia(kind === "video");

  const others = (chat.members || [])
    .filter((m) => m.id !== myId())
    .slice(0, 4)
    .map((m) => ({
      id: m.id,
      name: m.name,
      username: m.username,
      avatar: m.avatar,
      state: "ringing",
      muted: false,
      video: false,
      is_me: false,
    }));
  call.active = {
    id: 0,
    chat_id: chat.id,
    chat_title: chat.title || (chat.peer && chat.peer.name) || "Звонок",
    kind,
    state: "ringing",
    initiator: myId(),
    initiator_name: state.me.name,
    members: [
      {
        id: myId(),
        name: state.me.name,
        username: state.me.username,
        avatar: state.me.avatar,
        state: "joined",
        muted: false,
        video: kind === "video",
        is_me: true,
      },
      ...others,
    ],
  };
  call.phase = "outgoing";
  renderCall();
  startRing("back");

  if (!wsSend({ type: "call.start", chat_id: chat.id, kind })) {
    stopRing();
    teardownCall();
    call.active = null;
    call.phase = "idle";
    renderCall();
    toast("Нет соединения с сервером", true);
  }
}

async function acceptCall(withVideo) {
  const p = call.active;
  if (!p) return;
  stopRing();
  call.minimized = false;
  if (withVideo || p.kind === "video") {
    await openMedia(true);
    call.camOff = !(call.local && call.local.getVideoTracks().length);
  } else {
    call.camOff = !(call.local && call.local.getVideoTracks().length) ? true : call.camOff;
  }
  wsSend({ type: "call.join", call_id: p.id });
  call.phase = "active";
  call.started = Date.now();
  renderCall();
  syncPeers();
}

function declineCall() {
  const p = call.active;
  if (p) wsSend({ type: "call.decline", call_id: p.id });
  teardownCall();
  call.active = null;
  call.phase = "idle";
  renderCall();
}

function hangupCall() {
  const p = call.active;
  if (p) wsSend({ type: p.id ? "call.leave" : "call.decline", call_id: p.id });
  teardownCall();
  call.active = null;
  call.phase = "idle";
  call.started = 0;
  call.minimized = false;
  renderCall();
}

function setMini(v) {
  call.minimized = !!v;
  const root = $("#call-view");
  if (root) root.classList.toggle("mini", call.minimized);
}

/* -------------------------------------------------------------- медиа */

function withTimeout(promise, ms) {
  return Promise.race([
    promise.then((v) => v).catch(() => null),
    new Promise((r) => setTimeout(() => r(null), ms)),
  ]);
}

async function openMedia(wantVideo) {
  if (call.local) {
    if (wantVideo && !call.local.getVideoTracks().length) await addVideoTrack();
    return call.local;
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    toast("Браузер не поддерживает запись звука", true);
    return null;
  }
  let pending;
  try {
    pending = navigator.mediaDevices.getUserMedia({
      audio: true,
      video: wantVideo ? { width: { ideal: 640 }, height: { ideal: 480 } } : false,
    });
  } catch {
    return null;
  }
  const stream = await withTimeout(pending, 8000);
  if (stream) return adoptStream(stream);
  // разрешение не дали быстро — работаем без звука, а если его дадут позже, подключим
  pending
    .then((s) => {
      if (call.local || !call.active) {
        s.getTracks().forEach((t) => t.stop());
        return;
      }
      adoptStream(s);
      renderCall();
    })
    .catch(() => {});
  return null;
}

function adoptStream(stream) {
  call.local = stream;
  call.hasMic = stream.getAudioTracks().length > 0;
  stream.getAudioTracks().forEach((t) => (t.enabled = !call.muted));
  stream.getVideoTracks().forEach((t) => (t.enabled = !call.camOff));
  addTracksToPeers();
  renderCall();
  return stream;
}

async function addVideoTrack() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return null;
  try {
    const s = await navigator.mediaDevices.getUserMedia({
      video: { width: { ideal: 640 }, height: { ideal: 480 } },
    });
    const t = s.getVideoTracks()[0];
    if (!t) return null;
    call.local.addTrack(t);
    call.camOff = false;
    addTracksToPeers();
    renderCall();
    return t;
  } catch {
    toast("Камера недоступна", true);
    return null;
  }
}

function addTracksToPeers() {
  if (!call.local) return;
  const tracks = call.local.getTracks();
  call.peers.forEach((pc) => {
    tracks.forEach((t) => {
      if (pc.getSenders().some((s) => s.track === t)) return;
      try {
        pc.addTrack(t, call.local);
      } catch {}
    });
  });
}

function toggleMute() {
  call.muted = !call.muted;
  if (call.local) call.local.getAudioTracks().forEach((t) => (t.enabled = !call.muted));
  if (call.active) {
    wsSend({ type: "call.media", call_id: call.active.id, muted: call.muted, video: !call.camOff });
  }
  renderCall();
}

async function toggleCam() {
  const hasCam = !!(call.local && call.local.getVideoTracks().length);
  if (hasCam && !call.camOff) {
    call.camOff = true;
    call.local.getVideoTracks().forEach((t) => (t.enabled = false));
    wsSend({ type: "call.media", call_id: call.active.id, muted: call.muted, video: false });
    renderCall();
    return;
  }
  if (hasCam) {
    call.camOff = false;
    call.local.getVideoTracks().forEach((t) => (t.enabled = true));
    wsSend({ type: "call.media", call_id: call.active.id, muted: call.muted, video: true });
    renderCall();
    return;
  }
  if (!call.local) await openMedia(true);
  else await addVideoTrack();
  if (call.local && call.local.getVideoTracks().length) {
    wsSend({ type: "call.media", call_id: call.active.id, muted: call.muted, video: true });
  }
  renderCall();
}

/* -------------------------------------------------------- WebRTC-пиры */

function ensurePeer(uid) {
  let pc = call.peers.get(uid);
  if (pc) return pc;
  pc = new RTCPeerConnection(RTC_CFG);
  call.peers.set(uid, pc);

  pc.onicecandidate = (e) => {
    if (e.candidate && call.active) {
      wsSend({
        type: "rtc",
        to: uid,
        call_id: call.active.id,
        kind: "ice",
        payload: e.candidate.toJSON(),
      });
    }
  };
  pc.onnegotiationneeded = async () => {
    try {
      if (pc.signalingState !== "stable") return;
      const offer = await pc.createOffer();
      if (pc.signalingState !== "stable") return;
      await pc.setLocalDescription(offer);
      if (call.active) {
        wsSend({
          type: "rtc",
          to: uid,
          call_id: call.active.id,
          kind: "offer",
          payload: { type: pc.localDescription.type, sdp: pc.localDescription.sdp },
        });
      }
    } catch {}
  };
  pc.ontrack = (e) => {
    let stream = call.remotes.get(uid);
    if (!stream) {
      stream = new MediaStream();
      call.remotes.set(uid, stream);
    }
    if (!stream.getTracks().includes(e.track)) stream.addTrack(e.track);
    attachAllMedia();
  };
  pc.onconnectionstatechange = () => {
    if (pc.connectionState === "failed") restartPeer(uid);
    const tile = document.querySelector(`#call-view .call-tile[data-peer="${uid}"]`);
    if (tile) tile.classList.toggle("waiting", pc.connectionState !== "connected");
  };
  pc.oniceconnectionstatechange = () => {
    if (pc.iceConnectionState === "failed") restartPeer(uid);
  };
  // дата-канал держит соединение живым даже без медиа
  if (myId() < uid) {
    try {
      pc.createDataChannel("kofi");
    } catch {}
  }
  pc.ondatachannel = () => {};
  if (call.local) {
    call.local.getTracks().forEach((t) => {
      try {
        pc.addTrack(t, call.local);
      } catch {}
    });
  }
  return pc;
}

function restartPeer(uid) {
  const pc = call.peers.get(uid);
  if (pc) {
    call.peers.delete(uid);
    try {
      pc.close();
    } catch {}
  }
  call.remotes.delete(uid);
  call.iceWait.delete(uid);
  if (call.active && call.phase === "active") ensurePeer(uid);
}

function syncPeers() {
  const p = call.active;
  if (!p || call.phase !== "active") return;
  const wanted = (p.members || [])
    .filter((m) => !m.is_me && m.state !== "left")
    .map((m) => m.id);
  [...call.peers.keys()].forEach((uid) => {
    if (wanted.includes(uid)) return;
    const pc = call.peers.get(uid);
    call.peers.delete(uid);
    call.remotes.delete(uid);
    call.iceWait.delete(uid);
    try {
      pc.close();
    } catch {}
  });
  wanted.forEach((uid) => ensurePeer(uid));
}

async function rtcMessage(data) {
  const uid = Number(data.from);
  if (!uid || !call.active || Number(data.call_id) !== call.active.id) return;
  if (data.kind === "offer") return onOffer(uid, data.payload);
  if (data.kind === "answer") return onAnswer(uid, data.payload);
  if (data.kind === "ice") return onIce(uid, data.payload);
}

async function onOffer(uid, payload) {
  if (!payload) return;
  const pc = ensurePeer(uid);
  const polite = myId() > uid; // вежливая сторона уступает при одновременных офферах
  try {
    if (pc.signalingState !== "stable") {
      if (!polite) return;
      try {
        await pc.setLocalDescription({ type: "rollback" });
      } catch {}
    }
    await pc.setRemoteDescription(payload);
    await flushIce(uid);
    const answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);
    wsSend({
      type: "rtc",
      to: uid,
      call_id: call.active.id,
      kind: "answer",
      payload: { type: pc.localDescription.type, sdp: pc.localDescription.sdp },
    });
  } catch {}
}

async function onAnswer(uid, payload) {
  const pc = call.peers.get(uid);
  if (!pc || !payload) return;
  try {
    if (pc.signalingState !== "have-local-offer") return;
    await pc.setRemoteDescription(payload);
    await flushIce(uid);
  } catch {}
}

async function onIce(uid, payload) {
  const pc = call.peers.get(uid);
  if (!pc || !payload) return;
  if (!pc.remoteDescription || !pc.remoteDescription.type) {
    const q = call.iceWait.get(uid) || [];
    q.push(payload);
    call.iceWait.set(uid, q);
    return;
  }
  try {
    await pc.addIceCandidate(payload);
  } catch {}
}

function flushIce(uid) {
  const q = call.iceWait.get(uid);
  if (!q || !q.length) return;
  call.iceWait.delete(uid);
  const pc = call.peers.get(uid);
  if (!pc) return;
  q.forEach((c) => {
    pc.addIceCandidate(c).catch(() => {});
  });
}

/* ------------------------------------------------------- звук звонка */

function ringTone(kind) {
  try {
    _audioCtx = _audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (_audioCtx.state === "suspended") _audioCtx.resume().catch(() => {});
    const t0 = _audioCtx.currentTime;
    const seq =
      kind === "back"
        ? [
            [425, 0, 0.42],
            [425, 1.0, 0.42],
          ]
        : [
            [440, 0, 0.45],
            [480, 0.02, 0.45],
            [440, 1.1, 0.45],
            [480, 1.12, 0.45],
          ];
    seq.forEach(([freq, off, dur]) => {
      const osc = _audioCtx.createOscillator();
      const gain = _audioCtx.createGain();
      const t = t0 + off;
      osc.type = "sine";
      osc.frequency.setValueAtTime(freq, t);
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(0.09, t + 0.03);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + dur);
      osc.connect(gain).connect(_audioCtx.destination);
      osc.start(t);
      osc.stop(t + dur + 0.02);
    });
  } catch {}
}

function startRing(kind) {
  stopRing();
  ringTone(kind);
  call.ringTimer = setInterval(() => ringTone(kind), kind === "back" ? 3200 : 3000);
}

function stopRing() {
  if (call.ringTimer) clearInterval(call.ringTimer);
  call.ringTimer = null;
}

function teardownCall() {
  stopRing();
  if (call.tickTimer) clearInterval(call.tickTimer);
  call.tickTimer = null;
  call.peers.forEach((pc) => {
    try {
      pc.close();
    } catch {}
  });
  call.peers.clear();
  call.remotes.clear();
  call.iceWait.clear();
  if (call.local) call.local.getTracks().forEach((t) => t.stop());
  call.local = null;
  call.hasMic = false;
  call.muted = false;
  call.camOff = false;
}

/** Выход из аккаунта или протухшая сессия — закрываем звонок и сокет. */
function stopCallSession() {
  teardownCall();
  call.active = null;
  call.phase = "idle";
  call.started = 0;
  call.minimized = false;
  renderCall();
  closeCallWS();
}

async function restoreActiveCall() {
  if (!state.me || call.active) return;
  let payload;
  try {
    payload = await api("/api/calls/active");
  } catch {
    return;
  }
  const p = payload && payload.calls && payload.calls[0];
  if (!p || call.active) return;
  const mine = (p.members || []).find((m) => m.is_me);
  if (!mine || mine.state === "left") return;
  call.active = p;
  call.kind = p.kind || "audio";
  if (mine.state === "joined") {
    call.phase = "active";
    call.started = Date.now();
    await openMedia(p.kind === "video");
    renderCall();
    syncPeers();
  } else {
    call.phase = "incoming";
    startRing("in");
    renderCall();
  }
}

/* ---------------------------------------------------------- картинка */

function callTileHTML(m) {
  const isMe = !!m.is_me;
  const camOn = isMe
    ? !!(call.local && call.local.getVideoTracks().some((t) => t.enabled)) && !call.camOff
    : !!m.video;
  const muted = isMe ? call.muted : !!m.muted;
  const joined = m.state === "joined";
  const body = camOn
    ? `<video ${isMe ? "muted " : ""}playsinline autoplay data-peer="${m.id}"></video>`
    : avatarHTML({ name: m.name, avatar: m.avatar });
  return `<div class="call-tile${joined ? "" : " waiting"}" data-peer="${m.id}">
      ${body}
      ${muted ? `<span class="ct-ic" title="Микрофон выключен">${icon("mute")}</span>` : ""}
      <span class="ct-name">${esc(m.name)}${isMe ? " · вы" : ""}</span>
      ${joined ? "" : `<span class="ct-state">Звонит…</span>`}
    </div>`;
}

function callStripHTML(title, timer) {
  const p = call.active;
  const me = (p.members || []).find((m) => m.is_me) || {};
  const other = (p.members || []).find((m) => !m.is_me) || me;
  return `<div class="call-strip glass">
      ${avatarHTML({ name: other.name, avatar: other.avatar })}
      <span class="cs-names"><b>${esc(title)}</b><small class="call-timer">${timer}</small></span>
      <button class="cc-btn danger" data-c="hang" title="Завершить звонок">${icon("call-end")}</button>
      <button class="icon-btn ghost" data-c="expand" title="Развернуть">${icon("chev-down")}</button>
    </div>`;
}

/** Лицо звонка: аватар группы, если чат открыт, иначе собеседник. */
function callFace(p, members, others, title) {
  const chat = state.chat;
  if (
    chat &&
    chat.id === p.chat_id &&
    (chat.type === "group" || chat.type === "channel") &&
    chat.avatar
  ) {
    return { name: chat.title || title, avatar: chat.avatar };
  }
  const o = others[0] || members[0] || {};
  return { name: o.name || title, avatar: o.avatar || "" };
}

function renderCall() {
  const root = $("#call-view");
  if (!root) return;
  if (!call.active || call.phase === "idle") {
    root.classList.add("hidden");
    root.classList.remove("mini");
    root.innerHTML = "";
    return;
  }

  const p = call.active;
  const members = (p.members || []).slice(0, 5);
  const mine = members.find((m) => m.is_me) || { id: myId(), name: state.me.name, state: "joined" };
  const others = members.filter((m) => !m.is_me);
  const caller = members.find((m) => m.id === p.initiator) || others[0] || mine;
  const title = p.chat_title || "Звонок";
  const timer = call.phase === "active" && call.started ? fmtDur((Date.now() - call.started) / 1000) : "0:00";
  const noMic = call.phase === "active" && !call.hasMic;
  const tiles = members.map(callTileHTML).join("");
  const count = `${members.length} ${plural(members.length, "участник", "участника", "участников")}`;
  const strip = callStripHTML(title, timer);

  let html = "";
  if (call.phase === "incoming") {
    html = `<div class="call-card glass">
      <div class="call-lead">
        <span class="call-pulse">${avatarHTML({ name: caller.name, avatar: caller.avatar })}</span>
        <b>${esc(caller.name)}</b>
        <span class="kind">${p.kind === "video" ? "Видеозвонок" : "Голосовой звонок"}</span>
        <span class="kind">Входящий звонок</span>
      </div>
      <div class="call-ctrl">
        <button class="cc-btn danger" data-c="decline" title="Отклонить">${icon("call-end")}</button>
        <button class="cc-btn ok" data-c="accept" title="Ответить">${icon("call")}</button>
        ${
          p.kind === "video"
            ? `<button class="cc-btn ok" data-c="accept-video" title="Ответить с видео">${icon("video")}</button>`
            : ""
        }
      </div>
    </div>${strip}`;
  } else if (call.phase === "outgoing") {
    html = `<div class="call-card glass">
      <div class="call-top">
        <div class="call-id">
          ${avatarHTML(callFace(p, members, others, title))}
          <span class="call-names">
            <b>${esc(title)}</b>
            <small>Идёт вызов <span class="dots"><i></i><i></i><i></i></span></small>
          </span>
        </div>
        <button class="icon-btn ghost" data-c="min" title="Свернуть">${icon("chev-down")}</button>
      </div>
      <div class="call-tiles${members.length === 1 ? " solo" : ""}">${tiles}</div>
      <div class="call-ctrl">
        <button class="cc-btn${call.muted ? " on" : ""}" data-c="mic" title="${call.muted ? "Включить микрофон" : "Выключить микрофон"}">${icon(call.muted ? "mute" : "mic")}</button>
        <button class="cc-btn${call.camOff ? "" : " on"}" data-c="cam" title="${call.camOff ? "Включить камеру" : "Выключить камеру"}">${icon(call.camOff ? "video-off" : "video")}</button>
        <button class="cc-btn danger" data-c="hang" title="Отменить">${icon("call-end")}</button>
      </div>
    </div>${strip}`;
  } else {
    html = `<div class="call-card glass">
      <div class="call-top">
        <div class="call-id">
          ${avatarHTML(callFace(p, members, others, title))}
          <span class="call-names">
            <b>${esc(title)}</b>
            <small><span class="call-timer">${timer}</span> · ${count}</small>
          </span>
        </div>
        <button class="icon-btn ghost" data-c="min" title="Свернуть">${icon("chev-down")}</button>
      </div>
      ${noMic ? `<div class="call-notice">${icon("shield")}Микрофон недоступен: разрешите доступ к микрофону в браузере — ваш звук пока не передаётся.</div>` : ""}
      <div class="call-tiles${members.length === 1 ? " solo" : ""}">${tiles}</div>
      <div class="call-ctrl">
        <button class="cc-btn${call.muted ? " on" : ""}" data-c="mic" title="${call.muted ? "Включить микрофон" : "Выключить микрофон"}">${icon(call.muted ? "mute" : "mic")}</button>
        <button class="cc-btn${call.camOff || !call.local ? "" : " on"}" data-c="cam" title="${call.camOff ? "Включить камеру" : "Выключить камеру"}">${icon(call.camOff || !call.local ? "video-off" : "video")}</button>
        ${
          state.chat && state.chat.id === p.chat_id && members.length < 5
            ? `<button class="cc-btn" data-c="add" title="Добавить участника">${icon("user-plus")}</button>`
            : ""
        }
        <button class="cc-btn danger" data-c="hang" title="Завершить">${icon("call-end")}</button>
      </div>
    </div>${strip}`;
  }

  root.innerHTML = html;
  root.classList.remove("hidden");
  root.classList.toggle("mini", call.minimized);
  attachAllMedia();
  if (call.phase === "active" && !call.tickTimer) {
    call.tickTimer = setInterval(() => {
      if (!call.active || call.phase !== "active") return;
      const sec = call.started ? (Date.now() - call.started) / 1000 : 0;
      document.querySelectorAll(".call-timer").forEach((n) => (n.textContent = fmtDur(sec)));
    }, 500);
  }
}

function attachAllMedia() {
  const root = $("#call-view");
  if (!root) return;
  root.querySelectorAll("video[data-peer]").forEach((v) => {
    const uid = Number(v.dataset.peer);
    if (uid === myId()) {
      if (call.local) v.srcObject = call.local;
      v.muted = true;
      return;
    }
    const st = call.remotes.get(uid);
    if (st && v.srcObject !== st) v.srcObject = st;
  });
}

async function inviteDialog() {
  const p = call.active;
  const chat = state.chat;
  if (!p || !chat) return;
  const inside = new Set((p.members || []).map((m) => m.id));
  const list = (chat.members || []).filter((m) => !inside.has(m.id) && m.id !== myId());
  if (!list.length) return toast("Все участники уже в звонке", true);
  const pick = await choiceModal({
    title: "Добавить в звонок",
    hint: "Приглашение увидит только выбранный человек.",
    options: list.map((m) => ({ label: m.name, desc: m.username ? `@${m.username}` : "", value: m.id })),
  });
  if (!pick) return;
  wsSend({ type: "call.invite", call_id: p.id, user_id: pick });
  toast("Приглашение отправлено");
}

$("#call-view").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-c]");
  if (!btn) return;
  const c = btn.dataset.c;
  if (c === "accept") return acceptCall(false);
  if (c === "accept-video") return acceptCall(true);
  if (c === "decline") return declineCall();
  if (c === "hang") return hangupCall();
  if (c === "mic") return toggleMute();
  if (c === "cam") return toggleCam();
  if (c === "min") return setMini(true);
  if (c === "expand") return setMini(false);
  if (c === "add") return inviteDialog();
});

window.addEventListener("hashchange", route);

let booted = false;
async function bootOnce() {
  if (booted) return;
  booted = true;
  renderSidebar();
  await boot();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", bootOnce);
} else {
  bootOnce();
}
