"use strict";
const $ = (s) => document.querySelector(s);
const NAME = { claude: "Claude", codex: "Codex" };
const WINDOW = { five_hour: "5 ч", seven_day: "неделя" };
let me = null, models = null, current = null, stream = null, count = 0, running = false;
let attachments = [];

async function api(path, opts = {}) {
  if (opts.json !== undefined) {
    opts.body = JSON.stringify(opts.json);
    opts.headers = { "Content-Type": "application/json" };
  }
  const r = await fetch(path, opts);
  if (r.status === 401 && path !== "/api/login") { showLogin(); throw new Error("Нужно войти"); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : r.statusText);
  return data;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

// Официальный логотип сети; рядом всегда стоит её имя, поэтому alt пустой.
function logo(engine) {
  const img = el("img", "logo");
  img.src = `icons/${engine}.svg`;
  img.alt = "";
  img.width = img.height = 16;  // размер и без CSS: на случай старого style.css в кэше
  return img;
}

function engineLabel(engine, text = NAME[engine]) {
  const span = el("span", "engine-label");
  span.append(logo(engine), text);
  return span;
}

function fileLink(path) {
  const a = el("a", "chip file", path.split("/").pop());
  a.href = `/api/chats/${current.id}/file?path=${encodeURIComponent(path)}`;
  a.download = "";
  return a;
}

// --- Вход ---

function showLogin() {
  $("#main").hidden = true;
  $("#login").hidden = false;
}

$("#login").onsubmit = async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    me = await api("/api/login", { method: "POST", json: { name: f.get("name"), password: f.get("password") } });
    e.target.reset();
    start();
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
};

$("#logout").onclick = async () => { await api("/api/logout", { method: "POST" }); location.reload(); };

$("#passwd").onclick = async () => {
  const password = prompt("Новый пароль");
  if (!password) return;
  try {
    await api(`/api/users/${me.id}/password`, { method: "POST", json: { password } });
    alert("Пароль изменён, войдите заново");
    location.reload();
  } catch (err) { alert(err.message); }
};

async function start() {
  $("#login").hidden = true;
  $("#main").hidden = false;
  me = await api("/api/me");  // после входа в me только имя: сети и лимиты берём здесь
  $("#me").textContent = me.name;
  $("#settings").classList.toggle("admin", me.is_admin);
  models = await api("/api/models");
  showMe();
  await loadChats();
  const id = location.hash.slice(1);
  if (id) openChat(id);
  else showEmpty();
}

// Пустой экран сразу предлагает начать беседу с одной из доступных сетей.
function showEmpty() {
  $("#title").textContent = "";
  const box = el("div", "empty");
  box.append(el("h2", "", "С кем поговорим?"));
  const list = el("div", "empty-engines");
  for (const engine of me.engines) {
    const b = el("button");
    b.append(engineLabel(engine));
    b.onclick = () => newChat(engine);
    list.append(b);
  }
  box.append(me.engines.length ? list : el("p", "muted", "Нет доступных сетей. Попросите администратора включить их."));
  $("#feed").replaceChildren(box);
}

