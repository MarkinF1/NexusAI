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
// Правило: любое скачивание — ссылка с onclick → shareFile при iosApp, или saveFile(url, name).
const iosApp = navigator.standalone === true;

// Скачать по кнопке (не по ссылке): на iOS-приложении — «Поделиться», иначе обычная загрузка.
function saveFile(url, name) {
  if (iosApp) return shareFile(url, name);
  const a = el("a");
  a.href = url;
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
}

// Файл беседы открывается в окне просмотра; Ctrl/⌘-щелчок — как обычная ссылка.
function fileLink(path, f) {
  const name = path.split("/").pop();
  const a = el("a", "chip file", name);
  a.href = `/api/chats/${current.id}/file?path=${encodeURIComponent(path)}`;
  a.download = "";
  a.onclick = (e) => {
    if (e.ctrlKey || e.metaKey || e.shiftKey || e.button) return;
    e.preventDefault();
    viewChatFile(path, f);
  };
  return a;
}

function viewChatFile(path, f) {
  const base = `/api/chats/${current.id}`, q = `?path=${encodeURIComponent(path)}`;
  const v = `&v=${f?.mtime ? Math.round(f.mtime * 1000) : Date.now()}`;  // копии на сервере — по версии файла
  viewFile({ name: baseName(path), url: `${base}/file${q}`, large: `${base}/thumb${q}&size=1600${v}`,
    preview: `${base}/preview${q}${v}`, size: f?.size });
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

// --- Окно просмотра файла ---
// Во весь экран: сверху «Назад» и имя, снизу «Поделиться» (и «Скачать» на компьютере).
// Картинки, PDF, документы Office (PDF с сервера), Markdown, JSON, таблицы CSV, текст и
// код, аудио и видео; остальное — значок файла.

const VIEW_IMAGE = /\.(png|jpe?g|gif|webp|avif|bmp|ico|svg|heic|heif|tiff?)$/i;
const VIEW_OFFICE = /\.(docx?|odt|rtf|xlsx?|ods|pptx?|odp)$/i;
const VIEW_AUDIO = /\.(mp3|m4a|ogg|oga|opus|wav|flac|aac)$/i;
const VIEW_VIDEO = /\.(mp4|m4v|mov|webm|mkv)$/i;
const VIEW_TEXT = /\.(txt|text|log|md|markdown|json|jsonl|ndjson|csv|tsv|py|pyw|js|mjs|cjs|ts|jsx|tsx|html?|css|scss|less|xml|svg|ya?ml|toml|ini|cfg|conf|env|sh|bash|zsh|fish|ps1|bat|cmd|sql|c|cc|cpp|cxx|h|hh|hpp|cs|java|kt|kts|go|rs|rb|php|pl|lua|r|swift|dart|scala|vue|svelte|pro|pri|ui|qml|cmake|mk|gradle|properties|srt|vtt|tex|bib|diff|patch|gitignore|dockerfile|makefile)$/i;
const VIEW_TEXT_MAX = 5 * 2 ** 20;  // больше — тяжело для телефона, только «Поделиться»
const canShareFiles = !!navigator.canShare?.({ files: [new File([""], "a.txt", { type: "text/plain" })] });

let viewing = null;  // { token, cleanup } открытого файла

function viewKind(name) {
  if (/\.svg$/i.test(name)) return "image";  // SVG — картинкой, текстом его не смотрят
  if (VIEW_IMAGE.test(name)) return "image";
  if (/\.pdf$/i.test(name)) return "pdf";
  if (VIEW_OFFICE.test(name)) return "office";
  if (/\.(md|markdown)$/i.test(name)) return "markdown";
  if (/\.json$/i.test(name)) return "json";
  if (/\.(csv|tsv)$/i.test(name)) return "table";
  if (VIEW_AUDIO.test(name)) return "audio";
  if (VIEW_VIDEO.test(name)) return "video";
  if (VIEW_TEXT.test(name) || /^(dockerfile|makefile|readme|license)$/i.test(name)) return "text";
  return "other";
}

// file: { name, url — содержимое, download — для «Поделиться»/«Скачать»,
//         large — крупная копия фото (HEIC, TIFF), preview — PDF документа Office, size }
function viewFile(file) {
  closeViewer();
  const dialog = $("#viewer");
  const token = {};
  viewing = { token, cleanup: [] };
  $("#viewer-name").textContent = file.name;
  $("#viewer-name").title = file.name;
  const share = $("#viewer-share"), download = $("#viewer-download");
  share.hidden = !iosApp && !canShareFiles;
  download.hidden = iosApp;
  share.onclick = () => shareFile(file.download || file.url, file.name);
  download.onclick = () => saveFile(file.download || file.url, file.name);
  const body = $("#viewer-body");
  body.className = "";
  body.replaceChildren(el("div", "viewer-wait muted", "Загрузка…"));
  dialog.showModal();
  dialog.scrollTop = 0;
  const kind = viewKind(file.name);
  body.dataset.kind = kind;
  const live = () => viewing?.token === token;
  const fail = (note) => { if (live()) viewIcon(body, file, note); };
  const shows = {
    image: () => viewImage(body, file, fail),
    pdf: () => viewPdf(body, file.url, live, fail),
    office: () => file.preview ? viewPdf(body, file.preview, live, () => fail("Предпросмотр этого документа недоступен"))
      : fail(),
    audio: () => viewMedia(body, "audio", file.url, fail),
    video: () => viewMedia(body, "video", file.url, fail),
    markdown: () => viewText(body, file, live, fail, (text) => {
      const div = el("div", "viewer-doc msg-text");
      div.innerHTML = DOMPurify.sanitize(marked.parse(text));
      div.querySelectorAll("a[href]").forEach((a) => { a.target = "_blank"; a.rel = "noopener"; });
      return div;
    }),
    json: () => viewText(body, file, live, fail, (text) => {
      try { text = JSON.stringify(JSON.parse(text), null, 2); } catch { /* битый JSON — как есть */ }
      return el("pre", "viewer-code", text);
    }),
    table: () => viewText(body, file, live, fail, (text) => csvTable(text, /\.tsv$/i.test(file.name) ? "\t" : null)),
    text: () => viewText(body, file, live, fail, (text) => el("pre", "viewer-code", text)),
    other: () => fail(),
  };
  shows[kind]();
}

function closeViewer() {
  if (!viewing) return;
  for (const done of viewing.cleanup) { try { done(); } catch { /* уже закрыто */ } }
  viewing = null;
  const body = $("#viewer-body");
  body.querySelectorAll("video, audio").forEach((m) => { m.pause(); m.removeAttribute("src"); m.load(); });
  body.replaceChildren();
}

function viewIcon(body, file, note) {
  const box = el("div", "viewer-icon");
  const ext = file.name.includes(".") ? file.name.split(".").pop().slice(0, 5).toUpperCase() : "";
  const icon = el("span", "file-thumb");
  icon.innerHTML = '<svg width="72" height="72"><use href="#i-file"/></svg>';
  if (ext) icon.append(el("span", "file-ext", ext));
  box.append(icon, el("div", "viewer-icon-name", file.name));
  if (file.size !== undefined) box.append(el("div", "muted", size(file.size)));
  const how = $("#viewer-share").hidden ? "Скачайте его и откройте в другом приложении." : "Откройте его через «Поделиться» в другом приложении.";
  box.append(el("p", "muted", note || "Этот формат здесь не показать. " + how));
  body.className = "";
  body.replaceChildren(box);
}

function viewImage(body, file, fail) {
  const img = el("img", "viewer-image");
  img.alt = file.name;
  img.decoding = "async";
  let tried = false;
  img.onerror = () => {
    // HEIC и TIFF браузер не показывает — берём крупную копию с сервера.
    if (!tried && file.large) { tried = true; img.src = file.large; } else fail();
  };
  img.onload = () => body.replaceChildren(img);
  // Нажатие — в натуральную величину и обратно; крупное фото тогда прокручивается.
  img.onclick = () => body.classList.toggle("zoomed");
  img.src = file.url;
}

function viewMedia(body, tag, url, fail) {
  const media = el(tag, "viewer-" + tag);
  media.controls = true;
  media.playsInline = true;
  media.preload = "metadata";
  media.onerror = () => fail("Браузер не умеет проигрывать этот формат");
  media.src = url;
  body.replaceChildren(media);
}

async function viewText(body, file, live, fail, render) {
  let text;
  try {
    const r = await fetch(file.url);
    if (r.status === 401) { closeViewer(); $("#viewer").close(); showLogin(); return; }
    if (!r.ok) throw new Error(r.status);
    const length = +r.headers.get("content-length");
    if (length > VIEW_TEXT_MAX) return fail(`Файл большой (${size(length)}) для просмотра здесь.`);
    const bytes = await r.arrayBuffer();
    if (bytes.byteLength > VIEW_TEXT_MAX) return fail(`Файл большой (${size(bytes.byteLength)}) для просмотра здесь.`);
    try {
      text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    } catch {
      text = new TextDecoder("windows-1251").decode(bytes);  // старые файлы из Windows
    }
  } catch { return fail("Не удалось загрузить файл"); }
  if (!live()) return;
  body.replaceChildren(render(text));
}

// CSV/TSV: разделитель — то, чего в первой строке больше; кавычки по RFC 4180.
function csvTable(text, sep) {
  const first = text.slice(0, text.indexOf("\n") + 1 || undefined);
  sep ||= [",", ";", "\t"].map((s) => [s, first.split(s).length]).sort((a, b) => b[1] - a[1])[0][0];
  const rows = [];
  let row = [], cell = "", quoted = false;
  for (let i = 0; i < text.length && rows.length <= 2000; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') { cell += '"'; i++; } else if (c === '"') quoted = false; else cell += c;
    } else if (c === '"' && !cell) quoted = true;
    else if (c === sep) { row.push(cell); cell = ""; }
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(cell); rows.push(row); row = []; cell = "";
    } else cell += c;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }
  const wrap = el("div", "viewer-doc msg-text");
  const table = el("table");
  rows.slice(0, 2000).forEach((cells, i) => {
    const tr = el("tr");
    cells.forEach((v) => tr.append(el(i ? "td" : "th", "", v)));
    table.append(tr);
  });
  wrap.append(table);
  if (rows.length > 2000) wrap.append(el("p", "muted", "Показаны первые 2000 строк"));
  return wrap;
}

