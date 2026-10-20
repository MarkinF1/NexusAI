// Actual rendering functions: historical provenance must survive a new chat theme.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(__dirname + '/static/app.js', 'utf8');
const css = fs.readFileSync(__dirname + '/static/style.css', 'utf8');
class Node {
  constructor(tag, cls='', text='') { this.tag=tag; this.className=cls; this.textContent=text; this.dataset={}; this.children=[]; }
  get classList() { return {contains:c=>this.className.split(' ').includes(c)}; }
  append(...nodes) { this.children.push(...nodes); }
  insertBefore(node, before) { const at=this.children.indexOf(before); if(at<0)this.append(node); else this.children.splice(at,0,node); }
  get lastElementChild() { return this.children.at(-1); }
  querySelectorAll(cls) { return this.children.filter(n=>n.classList?.contains(cls.slice(1))); }
  querySelector(cls) { return this.querySelectorAll(cls)[0]; }
  setAttribute() {}
}
const feed=new Node('div');
const context = {current:{engine:'claude',id:'chat'},turn:[],
  NAME:{claude:'Claude',codex:'Codex'},
  $:selector=>selector==='#feed'?feed:null,
  el:(...args)=>new Node(...args),copyBar:text=>new Node('div','msg-actions',text),
  marked:{parse:text=>text},DOMPurify:{sanitize:text=>text},plural:()=> 'команды',
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function endTurn()'),source.indexOf('function actionButton(')),context);
const render = event => context.render(event);
render({kind:'user',text:'Старый вопрос',engine:'codex'});
render({kind:'text',text:'Старый ответ',engine:'codex'});
render({kind:'tool',name:'Read',input:'one',engine:'codex'});
render({kind:'tool',name:'Read',input:'two',engine:'codex'});
assert.equal(feed.children.filter(n=>n.className==='msg-tools').length,1);
render({kind:'engine_changed',from_engine:'codex',engine:'claude',model:'sonnet'});
assert.equal(vm.runInContext('lastAsk',context),null,'cannot retry an old-provider turn');
assert.equal(feed.children.find(n=>n.className==='msg-actions'&&n.textContent==='Старый ответ').dataset.engine,'codex');
render({kind:'user',text:'Новый вопрос'}); // live events need not have explicit provenance
render({kind:'text',text:'Новый ответ'});
render({kind:'tool',name:'Read',input:'three'});
render({kind:'error',text:'test'});
render({kind:'compacted',auto:true});
render({kind:'done'});
const texts=feed.children.filter(n=>n.className==='msg-text');
assert.deepEqual(texts.map(n=>n.dataset.engine),['codex','claude']);
assert.equal(feed.children.find(n=>n.className==='msg-actions'&&n.textContent==='Новый ответ').dataset.engine,'claude');
assert.match(feed.children.find(n=>n.className.includes('msg-engine-change')).textContent,/Codex → Claude/);
assert.equal(feed.children.find(n=>n.className==='msg-error').dataset.engine,'claude');
assert.ok(feed.children.some(n=>n.textContent==='Claude сам сжал беседу'));
assert.ok(css.includes('#feed > [data-engine] { --rail: color-mix(in srgb, var(--engine) 38%, transparent); --error: var(--engine); }'));
console.log('PASS: old/new message and action colors, tool provenance, technical note and retry boundary');
