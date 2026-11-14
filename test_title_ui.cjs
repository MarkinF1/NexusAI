// Live auto-title updates the header/list without waiting for the main answer.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(__dirname + '/static/app.js', 'utf8');
const header = {textContent: 'Первый запрос'};
let reloads = 0;
const context = {
  current: {id: 'first', title: 'Первый запрос'}, count: 1,
  $: () => header, loadChats() { reloads++; },
  render() {}, scrollDown() {},
  EventSource: class {
    constructor() { this.handlers = {}; }
    addEventListener(kind, fn) { this.handlers[kind] = fn; }
  },
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function follow()'), source.indexOf('// --- Отправка ---')), context);
context.follow();
context.stream.onmessage({data: JSON.stringify({kind: 'title', title: 'Настройка сервера'})});
assert.equal(context.current.title, 'Настройка сервера');
assert.equal(header.textContent, 'Настройка сервера');
assert.equal(reloads, 1);
assert.equal(context.count, 2, 'title events must advance the raw SSE cursor');
context.current = {id: 'second', title: 'Другая беседа'};
context.stream.onmessage({data: JSON.stringify({kind: 'title', title: 'Устаревшее название'})});
assert.equal(context.current.title, 'Другая беседа');
assert.equal(reloads, 1, 'stale stream must not update another chat');
console.log('PASS: auto-title header/list refresh, raw SSE cursor and stale chat protection');