// PDF — через pdf.js: встроенный просмотрщик в окне сайта есть не везде (iPhone).
// Страницы рисуются, когда доходят до экрана.
let pdfjs = null;
async function viewPdf(body, url, live, fail) {
  let doc;
  try {
    if (!pdfjs) {
      pdfjs = await import("/vendor/pdfjs/pdf.min.mjs");
      pdfjs.GlobalWorkerOptions.workerSrc = "/vendor/pdfjs/pdf.worker.min.mjs";
    }
    const task = pdfjs.getDocument({ url, isEvalSupported: false, cMapUrl: "/vendor/pdfjs/cmaps/",
      cMapPacked: true, standardFontDataUrl: "/vendor/pdfjs/standard_fonts/" });
    viewing?.cleanup.push(() => task.destroy());
    doc = await task.promise;
  } catch (err) {
    if (err?.name === "PasswordException") return fail("PDF защищён паролем — здесь его не открыть.");
    return fail("Не удалось открыть PDF");
  }
  if (!live()) return;
  const pages = el("div", "viewer-pages");
  body.replaceChildren(pages);
  const first = (await doc.getPage(1)).getViewport({ scale: 1 });
  const observer = new IntersectionObserver((entries) => {
    for (const e of entries) if (e.isIntersecting) { observer.unobserve(e.target); drawPage(doc, e.target, live); }
  }, { root: body, rootMargin: "100% 0px" });
  viewing.cleanup.push(() => observer.disconnect());
  for (let n = 1; n <= doc.numPages; n++) {
    const page = el("div", "viewer-page");
    page.dataset.page = n;
    page.style.aspectRatio = `${first.width} / ${first.height}`;  // до отрисовки — по первой странице
    pages.append(page);
    observer.observe(page);
  }
}

