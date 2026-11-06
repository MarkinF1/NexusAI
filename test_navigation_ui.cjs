// PWA network stalls, response bodies, immediate dialogs and safe retry behavior.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(__dirname + '/static/app.js', 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));

function network(fetch) {
  const timers = new Map(); let timerID = 0;
  const c = {AbortController, TypeError, fetch, showLogin() { c.loggedOut = true; },
    setTimeout(fn) { const id=++timerID; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf('async function apiResponse('), source.indexOf('function el(')), c);
  return {c, timers, timeout() { const [id, fn]=timers.entries().next().value; timers.delete(id); fn(); }};
}

class Node {
  constructor(tag, cls='', text='') { Object.assign(this,{tag,className:cls,textContent:text,children:[],value:'',open:false,attributes:{},handlers:{}}); }
  get options() { return this.children; }
  get classList() { return {toggle() {}}; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children=items; }
  setAttribute(k,v) { this.attributes[k]=v; }
  removeAttribute(k) { delete this.attributes[k]; }
  addEventListener(name,fn) { this.handlers[name]=fn; }
  showModal() { this.open=true; }
  close() { this.open=false; this.handlers.close?.(); }
}

function browser() {
  const nodes = new Map();
  const c = {$: id => { if(!nodes.has(id))nodes.set(id,new Node('div')); return nodes.get(id); },
    el: (...args)=>new Node(...args), KINDS:[],kindOf:()=> 'other',baseName:p=>p,filesWord:n=>`${n} files`,size:n=>`${n}`,
    console:{error() {}}, Set, confirm:()=>false,
  };
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf('function fileBrowser('),source.indexOf('// --- Настройки отдельной беседы ---')),c);
  const b=c.fileBrowser('library',{empty:'empty',card:f=>new Node('li','',f.path)});
  return {b,c,nodes,dialog:c.$('#library')};
}

(async()=>{
  let requests=0;
  const read=network(async()=>{requests++;return requests===1 ? new Promise(()=>{}) : new Response('{"ok":true}');});
  const reading=read.c.api('/api/library');
  read.timeout(); await tick();
  assert.equal((await reading).ok,true,'a stalled GET is retried once even if fetch ignores abort');
  assert.equal(requests,2);assert.equal(read.timers.size,0);

  const body=network(async()=>({status:200,ok:true,json:()=>new Promise(()=>{})}));
  const stalledBody=body.c.api('/api/users').catch(e=>e);
  body.timeout();await tick();body.timeout();
  assert.equal((await stalledBody).name,'TimeoutError','response-body reads also have a deadline');

  let writes=0;
  const write=network(async()=>{writes++;return new Promise(()=>{});});
  const writing=write.c.api('/api/chats/chat/messages',{method:'POST',json:{text:'hello'}}).catch(e=>e);
  write.timeout();assert.equal((await writing).name,'TimeoutError');assert.equal(writes,1,'never replay a possibly committed mutation');

  const abort=new AbortController();let cancelled=0;
  const external=network(async()=>{cancelled++;return new Promise(()=>{});});
  const cancelling=external.c.api('/api/chats/chat',{signal:abort.signal}).catch(e=>e);
  abort.abort();assert.equal((await cancelling).name,'AbortError');assert.equal(cancelled,1);assert.equal(external.timers.size,0);

  const auth=network(async()=>new Response('{"detail":"login"}',{status:401}));
  await assert.rejects(auth.c.api('/api/me'),/Нужно войти/);assert.equal(auth.c.loggedOut,true);

  const files=browser();let finish;
  const loading=files.b.load(()=>new Promise(resolve=>{finish=resolve;}));
  assert.equal(files.dialog.open,true,'the dialog opens synchronously before the server responds');
  assert.equal(files.dialog.attributes['aria-busy'],'true');
  finish([{path:'memory.md',size:12}]);await loading;
  assert.equal(files.c.$('#library-list').children[0].textContent,'memory.md');
  assert.equal(files.dialog.attributes['aria-busy'],undefined);

  let late;
  const old=files.b.load(()=>new Promise(resolve=>{late=resolve;}));
  files.dialog.close();late([{path:'late.md'}]);await old;
  assert.equal(files.dialog.open,false,'late responses must not reopen a closed window');

  let attempts=0;
  await files.b.load(async()=>{attempts++;throw new Error('offline');});
  assert.equal(files.dialog.open,true);
  const retry=files.c.$('#library-list').children[0].children[0];
  assert.equal(retry.textContent,'Повторить');await retry.onclick();assert.equal(attempts,2);

  const settings=browser();let usersDone;
  Object.assign(settings.c,{me:{name:'owner',is_admin:true},pauseVoice(){},side(){},setLevel(){},showPush(){},
    loadUsers:()=>new Promise(resolve=>{usersDone=resolve;})});
  vm.runInContext(source.slice(source.indexOf('let settingsOpenGen ='),source.indexOf('function startupError(')),settings.c);
  const opening=settings.c.$('#settings-open').onclick();
  assert.equal(settings.c.$('#settings').open,true,'settings must open before loading the participants');
  usersDone();await opening;
  Object.assign(settings.c,{showLogin(){settings.c.loginShown=true;},start:async()=>{throw new Error('network unavailable');}});
  vm.runInContext(source.slice(source.indexOf('function startupError('),source.indexOf('start().catch(startupError)')),settings.c);
  settings.c.startupError(new Error('network unavailable'));
  assert.equal(settings.c.loginShown,true);
  assert.equal(settings.c.$('#startup-retry').hidden,false);
  assert.equal(settings.c.$('#login-error').textContent,'network unavailable','startup errors must not leave a blank page');
  await settings.c.$('#startup-retry').onclick();
  assert.equal(settings.c.$('#startup-retry').disabled,false);
  settings.c.startupError(new Error('Нужно войти'));
  assert.equal(settings.c.$('#startup-retry').hidden,true);
  const oldHTML=browser();
  const oldQuery=oldHTML.c.$;
  Object.assign(oldHTML.c,{$:id=>id==='#startup-retry'?null:oldQuery(id),showLogin(){},start:async()=>{}});
  vm.runInContext(source.slice(source.indexOf('function startupError('),source.indexOf('start().catch(startupError)')),oldHTML.c);
  oldHTML.c.startupError(new Error('offline'));
  assert.equal(oldQuery('#login-error').textContent,'offline','cached HTML without the new button must remain usable');
  console.log('PASS: PWA stalled requests/body, cancellation, mutation safety, synchronous dialogs, close races and retry');
})().catch(e=>{console.error(e);process.exitCode=1;});