function size(n) {
  const units = ["Б", "КБ", "МБ", "ГБ"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + " " + units[i];
}

function when(ts) {
  return ts ? new Date(ts * 1000).toLocaleString("ru", { dateStyle: "short", timeStyle: "short" }) : "—";
}

// Полоса расхода: заполнение — потрачено, риска — лимит пользователя.
function meter(name, used, limit, text) {
  const row = el("div", "meter" + (limit && used >= limit ? " over" : ""));
  const bar = el("div", "bar");
  const fill = el("i");
  fill.style.width = Math.min(used, 100) + "%";
  bar.append(fill);
  if (limit && limit < 100) {
    const tick = el("b");
    tick.style.left = limit + "%";
    bar.append(tick);
  }
  row.append(el("span", "", name), el("span", "value", text), bar);
  return row;
}

// Сводка внизу панели и плашка над полем ввода: диск и расход подписки.
function showMe() {
  const blocks = [], warnings = [];
  for (const [engine, windows] of Object.entries(me.limits)) {
    const block = el("div", "budget");
    block.dataset.engine = engine;
    block.append(engineLabel(engine));
    for (const w of Object.keys(WINDOW)) {
      const x = windows[w];
      if (x.limit < 100 && x.used >= x.limit) warnings.push(`Лимит ${NAME[engine]} (${WINDOW[w]}) исчерпан, сброс ${when(x.resets_at)}.`);
      block.append(meter(WINDOW[w], x.used, x.limit, `${x.used}%` + (x.limit < 100 ? ` из ${x.limit}%` : "")));
    }
    blocks.push(block);
  }
  const disk = me.disk;
  if (disk.quota && disk.used >= disk.quota) warnings.push("Память закончилась: новые файлы загрузить не получится.");
  const diskBlock = el("div", "budget");
  diskBlock.append(disk.quota
    ? meter("Диск", disk.used / disk.quota * 100, 100, `${size(disk.used)} из ${size(disk.quota)}`)
    : meter("Диск", 0, 0, size(disk.used)));
  blocks.push(diskBlock);
  $("#quota").replaceChildren(...blocks);
  $("#banner").hidden = !warnings.length;
  $("#banner").textContent = warnings.join(" ");
}

async function refreshMe() {
  me = await api("/api/me");
  showMe();
}

// --- Беседы ---

async function loadChats() {
  const list = await api("/api/chats");
  const nav = $("#chats");
  nav.replaceChildren(...list.map((c) => {
    const a = el("a", (c.id === current?.id ? "active" : "") + (c.running ? " running" : ""));
    a.append(logo(c.engine), el("span", "", c.title || "Новая беседа"));
    a.title = c.running ? "Отвечает" : "";
    a.dataset.engine = c.engine;
    a.href = "#" + c.id;
    a.onclick = (e) => { e.preventDefault(); openChat(c.id); };
    return a;
  }));
}

// «+» у заголовка «Чаты»: выпадающий список доступных пользователю сетей.
$("#add").onclick = (e) => {
  e.stopPropagation();
  const menu = $("#add-menu");
  menu.replaceChildren(...(me.engines.length ? me.engines.map((engine) => {
    const b = el("button");
    b.append(engineLabel(engine));
    b.setAttribute("role", "menuitem");
    b.onclick = () => { menu.hidden = true; newChat(engine); };
    return b;
  }) : [el("div", "muted", "Нет доступных сетей")]));
  menu.hidden = !menu.hidden;
};

async function newChat(engine) {
  try {
    const chat = await api("/api/chats", { method: "POST", json: { engine } });
    await loadChats();
    openChat(chat.id);
  } catch (err) { alert(err.message); }
}
document.addEventListener("click", () => { $("#add-menu").hidden = true; });

async function openChat(id) {
  stream?.close();
  stream = null;
  let data;
  try { data = await api(`/api/chats/${id}`); } catch (err) { alert(err.message); return; }
  current = data.chat;
  history.replaceState(null, "", "#" + id);
  side(false);
  $("#feed").replaceChildren();
  count = 0;
  data.history.forEach(render);
  count = data.history.length;
  showHeader();
  $("#composer").hidden = false;
  setRunning(data.running);
  if (data.running) follow();
  loadChats();
  scrollDown(true);
}

function showHeader() {
  $("#title").textContent = current.title || "Новая беседа";
  $("#chat").dataset.engine = current.engine;
  const list = models[current.engine] || [];
  const model = $("#model");
  model.replaceChildren(...list.map((m) => {
    const o = el("option", "", m.name);
    o.value = m.slug;
    o.title = m.about;
    return o;
  }));
  model.value = current.model;
  const effort = $("#effort");
  const levels = list.find((m) => m.slug === current.model)?.efforts || [];
  effort.replaceChildren(el("option", "", "размышление: авто"), ...levels.map((l) => el("option", "", l)));
  effort.options[0].value = "";
  effort.value = current.effort;
  for (const id of ["#model", "#effort", "#files-open", "#delete"]) $(id).hidden = false;
  effort.hidden = !levels.length;
}

async function change(fields) {
  try {
    current = await api(`/api/chats/${current.id}`, { method: "PATCH", json: fields });
    loadChats();
  } catch (err) { alert(err.message); }
  showHeader();
}

$("#model").onchange = (e) => change({ model: e.target.value });
$("#effort").onchange = (e) => change({ effort: e.target.value });
$("#title").onclick = () => {
  if (!current) return;
  const title = prompt("Название беседы", current.title);
  if (title !== null) change({ title });
};

$("#delete").onclick = async () => {
  if (!confirm("Удалить беседу вместе с её файлами?")) return;
  try {
    await api(`/api/chats/${current.id}`, { method: "DELETE" });
    location.hash = "";
    location.reload();
  } catch (err) { alert(err.message); }
};

// На телефоне панель бесед выезжает поверх беседы; тап по затемнению её закрывает.
function side(open) {
  $("#side").classList.toggle("open", open);
  $("#scrim").hidden = !open;
}
$("#menu").onclick = () => side(true);
$("#scrim").onclick = () => side(false);

// --- Лента ---

function scrollDown(force) {
  const feed = $("#feed");
  if (force || feed.scrollHeight - feed.scrollTop - feed.clientHeight < 200) feed.scrollTop = feed.scrollHeight;
}

function render(ev) {
  const feed = $("#feed");
  let node;
  if (ev.kind === "user") {
    node = el("div", "msg-user", ev.text);
    if (ev.files?.length) {
      const chips = el("div", "chips");
      ev.files.forEach((name) => chips.append(el("span", "chip file", name)));
      node.append(chips);
    }
  } else if (ev.kind === "text") {
    node = el("div", "msg-text");
    node.innerHTML = DOMPurify.sanitize(marked.parse(ev.text));
  } else if (ev.kind === "tool") {
    node = el("div", "msg-tool");
    node.append(el("b", "", ev.name), " " + ev.input);
    node.title = ev.input;
  } else if (ev.kind === "warning") {
    node = el("div", "msg-error", ev.text);
  } else if (ev.kind === "error") {
    node = el("div", "msg-error", "Ошибка: " + ev.text);
  } else if (ev.kind === "stopped") {
    node = el("div", "msg-note", "Остановлено");
  } else if (ev.kind === "done" && ev.files?.length) {
    node = el("div", "chips");
    ev.files.forEach((p) => node.append(fileLink(p)));
  }
  if (node) feed.insertBefore(node, $("#feed > .thinking"));
}

function setRunning(on) {
  running = on;
  $("#send").textContent = on ? "Стоп" : "Отправить";
  $("#send").classList.toggle("stop", on);
  $("#feed > .thinking")?.remove();
  if (on) $("#feed").append(el("div", "thinking"));
}

function follow() {
  const chat = current.id;
  stream = new EventSource(`/api/chats/${chat}/events?start=${count}`);
  stream.onmessage = (e) => {
    render(JSON.parse(e.data));
    count++;
    scrollDown();
  };
  stream.addEventListener("end", () => {
    stream.close();
    stream = null;
    setRunning(false);
    loadChats();
    refreshMe();
  });
  stream.onerror = () => {
    // Сеть моргнула или вкладка спала: перечитываем беседу, ответ на сервере идёт дальше.
    stream.close();
    stream = null;
    setTimeout(() => { if (current?.id === chat) openChat(chat); }, 3000);
  };
}

// --- Отправка ---

function showAttachments() {
  $("#attached").replaceChildren(...attachments.map((f, i) => {
    const chip = el("span", "chip file", f.name + " ✕");
    chip.title = "Убрать";
    chip.onclick = () => { attachments.splice(i, 1); showAttachments(); };
    return chip;
  }));
}

$("#file").onchange = (e) => {
  attachments.push(...e.target.files);
  e.target.value = "";
  showAttachments();
};

$("#text").addEventListener("paste", (e) => {
  const files = [...e.clipboardData.files];
  if (!files.length) return;
  e.preventDefault();
  attachments.push(...files);
  showAttachments();
});

$("#text").addEventListener("input", (e) => {
  e.target.style.height = "auto";
  e.target.style.height = e.target.scrollHeight + 2 + "px";
});

$("#text").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    $("#composer").requestSubmit();
  }
});