async function drawPage(doc, holder, live) {
  const page = await doc.getPage(+holder.dataset.page);
  if (!live()) return;
  const base = page.getViewport({ scale: 1 });
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  const viewport = page.getViewport({ scale: (holder.clientWidth / base.width) * ratio });
  const canvas = el("canvas");
  canvas.width = Math.floor(viewport.width);
  canvas.height = Math.floor(viewport.height);
  holder.style.aspectRatio = `${base.width} / ${base.height}`;
  holder.replaceChildren(canvas);
  await page.render({ canvasContext: canvas.getContext("2d"), viewport }).promise.catch(() => {});
}

// Событие close приходит позже: если окно уже открыли снова с другим файлом, его не трогаем.
$("#viewer").addEventListener("close", () => { if (!$("#viewer").open) closeViewer(); });
$("#viewer-back").onclick = () => $("#viewer").close();

function askShare(file) {
  const box = $("#file-share");
  $("#file-share-name").textContent = file.name;
  $("#file-share-go").onclick = () => {
    navigator.share({ files: [file] }).catch((err) => { if (err.name !== "AbortError") alert(err.message); });
  };
  box.showModal();
}

// --- Уведомления Web Push: «Ответ готов» на этом устройстве ---
// На iPhone — только у сайта на экране «Домой» (iOS 16.4+) и только по https.

const pushReady = "serviceWorker" in navigator && "PushManager" in window && window.isSecureContext;
const swReady = pushReady ? navigator.serviceWorker.register("/sw.js").catch(() => null) : Promise.resolve(null);

// Нажали на уведомление, а приложение уже открыто — переходим к беседе.
if (pushReady) navigator.serviceWorker.addEventListener("message", (e) => {
  const id = e.data?.open;
  if (id && me) openChat(id);
});

function urlKey(b64) {
  const raw = atob(b64.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - b64.length % 4) % 4));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

function deviceName() {
  const ua = navigator.userAgent;
  return /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android"
    : /Mac/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows" : "Браузер";
}

