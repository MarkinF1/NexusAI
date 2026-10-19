// Pure UI state tests; no browser, server, credentials or external dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, 'static/app.js'), 'utf8');
const fragment = source.slice(source.indexOf('const ENGINE_STATUS ='), source.indexOf('$("#general-open").onclick = openGeneral;'));

function fixture(items) {
  const timers = [], calls = [];
  let current, replacements = 0;
  function el(tag, cls = '', text = '') {
    return { tag, cls, textContent: text, children: [], append(...nodes) { this.children.push(...nodes); },
      replaceWith(node) { current = node; replacements++; } };
  }
  const context = { Set, Map, Promise, el, models: {}, confirm: () => true, alert: () => {},
    card: null, setLevel: () => {},
    $: () => ({replaceChildren(form) { current = form.children.find(n => n.cls === 'engine-settings'); }}),
    setTimeout: (fn) => timers.push(fn),
    document: { querySelector: () => current, querySelectorAll: () => [] },
    refreshMe: async () => {},
    api: async (url, options) => {
      calls.push([url, options?.method]);
      if (url === '/api/engines') return items;
      if (url === '/api/settings') return [];
      if (url.endsWith('/check') && context.checkHandler) return context.checkHandler(url);
      if (url.endsWith('/install')) return {running: false, complete: true};
      if (url.endsWith('/login')) return {running: false, complete: true};
      return {};
    } };
  vm.createContext(context);
  vm.runInContext(fragment + '\nglobalThis.testApi = {engineSettings, installEngine, waitEngineInstall, waitEngineLogin, engineInstallWatchers, engineChecks, openGeneral, checkEngineSettings};', context);
  return {context, timers, calls, api: context.testApi, setItems: (value) => items = value,
    setCurrent: (value) => current = value, current: () => current, replacements: () => replacements};
}
function buttons(node) {
  return [ ...(node.tag === 'button' ? [node] : []), ...node.children.flatMap(buttons) ];
}
const base = {engine: 'codex', name: 'Codex', install_url: 'https://chatgpt.com/codex/install.sh'};

(async () => {
  for (const [state, installed, login, expected] of [
    ['missing', false, false, ['Установить', 'Проверить']],
    ['login', true, true, ['Авторизоваться', 'Проверить']],
    ['available', true, false, ['Проверить']],
    ['limited', true, false, ['Проверить']],
    ['error', true, false, ['Проверить']],
    ['installing', false, false, []],
    ['checking', true, true, []],
    ['authorizing', true, false, ['Открыть вход']],
  ]) {
    const f = fixture([]);
    const node = f.api.engineSettings([{...base, state, installed, login, installing: state === 'installing'}]);
    assert.deepEqual(buttons(node).map(b => b.textContent), expected, state);
  }
  const failed = fixture([]);
  assert.equal(buttons(failed.api.engineSettings([{...base, state:'install_error', installed:false, install_error:'Failed'}]))[0].textContent,
    'Повторить установку');

  const f = fixture([{...base, state:'login', installed:true, login:true}]);
  f.setCurrent(f.api.engineSettings([{...base, state:'missing', installed:false}]));
  const pending = f.api.waitEngineInstall('codex');
  assert.equal(f.api.engineInstallWatchers.size, 1);
  await f.api.waitEngineInstall('codex');
  assert.equal(f.timers.length, 1, 'duplicate polling must not start');
  f.timers.shift()();
  await pending;
  assert.ok(f.replacements() >= 1, 'only engine section is replaced, not general settings');
  assert.equal(f.api.engineInstallWatchers.size, 0);
  assert.ok(f.calls.some(([url]) => url === '/api/models'));
  assert.ok(f.calls.some(([url, method]) => url === '/api/engines/codex/check' && method === 'POST'),
    'installation completion must request a new system check');

  const login = fixture([{...base, state:'available', installed:true, login:false}]);
  const loginPending = login.api.waitEngineLogin('codex');
  login.timers.shift()();
  await loginPending;
  assert.ok(login.calls.some(([url, method]) => url === '/api/engines/codex/check' && method === 'POST'),
    'login completion must recheck the system without reopening/resetting the form');

  let releaseClaude, releaseCodex;
  const claudeWait = new Promise(resolve => releaseClaude = resolve);
  const codexWait = new Promise(resolve => releaseCodex = resolve);
  const opened = fixture([{...base, state:'available', installed:true},
    {...base, engine:'claude', name:'Claude', state:'available', installed:true}]);
  opened.context.checkHandler = url => url.includes('/claude/') ? claudeWait : codexWait;
  await opened.api.openGeneral();
  assert.equal(opened.api.engineChecks.size, 2);
  assert.deepEqual(buttons(opened.current()).map(b => b.textContent), [], 'cached available state is hidden while checking');
  const codexPending = opened.api.engineChecks.get('codex');
  opened.setItems([{...base, state:'missing', installed:false},
    {...base, engine:'claude', name:'Claude', state:'available', installed:true}]);
  releaseCodex();
  await codexPending;
  assert.ok(buttons(opened.current()).some(b => b.textContent === 'Установить'), 'removed CLI is detected, not a fixed state transition');
  assert.equal(opened.api.engineChecks.size, 1, 'slow Claude must not delay the Codex result');
  const claudePending = opened.api.engineChecks.get('claude');
  releaseClaude();
  await claudePending;
  assert.equal(opened.calls.filter(([url, method]) => url.endsWith('/check') && method === 'POST').length, 2);

  const offline = fixture([{...base, state:'missing', installed:false}]);
  offline.context.checkHandler = () => { throw new Error('offline'); };
  await offline.api.openGeneral();
  await offline.api.engineChecks.get('codex');
  assert.deepEqual(buttons(offline.current()).map(b => b.textContent), ['Проверить'], 'failed check must not offer install/login based on stale state');

  const cancel = fixture([]);
  cancel.context.confirm = () => false;
  await cancel.api.installEngine(base, {});
  assert.equal(cancel.calls.length, 0, 'cancel must not install');
  const stillMissing = fixture([{...base, state:'missing', installed:false}]);
  stillMissing.setCurrent(stillMissing.api.engineSettings([{...base, state:'missing', installed:false}]));
  const installDone = stillMissing.api.waitEngineInstall('codex');
  stillMissing.timers.shift()();
  await installDone;
  assert.deepEqual(buttons(stillMissing.current()).map(b => b.textContent), ['Установить', 'Проверить'],
    'successful task exit does not force login when the system still reports missing');

  const failedLogin = fixture([{...base, state:'login', installed:true, login:true}]);
  const failedLoginApi = failedLogin.context.api;
  failedLogin.context.api = async (url, options) => {
    const result = await failedLoginApi(url, options);
    return url.endsWith('/login') ? {running:false, complete:false, error:'Cancelled'} : result;
  };
  const cancelled = failedLogin.api.waitEngineLogin('codex');
  failedLogin.timers.shift()();
  await cancelled;
  assert.ok(failedLogin.calls.some(([url, method]) => url.endsWith('/check') && method === 'POST'),
    'failed login also triggers a system recheck');
  console.log('PASS: fresh per-provider checks on settings open and task completion/failure, independent results, system-driven buttons, error handling and isolated refresh');
})().catch(error => { console.error(error); process.exitCode = 1; });
