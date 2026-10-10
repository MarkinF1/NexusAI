"use strict";
const $ = (s) => document.querySelector(s);
const MARK = { claude: "🟠", codex: "🟢" };
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

function fileLink(path) {
  const a = el("a", "chip", path.split("/").pop());
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
  $("#users-open").hidden = !me.is_admin;
  models = await api("/api/models");
  showMe();
  await loadChats();
  const id = location.hash.slice(1);
  if (id) openChat(id);
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

// Сводка внизу панели и плашка над полем ввода: диск и расход подписки.
function showMe() {
  const lines = [], warnings = [];
  const disk = me.disk;
  lines.push(`Диск ${size(disk.used)}` + (disk.quota ? ` из ${size(disk.quota)}` : ""));
  if (disk.quota && disk.used >= disk.quota) warnings.push("Память закончилась: новые файлы загрузить не получится.");
  for (const [engine, windows] of Object.entries(me.limits)) {
    const parts = Object.keys(WINDOW).map((w) => {
      const x = windows[w];
      if (x.limit < 100 && x.used >= x.limit) warnings.push(`Лимит ${NAME[engine]} (${WINDOW[w]}) исчерпан, сброс ${when(x.resets_at)}.`);
      return `${WINDOW[w]} ${x.used}%` + (x.limit < 100 ? ` из ${x.limit}%` : "");
    });
    lines.push(`${MARK[engine]} ${parts.join(" · ")}`);
  }
  $("#quota").replaceChildren(...lines.map((l) => el("div", "", l)));
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
    const a = el("a", c.id === current?.id ? "active" : "",
      `${MARK[c.engine]} ${c.running ? "⏳ " : ""}${c.title || "Новая беседа"}`);
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
    const b = el("button", "", `${MARK[engine]} ${NAME[engine]}`);
    b.setAttribute("role", "menuitem");
    b.onclick = async () => {
      menu.hidden = true;
      try {
        const chat = await api("/api/chats", { method: "POST", json: { engine } });
        await loadChats();
        openChat(chat.id);
      } catch (err) { alert(err.message); }
    };
    return b;
  }) : [el("div", "muted", "Нет доступных сетей")]));
  menu.hidden = !menu.hidden;
};
document.addEventListener("click", () => { $("#add-menu").hidden = true; });