$("#composer").onsubmit = async (e) => {
  e.preventDefault();
  if (running) {
    await api(`/api/chats/${current.id}/stop`, { method: "POST" }).catch((err) => alert(err.message));
    return;
  }
  const text = $("#text").value;
  if (!text.trim()) return;
  const body = new FormData();
  body.append("text", text);
  attachments.forEach((f) => body.append("files", f, f.name));
  $("#send").disabled = true;
  try {
    await api(`/api/chats/${current.id}/messages`, { method: "POST", body });
    $("#text").value = "";
    $("#text").style.height = "auto";
    attachments = [];
    showAttachments();
    setRunning(true);
    follow();
    scrollDown(true);
  } catch (err) {
    alert(err.message);
  } finally {
    $("#send").disabled = false;
  }
};

// --- Файлы беседы ---

$("#files-open").onclick = async () => {
  const list = await api(`/api/chats/${current.id}/files`);
  $("#files-list").replaceChildren(...(list.length ? list.map((p) => {
    const li = el("li");
    const a = fileLink(p);
    a.textContent = p.replace(/^files\//, "");
    li.append(a);
    return li;
  }) : [el("li", "muted", "Пока пусто")]));
  $("#files").showModal();
};

// --- Пользователи ---

// Галочки «Доступ» у каждой сети свои (ключи — как users.ACCESS на сервере).
const ACCESS = {
  claude: [
    ["bash", "Запуск команд", "Bash в песочнице: распаковка архивов, скрипты, конвертация файлов."],
    ["write", "Изменение файлов", "Write и Edit: создавать и править файлы в рабочей папке, вести память. Без этого модель только читает."],
    ["network", "Интернет для команд", "Команды Bash (curl, pip, git) могут выходить в сеть."],
    ["web", "Веб-поиск и сайты", "WebSearch и WebFetch: поиск и чтение страниц. Сеть для команд они не открывают."],
    ["all_chats", "Файлы других бесед", "Модель видит рабочие папки всех бесед пользователя. Без этого — только текущую беседу и общую память."],
  ],
  codex: [
    ["write", "Изменение файлов", "Запись в рабочую папку и память. Без этого песочница открыта только на чтение."],
    ["network", "Интернет для команд", "Команды Codex (curl, pip, git) могут выходить в сеть."],
    ["web", "Веб-поиск", "Встроенный поиск Codex (web_search)."],
    ["all_chats", "Файлы других бесед", "Модель видит рабочие папки всех бесед пользователя. Без этого — только текущую беседу и общую память."],
  ],
};
const ACCESS_NOTE = { codex: "Запуск команд у Codex отключить нельзя: команды всегда идут в песочнице." };
let card = null, cardTab = "info";
const mobile = matchMedia("(max-width: 760px)");

// Шаг окна настроек. На компьютере видно всё сразу, на телефоне — один шаг:
// list (аккаунт и участники) → user (разделы участника) → section (раздел).
function setLevel(level) {
  $("#settings").dataset.level = level;
  const title = level === "user" ? card.user.name
    : level === "section" ? (card ? TABS[cardTab] : "Новый участник") : "Настройки";
  $("#settings-title").textContent = mobile.matches ? title : "Настройки";
}

$("#settings-back").onclick = () => setLevel($("#settings").dataset.level === "section" && card ? "user" : "list");

async function loadUsers(select) {
  const list = await api("/api/users");
  $("#users-list").replaceChildren(...list.map((u) => {
    const li = el("li");
    const b = el("button", card?.user.id === u.id ? "active" : "", u.name + (u.is_admin ? " ★" : ""));
    b.dataset.id = u.id;
    b.onclick = () => openCard(u.id);
    li.append(b);
    return li;
  }));
  if (select) openCard(select);
}

async function openCard(uid, stay) {
  try { card = await api(`/api/users/${uid}`); } catch (err) { alert(err.message); return; }
  // Разворачиваем права полностью по каталогу: так галочки однозначны, а модели,
  // появившиеся в каталоге позже, останутся разрешёнными (их нет в настройках).
  for (const [engine, list] of Object.entries(card.catalog)) {
    const conf = card.settings.engines[engine] ||= { on: true, limit_5h: 100, limit_week: 100, models: {} };
    for (const m of list) conf.models[m.slug] ||= { on: true, efforts: [...m.efforts] };
  }
  // Снимок для кнопки «Сохранить»: она видна, только пока настройки отличаются от сохранённых.
  card.saved = JSON.stringify(card.settings);
  document.querySelectorAll("#users-list button").forEach((b) => b.classList.toggle("active", b.dataset.id == card.user.id));
  showCard();
  if (!stay) setLevel("user");
}

function checkbox(label, checked, onchange, disabled) {
  const l = el("label", "check");
  const input = el("input");
  input.type = "checkbox";
  input.checked = checked;
  input.disabled = !!disabled;
  input.onchange = () => { onchange(input.checked); changed(); };
  l.append(input, label);
  return l;
}

function numberField(label, value, onchange, disabled, max) {
  const l = el("label", "num", label + " ");
  const input = el("input");
  input.type = "number";
  input.min = 0;
  if (max) input.max = max;
  input.value = value;
  input.disabled = !!disabled;
  input.oninput = () => { onchange(Math.max(0, Math.min(max || Infinity, parseInt(input.value) || 0))); changed(); };
  l.append(input);
  return l;
}

function changed() {
  const save = $("#card .actions.save");
  if (save) save.hidden = JSON.stringify(card.settings) === card.saved;
}

function row(name, value) {
  const tr = el("tr");
  const th = el("th");
  th.append(name);
  tr.append(th, el("td", "", value));
  return tr;
}

const TABS = { info: "Информация", memory: "Память", access: "Доступ", engines: "Сети" };

function showCard() {
  const { user, settings, stats } = card;
  const tabs = el("nav", "tabs");
  for (const [id, name] of Object.entries(TABS)) {
    const b = el("button", id === cardTab ? "active" : "", name);
    b.onclick = () => { cardTab = id; showCard(); setLevel("section"); };
    tabs.append(b);
  }
  const panel = el("div", "panel");
  // Перерисовка той же вкладки (например, по галочке сети) не сбрасывает прокрутку.
  panel.dataset.key = `${user.id}:${cardTab}`;
  const old = $("#card .panel");
  const top = old?.dataset.key === panel.dataset.key ? old.scrollTop : 0;
  panel.append(el("h3", "", user.name + (user.is_admin ? " (админ)" : "")));

  if (cardTab === "info") {
    const t = el("table", "info");
    t.append(row("Бесед", stats.chats), row("Сообщений", stats.messages),
      row("Отвечает сейчас", stats.running || "нет"), row("Последняя активность", when(stats.last)),
      row("Диск", size(stats.disk.total)));
    for (const engine of Object.keys(card.catalog)) {
      const x = stats.limits[engine];
      for (const w of Object.keys(WINDOW)) {
        t.append(row(engineLabel(engine, `${NAME[engine]}, ${WINDOW[w]}`),
          `${x[w].used}% из ${x[w].limit}%` + (x[w].resets_at ? `, сброс ${when(x[w].resets_at)}` : "")));
      }
      t.append(row(engineLabel(engine, `${NAME[engine]}, токенов всего`), `вход ${x.tokens.in.toLocaleString("ru")}, выход ${x.tokens.out.toLocaleString("ru")}`));
      const g = card.subscription[engine];
      if (g) t.append(row(engineLabel(engine, `${NAME[engine]}, подписка целиком`),
        Object.keys(WINDOW).filter((w) => g[w]).map((w) => `${WINDOW[w]} ${g[w].percent}%`).join(", ")));
    }
    panel.append(t, el("p", "muted", "Расход — сдвиг общих процентов подписки за время запросов пользователя, точность около 1%. Использование подписки мимо сайта (бот, свой CLI) в момент запроса тоже попадает в расход."));
    const actions = el("div", "actions");
    const pw = el("button", "", "Сменить пароль");
    pw.onclick = async () => {
      const password = prompt(`Новый пароль для ${user.name}`);
      if (password) await api(`/api/users/${user.id}/password`, { method: "POST", json: { password } }).then(() => alert("Пароль изменён")).catch((e) => alert(e.message));
    };
    actions.append(pw);
    if (user.id !== me.id) {
      const del = el("button", "danger", "Удалить участника");
      del.onclick = async () => {
        if (!confirm(`Удалить ${user.name} со всеми беседами и файлами?`)) return;
        try {
          await api(`/api/users/${user.id}`, { method: "DELETE" });
          card = null;
          $("#card").replaceChildren(el("p", "muted", "Выберите участника слева."));
          setLevel("list");
          loadUsers();
        } catch (e) { alert(e.message); }
      };
      actions.append(del);
    }
    panel.append(actions);
  }

  if (cardTab === "memory") {
    const d = stats.disk, quota = settings.quota_mb * 2 ** 20;
    const t = el("table", "info");
    t.append(row("Занято всего", size(d.total) + (quota ? ` из ${size(quota)}` : "")),
      row("Сессии и история", size(d.sessions)), row("Файлы и память модели", size(d.files)));
    panel.append(t);
    if (quota) {
      const bar = el("progress");
      bar.max = quota;
      bar.value = Math.min(d.total, quota);
      panel.append(bar);
    }
    panel.append(numberField("Лимит, МБ (0 — без лимита):", settings.quota_mb, (v) => { settings.quota_mb = v; }),
      el("p", "muted", "При превышении пользователь не сможет загружать новые файлы и увидит предупреждение. Писать сообщения он сможет."));
  }

  if (cardTab === "access") {
    panel.append(el("p", "muted", "Песочница включена всегда: модель видит только папку этого пользователя."));
    const engines = Object.keys(card.catalog);
    if (!engines.length) panel.append(el("p", "muted", "В системе нет сетей из NEXUS_ENGINES."));
    for (const engine of engines) {
      const access = settings.access[engine];
      const box = el("fieldset", "engine");
      box.dataset.engine = engine;
      const legend = el("legend");
      legend.append(engineLabel(engine));
      box.append(legend);
      for (const [key, name, about] of ACCESS[engine]) {
        const option = el("div", "option");
        option.append(checkbox(name, access[key], (v) => { access[key] = v; }), el("div", "muted", about));
        box.append(option);
      }
      if (ACCESS_NOTE[engine]) box.append(el("p", "muted", ACCESS_NOTE[engine]));
      panel.append(box);
    }
  }

  if (cardTab === "engines") {
    panel.append(el("p", "muted", "Лимит — доля общего окна подписки на этого пользователя: двое по 50% вместе могут потратить всё. 100% — без лимита. Уровень «авто» доступен, только если отмечены все уровни модели."));
    const engines = Object.entries(card.catalog);
    if (!engines.length) panel.append(el("p", "muted", "В системе нет сетей из NEXUS_ENGINES."));
    for (const [engine, list] of engines) {
      const conf = settings.engines[engine];
      const box = el("fieldset", "engine");
      const legend = el("legend");
      box.dataset.engine = engine;
      legend.append(checkbox(engineLabel(engine), conf.on, (v) => { conf.on = v; showCard(); }));
      box.append(legend);
      const limits = el("div", "limits");
      limits.append(numberField("Лимит 5 часов, %:", conf.limit_5h, (v) => { conf.limit_5h = v; }, !conf.on, 100),
        numberField("Неделя, %:", conf.limit_week, (v) => { conf.limit_week = v; }, !conf.on, 100));
      box.append(limits);
      for (const m of list) {
        const rule = conf.models[m.slug];
        const model = el("div", "model");
        const head = checkbox(m.name, rule.on, (v) => { rule.on = v; showCard(); }, !conf.on);
        head.title = m.about;
        model.append(head);
        const efforts = el("div", "efforts");
        for (const level of m.efforts) {
          efforts.append(checkbox(level, rule.efforts.includes(level), (v) => {
            rule.efforts = v ? [...rule.efforts, level] : rule.efforts.filter((x) => x !== level);
          }, !conf.on || !rule.on));
        }
        model.append(efforts);
        box.append(model);
      }
      panel.append(box);
    }
  }

  if (cardTab !== "info") {
    const save = el("button", "primary", "Сохранить");
    save.onclick = async () => {
      try {
        card.settings = await api(`/api/users/${user.id}/settings`, { method: "PUT", json: settings });
        await openCard(user.id, true);
        if (user.id === me.id) { models = await api("/api/models"); refreshMe(); }
      } catch (e) { alert(e.message); }
    };
    const actions = el("div", "actions save");
    actions.append(save);
    panel.append(actions);
  }
  $("#card").replaceChildren(tabs, panel);
  panel.scrollTop = top;
  changed();
}

$("#user-new").onclick = () => {
  card = null;
  document.querySelectorAll("#users-list button").forEach((b) => b.classList.remove("active"));
  const form = el("form", "new-user");
  form.innerHTML = `<h3>Новый участник</h3>
    <input name="name" placeholder="Имя" required>
    <input name="password" type="password" placeholder="Пароль" autocomplete="new-password" required>
    <label class="check"><input name="admin" type="checkbox"> администратор</label>
    <button class="primary">Создать</button>`;
  form.onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(form);
    try {
      const { id } = await api("/api/users", { method: "POST", json: { name: f.get("name"), password: f.get("password"), admin: f.get("admin") === "on" } });
      loadUsers(id);
    } catch (err) { alert(err.message); }
  };
  $("#card").replaceChildren(form);
  setLevel("section");
};

$("#settings-open").onclick = async () => {
  side(false);
  $("#account-name").textContent = me.name;
  $("#account-role").textContent = me.is_admin ? "Администратор" : "Участник";
  if (me.is_admin) await loadUsers();
  setLevel("list");
  $("#settings").showModal();
};

api("/api/me").then((user) => { me = user; start(); }).catch(() => {});