// Галочка под именем в «Настройках». Включение просит разрешение, подписывает устройство и
// присылает пробное уведомление; выключение отписывает. Не поддерживается — галочка серая и почему.
async function showPush() {
  const box = $("#push-account");
  const l = el("label", "check");
  const input = el("input");
  input.type = "checkbox";
  l.append(input, "Уведомлять о новых сообщениях");
  const hint = (text) => { input.disabled = true; box.replaceChildren(l, el("div", "muted", text)); };
  box.replaceChildren(l);
  if (!pushReady) {
    return hint(!window.isSecureContext ? "Нужен вход на сайт по https."
      : /iPhone|iPad/.test(navigator.userAgent) && !iosApp
        ? "На iPhone — только из приложения: «Поделиться» → «На экран Домой» (iOS 16.4+)."
        : "Этот браузер не поддерживает уведомления.");
  }
  if (Notification.permission === "denied") return hint("Запрещены в настройках браузера или телефона.");
  const reg = await swReady;
  let sub = await reg?.pushManager.getSubscription();
  input.checked = !!sub;
  input.onchange = async () => {
    input.disabled = true;
    try {
      if (input.checked) {
        // Разрешение — сразу по нажатию: после await iOS сочтёт запрос не пользовательским.
        if (await Notification.requestPermission() !== "granted") return showPush();
        const { key } = await api("/api/push/key");
        sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlKey(key) });
        await api("/api/push", { method: "POST", json: { subscription: sub.toJSON(), label: deviceName() } });
        api("/api/push/test", { method: "POST" }).catch(() => {});  // пробное — видно, что работает
      } else if (sub) {
        await api("/api/push/remove", { method: "POST", json: { endpoint: sub.endpoint } });
        await sub.unsubscribe();
        sub = null;
      }
    } catch (err) {
      alert(err.message);
      input.checked = !!sub;
    }
    input.disabled = false;
  };
}

// Свёрнутая вкладка или приложение — не «смотрит» беседу: закрываем поток событий, чтобы
// сервер прислал уведомление. Вернулись — перечитываем беседу и снова подключаемся.
let resumeChat = null;
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    if (stream && current) {
      stream.close();
      stream = null;
      resumeChat = current.id;
    }
  } else if (resumeChat) {
    const id = resumeChat;
    resumeChat = null;
    if (current?.id === id) openChat(id);
  }
});

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

// Ярлык iPhone запускает корневой адрес; последнюю беседу храним отдельно для каждого аккаунта.
function lastChat(id) {
  const key = `nexus:last-chat:${me.id}`;
  try {
    if (!arguments.length) return localStorage.getItem(key) || "";
    if (id) localStorage.setItem(key, id);
    else localStorage.removeItem(key);
  } catch {
    // Запрет хранилища в браузере не должен мешать открывать беседы.
  }
  return "";
}

async function start() {
  $("#login").hidden = true;
  $("#main").hidden = false;
  me = await api("/api/me");  // после входа в me только имя: сети и лимиты берём здесь
  $("#me").textContent = me.name;
  $("#settings").classList.toggle("admin", me.is_admin);
  models = await api("/api/models");
  showMe();
  showTg();
  const chats = await loadChats();
  const saved = lastChat();
  const remembered = chats.some((chat) => chat.id === saved) ? saved : "";
  if (saved && !remembered) lastChat("");
  const id = location.hash.slice(1) || remembered;
  if (id && await openChat(id)) return;
  showEmpty();
}

