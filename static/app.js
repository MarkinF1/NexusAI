"use strict";
const $ = (s) => document.querySelector(s);

// Чем пользуются сейчас: пальцем или мышью — без обводки фокуса, клавиатурой — с ней (см. style.css).
document.documentElement.dataset.input = "pointer";
document.addEventListener("pointerdown", () => { document.documentElement.dataset.input = "pointer"; }, true);
document.addEventListener("keydown", (e) => {
  if (e.key === "Tab" || e.key.startsWith("Arrow")) document.documentElement.dataset.input = "keyboard";
}, true);
const NAME = { claude: "Claude", codex: "Codex" };
const WINDOW = { five_hour: "5 ч", seven_day: "неделя" };
let me = null, models = null, current = null, stream = null, count = 0, running = false;
// Тексты текущего ответа ИИ: кнопка «Копировать» под ответом берёт их все разом.
let turn = [];
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

// Веб-приложение с экрана «Домой» на iOS открывает <a download> прямо в своём окне,
// где нет кнопки «Назад», и выйти из просмотра можно только перезапуском.
// Поэтому там файл отдаётся через системное меню «Поделиться».
const iosApp = navigator.standalone === true;

function fileLink(path) {
  const name = path.split("/").pop();
  const a = el("a", "chip file", name);
  a.href = `/api/chats/${current.id}/file?path=${encodeURIComponent(path)}`;
  a.download = "";
  if (iosApp) a.onclick = (e) => { e.preventDefault(); shareFile(a.href, name); };
  return a;
}

async function shareFile(url, name) {
  let file;
  try {
    const r = await fetch(url);
    if (r.status === 401) { showLogin(); return; }
    if (!r.ok) throw new Error(`Не удалось получить файл: ${r.status} ${r.statusText}`);
    const blob = await r.blob();
    file = new File([blob], name, { type: blob.type || "application/octet-stream" });
    if (!navigator.canShare?.({ files: [file] })) throw new Error("Это устройство не умеет передавать файлы из веб-приложения. Откройте сайт в Safari.");
  } catch (err) { alert(err.message); return; }
  try {
    await navigator.share({ files: [file] });
  } catch (err) {
    // Пока файл скачивался, iOS могла «забыть» нажатие: тогда нужен ещё один тап.
    if (err.name === "NotAllowedError") askShare(file);
    else if (err.name !== "AbortError") alert(err.message);
  }
}

function askShare(file) {
  const box = $("#file-share");
  $("#file-share-name").textContent = file.name;
  $("#file-share-go").onclick = () => {
    navigator.share({ files: [file] }).catch((err) => { if (err.name !== "AbortError") alert(err.message); });
  };
  box.showModal();
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

$("#logout").onclick = async () => {
  pauseVoice(); await api("/api/logout", { method: "POST" }); location.reload(); };

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
  showTg();
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

// Диск внизу панели, лимиты сети — в шапке беседы, плашка над полем ввода.
let diskFull = false;

function showMe() {
  const disk = me.disk;
  diskFull = !!disk.quota && disk.used >= disk.quota;
  const diskBlock = el("div", "budget");
  diskBlock.append(disk.quota
    ? meter("Диск", disk.used / disk.quota * 100, 100, `${size(disk.used)} из ${size(disk.quota)}`)
    : meter("Диск", 0, 0, size(disk.used)));
  $("#quota").replaceChildren(diskBlock);
  showLimits();
}

// Лимиты сети открытой беседы: сначала 5-часовое окно, под ним недельное.
const percent = (n) => `${Math.round(n * 100) / 100}%`;

// Полоса — сколько своей части окна уже нет: своими запросами или потому, что подписки
// осталось меньше, чем положено по лимиту.
function shareText(x) {
  return x.limit < 100
    ? `Ваша часть — ${x.limit}% подписки. Вы потратили ${percent(x.used)}, подписка занята на ${percent(x.subscription)}.`
    : `Без лимита: занятость всей подписки. Вы потратили ${percent(x.used)}.`;
}

// Плашка «лимит исчерпан» — только про сеть открытой беседы и в её цвете.
function showLimits() {
  const engine = current?.engine;
  const windows = engine && me.limits[engine];
  const warnings = windows ? Object.keys(WINDOW).filter((w) => windows[w].share >= 100)
    .map((w) => `Лимит ${NAME[engine]} (${WINDOW[w]}) исчерпан, сброс ${when(windows[w].resets_at)}.`) : [];
  const limited = warnings.length > 0;
  if (diskFull) warnings.push("Память закончилась: новые файлы загрузить не получится.");
  $("#limits").replaceChildren(...(windows ? Object.keys(WINDOW).map((w) => {
    const x = windows[w];
    const row = meter(WINDOW[w], x.share, 100, percent(x.share));
    row.title = `${shareText(x)} Сброс ${when(x.resets_at)}`;
    return row;
  }) : []));
  const banner = $("#banner");
  banner.hidden = !warnings.length;
  banner.textContent = warnings.join(" ");
  banner.classList.toggle("limit", limited);
}

let compactOffer = false;  // сессия открытой беседы больше порога сжатия

$("#compact").onclick = async () => {
  if (!confirm(`Сжать беседу? ${NAME[current.engine]} заменит старую часть разговора кратким содержанием — `
    + "подробности оттуда модель забудет. Переписка на сайте останется.")) return;
  try {
    await api(`/api/chats/${current.id}/compact`, { method: "POST" });
  } catch (err) { return alert(err.message); }
  setRunning(true);
  follow();
  scrollDown(true);
};

// Размер беседы в шапке: главное — сессия модели, остальное в подсказке.
async function showSize() {
  const id = current?.id;
  if (!id) return;
  let s;
  try { s = await api(`/api/chats/${id}/size`); } catch { return; }
  if (current?.id !== id) return;
  const box = $("#chat-size");
  box.textContent = "Сессия " + size(s.context);
  box.title = `Сессия модели: ${size(s.context)} — модель перечитывает её на каждом ответе`
    + (s.session > s.context ? ` (после последнего сжатия; файл целиком ${size(s.session)})` : "")
    + `\nЖурнал сайта: ${size(s.history)}\nФайлы беседы: ${size(s.files)}\nГолосовые: ${size(s.voice)}`;
  box.hidden = false;
  // Сессия больше порога пользователя — предлагаем сжать (только Claude: Codex сжимает сам).
  compactOffer = s.compactable && s.limit > 0 && s.context > s.limit;
  $("#compact-text").textContent = `Беседа выросла до ${size(s.context)}`;
  $("#compact-bar").title = `Порог — ${size(s.limit)}. Модель перечитывает сессию на каждом ответе: `
    + "после сжатия лимиты будут уходить медленнее.";
  $("#compact-bar").hidden = !compactOffer || running;
}

async function refreshMe() {
  me = await api("/api/me");
  showMe();
}

// --- Беседы ---

// Поиск по беседам: вместо списка — найденные беседы с кусочком текста.
let searchTimer = 0, searchGen = 0;

$("#search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadChats, 250);
});
$("#search").addEventListener("keydown", (e) => {
  if (e.key === "Escape") { e.target.value = ""; loadChats(); }
});

function marked_(text, q) {
  const span = el("span", "snippet");
  const low = text.toLowerCase(), needle = q.toLowerCase();
  let at = 0;
  for (let i = low.indexOf(needle); i >= 0; i = low.indexOf(needle, at)) {
    span.append(text.slice(at, i), el("mark", "", text.slice(i, i + needle.length)));
    at = i + needle.length;
  }
  span.append(text.slice(at));
  return span;
}

