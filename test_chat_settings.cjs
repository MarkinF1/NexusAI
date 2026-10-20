// Chat header/dialog/download regression tests, including iPhone standalone sharing.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { File } = require('node:buffer');
const root = __dirname;
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
const source = fs.readFileSync(path.join(root, 'static/app.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'static/style.css'), 'utf8');
const header = html.match(/<header>([\s\S]*?)<\/header>/)[1];
assert.ok(header.indexOf('id="files-open"') < header.indexOf('id="chat-settings-open"'));
assert.ok(header.indexOf('id="chat-settings-open"') < header.indexOf('id="delete"'));
assert.ok(!header.includes('id="export"'));
assert.ok(html.match(/<dialog id="chat-settings"[\s\S]*?id="export"[\s\S]*?<\/dialog>/));
assert.ok(css.includes('#files-open, #chat-settings-open, #delete { order: 2; }'));
const settings = source.slice(source.indexOf('// --- Настройки отдельной беседы ---'), source.indexOf('$("#files-open").onclick'));
const saving = source.slice(source.indexOf('const iosApp ='), source.indexOf('// Файл беседы открывается'));
const sharing = source.slice(source.indexOf('async function shareFile('), source.indexOf('// --- Окно просмотра файла ---'));

function fixture(ios) {
  const nodes = new Map(), downloads = [], shares = [], requests = [], calls = [], opened = [];
  function node() { return {dataset: {}, textContent:'', open:false, children:[], value:'',
    replaceChildren(...items) { this.children = items; }, append(...items) { this.children.push(...items); },
    showModal() { this.open = true; }, close() { this.open = false; }}; }
  for (const id of ['#chat-settings', '#chat-settings-name', '#chat-settings-open', '#export', '#chat-transfer-target',
    '#chat-transfer-status', '#chat-transfer-confirm', '#chat-transfer-description']) nodes.set(id, node());
  const context = { File, Blob, Response,
    current: {id:'a'.repeat(32), title:'Работа: проект/дом?', engine:'codex'},
    NAME:{codex:'Codex', claude:'Claude'}, models:{},
    options:{engine:'codex', updated:123, targets:[{engine:'claude',name:'Claude',model:'sonnet'}], blocked:''},
    api: async (url, opts) => { calls.push({url,opts}); return url.endsWith('/transfer') && !opts ? context.options : {}; },
    openChat: async id => { opened.push(id); }, loadChats:async()=>{}, refreshMe:async()=>{},
    $: id => nodes.get(id),
    navigator: {standalone:ios, canShare:() => true, share:async data => shares.push(data)},
    document: {body:{append:() => {}}},
    el: (tag, cls, text) => Object.assign(node(), {textContent:text, click() { downloads.push({url:this.href, name:this.download}); }, remove() {}}),
    fetch: async url => { requests.push(url); return new Response('История', {headers:{'Content-Type':'text/markdown'}}); },
    showLogin:() => assert.fail('unexpected login'), alert:message => assert.fail(message),
    askShare:() => assert.fail('unexpected fallback'),
  };
  vm.createContext(context);
  vm.runInContext(saving + sharing + settings, context);
  return {context, nodes, downloads, shares, requests, calls, opened};
}

(async () => {
  for (const ios of [false, true]) {
    const f = fixture(ios);
    await f.nodes.get('#chat-settings-open').onclick();
    assert.equal(f.nodes.get('#chat-settings').open, true);
    assert.equal(f.nodes.get('#chat-settings-name').textContent, 'Работа: проект/дом?');
    // A navigation while a dialog is open must not export another chat.
    f.context.current = {id:'b'.repeat(32), title:'Другая беседа'};
    await f.nodes.get('#export').onclick();
    assert.equal(f.nodes.get('#chat-settings').open, false);
    const url = '/api/chats/' + 'a'.repeat(32) + '/export';
    if (ios) {
      assert.deepEqual(f.requests, [url]);
      assert.equal(f.downloads.length, 0, 'iPhone must not navigate to a download URL');
      assert.equal(f.shares[0].files[0].name, 'Работа проект дом.md');
    } else {
      assert.deepEqual(f.downloads, [{url, name:'Работа проект дом.md'}]);
      assert.equal(f.shares.length, 0);
    }
    f.context.current = null;
    f.nodes.get('#chat-settings-open').onclick();
    assert.equal(f.nodes.get('#chat-settings').open, false);
  }
  const empty = fixture(false);
  empty.context.current.title = '';
  await empty.nodes.get('#chat-settings-open').onclick();
  await empty.nodes.get('#export').onclick();
  assert.equal(empty.downloads[0].name, 'Беседа.md');
  const f = fixture(false), select=f.nodes.get('#chat-transfer-target'), confirmation=f.nodes.get('#chat-transfer-confirm');
  await f.nodes.get('#chat-settings-open').onclick();
  assert.equal(select.disabled, false);
  assert.equal(select.children[1].value, 'claude');
  assert.equal(select.children.length, 2);
  select.value='claude'; select.onchange();
  assert.equal(confirmation.open,true);
  assert.match(f.nodes.get('#chat-transfer-description').textContent,/Codex → Claude/);
  confirmation.returnValue=''; await confirmation.onclose();
  assert.equal(f.calls.filter(c=>c.opts?.method==='POST').length,0,'cancel must not transfer');
  select.value='claude'; select.onchange();
  confirmation.returnValue='ok'; await confirmation.onclose();
  const posted=f.calls.find(c=>c.opts?.method==='POST');
  assert.equal(posted.url,'/api/chats/'+'a'.repeat(32)+'/transfer');
  assert.deepEqual(JSON.parse(JSON.stringify(posted.opts.json)),{engine:'claude',from_engine:'codex',revision:123});
  assert.deepEqual(f.opened,['a'.repeat(32)]);
  assert.equal(f.nodes.get('#chat-settings').open,false);
  f.context.options.targets=[];
  await f.nodes.get('#chat-settings-open').onclick();
  assert.equal(select.disabled,true);
  assert.match(f.nodes.get('#chat-transfer-status').textContent,/нет доступных моделей/);
  f.context.options.blocked='Дождитесь окончания ответа';
  await f.nodes.get('#chat-settings-open').onclick();
  assert.equal(select.disabled,true);
  assert.equal(f.nodes.get('#chat-transfer-status').textContent,'Дождитесь окончания ответа');
  // Stale GET must not replace the options of a more recently opened chat.
  const resolvers=[];
  f.context.api=()=>new Promise(resolve=>resolvers.push(resolve));
  const first=f.nodes.get('#chat-settings-open').onclick();
  f.context.current={id:'b'.repeat(32),title:'Новая'};
  const second=f.nodes.get('#chat-settings-open').onclick();
  resolvers[1]({engine:'claude',updated:456,targets:[],blocked:''}); await second;
  resolvers[0]({engine:'codex',updated:123,targets:[{engine:'claude',name:'Claude',model:'sonnet'}],blocked:''}); await first;
  assert.equal(f.nodes.get('#chat-settings').dataset.fromEngine,'claude');
  assert.equal(select.disabled,true);
  console.log('PASS: chat settings/download, iPhone sharing, transfer options, confirmation/cancel, captured revision, unavailable/busy states and stale responses');
})().catch(error => {console.error(error); process.exitCode=1;});