// Пустой экран сразу предлагает начать беседу с одной из доступных сетей.
function showEmpty() {
  $("#title").textContent = "";
  const box = el("div", "empty");
  box.append(el("p", "empty-eyebrow", "НОВАЯ БЕСЕДА"),
    el("h2", "", "С кем поговорим?"),
    el("p", "empty-intro", "Выберите помощника и начните с того, что вам важно."));
  const list = el("div", "empty-engines");
  const descriptions = {
    claude: "Обсудить идею, разобраться в сложном или поработать с текстом.",
    codex: "Написать код, найти решение или поработать над проектом.",
  };
  for (const engine of me.engines) {
    const b = el("button", "welcome-card");
    b.type = "button";
    b.dataset.engine = engine;
    const mark = el("span", "welcome-mark");
    mark.append(logo(engine));
    const body = el("span", "welcome-body");
    body.append(el("span", "welcome-name", NAME[engine] || engine),
      el("span", "welcome-about", descriptions[engine] || "Помощник для ваших задач."));
    const action = el("span", "welcome-action", "Начать беседу");
    const arrow = el("span", "welcome-arrow", "→");
    arrow.setAttribute("aria-hidden", "true");
    action.append(arrow);
    b.append(mark, body, action);
    b.onclick = async () => {
      b.disabled = true;
      try { await newChat(engine); } finally { b.disabled = false; }
    };
    list.append(b);
  }
  box.append(me.engines.length ? list : el("p", "empty-unavailable", "Пока нет подключённых помощников. Попросите администратора проверить подключение."));
  if (me.engines.length) box.append(el("p", "empty-hint", "Модель можно выбрать внутри беседы."));
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
  return list;
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
  lastChat(id);
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
  return true;
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
    lastChat("");
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

// Подряд идущие команды модели — одна группа: видна последняя строкой, «Ещё N»
// раскрывает весь список, и в раскрытом виде каждая команда показана целиком.
function toolLine(ev) {
  const feed = $("#feed");
  const thinking = $("#feed > .thinking");
  let group = thinking ? thinking.previousElementSibling : feed.lastElementChild;
  if (!group?.classList.contains("msg-tools")) {
    group = el("div", "msg-tools");
    const more = el("button", "tools-more");
    more.type = "button";
    more.onclick = () => toggleTools(group);
    group.append(more);
    feed.insertBefore(group, thinking);
  }
  const line = el("div", "msg-tool");
  line.append(el("b", "", ev.name), " " + ev.input);
  // Одну команду раскрывает щелчок по ней; выделение текста для копирования — не щелчок.
  line.onclick = () => { if (getSelection().isCollapsed) toggleTools(group); };
  group.append(line);
  toolsLabel(group);
}

function toggleTools(group) {
  group.classList.toggle("open");
  toolsLabel(group);
}

function toolsLabel(group) {
  const count = group.querySelectorAll(".msg-tool").length;
  const more = group.querySelector(".tools-more");
  const open = group.classList.contains("open");
  more.hidden = count < 2;
  more.textContent = open ? "Свернуть" : `Ещё ${count - 1} ${plural(count - 1, "команда", "команды", "команд")}`;
  more.setAttribute("aria-expanded", open);
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
    toolLine(ev);
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

// Перетаскивание файлов в беседу на компьютере: подсветка поля ввода и вложения как у 📎.
let dragDepth = 0;
const dragging = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
const canDrop = () => current && !$("#composer").hidden && !editing;

$("#chat").addEventListener("dragenter", (e) => {
  if (!dragging(e) || !canDrop()) return;
  e.preventDefault();
  dragDepth++;
  $("#composer").classList.add("dropping");
});
$("#chat").addEventListener("dragover", (e) => {
  if (!dragging(e) || !canDrop()) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = "copy";
});
$("#chat").addEventListener("dragleave", (e) => {
  if (!dragging(e)) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) $("#composer").classList.remove("dropping");
});
$("#chat").addEventListener("drop", (e) => {
  if (!dragging(e)) return;
  e.preventDefault();
  dragDepth = 0;
  $("#composer").classList.remove("dropping");
  if (!canDrop()) return;
  attachments.push(...e.dataTransfer.files);
  showAttachments();
  $("#text").focus();
});
// Мимо беседы браузер открыл бы файл вместо сайта.
window.addEventListener("dragover", (e) => { if (dragging(e)) e.preventDefault(); });
window.addEventListener("drop", (e) => { if (dragging(e)) e.preventDefault(); });

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
  yes.type = fix.type = "button";
  fix.title = "Перенести текст в поле ввода, поправить и отправить";
  yes.onclick = async () => {
    yes.disabled = true;
    try { await sendMessage(ev.text, [], ev.voice); } catch (err) { alert(err.message); }
    yes.disabled = false;
  };
  fix.onclick = () => startFix(ev);
  ask.append(el("span", "", "Отправить?"), yes, fix);
  node.append(label, el("div", "transcript-text", ev.text), ask);
  return node;
}

// --- Файлы беседы ---

const IMAGE = /\.(png|jpe?g|gif|webp|avif|bmp|heic|heif|tiff?|svg)$/i;