async function openChat(id) {
  stream?.close();
  stream = null;
  let data;
  try { data = await api(`/api/chats/${id}`); } catch (err) { alert(err.message); return; }
  current = data.chat;
  history.replaceState(null, "", "#" + id);
  $("#side").classList.remove("open");
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
  $("#title").textContent = `${MARK[current.engine]} ${current.title || "Новая беседа"}`;
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

$("#menu").onclick = () => $("#side").classList.toggle("open");

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
      ev.files.forEach((name) => chips.append(el("span", "chip", "📎 " + name)));
      node.append(chips);
    }
  } else if (ev.kind === "text") {
    node = el("div", "msg-text");
    node.innerHTML = DOMPurify.sanitize(marked.parse(ev.text));
  } else if (ev.kind === "tool") {
    node = el("div", "msg-tool", `⚙ ${ev.name}: ${ev.input}`);
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
    const chip = el("span", "chip", "📎 " + f.name + " ✕");
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

const ACCESS = [
  ["bash", "Запуск команд", "Bash в песочнице: распаковка архивов, скрипты, конвертация файлов. У Codex команды есть всегда — галочка влияет только на Claude."],
  ["write", "Изменение файлов", "Создавать и править файлы в рабочей папке и вести память. Без этого модель только читает."],
  ["network", "Интернет для команд", "Команды (curl, pip, git) могут выходить в сеть."],
  ["web", "Веб-поиск и сайты", "Поиск в интернете и чтение страниц встроенными инструментами модели."],
  ["all_chats", "Файлы других бесед", "Модель видит рабочие папки всех бесед пользователя. Без этого — только текущую беседу и общую память."],
];
let card = null, cardTab = "info";

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

async function openCard(uid) {
  try { card = await api(`/api/users/${uid}`); } catch (err) { alert(err.message); return; }
  // Разворачиваем права полностью по каталогу: так галочки однозначны, а модели,
  // появившиеся в каталоге позже, останутся разрешёнными (их нет в настройках).
  for (const [engine, list] of Object.entries(card.catalog)) {
    const conf = card.settings.engines[engine] ||= { on: true, limit_5h: 100, limit_week: 100, models: {} };
    for (const m of list) conf.models[m.slug] ||= { on: true, efforts: [...m.efforts] };
  }
  document.querySelectorAll("#users-list button").forEach((b) => b.classList.toggle("active", b.dataset.id == card.user.id));
  showCard();
}

function checkbox(label, checked, onchange, disabled) {
  const l = el("label", "check");
  const input = el("input");
  input.type = "checkbox";
  input.checked = checked;
  input.disabled = !!disabled;
  input.onchange = () => onchange(input.checked);
  l.append(input, " " + label);
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
  input.onchange = () => onchange(Math.max(0, Math.min(max || Infinity, parseInt(input.value) || 0)));
  l.append(input);
  return l;
}

function row(name, value) {
  const tr = el("tr");
  tr.append(el("th", "", name), el("td", "", value));
  return tr;
}

const TABS = { info: "Информация", memory: "Память", access: "Доступ", engines: "Сети" };

function showCard() {
  const { user, settings, stats } = card;
  const tabs = el("nav", "tabs");
  for (const [id, name] of Object.entries(TABS)) {
    const b = el("button", id === cardTab ? "active" : "", name);
    b.onclick = () => { cardTab = id; showCard(); };
    tabs.append(b);
  }
  const panel = el("div", "panel");
  panel.append(el("h3", "", user.name + (user.is_admin ? " (админ)" : "")));

  if (cardTab === "info") {
    const t = el("table", "info");
    t.append(row("Бесед", stats.chats), row("Сообщений", stats.messages),
      row("Отвечает сейчас", stats.running || "нет"), row("Последняя активность", when(stats.last)),
      row("Диск", size(stats.disk.total)));
    for (const engine of Object.keys(card.catalog)) {
      const x = stats.limits[engine];
      for (const w of Object.keys(WINDOW)) {
        t.append(row(`${MARK[engine]} ${NAME[engine]}, ${WINDOW[w]}`,
          `${x[w].used}% из ${x[w].limit}%` + (x[w].resets_at ? `, сброс ${when(x[w].resets_at)}` : "")));
      }
      t.append(row(`${MARK[engine]} токенов всего`, `вход ${x.tokens.in.toLocaleString("ru")}, выход ${x.tokens.out.toLocaleString("ru")}`));
      const g = card.subscription[engine];
      if (g) t.append(row(`${MARK[engine]} подписка целиком`,
        Object.keys(WINDOW).filter((w) => g[w]).map((w) => `${WINDOW[w]} ${g[w].percent}%`).join(" · ")));
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
      const del = el("button", "danger", "Удалить пользователя");
      del.onclick = async () => {
        if (!confirm(`Удалить ${user.name} со всеми беседами и файлами?`)) return;
        try {
          await api(`/api/users/${user.id}`, { method: "DELETE" });
          card = null;
          $("#card").replaceChildren(el("p", "muted", "Выберите пользователя слева."));
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
    for (const [key, name, about] of ACCESS) {
      const box = el("div", "option");
      box.append(checkbox(name, settings.access[key], (v) => { settings.access[key] = v; }), el("div", "muted", about));
      panel.append(box);
    }
    panel.append(el("p", "muted", "Песочница включена всегда: модель видит только папку этого пользователя."));
  }

  if (cardTab === "engines") {
    const engines = Object.entries(card.catalog);
    if (!engines.length) panel.append(el("p", "muted", "В системе нет сетей из NEXUS_ENGINES."));
    for (const [engine, list] of engines) {
      const conf = settings.engines[engine];
      const box = el("fieldset", "engine");
      const legend = el("legend");
      legend.append(checkbox(`${MARK[engine]} ${NAME[engine]}`, conf.on, (v) => { conf.on = v; showCard(); }));
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
    panel.append(el("p", "muted", "Лимит — доля общего окна подписки на этого пользователя: двое по 50% вместе могут потратить всё. 100% — без лимита. Уровень «авто» доступен, только если отмечены все уровни модели."));
  }

  if (cardTab !== "info") {
    const save = el("button", "primary", "Сохранить");
    save.onclick = async () => {
      try {
        card.settings = await api(`/api/users/${user.id}/settings`, { method: "PUT", json: settings });
        await openCard(user.id);
        if (user.id === me.id) { models = await api("/api/models"); refreshMe(); }
        save.textContent = "Сохранено";
      } catch (e) { alert(e.message); }
    };
    const actions = el("div", "actions save");
    actions.append(save);
    panel.append(actions);
  }
  $("#card").replaceChildren(tabs, panel);
}

$("#user-new").onclick = () => {
  card = null;
  document.querySelectorAll("#users-list button").forEach((b) => b.classList.remove("active"));
  const form = el("form", "new-user");
  form.innerHTML = `<h3>Новый пользователь</h3>
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
};

$("#users-open").onclick = async () => { await loadUsers(); $("#users").showModal(); };

api("/api/me").then((user) => { me = user; start(); }).catch(() => {});
