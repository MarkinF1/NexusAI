// Shared skills: uploads, pending selections and stale navigation responses.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(__dirname+'/static/app.js','utf8');
const fragment=source.slice(source.indexOf('// Одна общая папка скиллов'),source.indexOf('let settingsOpenGen ='));
class Node {
  constructor(tag,cls='',text=''){Object.assign(this,{tag,className:cls,textContent:text,children:[],hidden:false,disabled:false,value:'',attributes:{}});}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  setAttribute(k,v){this.attributes[k]=v;}
  click(){this.clicked=true;}
  get classList(){return {add:n=>{this.className+=' '+n;},remove:()=>{},toggle:()=>{}};}
}
const all=n=>[n,...n.children.flatMap(all)];
const data={account:{account:'Account skill'},site:{manual:'Shared instructions'},networks:['Claude','Codex']};
function fixture(handler){
  const nodes=new Map(),calls=[];
  const node=id=>{if(!nodes.has(id))nodes.set(id,new Node('div'));return nodes.get(id);};
  const c={el:(...a)=>new Node(...a),$:node,Set,card:null,setLevel(){},
    document:{querySelectorAll:()=>[],querySelector:selector=>selector==='#card .site-skills'?all(node('#card')).find(n=>n.className.split(' ').includes('site-skills')):null},
    checkbox(name,checked,change){const n=new Node('label','',name);Object.assign(n,{checked,change});return n;},
    FormData:class{constructor(){this.parts=[];}append(...x){this.parts.push(x);}},
    api:async(url,options={})=>{calls.push({url,...options});return handler?handler(url,options):JSON.parse(JSON.stringify(data));},
  };
  vm.createContext(c);vm.runInContext(fragment,c);
  return {c,node,calls,open:()=>c.openSiteSkills(),elements:()=>all(node('#card')),
    find:fn=>all(node('#card')).find(fn)};
}
const event={preventDefault(){}};
(async()=>{
  const f=fixture();await f.open();
  assert.equal(f.elements().filter(n=>n.tag==='select').length,0,'one shared list, without an engine selector');
  assert.ok(f.elements().some(n=>n.textContent.includes('Claude и Codex')));
  assert.ok(f.elements().some(n=>n.tag==='strong'&&n.textContent==='manual'),'local skills are shown with their descriptions');
  assert.ok(f.elements().some(n=>n.textContent==='Shared instructions'));
  assert.equal(f.find(n=>n.textContent==='Добавить скилл').disabled,false);

  const empty=fixture(()=>({account:{},site:{},networks:['Claude','Codex']}));await empty.open();
  assert.ok(empty.elements().some(n=>n.textContent.includes('Общих скиллов пока нет')));
  assert.equal(empty.find(n=>n.textContent==='Добавить скилл').hidden,false);

  const upload=fixture((url,opts)=>opts.method==='POST'?{...data,site:{...data.site,added:'New description'},added:'added'}:data);
  await upload.open();upload.find(n=>n.tag==='label'&&n.textContent==='account').change(true);
  const input=upload.find(n=>n.tag==='input'&&n.type==='file');
  input.files=[{name:'demo.zip',size:120}];await input.onchange();
  assert.equal(upload.calls.filter(c=>c.method==='POST').length,1);
  assert.equal(upload.calls.find(c=>c.method==='POST').body.parts[0][0],'file');
  assert.equal(upload.find(n=>n.tag==='label'&&n.textContent==='account').checked,true,'upload preserves unsaved account choices');
  assert.ok(upload.elements().some(n=>n.tag==='strong'&&n.textContent==='added'));
  assert.equal(input.value,'');
  await upload.find(n=>n.tag==='form').onsubmit(event);
  assert.ok(upload.calls.find(c=>c.method==='PUT').json.includes('account'));

  const failed=fixture((url,opts)=>{if(opts.method==='POST')throw Error('already exists');return data;});
  await failed.open();const bad=failed.find(n=>n.tag==='input');bad.files=[{name:'duplicate.md',size:10}];await bad.onchange();
  assert.ok(failed.elements().some(n=>n.textContent==='already exists'));
  assert.equal(failed.find(n=>n.textContent==='Добавить скилл').disabled,false);
  const prior=failed.calls.length;bad.files=[{name:'huge.zip',size:6*1024*1024}];await bad.onchange();
  assert.equal(failed.calls.length,prior,'oversized files never leave the browser');

  let done;
  const late=fixture(()=>new Promise(r=>{done=r;}));const opening=late.open();
  const other=new Node('form','other-section','Elsewhere');late.node('#card').replaceChildren(other);
  done(data);await opening;
  assert.equal(late.node('#card').children[0],other,'late responses must not reopen the section');
  console.log('PASS: common skill list, empty state, upload, pending choices, errors, limits and navigation races');
})().catch(e=>{console.error(e);process.exitCode=1;});