// Миниатюру фото сервер делает сам (WebP ~360 точек): окно с сотней фото не тянет
// мегабайты оригиналов. SVG и так лёгкий — он показывается как есть. v= — время
// изменения: пока файл тот же, миниатюра берётся из кэша браузера.
function thumbUrl(base, path, mtime, original) {
  if (/\.svg$/i.test(path)) return original;
  return `${base}/thumb?path=${encodeURIComponent(path)}&v=${Math.round((mtime || 0) * 1000)}`;
}

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
function fileTile(f) {
  const a = fileLink(f.path, f);
  a.className = "";
  a.title = f.path.replace(/^files\//, "");
  return fillTile(a, a.textContent, thumbUrl(`/api/chats/${current.id}`, f.path, f.mtime, a.href));
}

const baseName = (path) => path.split("/").pop();

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

// Типы для фильтра; что не подошло ни под один — «Другое».
const KINDS = [
  ["image", "Фото и картинки", IMAGE],
  ["doc", "Документы", /\.(pdf|docx?|odt|rtf|txt|md|pages|epub)$/i],
  ["sheet", "Таблицы", /\.(xlsx?|xlsm|ods|csv|tsv|numbers)$/i],
  ["slides", "Презентации", /\.(pptx?|odp|key)$/i],
  ["media", "Аудио и видео", /\.(mp3|m4a|ogg|oga|opus|wav|flac|aac|mp4|m4v|mov|webm|mkv|avi)$/i],
  ["archive", "Архивы", /\.(zip|rar|7z|tar|gz|tgz|bz2|xz)$/i],
  ["code", "Код и данные", /\.(py|js|mjs|ts|jsx|tsx|html?|css|json|ya?ml|xml|sh|sql|c|cc|cpp|h|hpp|java|kt|go|rs|rb|php|pro|pri|ui|qml|ini|toml|conf|log)$/i],
];
const kindOf = (path) => (KINDS.find(([, , re]) => re.test(path)) || ["other"])[0];

function plural(n, one, few, many) {
  const d = n % 10, h = n % 100;
  return d === 1 && h !== 11 ? one : d >= 2 && d <= 4 && (h < 12 || h > 14) ? few : many;
}
const filesWord = (n) => `${n} ${plural(n, "файл", "файла", "файлов")}`;

// Окно с плитками файлов: поиск по имени, фильтр по типу и режим «Выбрать» с
// удалением пачкой. Одно и то же для «Файлов беседы» и библиотеки; id элементов —
// `${id}-search`, `${id}-kind`, `${id}-select`, `${id}-list`, `${id}-bar`…
// spec: card(f) — плитка, empty — текст пустого окна, remove(files) — удалить на
// сервере (вернуть удалённые пути или null при отказе), changed() — после удаления.
function fileBrowser(id, spec) {
  const $$ = (part) => $(`#${id}-${part}`);
  const dialog = $(`#${id}`);
  const b = { items: [], picked: new Set(), selecting: false };

  // Типы — только те, что есть, с числом файлов; выбранный сохраняется, пока такие есть.
  function fillKinds() {
    const select = $$("kind"), was = select.value;
    const kinds = {};
    for (const f of b.items) { const k = kindOf(f.path); kinds[k] = (kinds[k] || 0) + 1; }
    const option = (value, label) => { const o = el("option", "", label); o.value = value; return o; };
    select.replaceChildren(option("", "Все типы"),
      ...[...KINDS, ["other", "Другое"]].filter(([k]) => kinds[k]).map(([k, label]) => option(k, `${label} (${kinds[k]})`)));
    select.value = [...select.options].some((o) => o.value === was) ? was : "";
  }

  b.shown = () => {
    const query = $$("search").value.trim().toLowerCase(), kind = $$("kind").value;
    return b.items.filter((f) => baseName(f.path).toLowerCase().includes(query) && (!kind || kindOf(f.path) === kind));
  };

  // Внизу: сколько выбрано и сколько это места; «Выбрать все» — по текущему фильтру.
  function showPicked() {
    const files = b.items.filter((f) => b.picked.has(f.path));
    const total = files.reduce((sum, f) => sum + f.size, 0);
    $$("picked").textContent = files.length ? `${filesWord(files.length)} · ${size(total)}` : "Нажмите на файлы";
    $$("delete").disabled = !files.length;
    const shown = b.shown();
    $$("all").textContent = shown.length && shown.every((f) => b.picked.has(f.path)) ? "Снять все" : "Выбрать все";
    $$("all").disabled = !shown.length;
  }

  function card(f) {
    const li = spec.card(f);
    li.classList.toggle("picked", b.picked.has(f.path));
    // В режиме выбора нажатие по плитке (и по ссылкам в ней) только отмечает файл.
    li.addEventListener("click", (e) => {
      if (!b.selecting) return;
      e.preventDefault();
      e.stopPropagation();
      if (b.picked.has(f.path)) b.picked.delete(f.path); else b.picked.add(f.path);
      li.classList.toggle("picked", b.picked.has(f.path));
      showPicked();
    }, true);
    const mark = el("span", "file-pick");
    mark.innerHTML = '<svg width="14" height="14"><use href="#i-check"/></svg>';
    li.append(mark);
    return li;
  }

  b.show = () => {
    const items = b.shown();
    const total = b.items.reduce((sum, f) => sum + f.size, 0);
    $$("total").textContent = `${b.items.length} · ${size(total)}`;
    $$("select").disabled = !b.items.length;
    $$("list").replaceChildren(...(items.length ? items.map(card)
      : [el("li", "muted", b.items.length ? "Ничего не найдено" : spec.empty)]));
    if (b.selecting) showPicked();
  };

  b.select = (on) => {
    b.selecting = on;
    b.picked = new Set();
    dialog.classList.toggle("selecting", on);
    $$("select").textContent = on ? "Готово" : "Выбрать";
    $$("bar").hidden = !on;
    b.show();
  };

  // Файл ушёл (крестик на плитке или удаление пачкой).
  b.drop = (paths) => {
    const gone = new Set(paths);
    b.items = b.items.filter((f) => !gone.has(f.path));
    fillKinds();
    b.show();
    spec.changed?.();
  };

  b.open = (items) => {
    b.items = items;
    $$("search").value = "";
    $$("kind").value = "";
    fillKinds();
    b.select(false);
    dialog.showModal();
  };

  $$("search").oninput = b.show;
  $$("kind").onchange = b.show;
  $$("select").onclick = () => b.select(!b.selecting);
  dialog.addEventListener("close", () => { if (b.selecting) b.select(false); });
  $$("all").onclick = () => {
    const shown = b.shown();
    const all = shown.every((f) => b.picked.has(f.path));
    for (const f of shown) all ? b.picked.delete(f.path) : b.picked.add(f.path);
    b.show();
  };
  $$("delete").onclick = async () => {
    const files = b.items.filter((f) => b.picked.has(f.path));
    if (!files.length) return;
    let deleted;
    try {
      deleted = await spec.remove(files);
    } catch (err) { return alert(err.message); }
    if (!deleted) return;  // передумали в подтверждении
    // Файлы, которых уже не было (удалили в другом окне), тоже убираем из списка.
    b.select(false);
    b.drop([...files.map((f) => f.path), ...deleted]);
  };
  return b;
}

const chatFiles = fileBrowser("files", {
  empty: "Пока пусто",
  card(f) {
    const li = el("li");
    const name = baseName(f.path);
    li.append(fileTile(f), deleteButton("Удалить " + name, async () => {
      // Файлы из general уже в библиотеке: там они остаются.
      const kept = f.path.startsWith("files/general/");
      if (!confirm(kept ? `Убрать «${name}» из беседы? В библиотеке файл останется.` : `Удалить «${name}»?`)) return;
      try {
        await api(`/api/chats/${current.id}/file?path=${encodeURIComponent(f.path)}`, { method: "DELETE" });
      } catch (err) { return alert(err.message); }
      chatFiles.drop([f.path]);
    }));
    return li;
  },
  async remove(files) {
    const kept = files.filter((f) => f.path.startsWith("files/general/")).length;
    const note = kept === files.length ? " В библиотеке они останутся."
      : kept ? ` ${filesWord(kept)} из них останутся в библиотеке.` : "";
    if (!confirm(`Убрать из беседы ${filesWord(files.length)} (${size(files.reduce((s, f) => s + f.size, 0))})?${note}`)) return null;
    const { deleted } = await api(`/api/chats/${current.id}/files/delete`, { method: "POST", json: { paths: files.map((f) => f.path) } });
    return deleted;
  },
  changed: () => showSize(),
});

$("#export").onclick = () => {
  // Имя — как у сервера: название беседы без недопустимых в именах файлов знаков.
  const name = (current.title || "Беседа").replace(/[\\/:*?"<>|\x00-\x1f]+/g, " ").replace(/\s+/g, " ").trim().slice(0, 80) || "Беседа";
  viewFile({ name: name + ".md", url: `/api/chats/${current.id}/export` });
};

$("#files-open").onclick = async () => {
  let items;
  try {
    items = await api(`/api/chats/${current.id}/files`);
  } catch (err) { return alert(err.message); }
  chatFiles.open(items);
};

// --- Библиотека ---

function libraryUrl(path, download) {
  return `/api/library/file?path=${encodeURIComponent(path)}` + (download ? "&download=true" : "");
}

const library = fileBrowser("library", {
  empty: "Пока пусто: сюда попадают вложения и файлы от модели",
  // Плитка как в «Файлах беседы», под именем — беседы с этим файлом, размер и дата.
  card(f) {
    const li = el("li", "file-card");
    const name = baseName(f.path);
    const open = el("a");
    open.href = libraryUrl(f.path);
    open.target = "_blank";
    open.rel = "noopener";
    open.title = f.path;
    open.onclick = (e) => {
      if (e.ctrlKey || e.metaKey || e.shiftKey || e.button) return;
      e.preventDefault();
      const q = `?path=${encodeURIComponent(f.path)}`, v = `&v=${Math.round(f.mtime * 1000)}`;
      viewFile({ name, url: libraryUrl(f.path), download: libraryUrl(f.path, true),
        large: `/api/library/thumb${q}&size=1600${v}`, preview: `/api/library/preview${q}${v}`, size: f.size });
    };
    fillTile(open, name, thumbUrl("/api/library", f.path, f.mtime, libraryUrl(f.path)));
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
      get.onclick = (e) => { e.preventDefault(); shareFile(libraryUrl(f.path, true), name); };
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
      library.drop([f.path]);
    }));
    return li;
  },
  async remove(files) {
    const chats = [...new Set(files.flatMap((f) => f.chats.map((c) => `«${c.title || "Новая беседа"}»`)))];
    const where = chats.length ? ` Они пропадут и из бесед: ${chats.slice(0, 5).join(", ")}${chats.length > 5 ? ` и ещё ${chats.length - 5}` : ""}.` : "";
    if (!confirm(`Удалить ${filesWord(files.length)} (${size(files.reduce((s, f) => s + f.size, 0))}) насовсем?${where}`)) return null;
    const { deleted } = await api("/api/library/delete", { method: "POST", json: { paths: files.map((f) => f.path) } });
    return deleted;
  },
});