async function showSearch(q) {
  const gen = ++searchGen;
  let list;
  try { list = await api(`/api/search?q=${encodeURIComponent(q)}`); } catch { return; }
  if (gen !== searchGen) return;
  const nav = $("#chats");
  if (!list.length) return nav.replaceChildren(el("div", "muted", "Ничего не нашлось"));
  nav.replaceChildren(...list.map((c) => {
    const a = el("a", "found" + (c.id === current?.id ? " active" : ""));
    a.append(logo(c.engine), marked_(c.title, q));
    a.lastChild.className = "";
    if (c.snippet) a.append(marked_(c.snippet, q));
    a.title = c.hits ? `Совпадений в сообщениях: ${c.hits}` : "Совпадение в названии";
    a.dataset.engine = c.engine;
    a.href = "#" + c.id;
    a.onclick = (e) => { e.preventDefault(); openChat(c.id, q); };
    return a;
  }));
}

// Открытая из поиска беседа: прокрутка к первому совпадению и подсветка.
function showFound(q) {
  const needle = q.toLowerCase();
  const hit = [...$("#feed").querySelectorAll(":scope > .msg-user, :scope > .msg-text, :scope > .msg-transcript")]
    .find((n) => n.textContent.toLowerCase().includes(needle));
  if (!hit) return;
  hit.scrollIntoView({ block: "center" });
  hit.classList.add("flash");
  setTimeout(() => hit.classList.remove("flash"), 2000);
}

