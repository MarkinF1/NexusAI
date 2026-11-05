// History pagination uses the actual opener, loader and renderer without a browser.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(__dirname + '/static/app.js', 'utf8');

class Node {
  constructor(tag, cls = '', text = '') {
    Object.assign(this, {tag, className: cls, textContent: text, dataset: {}, children: [], parent: null, scrollTop: 0});
  }
  get childNodes() { return [...this.children]; }
  get lastElementChild() { return this.children.at(-1); }
  get classList() { return {contains: c => this.className.split(' ').includes(c), toggle() {}}; }
  append(...nodes) { for (const node of nodes) { node.remove?.(); node.parent = this; this.children.push(node); } }
  prepend(...nodes) {
    for (const node of nodes) { node.remove(); node.parent = this; }
    this.children.unshift(...nodes);
  }
  remove() {
    if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1);
    this.parent = null;
  }
  replaceChildren(...nodes) { this.children.forEach(n => n.parent = null); this.children = []; this.append(...nodes); }
  insertBefore(node, before) {
    if (!before) return this.append(node);
    node.remove(); node.parent = this; this.children.splice(this.children.indexOf(before), 0, node);
  }
  querySelectorAll(selector) {
    const cls = selector.replace(':scope > ', '').slice(1);
    return this.children.filter(n => n.classList.contains(cls));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  get scrollHeight() { return this.children.length * 30; }
  getBoundingClientRect() {
    const top = this.parent ? this.parent.children.indexOf(this) * 30 - this.parent.scrollTop : 0;
    return {top, bottom: top + 30};
  }
  addEventListener(name, fn) { this[name] = fn; }
  setAttribute() {}
}

function fixture() {
  const feed = new Node('div'), nodes = new Map([['#feed', feed]]), requests = [], pending = [];
  const context = {current: null, stream: null, count: 0, turn: [], feedGen: 0, waiting: false,
    compactOffer: false, fixing: null, transcribing: new Map(),
    NAME: {codex: 'Codex', claude: 'Claude'}, history: {replaceState() {}},
    $: id => {
      if (id === '#history-more') return feed.children.find(n => n.id === 'history-more');
      if (!nodes.has(id)) nodes.set(id, new Node('div'));
      return nodes.get(id);
    },
    document: {querySelectorAll: () => []},
    el: (...args) => new Node(...args), copyBar: text => new Node('div', 'msg-actions', text),
    marked: {parse: text => text}, DOMPurify: {sanitize: text => text},
    api: url => { requests.push(url); return new Promise((resolve, reject) => pending.push({resolve, reject})); },
    pauseVoice() {}, lastChat() {}, side() {}, stopEdit() {}, showHeader() {}, showSize() {},
    setRunning() {}, loadChats() {}, scrollDown() {}, setTimeout() {},
    alert: message => assert.fail(message),
    follow: () => { context.followAt = context.count; },
    showFound: text => feed.children.some(n => n.textContent === text),
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('function endTurn('), source.indexOf('function actionButton(')), context);
  vm.runInContext(source.slice(source.indexOf('let chatOpenGen ='), source.indexOf('function showHeader(')), context);
  const page = (before, text = 'latest') => ({chat: {id: 'chat', engine: 'codex'},
    history: [{kind: 'user', text}, {kind: 'text', text: 'answer ' + text}, {kind: 'done'}],
    before, total: 120, running: true});
  return {c: context, feed, pending, requests, page};
}

(async () => {
  const f = fixture();
  const opened = f.c.openChat('chat', undefined, {refreshChats: false});
  f.pending.shift().resolve(f.page(90));
  await opened;
  assert.equal(f.c.followAt, 120, 'SSE must start after all raw events, not the page size');
  assert.ok(f.c.$('#history-more'));
  f.feed.scrollTop = 45;
  const anchor = f.feed.children[1], before = anchor.getBoundingClientRect().top;
  const older = f.c.loadOlderHistory();
  await f.c.loadOlderHistory();
  assert.equal(f.requests.length, 2, 'overlapping loads must share a single request');
  // A live response can arrive while the older page is in flight.
  f.c.render({kind: 'user', text: 'live question'});
  f.c.render({kind: 'text', text: 'live response'});
  const state = vm.runInContext('({turn, lastAsk, renderEngine, turnEngine})', f.c);
  f.pending.shift().resolve(f.page(null, 'older'));
  await older;
  assert.equal(f.c.count, 120, 'prepending must not move the SSE cursor');
  assert.equal(f.c.$('#history-more'), undefined, 'oldest page removes the loader');
  assert.equal(anchor.getBoundingClientRect().top, before, 'visible message must stay in place');
  const restored = vm.runInContext('({turn, lastAsk, renderEngine, turnEngine})', f.c);
  for (const key of Object.keys(state)) assert.equal(restored[key], state[key]);
  assert.ok(f.feed.children.some(n => n.textContent === 'older'));

  const stale = fixture();
  const a = stale.c.openChat('a'), b = stale.c.openChat('b');
  stale.pending[1].resolve({...stale.page(null, 'new chat'), chat: {id: 'b', engine: 'codex'}});
  await b;
  stale.pending[0].resolve(stale.page(90, 'stale'));
  await a;
  assert.equal(stale.c.current.id, 'b');
  assert.ok(!stale.feed.children.some(n => n.textContent === 'stale'));

  const failure = fixture();
  const ready = failure.c.openChat('chat');
  failure.pending.shift().resolve(failure.page(90)); await ready;
  const loading = failure.c.loadOlderHistory();
  failure.pending.shift().reject(new Error('offline')); await loading;
  assert.match(failure.c.$('#history-more').textContent, /Повторить/);
  assert.equal(failure.c.$('#history-more').disabled, false);

  const search = fixture();
  const found = search.c.openChat('chat', 'needle');
  search.pending.shift().resolve(search.page(90));
  await new Promise(resolve => setImmediate(resolve));
  search.pending.shift().resolve(search.page(null, 'needle'));
  await found;
  assert.ok(search.feed.children.some(n => n.textContent === 'needle'));
  console.log('PASS: paged opener, raw SSE cursor, scroll anchor, live renderer isolation, deduplication, stale navigation, retry and search');
})().catch(error => { console.error(error); process.exitCode = 1; });