$("#library-open").onclick = async () => {
  let items;
  try {
    items = await api("/api/library");
  } catch (err) { return alert(err.message); }
  library.open(items);
};

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
    ["skills", "Скиллы", "Опубликованные скиллы сайта: инструкции, скрипты и шаблоны. Системные скиллы Codex остаются доступными отдельно."],
    ["multi_agent", "Вспомогательные агенты", "Разрешить Codex делить задачу между дополнительными агентами. Они расходуют ту же подписку и наследуют ограничения доступа."],
    ["all_chats", "Файлы других бесед", "Модель видит рабочие папки всех бесед пользователя. Без этого — только текущую беседу и общую память."],
  ],
};
const ACCESS_NOTE = { codex: "Списки доменов ограничивают интернет для команд. Они не ограничивают встроенный веб-поиск, приложения и MCP. Команды выполняются в песочнице без запроса дополнительных прав; наружу выходят только команды из опасной зоны." };
// Опасная зона: действия вне песочницы. Галочки нужны с Bash, без него неактивны.
const ACCESS_DANGER = {
  claude: [
    ["unsandboxed", "Команды вне песочницы",
      "Перечисленные команды выполняются на сервере напрямую, с правами его хозяина. Наружу выходит только одиночный вызов: «docker ps» — да, «docker ps && cat …» или «… | docker …» целиком остаются в песочнице. Команда без аргументов совпадает с любыми аргументами: «docker» — это и «docker run …».",
      [["unsandboxed_commands", "Команды вне песочницы", "docker ps\nsystemctl --user status"]]],
  ],
  codex: [
    ["unsandboxed", "Команды вне песочницы",
      "Перечисленные команды выполняются на сервере напрямую, с правами его хозяина. Наружу выходит вызов, у которого разрешена каждая часть: «docker ps && docker images» — да, если разрешены обе, «docker ps && cat …» или «… | grep …» целиком остаются в песочнице. Команда совпадает и с дополнительными аргументами: «docker» — это и «docker run …». Звёздочки не поддерживаются.",
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
      return [list, el("p", "muted", "Снятый скилл сайта Codex не получает; системные скиллы Codex этой настройкой не отключаются.")];
    },
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
    if (!engines.length) panel.append(el("p", "muted", "На сервере нет подключённых нейросетей."));
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
    if (!engines.length) panel.append(el("p", "muted", "На сервере нет подключённых нейросетей."));
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

const ENGINE_STATUS = {
  available: "Доступна",
  limited: "Доступна, лимит исчерпан",
  login: "Нужен вход",
  missing: "Не установлена",
  error: "Недоступна",
  checking: "Проверяется",
};

async function waitEngineLogin(engine) {
  for (let attempt = 0; attempt < 450; attempt++) {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    let login;
    try { login = await api(`/api/engines/${engine}/login`); } catch { return; }
    if (login.running) continue;
    if (login.complete) {
      alert("Вход завершён");
      await openGeneral();
      models = await api("/api/models");
      await refreshMe();
    } else if (login.error) {
      alert(login.error);
      await openGeneral();
    }
    return;
  }
}

async function loginEngine(engine) {
  // Окно надо создать прямо в обработчике клика, иначе браузер заблокирует его после await.
  const authTab = window.open("about:blank", "_blank");
  let login;
  try { login = await api(`/api/engines/${engine}/login`, { method: "POST" }); }
  catch (err) { authTab?.close(); alert(err.message); return; }
  if (login.url) {
    if (authTab) authTab.location = login.url;
    else location.href = login.url;
  } else authTab?.close();
  if (login.device_code) {
    prompt("Введите этот одноразовый код на открывшейся странице:", login.device_code);
  }
  if (login.needs_code) {
    const code = prompt("После входа Claude покажет код. Вставьте его сюда:");
    if (code) {
      try { await api(`/api/engines/${engine}/login/code`, { method: "POST", json: { code } }); }
      catch (err) { alert(err.message); return; }
    }
  }
  waitEngineLogin(engine);
}

function engineSettings(items) {
  const section = el("section", "engine-settings");
  section.append(el("h4", "", "Подключение нейросетей"),
    el("p", "muted", "Сервис проверяет CLI, вход и пробный ответ при каждом запуске. Исчерпанный лимит не отключает сеть."));
  for (const item of items) {
    const row = el("div", `engine-state ${item.state}`);
    const text = el("div");
    text.append(el("strong", "", item.name), el("span", "state", ENGINE_STATUS[item.state] || item.state),
      el("small", "muted", item.message));
    const actions = el("div", "actions");
    if (item.login && item.installed) {
      const login = el("button", "primary", "Авторизоваться");
      login.type = "button";
      login.onclick = () => loginEngine(item.engine);
      actions.append(login);
    }
    if (item.installed) {
      const check = el("button", "", "Проверить");
      check.type = "button";
      check.onclick = async () => {
        check.disabled = true;
        try { await api(`/api/engines/${item.engine}/check`, { method: "POST" }); await openGeneral(); }
        catch (err) { alert(err.message); check.disabled = false; }
      };
      actions.append(check);
    }
    row.append(text, actions);
    section.append(row);
  }
  return section;
}

// Общие настройки сервиса (data/settings.json). Форма строится по списку с сервера:
// сколько параметров и как они называются — не важно.
async function openGeneral() {
  let list, engineStates;
  try { [list, engineStates] = await Promise.all([api("/api/settings"), api("/api/engines")]); }
  catch (e) { alert(e.message); return; }
  card = null;
  document.querySelectorAll("#users-list button").forEach((b) => b.classList.remove("active"));
  const form = el("form", "general-settings");
  form.append(el("h3", "", "Общие настройки"),
    el("p", "muted", "Хранятся в data/settings.json и применяются сразу, без перезапуска. "
      + ".env задаёт только значения при первом запуске."), engineSettings(engineStates));
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
  showPush();
};

api("/api/me").then((user) => { me = user; start(); }).catch(() => {});