async function loadChats() {
  const q = $("#search").value.trim();
  if (q.length >= 2) return showSearch(q);
  searchGen++;
  const list = await api("/api/chats");
  const nav = $("#chats");
  nav.replaceChildren(...list.map((c) => {
    const a = el("a", (c.id === current?.id ? "active" : "") + (c.running ? " running" : ""));
    a.append(logo(c.engine), el("span", "", c.title || "Новая беседа"));
    if (c.tg && !c.tg.moved) a.append(tgIcon());
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

async function openChat(id, found) {
  pauseVoice();
  stream?.close();
  stream = null;
  let data;
  try { data = await api(`/api/chats/${id}`); } catch (err) { alert(err.message); return; }
  current = data.chat;
  history.replaceState(null, "", "#" + id);
  side(false);
  $("#feed").replaceChildren();
  feedGen++;
  count = 0;
  turn = [];
  lastAsk = null;
  stopEdit(true);
  data.history.forEach(render);
  count = data.history.length;
  // Голосовое расшифровывается, а запрос ушёл из другой вкладки или до перезагрузки:
  // показываем ожидание и перечитываем беседу, пока расшифровка не появится.
  waiting = data.transcribing && !transcribing.has(id);
  if (waiting) {
    $("#feed").append(transcribingNode());
    const gen = feedGen;
    setTimeout(() => { if (current?.id === id && feedGen === gen) openChat(id); }, 2500);
  }
  showHeader();
  const inTg = !!current.tg && !current.tg.moved;
  $("#composer").hidden = inTg;
  $("#tg-bar").hidden = !inTg;
  setRunning(data.running);
  if (data.running) follow();
  if (found) showFound(found);
  $("#chat-size").hidden = true;
  compactOffer = false;
  $("#compact-bar").hidden = true;
  showSize();
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
  for (const id of ["#pick", "#export", "#files-open", "#delete"]) $(id).hidden = false;
  effort.hidden = !levels.length;
  showLimits();
  // Модель беседы из Telegram меняют в боте, пока её не перенесли.
  model.disabled = effort.disabled = !!current.tg && !current.tg.moved;
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

$("#move").onclick = async () => {
  if (running) return alert("Дождитесь ответа");
  try {
    await api(`/api/chats/${current.id}/move`, { method: "POST" });
    openChat(current.id);
  } catch (err) { alert(err.message); }
};

// Подтверждение удаления: файлы беседы удаляются только по галочке — из беседы и из библиотеки.
function deleteHint() {
  $("#delete-hint").textContent = $("#delete-files").checked
    ? "Файлы удалятся и из беседы, и из библиотеки."
    : "Файлы останутся в библиотеке.";
}
$("#delete-files").onchange = deleteHint;

$("#delete").onclick = () => {
  $("#delete-files").checked = false;
  deleteHint();
  $("#delete-chat").returnValue = "";
  $("#delete-chat").showModal();
};

$("#delete-chat").onclose = async () => {
  if ($("#delete-chat").returnValue !== "ok") return;
  try {
    await api(`/api/chats/${current.id}?files=${$("#delete-files").checked}`, { method: "DELETE" });
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

// --- Telegram ---

function tgIcon() {
  const img = logo("telegram");
  img.classList.add("tg");
  img.title = "Беседа из Telegram";
  return img;
}

// Блок в «Настройках»: привязка аккаунта и две галочки. Без бота блока нет.
function showTg() {
  const box = $("#tg-account");
  box.hidden = !me.tg_bot && !me.tg;
  if (box.hidden) return;
  const head = el("div", "tg-head");
  head.append(tgIcon(), el("span", "", me.tg ? `Telegram: ${me.tg.name}` : "Telegram не привязан"));
  const button = el("button", "", me.tg ? "Отвязать" : "Привязать Telegram");
  button.onclick = async () => {
    try {
      if (me.tg) {
        if (!confirm("Отвязать Telegram? Бот перестанет вам отвечать, группы отключатся.")) return;
        await api("/api/me/telegram", { method: "DELETE" });
      } else {
        // Окно открываем сразу по клику: после await браузер счёл бы его всплывающим.
        const tab = window.open("about:blank", "_blank");
        try {
          const { url } = await api("/api/me/telegram", { method: "POST" });
          if (tab) { tab.opener = null; tab.location.href = url; } else location.href = url;
        } catch (err) { tab?.close(); throw err; }
        alert("В Telegram нажмите «Запустить» у бота, затем закройте это окно.");
      }
      await refreshMe();
      showTg();
      loadChats();
    } catch (err) { alert(err.message); }
  };
  box.replaceChildren(head);
  if (me.tg) {
    const option = (label, key) => {
      const l = el("label", "check");
      const input = el("input");
      input.type = "checkbox";
      input.checked = me.tg[key];
      input.onchange = async () => {
        try {
          await api("/api/me/telegram", { method: "PATCH", json: { [key]: input.checked } });
          me.tg[key] = input.checked;
          loadChats();
        } catch (err) { input.checked = !input.checked; alert(err.message); }
      };
      l.append(input, label);
      return l;
    };
    box.append(option("Показывать беседы из Telegram", "show"), option("Сообщать о готовых ответах", "notify"));
  }
  box.append(button);
}

// --- Лента ---

function scrollDown(force) {
  const feed = $("#feed");
  if (force || feed.scrollHeight - feed.scrollTop - feed.clientHeight < 200) feed.scrollTop = feed.scrollHeight;
}

// Сайт часто открыт по http из домашней сети, а там navigator.clipboard нет.
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = el("textarea");
    area.value = text;
    area.style.cssText = "position:fixed;opacity:0";
    document.body.append(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    if (!ok) throw new Error("Браузер не дал скопировать текст");
  }
}

function copyBar(text) {
  const bar = el("div", "msg-actions");
  const button = el("button", "icon");
  button.title = "Копировать";
  button.setAttribute("aria-label", button.title);
  button.innerHTML = '<svg width="16" height="16"><use href="#i-copy"/></svg>';
  button.onclick = async () => {
    try { await copyText(text); } catch (err) { alert(err.message); return; }
    button.querySelector("use").setAttribute("href", "#i-check");
    button.title = "Скопировано";
    setTimeout(() => { button.querySelector("use").setAttribute("href", "#i-copy"); button.title = "Копировать"; }, 1500);
  };
  bar.append(button);
  return bar;
}

function endTurn() {
  if (!turn.length) return;
  const text = turn.join("\n\n");
  turn = [];
  $("#feed").insertBefore(copyBar(text), $("#feed > .thinking"));
}

// Последний запрос беседы: его можно повторить или изменить, пока после него не было сжатия.
let lastAsk = null;

function render(ev) {
  const feed = $("#feed");
  let node, after;
  if (ev.kind === "retry") {
    // Повтор: последний запрос и всё после него уходят из ленты, следом придёт новый.
    const asked = [...feed.querySelectorAll(":scope > .msg-user")].pop();
    for (let n = asked; n;) {
      const next = n.nextElementSibling;
      if (!n.classList.contains("thinking")) n.remove();
      n = next;
    }
    turn = [];
    lastAsk = null;
    return;
  }
  if (ev.kind === "user") {
    endTurn();
    node = el("div", "msg-user", ev.text);
    if (ev.files?.length) {
      const chips = el("div", "chips");
      ev.files.forEach((name) => chips.append(el("span", "chip file", name)));
      node.append(chips);
    }
    if (ev.text) after = copyBar(ev.text);
    lastAsk = { text: ev.text, bar: after };
  } else if (ev.kind === "text") {
    turn.push(ev.text);
    node = el("div", "msg-text");
    node.innerHTML = DOMPurify.sanitize(marked.parse(ev.text));
  } else if (ev.kind === "tool") {
    node = el("div", "msg-tool");
    node.append(el("b", "", ev.name), " " + ev.input);
    node.title = ev.input;
  } else if (ev.kind === "warning") {
    node = el("div", "msg-error", ev.text);
  } else if (ev.kind === "error") {
    // Лимит — не сбой: в цвете сети беседы; в старых записях флага нет, узнаём по тексту.
    const limit = ev.limit || /^Лимит .* исчерпан/.test(ev.text || "");
    node = el("div", "msg-error" + (limit ? " msg-limit" : ""), limit ? ev.text : "Ошибка: " + ev.text);
  } else if (ev.kind === "compact") {
    endTurn();
    lastAsk = null;
    node = el("div", "msg-note", "Сжатие беседы…");
  } else if (ev.kind === "compacted") {
    const was = ev.tokens ? `: было ≈${Math.round(ev.tokens / 1000).toLocaleString("ru")} тыс. токенов` : "";
    node = el("div", "msg-note", (ev.auto ? `${NAME[current.engine]} сам сжал беседу` : "Беседа сжата") + was);
  } else if (ev.kind === "stopped") {
    node = el("div", "msg-note", "Остановлено");
  } else if (ev.kind === "voice") {
    endTurn();
    node = voiceNode(ev, `/api/chats/${current.id}/voice/${ev.id}`);
  } else if (ev.kind === "transcript") {
    node = transcriptNode(ev);
  } else if (ev.kind === "voice_answer") {
    if (ev.voice === fixing) stopEdit(true);  // на вопрос уже ответили — править нечего
    // «Да» — текст ушёл репликой пользователя; «нет» — убираем только вопрос и кнопки.
    const asked = feed.querySelector(`.msg-transcript[data-voice="${ev.voice}"]`);
    if (ev.ok) asked?.remove();
    else asked?.querySelector(".transcript-ask")?.remove();
  } else if (ev.kind === "done" && ev.files?.length) {
    node = el("div", "chips");
    ev.files.forEach((p) => node.append(fileLink(p)));
  }
  if (node) feed.insertBefore(node, $("#feed > .thinking"));
  if (after) feed.insertBefore(after, $("#feed > .thinking"));
  if (ev.kind === "done") endTurn();
}

function actionButton(icon, title, onclick) {
  const b = el("button", "icon " + icon);
  b.type = "button";
  b.title = title;
  b.setAttribute("aria-label", title);
  b.innerHTML = `<svg width="16" height="16"><use href="#${icon}"/></svg>`;
  b.onclick = onclick;
  return b;
}

// «Изменить» — у последнего запроса, «Ответить заново» — под последним ответом.
function markLast() {
  $("#feed").querySelectorAll(".msg-actions .i-edit, .msg-actions .i-retry, .msg-actions.retry-only")
    .forEach((b) => b.remove());
  const locked = !current || (!!current.tg && !current.tg.moved);
  if (running || locked || !lastAsk?.bar?.isConnected) return;
  lastAsk.bar.append(actionButton("i-edit", "Изменить запрос", startEdit));
  let answer = [...$("#feed").querySelectorAll(":scope > .msg-actions")].pop();
  if (!answer || answer === lastAsk.bar) {
    // Ответа с текстом нет (ошибка, остановка) — своя строка с кнопкой в конце хода.
    answer = el("div", "msg-actions retry-only");
    $("#feed").insertBefore(answer, $("#feed > .thinking"));
  }
  answer.append(actionButton("i-retry", "Ответить заново", () => retry()));
}

async function retry(text) {
  const what = text === undefined ? "Ответить заново? Прежний ответ заменится новым, модель его забудет."
    : null;
  if (what && !confirm(what)) return false;
  try {
    await api(`/api/chats/${current.id}/retry`, { method: "POST", json: text === undefined ? {} : { text } });
  } catch (err) { alert(err.message); return false; }
  setRunning(true);
  follow();
  scrollDown(true);
  return true;
}

// Текст в поле ввода на правку. «Изменить запрос» — отправка заменит последний запрос
// и ответ; «Исправить» расшифровку — уйдёт обычной репликой вместо голосового.
let editing = false;
let fixing = null;  // голосовое, расшифровку которого правят

function editText(value, note) {
  $("#edit-text").textContent = note;
  $("#edit-bar").hidden = false;
  const text = $("#text");
  text.value = value;
  text.dispatchEvent(new Event("input"));
  text.focus();
  text.setSelectionRange(value.length, value.length);
}

function startEdit() {
  stopEdit(false);
  editing = true;
  $("#composer").classList.add("editing");
  editText(lastAsk.text, "Изменение последнего запроса: прежний ответ заменится новым");
}

function startFix(ev) {
  stopEdit(false);
  fixing = ev.voice;
  editText(ev.text, "Исправление расшифровки: поправьте текст и отправьте");
}

function stopEdit(clear) {
  if (!editing && !fixing) return;
  editing = false;
  fixing = null;
  $("#composer").classList.remove("editing");
  $("#edit-bar").hidden = true;
  if (clear) {
    $("#text").value = "";
    $("#text").style.height = "auto";
  }
  updateSend();
}

$("#edit-cancel").onclick = () => stopEdit(true);

// Кнопка справа: «стоп» во время ответа, самолётик при тексте в поле, иначе микрофон.
const SEND = {
  stop: ["#i-stop", "Остановить"],
  send: ["#i-send", "Отправить"],
  mic: ["#i-mic", "Удерживайте, чтобы записать голосовое"],
};

function updateSend() {
  const send = $("#send");
  const mode = running ? "stop" : $("#text").value.trim() ? "send" : "mic";
  send.dataset.mode = mode;
  send.querySelector("use").setAttribute("href", SEND[mode][0]);
  send.title = SEND[mode][1];
  send.setAttribute("aria-label", send.title);
  send.classList.toggle("stop", mode === "stop");
  // Пока расшифровывается голосовое, беседа занята: ни текста, ни новой записи.
  send.disabled = mode !== "stop" && !!current && (transcribing.has(current.id) || waiting);
}

function setRunning(on) {
  running = on;
  updateSend();
  $("#compact-bar").hidden = on || !compactOffer;
  $("#feed > .thinking")?.remove();
  if (on) $("#feed").append(el("div", "thinking"));
  markLast();
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
    showSize();
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
  updateSend();
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
  if (editing) {
    $("#send").disabled = true;
    if (await retry(text)) stopEdit(true);
    updateSend();
    return;
  }
  $("#send").disabled = true;
  try {
    await sendMessage(text, attachments, fixing);  // исправленная расшифровка закрывает вопрос
    stopEdit(false);
    $("#text").value = "";
    $("#text").style.height = "auto";
    attachments = [];
    showAttachments();
  } catch (err) {
    alert(err.message);
  } finally {
    updateSend();
  }
};

// voice — голосовое, расшифровку которого подтвердили: сервер отметит ответ «да».
async function sendMessage(text, files, voice) {
  const body = new FormData();
  body.append("text", text);
  files.forEach((f) => body.append("files", f, f.name));
  if (voice) body.append("voice", voice);
  await api(`/api/chats/${current.id}/messages`, { method: "POST", body });
  setRunning(true);
  follow();
  scrollDown(true);
}

// --- Голосовые: запись ---

const VOICE_LIMIT = 5 * 60;  // секунд; дальше запись отправляется сама
const transcribing = new Set();  // беседы, где эта вкладка ждёт расшифровку
let waiting = false;  // в открытой беседе расшифровка идёт по чужому запросу
let rec = null;  // идущая запись
let feedGen = 0;  // растёт при каждой перерисовке ленты

function clock(sec) {
  const s = Math.max(0, Math.floor(sec));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function micHint() {
  const text = $("#text");
  text.placeholder = "Удерживайте микрофон, чтобы записать голосовое";
  clearTimeout(micHint.timer);
  micHint.timer = setTimeout(() => { text.placeholder = "Сообщение"; }, 2500);
}

const sendButton = $("#send");
sendButton.addEventListener("contextmenu", (e) => { if (sendButton.dataset.mode === "mic") e.preventDefault(); });
// Нажатие на микрофон не отправляет форму: запись — удержанием.
sendButton.addEventListener("click", (e) => { if (sendButton.dataset.mode === "mic") e.preventDefault(); });
sendButton.addEventListener("pointerdown", (e) => {
  if (sendButton.dataset.mode !== "mic" || sendButton.disabled || rec || e.button > 0) return;
  e.preventDefault();
  startRecording(e);
});
sendButton.addEventListener("pointermove", (e) => {
  const r = rec;
  if (!r || e.pointerId !== r.pointer || !r.recorder) return;
  // Палец уходит влево — кнопка едет за ним, подсказка гаснет; дальше порога — отмена.
  const dx = Math.min(0, e.clientX - r.x);
  const limit = Math.min(120, $("#composer .row").clientWidth / 3);
  sendButton.style.setProperty("--drag", dx + "px");
  $("#rec").style.setProperty("--fade", String(1 - Math.min(1, -dx / limit)));
  if (-dx > limit) finishRecording(false);
});
sendButton.addEventListener("pointerup", (e) => { if (rec && e.pointerId === rec.pointer) finishRecording(true); });
sendButton.addEventListener("pointercancel", (e) => { if (rec && e.pointerId === rec.pointer) finishRecording(false); });

async function startRecording(e) {
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    alert(window.isSecureContext ? "Этот браузер не умеет записывать звук"
      : "Запись голоса браузер разрешает только по https или на localhost");
    return;
  }
  const r = rec = { pointer: e.pointerId, x: e.clientX, held: true, chunks: [], levels: [] };
  try { sendButton.setPointerCapture(e.pointerId); } catch {}
  // AudioContext — сразу в жесте: созданный после await iOS держит на паузе.
  const Context = window.AudioContext || window.webkitAudioContext;
  r.ctx = Context ? new Context() : null;
  try {
    r.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  } catch (err) {
    r.ctx?.close().catch(() => {});
    if (rec === r) rec = null;
    alert(err.name === "NotAllowedError" ? "Нет доступа к микрофону: разрешите его в настройках браузера"
      : "Микрофон недоступен: " + err.message);
    return;
  }
  if (!r.held) {  // отпустили, пока браузер спрашивал разрешение: в следующий раз запишется сразу
    stopTracks(r);
    return;
  }
  const type = ["audio/mp4", "audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"]
    .find((t) => MediaRecorder.isTypeSupported(t));
  r.recorder = new MediaRecorder(r.stream, type ? { mimeType: type } : undefined);
  r.recorder.ondataavailable = (ev) => { if (ev.data.size) r.chunks.push(ev.data); };
  r.recorder.start(250);
  if (r.ctx) {
    r.ctx.resume?.();
    r.analyser = r.ctx.createAnalyser();
    r.analyser.fftSize = 1024;
    r.ctx.createMediaStreamSource(r.stream).connect(r.analyser);
    r.samples = new Float32Array(r.analyser.fftSize);
  }
  r.started = performance.now();
  $("#composer").classList.add("recording");
  $("#rec").hidden = false;
  $("#rec-time").textContent = "0:00";
  navigator.vibrate?.(15);
  tick(r);
}

function tick(r) {
  if (rec !== r) return;
  const t = (performance.now() - r.started) / 1000;
  $("#rec-time").textContent = clock(t);
  if (r.analyser) {  // громкость для дорожки голосового
    r.analyser.getFloatTimeDomainData(r.samples);
    let sum = 0;
    for (const v of r.samples) sum += v * v;
    r.levels.push(Math.sqrt(sum / r.samples.length));
  }
  if (t >= VOICE_LIMIT) return finishRecording(true);
  r.frame = requestAnimationFrame(() => tick(r));
}

function stopTracks(r) {
  r.stream?.getTracks().forEach((t) => t.stop());
  r.ctx?.close().catch(() => {});
}

// ok — отпустили кнопку: отправить; иначе отмена.
function finishRecording(ok) {
  const r = rec;
  if (!r) return;
  rec = null;
  r.held = false;
  cancelAnimationFrame(r.frame);
  $("#composer").classList.remove("recording");
  $("#rec").hidden = true;
  sendButton.style.removeProperty("--drag");
  $("#rec").style.removeProperty("--fade");
  if (!r.recorder) {  // разрешение ещё не дали: дорогу освободит startRecording
    if (ok) micHint();
    return;
  }
  const duration = (performance.now() - r.started) / 1000;
  if (ok && duration < 0.6) micHint();
  r.recorder.onstop = () => {
    stopTracks(r);
    if (ok && duration >= 0.6 && r.chunks.length) {
      uploadVoice(new Blob(r.chunks, { type: r.recorder.mimeType || "audio/webm" }), duration, peaksOf(r.levels));
    }
  };
  r.recorder.stop();
}

// Громкость по кадрам — в 48 столбиков дорожки, самый громкий = 1.
function peaksOf(levels, n = 48) {
  if (!levels.length) return [];
  const out = [];
  for (let i = 0; i < n; i++) {
    const from = Math.floor(i * levels.length / n);
    const to = Math.max(Math.floor((i + 1) * levels.length / n), from + 1);
    out.push(Math.max(...levels.slice(from, to)));
  }
  const top = Math.max(...out) || 1;
  return out.map((v) => Math.round(v / top * 1000) / 1000);
}

function transcribingNode() {
  const node = el("div", "msg-transcribing");
  const eq = el("span", "eq");
  for (let i = 0; i < 5; i++) eq.append(el("span"));
  node.append(eq, el("span", "shimmer", "Расшифровываю…"));
  return node;
}

async function uploadVoice(blob, duration, peaks) {
  const chat = current.id, gen = feedGen;
  const feed = $("#feed");
  // До ответа сервера — голосовое из памяти браузера и «Расшифровываю…».
  const local = voiceNode({ duration, peaks }, URL.createObjectURL(blob));
  const wait = transcribingNode();
  feed.insertBefore(local, $("#feed > .thinking"));
  feed.insertBefore(wait, $("#feed > .thinking"));
  scrollDown(true);
  transcribing.add(chat);
  updateSend();
  const ext = blob.type.includes("mp4") ? "m4a" : blob.type.includes("ogg") ? "ogg" : "webm";
  const body = new FormData();
  body.append("audio", blob, "voice." + ext);
  body.append("duration", duration.toFixed(2));
  body.append("peaks", JSON.stringify(peaks));
  let events = null;
  try {
    ({ events } = await api(`/api/chats/${chat}/voice`, { method: "POST", body }));
  } catch (err) {
    alert(err.message);
  }
  transcribing.delete(chat);
  if (playing?.node === local) pauseVoice();
  URL.revokeObjectURL(local.voice.src);
  if (current?.id !== chat) return;
  if (feedGen !== gen) return openChat(chat);  // ленту перерисовали — берём журнал целиком
  local.remove();
  wait.remove();
  if (events) {
    events.forEach(render);
    count += events.length;
    scrollDown();
  }
  updateSend();
  showSize();
}

// --- Голосовые: плеер ---

const positions = new Map();  // голосовое -> секунда, где остановились
let playing = null;  // { node } — что играет сейчас

function voiceNode(ev, src) {
  const node = el("div", "msg-user msg-voice");
  const play = el("button", "voice-play");
  play.type = "button";
  const wave = el("div", "voice-wave");
  const peaks = ev.peaks?.length ? ev.peaks : Array(48).fill(0.35);
  const bars = (cls) => {
    const row = el("div", cls);
    peaks.forEach((p) => {
      const bar = el("span");
      bar.style.height = Math.max(12, p * 100) + "%";
      row.append(bar);
    });
    return row;
  };
  wave.append(bars("voice-bars"), bars("voice-bars played"));
  const time = el("span", "voice-time");
  node.append(play, wave, time);
  node.voice = { src, key: ev.id || src, duration: ev.duration || 0, play, wave, time, audio: null };
  voiceIcon(node, false);
  showProgress(node, positions.get(node.voice.key) || 0);
  play.onclick = () => (playing?.node === node ? pauseVoice() : playVoice(node));
  return node;
}

function voiceIcon(node, on) {
  const { play } = node.voice;
  play.innerHTML = `<svg width="16" height="16"><use href="#i-${on ? "pause" : "play"}"/></svg>`;
  play.title = on ? "Пауза" : "Воспроизвести";
  play.setAttribute("aria-label", play.title);
}

// Проигранная часть дорожки — цветом, остальное серым; время — где сейчас, а до старта — длина.
function showProgress(node, t) {
  const v = node.voice;
  const part = v.duration ? Math.min(1, t / v.duration) : 0;
  v.wave.style.setProperty("--played", (part * 100).toFixed(2) + "%");
  v.time.textContent = clock(t > 0 ? t : v.duration);
}

function playVoice(node) {
  pauseVoice();
  const v = node.voice;
  if (!v.audio) {
    // Новый элемент после перерисовки ленты: продолжаем с запомненного места.
    const from = positions.get(v.key) || 0;
    v.audio = new Audio(v.src + (from ? `#t=${from.toFixed(2)}` : ""));
    v.audio.onended = () => {
      positions.delete(v.key);
      if (playing?.node === node) playing = null;
      voiceIcon(node, false);
      showProgress(node, 0);
    };
  }
  playing = { node };
  voiceIcon(node, true);
  v.audio.play().catch((err) => {
    if (playing?.node === node) playing = null;
    voiceIcon(node, false);
    if (err.name !== "AbortError") alert("Не удалось воспроизвести: " + err.message);
  });
  const step = () => {
    if (playing?.node !== node) return;
    showProgress(node, v.audio.currentTime);
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

// Пауза на том же месте: при следующем «воспроизвести» звук пойдёт отсюда.
function pauseVoice() {
  if (!playing) return;
  const { node } = playing;
  const v = node.voice;
  playing = null;
  if (v.audio) {
    v.audio.pause();
    positions.set(v.key, v.audio.currentTime);
    showProgress(node, v.audio.currentTime);
  }
  voiceIcon(node, false);
}

// Ушли с сайта или свернули приложение — звук на паузу.
document.addEventListener("visibilitychange", () => { if (document.hidden) pauseVoice(); });
window.addEventListener("pagehide", pauseVoice);

function transcriptNode(ev) {
  const node = el("div", "msg-transcript");
  node.dataset.voice = ev.voice;
  const label = el("div", "transcript-label");
  label.innerHTML = '<svg width="14" height="14"><use href="#i-mic"/></svg>';
  label.append("Расшифровка голосового");
  const ask = el("div", "transcript-ask");
  const yes = el("button", "primary", "Да");
  const fix = el("button", "", "Исправить");
  const no = el("button", "", "Нет");
  yes.type = fix.type = no.type = "button";
  fix.title = "Перенести текст в поле ввода, поправить и отправить";
  yes.onclick = async () => {
    yes.disabled = no.disabled = true;
    try { await sendMessage(ev.text, [], ev.voice); } catch (err) { alert(err.message); }
    yes.disabled = no.disabled = false;
  };
  fix.onclick = () => startFix(ev);
  no.onclick = async () => {
    yes.disabled = no.disabled = true;
    try {
      await api(`/api/chats/${current.id}/voice/${ev.voice}/decline`, { method: "POST" });
      render({ kind: "voice_answer", voice: ev.voice, ok: false });
      count++;
    } catch (err) {
      alert(err.message);
      yes.disabled = no.disabled = false;
    }
  };
  ask.append(el("span", "", "Всё ли верно, отправлять?"), yes, fix, no);
  node.append(label, el("div", "transcript-text", ev.text), ask);
  return node;
}

// --- Файлы беседы ---

const IMAGE = /\.(png|jpe?g|gif|webp|avif|bmp|heic|heif|svg)$/i;

// Плитка: превью фото или значок файла с расширением, под ним только имя.
function fillTile(a, name, src) {
  a.classList.add("file-tile");
  const thumb = el("span", "file-thumb");
  const ext = name.includes(".") ? name.split(".").pop().slice(0, 5).toUpperCase() : "";
  const icon = () => {
    thumb.innerHTML = '<svg width="40" height="40"><use href="#i-file"/></svg>';
    if (ext) thumb.append(el("span", "file-ext", ext));
  };
  if (IMAGE.test(name)) {
    const img = el("img");
    img.alt = "";
    img.loading = "lazy";
    img.decoding = "async";
    img.onerror = icon;  // формат, который браузер не показывает, — обычный значок
    img.src = src;
    thumb.append(img);
  } else icon();
  a.replaceChildren(thumb, el("span", "file-name", name));
  return a;
}

// Папка (general, for_user…) — во всплывающей подсказке.
function fileTile(path) {
  const a = fileLink(path);
  a.className = "";
  a.title = path.replace(/^files\//, "");
  return fillTile(a, a.textContent, a.href);
}

const baseName = (path) => path.split("/").pop();

let chatFiles = [];

// Красный кружок с белым крестиком в углу плитки.
function deleteButton(label, onclick) {
  const button = el("button", "file-del");
  button.type = "button";
  button.title = "Удалить";
  button.setAttribute("aria-label", label);
  button.innerHTML = '<svg width="12" height="12"><use href="#i-close"/></svg>';
  button.onclick = onclick;
  return button;
}

function showFiles() {
  const query = $("#files-search").value.trim().toLowerCase();
  const items = chatFiles.filter((f) => baseName(f.path).toLowerCase().includes(query));
  const total = chatFiles.reduce((sum, f) => sum + f.size, 0);
  $("#files-total").textContent = `${chatFiles.length} · ${size(total)}`;
  $("#files-list").replaceChildren(...(items.length ? items.map((f) => {
    const li = el("li");
    const name = baseName(f.path);
    li.append(fileTile(f.path), deleteButton("Удалить " + name, async () => {
      // Файлы из general уже в библиотеке: там они остаются.
      const kept = f.path.startsWith("files/general/");
      if (!confirm(kept ? `Убрать «${name}» из беседы? В библиотеке файл останется.` : `Удалить «${name}»?`)) return;
      try {
        await api(`/api/chats/${current.id}/file?path=${encodeURIComponent(f.path)}`, { method: "DELETE" });
      } catch (err) { return alert(err.message); }
      chatFiles = chatFiles.filter((x) => x !== f);
      showFiles();
      showSize();
    }));
    return li;
  }) : [el("li", "muted", chatFiles.length ? "Ничего не найдено" : "Пока пусто")]));
}

$("#export").onclick = () => {
  const a = el("a");
  a.href = `/api/chats/${current.id}/export`;
  a.download = "";
  document.body.append(a);
  a.click();
  a.remove();
};

$("#files-open").onclick = async () => {
  try {
    chatFiles = await api(`/api/chats/${current.id}/files`);
  } catch (err) { return alert(err.message); }
  $("#files-search").value = "";
  showFiles();
  $("#files").showModal();
};
$("#files-search").oninput = showFiles;

// --- Библиотека ---

let library = [];

function libraryUrl(path, download) {
  return `/api/library/file?path=${encodeURIComponent(path)}` + (download ? "&download=true" : "");
}

// Плитка как в «Файлах беседы», под именем — беседы с этим файлом, размер и дата.
function libraryCard(f) {
  const li = el("li", "file-card");
  const name = baseName(f.path);
  const open = el("a");
  open.href = libraryUrl(f.path);
  open.target = "_blank";
  open.rel = "noopener";
  open.title = f.path;
  fillTile(open, name, libraryUrl(f.path));
  const chats = el("div", "file-chats");
  if (!f.chats.length) chats.append(el("span", "muted", "без беседы"));
  f.chats.forEach((c, i) => {
    const chat = el("a", "", c.title || "Новая беседа");
    chat.href = "#" + c.id;
    chat.title = "Открыть беседу";
    chat.onclick = (e) => {
      e.preventDefault();
      $("#library").close();
      side(false);
      openChat(c.id);
    };
    if (i) chats.append(", ");
    chats.append(chat);
  });
  const sub = el("div", "file-sub muted");
  const get = el("a", "icon");
  get.href = libraryUrl(f.path, true);
  get.download = "";
  get.title = "Скачать";
  get.setAttribute("aria-label", "Скачать " + name);
  get.innerHTML = '<svg width="16" height="16"><use href="#i-download"/></svg>';
  if (iosApp) {
    const share = (e) => { e.preventDefault(); shareFile(libraryUrl(f.path, true), name); };
    open.onclick = get.onclick = share;
  }
  const day = new Date(f.mtime * 1000).toLocaleDateString("ru", { day: "2-digit", month: "2-digit", year: "2-digit" });
  const info = el("span", "", `${size(f.size)} · ${day}`);
  info.title = when(f.mtime);
  sub.append(info, get);
  li.append(open, chats, sub, deleteButton("Удалить " + name, async () => {
    const where = f.chats.map((c) => `«${c.title || "Новая беседа"}»`).join(", ");
    if (!confirm(`Удалить «${name}» насовсем?` + (where ? ` Он пропадёт и из бесед: ${where}.` : ""))) return;
    try {
      await api(`/api/library/file?path=${encodeURIComponent(f.path)}`, { method: "DELETE" });
    } catch (err) { return alert(err.message); }
    library = library.filter((x) => x !== f);
    showLibrary();
  }));
  return li;
}

function showLibrary() {
  const query = $("#library-search").value.trim().toLowerCase();
  const items = library.filter((f) => f.path.toLowerCase().includes(query));
  const total = library.reduce((sum, f) => sum + f.size, 0);
  $("#library-total").textContent = `${library.length} · ${size(total)}`;
  $("#library-list").replaceChildren(...(items.length ? items.map(libraryCard)
    : [el("li", "muted", library.length ? "Ничего не найдено" : "Пока пусто: сюда попадают вложения и файлы от модели")]));
}

$("#library-open").onclick = async () => {
  try {
    library = await api("/api/library");
  } catch (err) { return alert(err.message); }
  $("#library-search").value = "";
  showLibrary();
  $("#library").showModal();
};
$("#library-search").oninput = showLibrary;

// --- Пользователи ---

// Галочки «Доступ» у каждой сети свои (ключи — как users.ACCESS на сервере).
const ACCESS = {
  claude: [
    ["bash", "Запуск команд", "Bash в песочнице: распаковка архивов, скрипты, конвертация файлов."],
    ["write", "Изменение файлов", "Write и Edit: создавать и править файлы в files/. Без этого модель их только читает; черновики в корне рабочей папки остаются доступны."],
    ["memory", "Изменение памяти", "Вести MEMORY.md: общую память пользователя и заметки беседы. Без этого модель память только читает."],
    ["network", "Интернет для команд", "Команды Bash (curl, pip, git) могут выходить в сеть."],
    ["web", "Веб-поиск и сайты", "WebSearch и WebFetch: поиск и чтение страниц. Сеть для команд они не открывают."],
    ["skills", "Скиллы", "Готовые инструкции и скрипты для задач: документы, таблицы, PDF. Работают в песочнице с теми же правами; скриптам обычно нужен «Запуск команд»."],
    ["all_chats", "Файлы других бесед", "Модель видит рабочие папки всех бесед пользователя. Без этого — только текущую беседу и общую память."],
  ],
  codex: [
    ["bash", "Запуск команд", "Команды в песочнице: скрипты, архивы, конвертация файлов. Без этого отключены инструменты запуска команд; чтение документов и работа с файлами могут быть ограничены."],
    ["write", "Изменение файлов", "Создавать и менять файлы в рабочих папках. Без этого все файлы, включая память, доступны только для чтения."],
    ["memory", "Изменение памяти", "Менять общую MEMORY.md и заметки доступных бесед. Можно запретить изменение памяти, оставив запись остальных файлов. Требует «Изменение файлов»."],
    ["network", "Интернет для команд", "Команды Codex (curl, pip, git) могут выходить в сеть."],
    ["web", "Веб-поиск", "Встроенный поиск Codex (web_search)."],
    ["multi_agent", "Вспомогательные агенты", "Разрешить Codex делить задачу между дополнительными агентами. Они расходуют ту же подписку и наследуют ограничения доступа."],
    ["all_chats", "Файлы других бесед", "Модель видит рабочие папки всех бесед пользователя. Без этого — только текущую беседу и общую память."],
  ],
};
const ACCESS_NOTE = { codex: "Списки доменов ограничивают интернет для команд. Они не ограничивают встроенный веб-поиск, приложения и MCP. Команды выполняются в песочнице без запроса дополнительных прав." };
// Опасная зона: действия вне песочницы. Галочки нужны с Bash, без него неактивны.
const ACCESS_DANGER = {
  claude: [
    ["unsandboxed", "Команды вне песочницы",
      "Перечисленные команды выполняются на сервере напрямую, с правами его хозяина. Наружу выходит только одиночный вызов: «docker ps» — да, «docker ps && cat …» или «… | docker …» целиком остаются в песочнице. Команда без аргументов совпадает с любыми аргументами: «docker» — это и «docker run …».",
      [["unsandboxed_commands", "Команды вне песочницы", "docker ps\nsystemctl --user status"]]],
  ],
};
const DANGER_NOTE = "Модель сможет выйти из песочницы: прочитать токены подписок, базу сайта и данные других пользователей, менять любые файлы хозяина сервера. Давайте только себе и только точные команды: «python», «bash», «sh» или «docker» без аргументов — это полный доступ к серверу.";

// Уточнения под галочкой сети: видны, только пока она стоит. Ключи — как users.LISTS.
const ACCESS_MORE = {
  codex: {
    network: (a) => [
      subOption(a, "site_allow", "Белый список доменов для команд",
        "Команды подключаются только к перечисленным доменам. Пустой список запрещает все внешние подключения. Используется экспериментальный сетевой прокси Codex.",
        [["allowed_sites", "Разрешённые домены", "pypi.org\nfiles.pythonhosted.org"]]),
      subOption(a, "site_deny", "Чёрный список доменов для команд",
        "Запрет подключения к этим доменам. Если домен есть в обоих списках, действует запрет.",
        [["denied_sites", "Запрещённые домены", "example.com\n*.example.com"]]),
      el("p", "muted", "Адрес по одному на строку. example.com — сам домен; *.example.com — поддомены. На веб-поиск эти списки не действуют."),
    ],
    web: (a) => [subOption(a, "web_cached", "Только кэш поиска",
      "Использовать сохранённые страницы вместо живого доступа веб-поиска. Данные могут быть устаревшими. Интернет для команд настраивается отдельно.", [])],
  },
  claude: {
    web: (a) => [
      subOption(a, "site_allow", "Белый список сайтов",
        "Модель открывает только перечисленные сайты. Поиск находит любые, но прочитать страницу вне списка нельзя. Пустой список — сайты не открываются вовсе.",
        [["allowed_sites", "Разрешённые сайты", "docs.python.org\n*.wikipedia.org"]]),
      subOption(a, "site_deny", "Чёрный список сайтов",
        "Перечисленные сайты закрыты, остальные открыты. Запрет действует и на команды с интернетом (curl, pip).",
        [["denied_sites", "Запрещённые сайты", "vk.com\n*.vk.com"]]),
      el("p", "muted", "Адрес по одному на строку. example.com — только этот адрес, без www; *.example.com — все поддомены, но не сам example.com."),
    ],
    skills: (a) => {
      const names = Object.keys(card.skills);
      if (!names.length) return [el("p", "muted", "Скиллов на сайте нет: отметьте их в «Скиллы сайта».")];
      const list = el("div", "skills");
      for (const name of names) {
        const option = el("div", "option");
        option.append(checkbox(name, !a.skills_off.includes(name), (v) => {
          a.skills_off = v ? a.skills_off.filter((x) => x !== name) : [...a.skills_off, name];
        }), el("div", "muted", card.skills[name]));
        list.append(option);
      }
      return [list, el("p", "muted", "Снятый скилл модель видит в списке, но запустить не может.")];
    },
    bash: (a) => [
      subOption(a, "commands", "Разрешение и запрет отдельных команд",
        "«git push» совпадает и с самой командой, и с любыми аргументами. Если список разрешённых не пуст, запускать можно только их; команды, которые лишь читают (ls, cat, grep), CLI разрешает всегда. Запрещённые нельзя никогда, в том числе внутри «a && b». Запрет — защита от случайности: ту же команду можно вызвать через python или sh, если они разрешены.",
        [["allowed_commands", "Разрешённые команды (пусто — все, кроме запрещённых)", "git status\npython"],
         ["denied_commands", "Запрещённые команды", "rm\ngit push"]]),
    ],
  },
};
let card = null, cardTab = "info";
const mobile = matchMedia("(max-width: 760px)");

// Шаг окна настроек. На компьютере видно всё сразу, на телефоне — один шаг:
// list (аккаунт и участники) → user (разделы участника) → section (раздел).
function setLevel(level) {
  $("#settings").dataset.level = level;
  const title = level === "user" ? card.user.name
    : level === "section" ? (card ? TABS[cardTab] : $("#card h3")?.textContent) : "Настройки";
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

// Галочка с описанием; пока она стоит, под ней поля списков [ключ, подпись, пример].
function subOption(access, key, name, about, lists, disabled) {
  const option = el("div", "option");
  option.append(checkbox(name, access[key], (v) => { access[key] = v; showCard(); }, disabled), el("div", "muted", about));
  if (access[key] && !disabled) {
    for (const [list, label, example] of lists) {
      option.append(listField(label, access[list], (v) => { access[list] = v; }, example));
    }
  }
  return option;
}

function listField(label, list, onchange, placeholder) {
  const l = el("label", "list", label);
  const area = el("textarea");
  area.rows = 3;
  area.placeholder = placeholder;
  area.value = list.join("\n");
  area.oninput = () => { onchange(area.value.split("\n").map((x) => x.trim()).filter(Boolean)); changed(); };
  l.append(area);
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
      row("Диск", size(stats.disk.total)), row("Telegram", user.tg ? user.tg.name : "не привязан"));
    for (const engine of Object.keys(card.catalog)) {
      const x = stats.limits[engine];
      for (const w of Object.keys(WINDOW)) {
        t.append(row(engineLabel(engine, `${NAME[engine]}, ${WINDOW[w]}`),
          `части использовано ${percent(x[w].share)}: потратил ${percent(x[w].used)} подписки из ${x[w].limit}%`
          + (x[w].resets_at ? `, сброс ${when(x[w].resets_at)}` : "")));
      }
      t.append(row(engineLabel(engine, `${NAME[engine]}, токенов всего`), `вход ${x.tokens.in.toLocaleString("ru")}, выход ${x.tokens.out.toLocaleString("ru")}`));
      const g = card.subscription[engine];
      if (g) t.append(row(engineLabel(engine, `${NAME[engine]}, подписка целиком`),
        Object.keys(WINDOW).filter((w) => g[w]).map((w) => `${WINDOW[w]} ${g[w].percent}%`).join(", ")));
    }
    panel.append(t, el("p", "muted", "Расход — сдвиг общих процентов подписки за время запросов пользователя, точность около 1%. Использование подписки мимо сайта (бот, свой CLI) в момент запроса тоже попадает в расход. «Части использовано» учитывает и чужой расход: если подписки осталось меньше, чем положено пользователю по лимиту, ему доступен только остаток."));
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
      el("p", "muted", "При превышении пользователь не сможет загружать новые файлы и увидит предупреждение. Писать сообщения он сможет."),
      numberField("Предлагать сжать беседу, когда сессия больше, МБ (0 — не предлагать):", settings.compact_mb, (v) => { settings.compact_mb = v; }),
      el("p", "muted", "Сессию модель перечитывает на каждом ответе: чем она больше, тем быстрее уходят лимиты. "
        + "Над полем ввода появится кнопка «Сжать беседу». Сжатие есть только у Claude, Codex сжимает беседы сам."));
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
        const disabled = engine === "codex" && key === "memory" && !access.write;
        option.append(checkbox(name, !disabled && access[key], (v) => { access[key] = v; showCard(); }, disabled), el("div", "muted", about));
        const more = ACCESS_MORE[engine]?.[key];
        if (more && access[key]) {
          const sub = el("div", "sub");
          sub.append(...more(access));
          option.append(sub);
        }
        box.append(option);
      }
      if (ACCESS_DANGER[engine]) {
        const danger = el("div", "danger-zone");
        danger.append(el("h4", "", "Опасная зона"), el("p", "", DANGER_NOTE));
        for (const [key, name, about, lists] of ACCESS_DANGER[engine]) {
          danger.append(subOption(access, key, name, about, lists, !access.bash));
        }
        if (!access.bash) danger.append(el("p", "muted", "Нужен «Запуск команд»."));
        box.append(danger);
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

// Общие настройки сервиса (data/settings.json). Форма строится по списку с сервера:
// сколько параметров и как они называются — не важно.
async function openGeneral() {
  let list;
  try { list = await api("/api/settings"); } catch (e) { alert(e.message); return; }
  card = null;
  document.querySelectorAll("#users-list button").forEach((b) => b.classList.remove("active"));
  const form = el("form", "general-settings");
  form.append(el("h3", "", "Общие настройки"),
    el("p", "muted", "Хранятся в data/settings.json и применяются сразу, без перезапуска. "
      + ".env задаёт только значения при первом запуске."));
  const changed = {};
  const rows = [];  // [строка, параметр] — для показа по условию shown_if
  const valueOf = (name) => changed[name] ?? list.find((x) => x.name === name)?.value;
  const showDependent = () => rows.forEach(([row, p]) => {
    if (p.shown_if) row.hidden = !(valueOf(p.shown_if.name) || []).includes(p.shown_if.value);
  });
  const bar = el("div", "save-bar");
  bar.append(el("button", "primary", "Сохранить"));
  bar.hidden = true;
  for (const p of list) {
    const row = el("label", "setting");
    row.title = p.name;  // техническое имя — в подсказке, на экране русское название
    let input;
    if (p.type === "list") {
      // Несколько значений: нажатие включает и выключает галочку справа.
      const picked = new Set(p.value);
      row.className = "setting setting-list";
      const box = el("div", "toggles");
      for (const c of p.choices) {
        const b = el("button", "toggle" + (picked.has(c.value) ? " on" : ""));
        b.type = "button";
        b.setAttribute("aria-pressed", String(picked.has(c.value)));
        b.append(el("span", "", c.label), el("span", "tick"));
        b.querySelector(".tick").innerHTML = '<svg width="18" height="18"><use href="#i-check"/></svg>';
        b.onclick = () => {
          if (picked.has(c.value)) picked.delete(c.value); else picked.add(c.value);
          b.classList.toggle("on", picked.has(c.value));
          b.setAttribute("aria-pressed", String(picked.has(c.value)));
          const now = p.choices.map((x) => x.value).filter((v) => picked.has(v));
          if (JSON.stringify(now) !== JSON.stringify(p.value)) changed[p.name] = now;
          else delete changed[p.name];
          row.classList.toggle("changed", p.name in changed);
          bar.hidden = !Object.keys(changed).length;
          showDependent();
        };
        box.append(b);
      }
      row.append(el("span", "setting-name", p.title), box);
      if (p.about) row.append(el("small", "muted", p.about));
      form.append(row);
      rows.push([row, p]);
      continue;
    }
    if (p.choices.length) {
      // Только определённые значения — выбор из списка.
      input = el("select");
      input.append(...p.choices.map((c) => { const o = el("option", "", c.label); o.value = c.value; return o; }));
    } else {
      input = el("input");
      input.type = p.secret ? "password" : p.type === "int" ? "number" : "text";
      if (p.type === "int") { input.min = "0"; input.step = "1"; }
      input.autocomplete = "off";
      input.spellcheck = false;
    }
    input.value = p.value;
    input.oninput = input.onchange = () => {
      if (input.value !== String(p.value)) changed[p.name] = input.value;
      else delete changed[p.name];
      row.classList.toggle("changed", p.name in changed);
      bar.hidden = !Object.keys(changed).length;
    };
    row.append(el("span", "setting-name", p.title), input);
    if (p.about) row.append(el("small", "muted", p.about));
    if (p.shown_if) row.classList.add("dependent");
    form.append(row);
    rows.push([row, p]);
  }
  showDependent();
  form.append(bar);
  form.onsubmit = async (e) => {
    e.preventDefault();
    try { await api("/api/settings", { method: "PUT", json: changed }); } catch (err) { alert(err.message); return; }
    await openGeneral();  // значения после сохранения — как их понял сервер
    refreshMe();  // могли поменяться сети или бот Telegram
  };
  $("#card").replaceChildren(form);
  setLevel("section");
}

$("#general-open").onclick = openGeneral;

// Скиллы аккаунта claude.ai, выложенные на сайт; пользоваться ими можно по галочке «Скиллы».
$("#skills-open").onclick = async () => {
  let data;
  try { data = await api("/api/skills"); } catch (e) { alert(e.message); return; }
  card = null;
  document.querySelectorAll("#users-list button").forEach((b) => b.classList.remove("active"));
  const on = new Set(Object.keys(data.site));
  const form = el("form", "site-skills");
  form.append(el("h3", "", "Скиллы сайта"),
    el("p", "muted", "Скиллы вашего аккаунта claude.ai. Отмеченные доступны всем, у кого в «Доступе» стоит галочка «Скиллы». Обновления из аккаунта подхватываются сами."));
  for (const [name, about] of Object.entries(data.account)) {
    const option = el("div", "option");
    option.append(checkbox(name, on.has(name), (v) => { if (v) on.add(name); else on.delete(name); }), el("div", "muted", about));
    form.append(option);
  }
  if (!Object.keys(data.account).length) form.append(el("p", "muted", "Скиллов аккаунта нет: их скачивает Claude Code хозяина сервера в ~/.claude/skills/synced."));
  const own = Object.keys(data.site).filter((name) => !(name in data.account));
  if (own.length) form.append(el("p", "muted", "Свои скиллы из data/plugin/skills: " + own.join(", ")));
  form.append(el("button", "primary", "Сохранить"));
  form.onsubmit = async (e) => {
    e.preventDefault();
    try { await api("/api/skills", { method: "PUT", json: [...on] }); alert("Сохранено"); } catch (err) { alert(err.message); }
  };
  $("#card").replaceChildren(form);
  setLevel("section");
};

$("#settings-open").onclick = async () => {
  pauseVoice();
  side(false);
  $("#account-name").textContent = me.name;
  $("#account-role").textContent = me.is_admin ? "Администратор" : "Участник";
  if (me.is_admin) await loadUsers();
  setLevel("list");
  $("#settings").showModal();
};

api("/api/me").then((user) => { me = user; start(); }).catch(() => {});
